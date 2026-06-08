"""
容器状态引擎 — 后台异步刷新，API/WS 零阻塞读内存

架构：后台循环 → aiodocker(本地列表/端口) + aiohttp(登录检测/远程节点)
     → 写入 InstanceSubsystem；API 读内存快照，响应 <1ms。
自适应刷新：事件活跃时 10s，长时间无变化逐步降频至 240s（4分钟）。
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

_REFRESH_INTERVAL_MIN = 10  # 事件活跃时的刷新间隔（秒）
_REFRESH_INTERVAL_MAX = 240  # 长时间无事件时的最大兜底间隔（4分钟）
_REFRESH_INTERVAL_STEP = 2  # 每次无事件时递增量（乘法退避基数）
_LOGIN_TTL_OK = 240  # 已登录容器的登录检测间隔（4分钟）
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
        self._local_fail_streak: int = 0  # 本地 Docker 连续失败次数

        # ---- WS 推送信号 — tick 完成后通知所有 WS 循环立即推送 ----
        self._push_event: asyncio.Event | None = None

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

    def get_qr_states_public(self) -> Dict[str, Dict]:
        """返回所有 QR 快照（公开版，不含二维码图片）。"""
        return instance_subsystem.get_qr_states_public()

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
        self._push_event = asyncio.Event()
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

    async def wait_push(self, timeout: float = 30.0) -> bool:
        """WS 循环调用 — 等待下一次 tick 完成后的推送信号。

        多个 WS 连接可同时 await，全部会被唤醒（广播语义）。
        返回 True 表示有新数据，False 表示超时（兜底心跳）。
        """
        evt = self._push_event  # 持有当前 event 的引用
        if not evt:
            await asyncio.sleep(timeout)
            return False
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _signal_push(self):
        """tick 完成后通知所有 WS 循环立即推送 — 广播模式。

        替换 event 对象：旧 event set() 唤醒所有当前 waiter，
        新 event 供下一轮 waiter 使用。
        """
        old = self._push_event
        self._push_event = asyncio.Event()
        if old:
            old.set()

    # ============ 后台主循环（自适应间隔 — 事件驱动） ============

    async def _loop(self):
        while self._running:
            t0 = time.monotonic()
            try:
                await self._tick_once()
            except Exception as e:
                logger.error("状态引擎异常: %s", e, exc_info=True)

            # tick 完成 → 通知 WS 循环立即推送
            self._signal_push()

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
                # 无事件 → 乘法退避降频（×1.5: 10→15→22→33→50→75→112→168→240）
                self._idle_interval = min(
                    self._idle_interval * 1.5,
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
        local_ok = False
        try:
            local_containers = await async_docker_manager.list_local_containers()
            local_ok = True
            self._local_fail_streak = 0
        except Exception as e:
            logger.debug("引擎: 本地容器列表异步获取异常: %s", e)
            local_containers = []
            self._local_fail_streak += 1
        for c in local_containers:
            c["node_id"] = "local"

        # 远程节点容器：aiohttp 纯异步 ⭐ Phase 4
        responded_nodes: set = set()  # 成功响应的远程节点 ID
        try:
            remote_containers, responded_nodes = await asyncio.wait_for(
                cluster_manager.list_remote_containers_async(),
                timeout=5,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug("引擎: 远程容器列表异步获取超时/异常: %s", e)
            remote_containers = []

        containers = local_containers + remote_containers
        if not containers and not instance_subsystem.count:
            return  # 首次空列表且无缓存，跳过

        # 可清理的节点集合：只清理成功响应的节点上已消失的容器
        # 本地连续失败 ≥5 次时强制清理，避免僵尸容器堆积
        cleanable_nodes: set = set()
        if local_ok or self._local_fail_streak >= 5:
            cleanable_nodes.add("local")
        cleanable_nodes.update(responded_nodes)

        # upsert 每个容器到 instance_subsystem
        active_keys: set = set()  # (node_id, name) 复合键
        running_local_names: List[str] = []
        for c in containers:
            name = c["name"]
            nid = c.get("node_id", "local")
            active_keys.add((nid, name))
            # 基础字段（Docker 始终提供）
            upsert_kw: dict = dict(
                node_id=nid,
                container_id=c.get("id", ""),
                status=c.get("status", "created"),
                image=c.get("image", ""),
                created=c.get("created", ""),
            )
            # 登录 / 心跳字段 — 仅远程节点返回时才覆盖；
            # 本地容器这些字段由 update_login / update_bot_heartbeat 管理，
            # Docker API 不提供，不能用空值覆盖。
            if "uin" in c:
                upsert_kw["uin"] = c["uin"]
            if "last_uin" in c:
                upsert_kw["last_uin"] = c["last_uin"]
            if "bot_online" in c:
                upsert_kw["bot_online"] = bool(c["bot_online"])
            if "bot_heartbeat_ts" in c:
                upsert_kw["bot_heartbeat_ts"] = float(c["bot_heartbeat_ts"] or 0)
            if "login_stage" in c:
                upsert_kw["login_stage"] = c["login_stage"]
                upsert_kw["logged_in"] = (c["login_stage"] == "logged_in") or bool(c.get("bot_online", False))
            if "login_method" in c:
                upsert_kw["login_method"] = c["login_method"]
            inst = instance_subsystem.upsert(name, **upsert_kw)
            # 容器停止时清理运行时数据
            if inst.status != "running":
                inst.clear_runtime()
            elif inst.node_id == "local":
                running_local_names.append(name)

        # 清理已不存在的容器（仅清理成功响应的节点上的容器）
        instance_subsystem.cleanup(active_keys, cleanable_nodes)

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
                inst = instance_subsystem.get(name, node_id)
                uin = inst.uin if inst else ""
                try:
                    await alert_manager.notify_instance_offline(name, node_id, uin)
                except Exception as e:
                    logger.debug("离线通知异常: %s", e)

        # 上线：非 running → running（首次 tick 跳过，避免启动时误报）
        came_online = curr_running - prev_running
        if came_online and self._engine_initialized:
            for name, node_id in came_online:
                inst = instance_subsystem.get(name, node_id)
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

        # ---- 1.7 已禁用 WebUI 存活兜底 避免假在线 ----
        # ---- 2. 增量登录检测 — SDK WS 主路径 + BS/HTTP 兜底 ⭐ ----
        from services.napcat_ws_service import napcat_ws_service

        now = time.time()
        need_login_instances = []
        for name in running_local_names:
            inst = instance_subsystem.get(name)
            if not inst:
                continue

            # 真实在线态校准：仅信任 OneBot 心跳服务（避免历史残留/假在线）
            try:
                from services.bot_heartbeat import bot_heartbeat_service
                if inst.uin:
                    hb_online = bot_heartbeat_service.is_online(inst.uin)
                    inst.update_bot_heartbeat(bool(hb_online))
            except Exception:
                pass
            # 近期二维码优先：若容器正在刷近期二维码，强制判定待登录
            recent_qr = False
            try:
                out = await async_login_checker._exec_in_container(
                    name,
                    "python3 -c \"import os,time; p='/app/napcat/cache/qrcode.png'; print('yes' if os.path.exists(p) and (time.time()-os.path.getmtime(p)<180) else 'no')\" 2>/dev/null || echo no",
                    timeout=2,
                )
                recent_qr = ((out or '').strip().split("\n")[0].strip() == 'yes')
            except Exception:
                recent_qr = False

            # 直接文件判定：有配置 uin 且无近期二维码 => 已登录
            uin_cfg = ""
            try:
                uin_cfg = await async_login_checker._get_uin_via_container_fs(name)
            except Exception:
                uin_cfg = ""
            if not uin_cfg:
                try:
                    out_u = await async_login_checker._exec_in_container(
                        name,
                        "python3 -c \"import glob,os,re; f=glob.glob('/app/napcat/config/onebot11_*.json'); b=os.path.basename(f[0]) if f else ''; m=re.search(r'onebot11_(\\d+)\\.json', b); print(m.group(1) if m else '')\" 2>/dev/null || echo ''",
                        timeout=6,
                    )
                    uin_cfg = (out_u or '').strip().split("\n")[0].strip()
                except Exception:
                    pass

            if uin_cfg and uin_cfg.isdigit() and not inst.last_uin:
                # 配置/旧账号文件只能作为“上次登录”展示线索，不能当作当前在线。
                inst.last_uin = uin_cfg

            if recent_qr:
                inst.update_login(
                    logged_in=False,
                    uin="",
                    stage="waiting",
                    method="",
                    reason="recent_qr_detected",
                )
                continue

            # WS 已连接时快速命中，跳过轮询 TTL 检查（实时更新）
            ws_result = napcat_ws_service.get_login_result(name)
            # 忽略 sdk_ws 作为登录真值源（会出现残留假在线）
            if ws_result.get("logged_in") and ws_result.get("method") == "sdk_ws":
                inst.update_login(
                    logged_in=False,
                    uin="",
                    stage="waiting",
                    method="",
                    reason="sdk_ws_ignored",
                )
                continue


            # 优先信任 Bot 心跳在线：在线即视为已登录（避免已在线却显示待登录）
            if inst.bot_online:
                uin_online = inst.uin
                if not uin_online:
                    try:
                        uin_online = await asyncio.to_thread(async_login_checker._get_uin_from_config, name)
                    except Exception:
                        uin_online = ""
                if not uin_online:
                    try:
                        uin_online = await async_login_checker._get_uin_via_container_fs(name)
                    except Exception:
                        uin_online = ""
                inst.update_login(
                    logged_in=True,
                    uin=uin_online or "",
                    stage="logged_in",
                    method="heartbeat_online",
                    reason="bot_heartbeat_online",
                )
                continue

            # ★ 修复 4：只在 WS 没有给出明确信息时才降级，信任 WS 的 is_alive + hb_online 结果
            if ws_result["logged_in"] and ws_result.get("method") != "sdk_ws":
                # WS 假在线兜底：若仅 ws_connected 且 bot_online=False，同时近期二维码在刷新，则强制待登录
                if (not inst.bot_online) and ws_result.get("method") == "sdk_ws":
                    try:
                        out2 = await async_login_checker._exec_in_container(
                            name,
                            "python3 -c \"import os,time; p='/app/napcat/cache/qrcode.png'; print(int(time.time()-os.path.getmtime(p)) if os.path.exists(p) else 999999)\" 2>/dev/null || echo 999999",
                            timeout=6,
                        )
                        age_s = int(((out2 or "").strip().split("\n")[0] or "999999").strip())
                    except Exception:
                        age_s = 999999
                    if age_s < 600:
                        inst.update_login(
                            logged_in=False,
                            uin="",
                            stage="waiting",
                            method="",
                            reason="recent_qr_overrides_ws",
                        )
                        continue

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
                    # 批量检测结果中忽略 sdk_ws 真值，避免残留连接导致假在线
                    r_logged = result.get("logged_in", False)
                    r_method = result.get("method", "")
                    if r_logged and r_method == "sdk_ws":
                        r_logged = False
                        result["uin"] = ""
                        result["stage"] = "waiting"
                        result["method"] = ""
                        result["reason"] = "sdk_ws_ignored"

                    inst.update_login(
                        logged_in=r_logged,
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

                # 统一回退：只要当前没拿到二维码，就从容器内读取最新 qrcode.png
                if not qr_data:
                    out = await async_login_checker._exec_in_container(
                        name,
                        "python3 -c \"import os,base64; p='/app/napcat/cache/qrcode.png'; print(base64.b64encode(open(p,'rb').read()).decode() if os.path.exists(p) else '')\" 2>/dev/null || echo ''",
                        timeout=2,
                    )
                    b64 = (out or '').strip().split("\n")[0].strip()
                    if b64:
                        qr_data = f"data:image/png;base64,{b64}"
                        is_expired = False
            except Exception as e:
                logger.debug("QR 读取失败 [%s]: %s", name, e)
            inst.update_qr(qr_data, expired=is_expired)

        # 记录本轮容器数（供 health_info 使用）
        self._container_count = len(containers)


# ============ 单例 ============
state_engine = ContainerStateEngine()
