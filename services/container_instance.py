"""
容器实例数据对象 — 单容器内存镜像

缓存容器状态、登录信息、QR码、心跳、资源统计；查询零 Docker API 调用。
"""

import time
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class ContainerInstance:
    """容器实例对象 — 等价于 MCSM 的 Instance 类。"""

    # ---- 基础属性（来自 Docker API / cluster_manager） ----
    name: str
    container_id: str = ""  # Docker short_id
    status: str = "created"  # running / exited / created / ...
    image: str = ""
    node_id: str = "local"
    created: str = ""

    # ---- 端口映射（来自 Docker API inspect，供异步登录检测使用） ----
    http_port: int = 0  # OneBot HTTP 端口 (3000/tcp 映射)
    webui_port: int = 0  # NapCat WebUI 端口 (6099/tcp 映射)

    # ---- 登录状态（来自 check_login_status） ----
    uin: str = ""
    logged_in: bool = False
    login_ts: float = 0.0  # 上次登录检测时间戳
    login_stage: str = "waiting"
    login_method: str = ""
    login_reason: str = ""

    # ---- 上一次确认登录的 QQ 号（掉线后保留，不参与在线判定） ----
    last_uin: str = ""

    # ---- 配置文件/onebot11 文件名里的 QQ 号（仅展示 fallback，不参与在线判定） ----
    configured_uin: str = ""

    # ---- QR 码状态（来自本地 qrcode.png 读取） ----
    qr_data: Optional[str] = None  # base64 data URL 或 None
    qr_ts: float = 0.0  # 上次 QR 元数据更新时间戳
    qr_generated_at: float = 0.0  # 二维码源文件/日志生成时间
    qr_fetched_at: float = 0.0  # 后端实际抓取时间
    qr_expires_at: float = 0.0  # 推测过期时间
    qr_source: str = ""  # container_file / host_file / log_latest / ...
    qr_type: str = ""  # image / url / ...
    qr_expired: bool = False  # QR 码是否已过期

    # ---- Bot 心跳状态（来自 OneBot WS 端点 meta_event.heartbeat） ----
    bot_online: bool = False  # 最近一次心跳判定是否在线
    bot_heartbeat_ts: float = 0.0  # 最近一次心跳时间戳（0 = 未收到过）

    # ---- 资源统计（来自 docker stats API） ----
    cpu_percent: float = 0.0
    mem_usage: float = 0.0  # MB — 字段名与 get_basic_stats() 保持一致
    mem_limit: float = 0.0  # MB
    stats_ts: float = 0.0  # 上次 stats 采集时间戳

    # ---- 最近一次从所属节点成功同步到本实例的时间 ----
    # 远程节点断开后其容器会保留在内存里（避免闪烁），必须靠它把陈旧数据标出来，
    # 否则节点宕机半小时面板上仍是鲜绿的 running。
    synced_at: float = 0.0

    # 超过该时长未同步即视为陈旧（远程节点轮询间隔的数倍）。
    STALE_AFTER_SECONDS = 90.0

    @property
    def is_stale(self) -> bool:
        if self.node_id == "local" or not self.synced_at:
            return False
        return (time.time() - self.synced_at) > self.STALE_AFTER_SECONDS

    def to_public_dict(self) -> Dict:
        """容器列表 API 返回格式 — 兼容 state_engine.get_containers()。"""
        uin_digits = (
            "".join(ch for ch in str(self.uin) if ch.isdigit())
            if (self.logged_in and self.uin)
            else ""
        )
        last_uin_digits = (
            "".join(ch for ch in str(self.last_uin) if ch.isdigit()) if self.last_uin else ""
        )
        configured_uin_digits = (
            "".join(ch for ch in str(self.configured_uin) if ch.isdigit()) if self.configured_uin else ""
        )
        # 头像优先使用当前确认在线 uin，无则用 last_uin/configured_uin（灰度离线展示）
        avatar_uin = uin_digits or last_uin_digits or configured_uin_digits
        stale = self.is_stale
        d: Dict = {
            "id": self.container_id,
            "name": self.name,
            # 所属节点失联后不能继续宣称 running/在线 —— 我们已经不知道真实状态了。
            "status": "unknown" if stale else self.status,
            "stale": stale,
            "synced_at": self.synced_at,
            "image": self.image,
            "created": self.created,
            "node_id": self.node_id,
            "bot_online": False if stale else self.bot_online,
            "bot_heartbeat_ts": self.bot_heartbeat_ts,
            "login_stage": "unknown" if stale else self.login_stage,
            "login_method": self.login_method,
            # 头像 URL — 本地代理缓存（无需认证）；有 uin 就显示，即使目前 logged_in=False
            "bot_avatar": f"/api/resource/avatar/{avatar_uin}" if avatar_uin else "",
            # uin / last_uin 始终输出（含空串）。uin 只代表当前确认登录账号；
            # last_uin 代表最后一次确认登录账号，不参与在线判定。
            "uin": "" if stale else (self.uin if self.logged_in else ""),
            "last_uin": self.last_uin,
            "configured_uin": self.configured_uin,
        }
        try:
            from services.action_jobs import action_job_manager
            action_job_manager.decorate_container(d)
        except Exception:
            d.setdefault("display_status", self.status)
        return d

    def to_stats_dict(self) -> Dict:
        """Stats API 返回格式 — 兼容 get_basic_stats() 输出。"""
        d: Dict = {
            "status": self.status,
            "created": self.created,
            "cpu_percent": self.cpu_percent,
            "mem_usage": self.mem_usage,
            "mem_limit": self.mem_limit,
            "uin": self.uin if self.logged_in else "",
            "last_uin": self.last_uin,
            "configured_uin": self.configured_uin,
            "login_stage": self.login_stage,
            "login_method": self.login_method,
            "bot_online": self.bot_online,
            "bot_heartbeat_ts": self.bot_heartbeat_ts,
        }
        try:
            from services.action_jobs import action_job_manager
            decorated = action_job_manager.decorate_container({
                "name": self.name,
                "node_id": self.node_id,
                "status": self.status,
            })
            for key in ("action_phase", "action", "operation_id", "action_started_at", "action_updated_at", "action_error", "display_status"):
                if key in decorated:
                    d[key] = decorated[key]
        except Exception:
            d.setdefault("display_status", self.status)
        return d

    def to_qr_dict(self) -> Dict:
        """QR 状态 API 返回格式 — 兼容 state_engine.get_qr_states()[name]。"""
        if self.logged_in:
            return {
                "status": "logged_in",
                "uin": self.uin,
                "last_uin": self.last_uin,
                "configured_uin": self.configured_uin,
                "stage": "logged_in",
                "method": self.login_method,
                "reason": self.login_reason,
            }
        if self.login_stage in {
            "scan_confirmed",
            "inject_pending",
            "injected",
            "onebot_ready",
        }:
            return {
                "status": self.login_stage,
                "uin": self.uin if self.logged_in else "",
                "last_uin": self.last_uin,
                "configured_uin": self.configured_uin,
                "stage": self.login_stage,
                "method": self.login_method,
                "reason": self.login_reason,
            }
        if self.qr_data:
            age = max(0, int(time.time() - (self.qr_generated_at or self.qr_ts or time.time())))
            expires_in = max(0, int((self.qr_expires_at or 0) - time.time())) if self.qr_expires_at else None
            return {
                "status": "ok",
                "url": self.qr_data,
                "type": self.qr_type or "image",
                "source": self.qr_source or "state_cache",
                "stage": "waiting",
                "last_uin": self.last_uin,
                "configured_uin": self.configured_uin,
                "generated_at": int(self.qr_generated_at or self.qr_ts or 0),
                "fetched_at": int(self.qr_fetched_at or self.qr_ts or 0),
                "age_seconds": age,
                "expires_at": int(self.qr_expires_at or 0),
                "expires_in": expires_in,
                "max_age_seconds": 120,
            }
        # 区分"二维码已过期"和"等待生成"两种状态
        if self.qr_expired:
            return {"status": "expired", "stage": "expired", "last_uin": self.last_uin, "configured_uin": self.configured_uin}
        return {"status": "waiting", "stage": self.login_stage or "waiting", "last_uin": self.last_uin, "configured_uin": self.configured_uin}

    def to_qr_dict_public(self) -> Dict:
        """公开 QR 状态 — 不包含二维码图片数据，仅返回阶段信息。"""
        if self.logged_in:
            return {
                "status": "logged_in",
                "uin": self.uin,
                "last_uin": self.last_uin,
                "configured_uin": self.configured_uin,
                "stage": "logged_in",
            }
        if self.login_stage in {
            "scan_confirmed",
            "inject_pending",
            "injected",
            "onebot_ready",
        }:
            return {
                "status": self.login_stage,
                "uin": self.uin if self.logged_in else "",
                "last_uin": self.last_uin,
                "configured_uin": self.configured_uin,
                "stage": self.login_stage,
            }
        # 有二维码但不返回 url — 告知前端"有码可扫"但需认证才能获取
        base: Dict = {}
        if self.qr_data:
            base = {
                "status": "need_auth",
                "stage": "waiting",
                "source": self.qr_source or "state_cache",
                "type": self.qr_type or "image",
                "generated_at": int(self.qr_generated_at or self.qr_ts or 0),
                "fetched_at": int(self.qr_fetched_at or self.qr_ts or 0),
                "age_seconds": max(0, int(time.time() - (self.qr_generated_at or self.qr_ts or time.time()))),
                "expires_at": int(self.qr_expires_at or 0),
                "expires_in": max(0, int((self.qr_expires_at or 0) - time.time())) if self.qr_expires_at else None,
                "max_age_seconds": 120,
            }
        elif self.qr_expired:
            base = {"status": "expired", "stage": "expired"}
        else:
            base = {"status": "waiting", "stage": self.login_stage or "waiting"}
        # 掉线后附带 last_uin 信息
        if self.last_uin:
            base["last_uin"] = self.last_uin
        if self.configured_uin:
            base["configured_uin"] = self.configured_uin
        return base

    def update_login(self, logged_in: bool, uin: str = "", **kw) -> None:
        """更新登录状态。

        语义：
        - uin 只表示当前确认登录账号；未登录或检测不确定时必须为空。
        - last_uin 表示最后一次确认登录账号；掉线、停止、二维码等待时保留。
        """
        stage = str(kw.get("stage") or ("logged_in" if logged_in else "waiting"))
        normalized_uin = "".join(ch for ch in str(uin or "") if ch.isdigit())

        self.logged_in = bool(logged_in and normalized_uin)
        if self.logged_in:
            self.login_stage = "logged_in"
        else:
            # 调用方可能传 logged_in=True 却没解析出 uin（stage 仍是 "logged_in"）。
            # 直接沿用会产出 logged_in=False + login_stage="logged_in" 的矛盾状态，
            # 前端据此显示「已登录但没有 QQ 号」。
            self.login_stage = "waiting" if stage == "logged_in" else stage
        self.login_method = str(kw.get("method", self.login_method or ""))
        self.login_reason = str(kw.get("reason", self.login_reason or ""))

        configured = "".join(ch for ch in str(kw.get("configured_uin") or "") if ch.isdigit())
        if configured:
            self.configured_uin = configured

        if self.logged_in:
            if self.uin and self.uin != normalized_uin:
                # 换号：旧号的一切缓存都必须立刻失效，否则面板会一直显示旧 QQ。
                self.on_account_changed(normalized_uin)
            self.uin = normalized_uin
            self.last_uin = normalized_uin
        else:
            # 不把配置文件/旧账号文件推断出的 uin 当作当前在线账号。
            self.uin = ""
            self.bot_online = False

        self.login_ts = time.time()

    def on_account_changed(self, new_uin: str) -> None:
        """当前登录账号发生变更 — 清掉一切以旧 uin 为键的缓存。"""
        from services.log import logger

        old_uin = self.uin
        logger.info("实例 %s 登录账号变更: %s → %s，清理旧账号缓存", self.name, old_uin or "(空)", new_uin)
        self.bot_online = False
        self.bot_heartbeat_ts = 0.0
        self.configured_uin = ""
        self.clear_qr()
        try:
            from services.bot_heartbeat import bot_heartbeat
            bot_heartbeat.forget(old_uin)
        except Exception as exc:
            logger.debug("清理旧账号心跳缓存失败 [%s]: %s", old_uin, exc)

    def update_stats(
        self,
        cpu_percent: float = 0.0,
        mem_usage: float = 0.0,
        mem_limit: float = 0.0,
        **_kw,
    ) -> None:
        """更新资源统计。"""
        self.cpu_percent = cpu_percent
        self.mem_usage = mem_usage
        self.mem_limit = mem_limit
        self.stats_ts = time.time()

    def update_qr(
        self,
        qr_data: Optional[str],
        expired: bool = False,
        *,
        generated_at: float = 0.0,
        fetched_at: float = 0.0,
        expires_at: float = 0.0,
        source: str = "",
        type: str = "",
    ) -> None:
        """更新 QR 码数据和来源时间元数据。"""
        now = time.time()
        self.qr_data = qr_data
        self.qr_expired = expired
        self.qr_ts = now
        self.qr_generated_at = generated_at or (now if qr_data else 0.0)
        self.qr_fetched_at = fetched_at or now
        self.qr_expires_at = expires_at or ((self.qr_generated_at + 120) if qr_data else 0.0)
        self.qr_source = source
        self.qr_type = type or ("image" if qr_data else "")

    def clear_qr(self) -> None:
        """清空旧 QR 展示缓存，常用于 restart/start accepted 后。"""
        self.qr_data = None
        self.qr_ts = 0.0
        self.qr_generated_at = 0.0
        self.qr_fetched_at = 0.0
        self.qr_expires_at = 0.0
        self.qr_source = ""
        self.qr_type = ""
        self.qr_expired = False

    def update_bot_heartbeat(self, online: bool) -> None:
        """更新 Bot 心跳在线状态（由 bot_heartbeat 服务调用）。"""
        self.bot_online = online
        self.bot_heartbeat_ts = time.time()

    def clear_runtime(self) -> None:
        """容器停止时清理运行时数据。"""
        self.cpu_percent = 0.0
        self.mem_usage = 0.0
        self.mem_limit = 0.0
        self.stats_ts = 0.0
        self.clear_qr()
        self.bot_online = False
        self.bot_heartbeat_ts = 0.0
        self.login_stage = "waiting"
        self.logged_in = False
        self.uin = ""
        # last_uin 保留，供离线状态显示“上次登录”。
        self.login_method = ""
        self.login_reason = ""
