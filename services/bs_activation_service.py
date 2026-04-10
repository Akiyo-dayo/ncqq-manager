"""
BS 连接健康监控服务（BSActivationService）

职责：
  - 定期轮询 BS connections API，检查每个连接的 target_endpoints
    是否包含管理器的 /ws/napcat/{name} 端点
  - 汇总连接健康概览：总数 / 已连接 / 缺失管理器端点 / 最后检查时间
  - 记录缺失端点信息并在前端展示，供管理员手动修复
  - 替代旧的"WS 客户端连出去"架构，不再需要手动填写外部 URL

设计意图：
  /ws/napcat/{name} 是管理器的 WS Server 端点，BS 作为 proxy
  连过来（通过 target_endpoints 配置）。本服务监控这一链路是否健康。
"""
import asyncio
import time
from typing import Any, Dict, List, Optional

from services.log import logger


# 健康检查间隔（秒）
_CHECK_INTERVAL = 30
# 启动延迟（秒）—— 等 BS 完全启动后再开始检查
_STARTUP_DELAY = 5


class BSActivationService:
    """BS 连接健康监控服务。

    生命周期跟随 BS 中间件：
      - BS 启动 → 监控自动启动
      - BS 停止 → 监控自动停止
      - 管理器启动时 BS 已在运行 → auto_resume() 自动拉起监控
    不再有独立的 enabled 持久化开关。
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._state: Dict[str, Any] = {
            "enabled": False,
            "status": "idle",           # idle / checking / active / error
            "connected": False,         # 至少一个连接有管理器端点且 WS 存活
            "source": "bs_activation",
            "last_error": "",
            "last_check_at": 0.0,
            # 连接统计
            "total_connections": 0,
            "managed_connections": 0,    # 包含管理器端点的连接数
            "active_connections": 0,     # WS 存活的连接数
            "injected_connections": 0,   # 本次自动修复注入的连接数
            "missing_endpoints": [],     # 缺失管理器端点的连接 name 列表
            # 连接明细（供前端展示）
            "connections": [],
        }

    # ---- 无需持久化，生命周期跟随 BS ----

    def _save_config(self, _enabled: bool) -> None:
        """不再持久化开关状态，监控生命周期完全跟随 BS。保留方法签名避免调用报错。"""
        pass

    def _get_manager_endpoint_pattern(self) -> str:
        """返回管理器 WS 端点的 URL 前缀（基于当前配置），用于匹配 target_endpoints。"""
        try:
            from services.config import app_config
            host = str(app_config.get("manager_host", "127.0.0.1"))
            port = int(app_config.get("manager_port", 8000))
            return f"ws://{host}:{port}/ws/napcat/"
        except Exception:
            return "ws://127.0.0.1:8000/ws/napcat/"

    def _get_compat_endpoint_pattern(self) -> str:
        """返回兼容端点的 URL（基于当前配置），用于匹配 target_endpoints。"""
        try:
            from services.config import app_config
            host = str(app_config.get("manager_host", "127.0.0.1"))
            port = int(app_config.get("manager_port", 8000))
            return f"ws://{host}:{port}/ws/onebot/v11/ws"
        except Exception:
            return "ws://127.0.0.1:8000/ws/onebot/v11/ws"

    def _get_all_manager_prefixes(self) -> list[str]:
        """返回管理器端点的所有可能 host 变体前缀。

        除当前配置的 host 外，还包含常见本地变体（127.0.0.1 / localhost / 0.0.0.0），
        解决管理器 host 配置变更后旧端点无法匹配的问题。
        """
        try:
            from services.config import app_config
            host = str(app_config.get("manager_host", "127.0.0.1"))
            port = int(app_config.get("manager_port", 8000))
        except Exception:
            host, port = "127.0.0.1", 8000

        hosts = {host, "127.0.0.1", "localhost", "0.0.0.0"}
        prefixes: list[str] = []
        for h in hosts:
            prefixes.append(f"ws://{h}:{port}/ws/napcat/")
        return prefixes

    def _get_all_compat_endpoints(self) -> list[str]:
        """返回兼容端点的所有可能 host 变体。"""
        try:
            from services.config import app_config
            host = str(app_config.get("manager_host", "127.0.0.1"))
            port = int(app_config.get("manager_port", 8000))
        except Exception:
            host, port = "127.0.0.1", 8000

        hosts = {host, "127.0.0.1", "localhost", "0.0.0.0"}
        return [f"ws://{h}:{port}/ws/onebot/v11/ws" for h in hosts]

    # ---- 公开 API ----

    async def auto_resume(self) -> None:
        """程序启动时调用：BS 已在运行则自动启动监控，无需手动开关。"""
        try:
            from services.botshepherd import botshepherd_manager
            if botshepherd_manager.running:
                logger.info("BS 已在运行，自动启动连接健康监控")
                await self.start()
        except Exception as e:
            logger.warning("连接健康监控自动启动失败: %s", e)

    def status(self) -> Dict[str, Any]:
        state = dict(self._state)
        state["running"] = bool(self._task and not self._task.done())
        state["enabled"] = self._running or state.get("enabled", False)
        return state

    async def start(self, url: str = "", token: str = "") -> Dict[str, Any]:
        """启动连接健康监控。

        同时确保 BS 中间件进程已启动——如果 BS 已安装且已初始化但未运行，
        会自动拉起 BS 进程，避免用户需要手动启动两个服务。

        url/token 参数保留接口兼容性，但不再作为 WS 客户端连接目标使用。
        """
        if self._task and not self._task.done():
            return {"success": True, "message": "already running", "activation": self.status()}

        # ★ 确保 BS 中间件已启动
        bs_started = self._ensure_bs_running()

        self._running = True
        self._state.update({
            "enabled": True,
            "status": "checking",
            "last_error": "",
        })
        self._task = asyncio.create_task(self._monitor_loop())
        self._save_config(True)
        msg = "started"
        if bs_started:
            msg = "started (BS auto-started)"
        return {"success": True, "message": msg, "activation": self.status()}

    def _ensure_bs_running(self) -> bool:
        """确保 BS 中间件进程已启动。返回 True 表示本次调用触发了启动。"""
        try:
            from services.botshepherd import botshepherd_manager
            if botshepherd_manager.running:
                return False
            if not botshepherd_manager.installed or not botshepherd_manager.initialized:
                logger.debug("BS 未安装或未初始化，跳过自动启动")
                return False
            result = botshepherd_manager.start()
            if result.get("status") == "ok":
                logger.info("连接健康监控自动启动了 BS 中间件: %s", result.get("message"))
                return True
            else:
                logger.warning("连接健康监控尝试启动 BS 失败: %s", result.get("message"))
                return False
        except Exception as e:
            logger.warning("连接健康监控启动 BS 异常: %s", e)
            return False

    async def stop(self) -> Dict[str, Any]:
        """停止连接健康监控。

        注意：仅停止监控循环，不会停止 BS 中间件进程。
        BS 进程由 botshepherd_manager 独立管理。
        """
        self._running = False
        self._state["enabled"] = False
        self._state["status"] = "idle"
        self._save_config(False)
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._state["connected"] = False
        self._state["status"] = "idle"
        return {"success": True, "message": "stopped", "activation": self.status()}

    # ---- 监控循环 ----

    async def _monitor_loop(self) -> None:
        """定期检查 BS connections 健康状态。"""
        # 启动延迟，等 BS 完全启动
        await asyncio.sleep(_STARTUP_DELAY)

        while self._running:
            try:
                await self._check_connections()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._state.update({
                    "status": "error",
                    "last_error": str(e),
                    "connected": False,
                })
                logger.warning("BS 连接健康检查异常: %s", e)

            if self._running:
                await asyncio.sleep(_CHECK_INTERVAL)

    async def _check_connections(self) -> None:
        """执行一次连接健康检查。"""
        from services.botshepherd import botshepherd_manager
        from services.napcat_ws_service import napcat_ws_service

        if not botshepherd_manager.running:
            self._state.update({
                "status": "active",
                "connected": False,
                "last_check_at": time.time(),
                "total_connections": 0,
                "managed_connections": 0,
                "active_connections": 0,
                "injected_connections": 0,
                "missing_endpoints": [],
                "connections": [],
                "last_error": "",
            })
            return

        # 拉取 BS connections
        conn_resp = await botshepherd_manager.get_connections()
        connections = conn_resp.get("connections", {})
        if not isinstance(connections, dict):
            connections = {}

        mgr_prefixes = self._get_all_manager_prefixes()
        compat_endpoints = set(self._get_all_compat_endpoints())

        total = 0
        managed = 0
        active = 0
        missing_names: List[str] = []
        conn_details: List[Dict[str, Any]] = []

        for conn_id, conn in connections.items():
            if not isinstance(conn, dict):
                continue
            total += 1

            # 检查 enabled
            enabled = conn.get("enabled", True)

            # 检查 target_endpoints 中是否包含管理器端点
            targets = conn.get("target_endpoints", [])
            if not isinstance(targets, list):
                targets = []
            has_mgr = any(
                any(t.startswith(p) for p in mgr_prefixes) or t in compat_endpoints
                for t in targets
            )

            # 检查 client_status（从 BS 实时状态获取）
            status_info = conn.get("status", {})
            if isinstance(status_info, dict):
                client_status = status_info.get("client_status", "unknown")
                self_id = status_info.get("self_id")
            else:
                client_status = "unknown"
                self_id = None

            ws_alive = client_status == "connected"

            # 检查 napcat_ws_service 中对应连接的 WS 状态
            conn_name = conn.get("name", conn_id)
            ws_registered = napcat_ws_service.is_connected(conn_id)

            if has_mgr:
                managed += 1
            elif enabled:
                missing_names.append(conn_id)

            if ws_alive:
                active += 1

            conn_details.append({
                "id": conn_id,
                "name": conn_name,
                "enabled": enabled,
                "client_status": client_status,
                "ws_alive": ws_alive,
                "has_manager_endpoint": has_mgr,
                "ws_registered": ws_registered,
                "self_id": self_id,
                "last_seen": time.time() if ws_alive else 0,
            })

        self._state.update({
            "status": "active",
            "connected": active > 0 and managed > 0,
            "last_check_at": time.time(),
            "total_connections": total,
            "managed_connections": managed,
            "active_connections": active,
            "injected_connections": 0,
            "missing_endpoints": missing_names,
            "connections": conn_details,
            "last_error": "",
        })

        if missing_names:
            logger.info("BS 连接缺失管理器端点: %s", missing_names)


bs_activation_service = BSActivationService()
