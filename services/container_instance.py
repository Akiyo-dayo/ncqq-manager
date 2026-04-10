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

    # ---- QR 码状态（来自本地 qrcode.png 读取） ----
    qr_data: Optional[str] = None  # base64 data URL 或 None
    qr_ts: float = 0.0  # 上次 QR 更新时间戳
    qr_expired: bool = False  # QR 码是否已过期

    # ---- Bot 心跳状态（来自 OneBot WS 端点 meta_event.heartbeat） ----
    bot_online: bool = False  # 最近一次心跳判定是否在线
    bot_heartbeat_ts: float = 0.0  # 最近一次心跳时间戳（0 = 未收到过）

    # ---- 资源统计（来自 docker stats API） ----
    cpu_percent: float = 0.0
    mem_usage: float = 0.0  # MB — 字段名与 get_basic_stats() 保持一致
    mem_limit: float = 0.0  # MB
    stats_ts: float = 0.0  # 上次 stats 采集时间戳

    def to_public_dict(self) -> Dict:
        """容器列表 API 返回格式 — 兼容 state_engine.get_containers()。"""
        uin_digits = (
            "".join(ch for ch in str(self.uin) if ch.isdigit()) if self.uin else ""
        )
        d: Dict = {
            "id": self.container_id,
            "name": self.name,
            "status": self.status,
            "image": self.image,
            "created": self.created,
            "node_id": self.node_id,
            "bot_online": self.bot_online,
            "bot_heartbeat_ts": self.bot_heartbeat_ts,
            "login_stage": self.login_stage,
            "login_method": self.login_method,
            # 头像 URL — 本地代理缓存（无需认证）；有 uin 就显示，即使目前 logged_in=False
            "bot_avatar": f"/api/resource/avatar/{uin_digits}" if uin_digits else "",
        }
        if self.uin:
            d["uin"] = self.uin
        return d

    def to_stats_dict(self) -> Dict:
        """Stats API 返回格式 — 兼容 get_basic_stats() 输出。"""
        return {
            "status": self.status,
            "created": self.created,
            "cpu_percent": self.cpu_percent,
            "mem_usage": self.mem_usage,
            "mem_limit": self.mem_limit,
        }

    def to_qr_dict(self) -> Dict:
        """QR 状态 API 返回格式 — 兼容 state_engine.get_qr_states()[name]。"""
        if self.logged_in:
            return {
                "status": "logged_in",
                "uin": self.uin,
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
                "uin": self.uin,
                "stage": self.login_stage,
                "method": self.login_method,
                "reason": self.login_reason,
            }
        if self.qr_data:
            return {
                "status": "ok",
                "url": self.qr_data,
                "type": "file",
                "stage": "waiting",
            }
        # 区分"二维码已过期"和"等待生成"两种状态
        if self.qr_expired:
            return {"status": "expired", "stage": "expired"}
        return {"status": "waiting", "stage": self.login_stage or "waiting"}

    def update_login(self, logged_in: bool, uin: str = "", **kw) -> None:
        """更新登录状态。"""
        self.logged_in = logged_in
        stage = str(kw.get("stage") or ("logged_in" if logged_in else "waiting"))
        self.login_stage = stage
        self.login_method = str(kw.get("method", self.login_method or ""))
        self.login_reason = str(kw.get("reason", self.login_reason or ""))
        if uin:
            self.uin = uin
        elif not logged_in and stage == "waiting":
            self.uin = ""
        self.login_ts = time.time()

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

    def update_qr(self, qr_data: Optional[str], expired: bool = False) -> None:
        """更新 QR 码数据。"""
        self.qr_data = qr_data
        self.qr_expired = expired
        self.qr_ts = time.time()

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
        self.qr_data = None
        self.qr_ts = 0.0
        self.qr_expired = False
        self.bot_online = False
        self.bot_heartbeat_ts = 0.0
        self.login_stage = "waiting"
        self.login_method = ""
        self.login_reason = ""
