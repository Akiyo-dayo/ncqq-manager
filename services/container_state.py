"""
容器状态引擎 — 后台异步刷新，API/WS 零阻塞读内存

架构：后台循环 → aiodocker(本地列表/端口) + aiohttp(登录检测/远程节点)
     → 写入 InstanceSubsystem；API 读内存快照，响应 <1ms。
自适应刷新：事件活跃时 3s，长时间无变化逐步降频至 30s。
"""

import asyncio
import base64
import os
import time
from typing import Dict, List

from services.log import logger
from services.instance_subsystem import instance_subsystem
from services.docker_async import async_login_checker, async_docker_manager


def _trigger_bs_inject(name: str, result: Dict, prev: Dict) -> None:
    """按登录判定结果触发 BS 注入（fire-and-forget）。"""
    try:
        # 注入依赖登录判定：未登录或缺少 uin 时不触发
        if not result.get("logged_in"):
            return
        uin = str(result.get("uin", ""))
        if not uin:
            return

        from services.docker_manager import docker_manager

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(
                None,
                docker_manager._on_login_detected,
                name,
                result,
                prev,
            )
    except Exception as e:
        logger.debug("BS 注入调度异常 [%s]: %s", name, e)


# ============ 常量 ============

_REFRESH_INTERVAL_MIN = 3  # 事件活跃时的刷新间隔（秒）
_REFRESH_INTERVAL_MAX = 30  # 长时间无事件时的最大兜底间隔
_REFRESH_INTERVAL_STEP = 3  # 每次无事件时递增量
_LOGIN_TTL_OK = 60  # 已登录容器的登录检测间隔
_LOGIN_TTL_FAIL = 8  # 未登录容器的登录检测间隔
_QR_MAX_AGE = 120  # QR 文件最大有效期（秒）


class ContainerStateEngine:
    """容器状态引擎单例 — 后台定时刷新，数据写入 InstanceSubsystem。"""

    def __init__(self):
        # ---- 内部状态 ----
        self._tick = 0
        self._idle_interval = _REFRESH_INTERVAL_MIN  # 自适应刷新间隔
        self._running = False
        self._task: asyncio.Task | None = None
        self._force_event: asyncio.Event | None = None  # 操作/事件后立即触发刷新
        # 首次 tick 完成前不发上线通知（避免启动时误报所有在线容器）
        self._engine_initialized: bool = False

        # ---- 监控指标（§9 — 观测性） ----
        self._last_tick_duration: float = 0.0  # 最近一次 tick 耗时（秒）
        self._slow_tick_count: int = 0  # 慢 tick 累计次数（>5s）
        self._container_count: int = 0  # 最近一次刷新的容器数

    # ============ 公开读接口（委托给 instance_subsystem，零阻塞） ============

    def get_containers(self) -> List[Dict]:
        """返回容器列表快照（附带 uin）— 兼容旧接口。"""
        return instance_subsystem.get_containers_list()

    def get_login_state(self, name: str) -> Dict:
        inst = instance_subsystem.get(name)
        if not inst:
            return {}
        return {"logged_in": inst.logged_in, "uin": inst.uin, "ts": inst.login_ts}

    def get_qr_states(self) -> Dict[str, Dict]:
        """返回所有 QR 快照 — 兼容旧接口。"""
        return instance_subsystem.get_qr_states()

    def get_all_stats(self) -> Dict[str, Dict]:
        """返回所有 Stats — 兼容旧接口。"""
        return instance_subsystem.get_all_stats()

    # ============ 控制接口 ============

    async def start(self):
        """在 FastAPI lifespan 中调用，启动后台任务。"""
        if self._running:
            return
        self._running = True
        self._force_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        logger.info("容器状态引擎已启动")

    async def stop(self):
        self._running = False
        if self._force_event:
            self._force_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("容器状态引擎已停止")

    def notify_change(self):
        """容器操作后调用，立即唤醒主循环刷新。"""
        if self._force_event:
            self._force_event.set()

    # ============ 后台主循环（自适应间隔 — 事件驱动） ============

    async def _loop(self):
        while self._running:
            t0 = time.monotonic()
            try:
                await self._tick_once()
            except Exception as e:
                logger.error("状态引擎异常: %s", e, exc_info=True)

            # §9 tick 耗时记录
            elapsed = time.monotonic() - t0
            self._last_tick_duration = elapsed
            if elapsed > 5.0:
                self._slow_tick_count += 1
                logger.warning(
                    "状态引擎 tick #%d 耗时 %.1fs（>5s），容器数=%d",
                    self._tick,
                    elapsed,
                    self._container_count,
                )

            # 等待事件唤醒 或 自适应超时
            # 收到 Docker 事件 / 用户操作 → 立即刷新 + 重置为高频
            # 长时间无事件 → 逐渐降频（3s → 6s → ... → 30s）
            try:
                await asyncio.wait_for(
                    self._force_event.wait(), timeout=self._idle_interval
                )
                self._force_event.clear()
                # 事件活跃 → 重置为高频
                self._idle_interval = _REFRESH_INTERVAL_MIN
            except asyncio.TimeoutError:
                # 无事件 → 逐渐降频
                self._idle_interval = min(
                    self._idle_interval + _REFRESH_INTERVAL_STEP,
                    _REFRESH_INTERVAL_MAX,
                )

            self._tick += 1

    @property
    def health_info(self) -> Dict:
        """返回引擎健康指标 — 供 /api/health 读取。"""
        return {
            "running": self._running,
            "tick": self._tick,
            "last_tick_ms": round(self._last_tick_duration * 1000, 1),
            "slow_ticks": self._slow_tick_count,
            "interval": self._idle_interval,
            "containers": self._container_count,
        }

    async def _tick_once(self):
        """单次刷新周期 — 写入 instance_subsystem。

        v5: 全部走纯异步 — 本地 aiodocker + 远程 aiohttp，零线程池。
        v6: 新增实例离线检测 — running → 非 running 时触发 webhook 告警。
        """
        from services.cluster_manager import cluster_manager
        from services.config import get_data_dir

        # ---- 0. 记录刷新前的运行状态（用于离线检测） ----
        prev_running: set = set()
        for inst in instance_subsystem.get_all():
            if inst.status == "running":
                prev_running.add((inst.name, inst.node_id))

        # ---- 1. 刷新容器列表 → upsert 到 instance_subsystem ----
        # 本地容器：aiodocker 纯异步 ⭐
        try:
            local_containers = await async_docker_manager.list_local_containers()
        except Exception as e:
            logger.debug("引擎: 本地容器列表异步获取异常: %s", e)
            local_containers = []
        for c in local_containers:
            c["node_id"] = "local"

        # 远程节点容器：aiohttp 纯异步 ⭐ Phase 4
        try:
            remote_containers = await asyncio.wait_for(
                cluster_manager.list_remote_containers_async(),
                timeout=5,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug("引擎: 远程容器列表异步获取超时/异常: %s", e)
            remote_containers = []

        containers = local_containers + remote_containers
        if not containers and not instance_subsystem.count:
            return  # 首次空列表且无缓存，跳过

        # upsert 每个容器到 instance_subsystem
        active_names: set = set()
        running_local_names: List[str] = []
        for c in containers:
            name = c["name"]
            active_names.add(name)
            inst = instance_subsystem.upsert(
                name,
                container_id=c.get("id", ""),
                status=c.get("status", "created"),
                image=c.get("image", ""),
                node_id=c.get("node_id", "local"),
                created=c.get("created", ""),
            )
            # 容器停止时清理运行时数据
            if inst.status != "running":
                inst.clear_runtime()
            elif inst.node_id == "local":
                running_local_names.append(name)

        # 清理已不存在的容器
        instance_subsystem.cleanup(active_names)

        # ---- 1.6 实例上线/离线检测 — running 集合差集触发通知 ----
        curr_running: set = set()
        for inst in instance_subsystem.get_all():
            if inst.status == "running":
                curr_running.add((inst.name, inst.node_id))
        from services.alert_manager import alert_manager

        # 离线：running → 非 running
        went_offline = prev_running - curr_running
        if went_offline:
            for name, node_id in went_offline:
                inst = instance_subsystem.get(name)
                uin = inst.uin if inst else ""
                try:
                    await alert_manager.notify_instance_offline(name, node_id, uin)
                except Exception as e:
                    logger.debug("离线通知异常: %s", e)

        # 上线：非 running → running（首次 tick 跳过，避免启动时误报）
        came_online = curr_running - prev_running
        if came_online and self._engine_initialized:
            for name, node_id in came_online:
                inst = instance_subsystem.get(name)
                uin = inst.uin if inst else ""
                try:
                    await alert_manager.notify_instance_online(name, node_id, uin)
                except Exception as e:
                    logger.debug("上线通知异常: %s", e)

        # 首次 tick 完成后标记初始化完成
        if not self._engine_initialized:
            self._engine_initialized = True

        # ---- 1.5 批量解析端口（运行中的本地容器）— aiodocker 纯异步 ⭐ ----
        need_ports = [
            n
            for n in running_local_names
            if instance_subsystem.get(n)
            and (
                instance_subsystem.get(n).http_port == 0
                or instance_subsystem.get(n).webui_port == 0
            )
        ]
        if need_ports:
            try:
                port_map = await async_docker_manager.resolve_ports(need_ports)
            except Exception as e:
                logger.warning("端口解析批量失败: %s", e)
                port_map = {}
            for name, ports in port_map.items():
                inst = instance_subsystem.get(name)
                if inst:
                    inst.http_port = ports.get("http_port", 0)
                    inst.webui_port = ports.get("webui_port", 0)

        # ---- 1.7 WebUI 存活探测（无 OneBot WS/心跳时的在线判定兜底） ----
        # 很多部署只映射 6099(WebUI) 而不映射 3000/3001(OneBot)，
        # 这会导致 bot_heartbeat 永远收不到心跳，从而前端误判“离线”。
        # 这里用 WebUI 根页面可达性作为容器在线的真实信号（轻量 GET）。
        try:
            from services.docker_async import async_login_checker
            import aiohttp

            if async_login_checker._session:
                sem = asyncio.Semaphore(30)

                async def _probe_one(name: str):
                    inst = instance_subsystem.get(name)
                    if not inst or not inst.webui_port:
                        return
                    from services.docker_async import get_host_gateway, _host_url
                    url = _host_url(get_host_gateway(), inst.webui_port, "/webui/")
                    ok = False
                    try:
                        async with sem:
                            async with async_login_checker._session.get(
                                url,
                                timeout=aiohttp.ClientTimeout(total=1.5, connect=0.8),
                                allow_redirects=False,
                            ) as resp:
                                ok = resp.status < 500
                    except Exception:
                        ok = False
                    inst.update_bot_heartbeat(bool(ok))

                await asyncio.gather(*[_probe_one(n) for n in running_local_names])
        except Exception:
            pass

        # ---- 2. 增量登录检测 — SDK WS 主路径 + BS/HTTP 兜底 ⭐ ----
        from services.napcat_ws_service import napcat_ws_service

        now = time.time()
        need_login_instances = []
        for name in running_local_names:
            inst = instance_subsystem.get(name)
            if not inst:
                continue
            # WS 已连接时快速命中，跳过轮询 TTL 检查（实时更新）
            ws_result = napcat_ws_service.get_login_result(name)

            # ★ 修复 4：只在 WS 没有给出明确信息时才降级，信任 WS 的 is_alive + hb_online 结果
            if ws_result["logged_in"]:
                old_uin = inst.uin
                new_uin = ws_result.get("uin", "")
                was_logged = inst.logged_in
                prev_login_state = {"logged_in": was_logged, "uin": old_uin}
                inst.update_login(
                    logged_in=True,
                    uin=new_uin,
                    stage="logged_in",
                    method=ws_result.get("method", "sdk_ws"),
                    reason=ws_result.get("reason", "ws_connected"),
                )
                if new_uin and (not was_logged or old_uin != new_uin):
                    _trigger_bs_inject(name, ws_result, prev_login_state)
                continue

            ttl = _LOGIN_TTL_OK if inst.logged_in else _LOGIN_TTL_FAIL
            if now - inst.login_ts >= ttl:
                need_login_instances.append(inst)

        # 记录检测前的登录状态（用于掉线扫码通知）
        prev_login: Dict[str, tuple] = {}
        for inst in need_login_instances:
            prev_login[inst.name] = (inst.logged_in, inst.uin, inst.node_id)

        if need_login_instances:
            login_results = await async_login_checker.batch_check_login(
                need_login_instances
            )
            for name, result in login_results.items():
                inst = instance_subsystem.get(name)
                if inst:
                    inst.update_login(
                        logged_in=result.get("logged_in", False),
                        uin=result.get("uin", ""),
                        stage=result.get("stage", "waiting"),
                        method=result.get("method", ""),
                        reason=result.get("reason", ""),
                    )
                    new_uin = result.get("uin", "")
                    if result.get("logged_in") and new_uin:
                        prev_was_logged, prev_uin, _ = prev_login.get(
                            name, (False, "", "local")
                        )
                        if not prev_was_logged or prev_uin != new_uin:
                            prev_login_state = {
                                "logged_in": prev_was_logged,
                                "uin": prev_uin,
                            }
                            _trigger_bs_inject(name, result, prev_login_state)

        # ---- 2.5 掉线扫码通知 — logged_in: true → false 时推送 ----
        for name, (was_logged_in, old_uin, nid) in prev_login.items():
            if not was_logged_in:
                continue  # 之前就没登录，跳过
            inst = instance_subsystem.get(name)
            if inst and not inst.logged_in:
                # 登录态丢失 — 异步推送通知
                try:
                    from services.alert_manager import alert_manager

                    await alert_manager.notify_login_lost(name, old_uin, nid)
                except Exception as e:
                    logger.debug("掉线扫码通知异常: %s", e)

        # ---- 3. QR 码刷新（未登录 & running） ----
        data_dir = get_data_dir()
        for name in running_local_names:
            inst = instance_subsystem.get(name)
            if not inst or inst.logged_in:
                continue
            qr_data = None
            is_expired = False
            try:
                qr_path = os.path.join(data_dir, name, "cache", "qrcode.png")
                exists = await asyncio.to_thread(os.path.exists, qr_path)
                if exists:
                    age = now - await asyncio.to_thread(os.path.getmtime, qr_path)
                    if age < _QR_MAX_AGE:
                        raw = await asyncio.to_thread(
                            lambda: open(qr_path, "rb").read()
                        )
                        b64 = base64.b64encode(raw).decode("utf-8")
                        qr_data = f"data:image/png;base64,{b64}"
                    else:
                        is_expired = True
            except Exception as e:
                logger.debug("QR 读取失败 [%s]: %s", name, e)
            inst.update_qr(qr_data, expired=is_expired)

        # 记录本轮容器数（供 health_info 使用）
        self._container_count = len(containers)


# ============ 单例 ============
state_engine = ContainerStateEngine()
