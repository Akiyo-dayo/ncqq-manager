"""
BotShepherd 集成管理器 - 已嵌入本项目，负责初始化、进程启停和状态查询
"""

import os
import sys
import signal
import subprocess
import asyncio
import json
import glob
import shutil
import threading
import collections
from typing import Optional, Dict, Any, List
import aiohttp
from services.log import logger
from services.config import BASE_DIR


def _resolve_botshepherd_dir() -> str:
    """在 BASE_DIR 下查找 BotShepherd 目录，大小写不敏感（兼容 Linux/Windows）。"""
    for candidate in ("BotShepherd", "botshepherd", "BOTSHEPHERD"):
        path = os.path.join(BASE_DIR, candidate)
        if os.path.isdir(path):
            return path
    # fallback：扫描目录匹配（最健壮，适配任意大小写）
    try:
        for entry in os.listdir(BASE_DIR):
            if entry.lower() == "botshepherd" and os.path.isdir(
                os.path.join(BASE_DIR, entry)
            ):
                return os.path.join(BASE_DIR, entry)
    except OSError:
        pass
    return os.path.join(BASE_DIR, "BotShepherd")  # 默认回退


BOTSHEPHERD_DIR = _resolve_botshepherd_dir()
BOTSHEPHERD_DEFAULT_PORT = 5100


def _get_venv_python() -> Optional[str]:
    """返回 BS venv 中的 python 路径（只找 venv/，由 uv 创建）。"""
    venv_dir = os.path.join(BOTSHEPHERD_DIR, "venv")
    if sys.platform == "win32":
        p = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        p = os.path.join(venv_dir, "bin", "python")
    return p if os.path.isfile(p) else None


def _bs_ensure_venv(uv_bin: str) -> bool:
    """用 uv 在 BS 目录下创建名为 venv 的虚拟环境并安装依赖；幂等，仅在需要时操作。"""
    req_file = os.path.join(BOTSHEPHERD_DIR, "requirements.txt")
    venv_dir = os.path.join(BOTSHEPHERD_DIR, "venv")
    cfg_file = os.path.join(venv_dir, "pyvenv.cfg")

    if not os.path.isfile(req_file):
        return True

    need_install = not os.path.isfile(cfg_file)
    if not need_install:
        need_install = os.path.getmtime(req_file) > os.path.getmtime(cfg_file)
    if not need_install:
        return True

    if not os.path.isfile(cfg_file):
        r = subprocess.run(
            [uv_bin, "venv", "venv", "--seed"],
            capture_output=True,
            text=True,
            cwd=BOTSHEPHERD_DIR,
        )
        if r.returncode != 0:
            logger.error("BS uv venv 创建失败: %s", r.stderr or r.stdout)
            return False

    env = {**os.environ, "VIRTUAL_ENV": venv_dir, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [uv_bin, "pip", "install", "-q", "-r", "requirements.txt"],
        capture_output=True,
        text=True,
        cwd=BOTSHEPHERD_DIR,
        env=env,
        timeout=300,
    )
    if r.returncode != 0:
        logger.error("BS 依赖安装失败: %s", r.stderr or r.stdout)
        return False
    logger.info("BS 依赖同步完成")
    return True


class BSApiClient:
    """异步 BS API 客户端 — 持久 aiohttp.ClientSession + cookie 复用 + 401 自动重登"""

    def __init__(self, manager: "BotShepherdManager"):
        self._mgr = manager
        self._session: Optional[aiohttp.ClientSession] = None
        self._authed = False

    @property
    def _base(self) -> str:
        return f"http://127.0.0.1:{self._mgr.port}"

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(cookie_jar=jar)
            self._authed = False
        return self._session

    async def _login(self) -> bool:
        sess = await self._ensure_session()
        auth = self._mgr._read_bs_auth()
        try:
            async with sess.post(
                f"{self._base}/login",
                data=auth,
                timeout=aiohttp.ClientTimeout(total=5),
                allow_redirects=False,
            ) as r:
                self._authed = r.status in (200, 302)
                return self._authed
        except Exception:
            self._authed = False
            return False

    async def request(self, method: str, path: str, **kwargs) -> Optional[Any]:
        """通用请求：自动登录 + 401 重试"""
        if not self._mgr.running:
            return None
        sess = await self._ensure_session()
        if not self._authed:
            if not await self._login():
                return None
        url = f"{self._base}{path}"
        try:
            async with sess.request(
                method, url, timeout=aiohttp.ClientTimeout(total=10), **kwargs
            ) as resp:
                if resp.status == 401:
                    if await self._login():
                        async with sess.request(
                            method,
                            url,
                            timeout=aiohttp.ClientTimeout(total=10),
                            **kwargs,
                        ) as r2:
                            if r2.status == 200:
                                return await r2.json()
                    return None
                if resp.status == 200:
                    return await resp.json()
                body = await resp.json()
                return {
                    "_error": True,
                    "status": resp.status,
                    **(body if isinstance(body, dict) else {"detail": body}),
                }
        except Exception as e:
            logger.debug("BS API %s %s failed: %s", method, path, e)
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self._authed = False

    def invalidate(self):
        """BS 重启后失效 session"""
        self._authed = False


class BotShepherdManager:
    _LOG_BUFFER_MAX = 500  # 环形缓冲区最大行数

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._port: int = BOTSHEPHERD_DEFAULT_PORT
        self._auto_start: bool = True
        self.api: BSApiClient = None  # type: ignore  — 延迟初始化
        self._log_buffer: collections.deque = collections.deque(
            maxlen=self._LOG_BUFFER_MAX
        )
        self._log_thread: Optional[threading.Thread] = None

    # ---- 进程日志捕获 ----

    def _reader_worker(self, stream) -> None:
        """后台线程：持续从进程 stdout/stderr 读取行并存入环形缓冲区"""
        try:
            for raw_line in iter(stream.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                self._log_buffer.append(line)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def read_logs(self, lines: int = 100) -> List[str]:
        """返回最近 N 行进程控制台输出"""
        buf = list(self._log_buffer)
        return buf[-lines:] if lines < len(buf) else buf

    def _ensure_api(self) -> BSApiClient:
        if self.api is None:
            self.api = BSApiClient(self)
        return self.api

    @property
    def installed(self) -> bool:
        return os.path.isfile(os.path.join(BOTSHEPHERD_DIR, "main.py"))

    @property
    def initialized(self) -> bool:
        return os.path.isdir(os.path.join(BOTSHEPHERD_DIR, "config"))

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def port(self) -> int:
        if self.installed:
            try:
                cfg = os.path.join(BOTSHEPHERD_DIR, "config", "global_config.json")
                if os.path.isfile(cfg):
                    with open(cfg, "r", encoding="utf-8") as f:
                        return json.load(f).get("web_port", self._port)
            except Exception:
                pass
        return self._port

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self.running else None

    def status(self) -> Dict[str, Any]:
        from services.bs_activation_service import bs_activation_service

        return {
            "installed": self.installed,
            "initialized": self.initialized,
            "running": self.running,
            "port": self.port,
            "pid": self.pid,
            "auto_start": self._auto_start,
            "dir": BOTSHEPHERD_DIR,
            "webui_port": self.port if self.running else None,
            "activation": bs_activation_service.status(),
        }

    async def setup(self) -> Dict[str, Any]:
        if not self.installed:
            return {
                "status": "error",
                "message": f"botshepherd/ 目录缺失（检查路径: {BOTSHEPHERD_DIR})",
            }
        logger.info("BotShepherd setup 开始...")

        # 用 uv 确保 BS venv 存在并依赖已安装
        uv_bin = os.environ.get("UV_BIN") or shutil.which("uv")
        if uv_bin:
            ok = await asyncio.to_thread(_bs_ensure_venv, uv_bin)
            if not ok:
                return {
                    "status": "error",
                    "message": "BotShepherd 依赖安装失败，请查看服务端日志",
                }
        else:
            logger.warning("未检测到 uv，跳过依赖安装（若缺少依赖将在启动时报错）")

        # 用 BS venv python 执行 --setup 初始化配置目录
        python = _get_venv_python() or sys.executable
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = await asyncio.to_thread(
            subprocess.run,
            [python, "main.py", "--setup"],
            capture_output=True,
            text=True,
            cwd=BOTSHEPHERD_DIR,
            timeout=300,
            env=env,
        )
        if proc.returncode != 0:
            return {"status": "error", "message": proc.stderr or proc.stdout}
        logger.info("BotShepherd setup 完成")
        return {"status": "ok", "message": "setup complete"}

    async def sync_deps(self) -> Dict[str, Any]:
        """强制重新同步 BS venv 依赖（用于环境损坏时恢复）。"""
        if not self.installed:
            return {"status": "error", "message": "BotShepherd 未安装"}
        uv_bin = os.environ.get("UV_BIN") or shutil.which("uv")
        if not uv_bin:
            return {"status": "error", "message": "未检测到 uv，无法同步依赖"}
        req_file = os.path.join(BOTSHEPHERD_DIR, "requirements.txt")
        venv_dir = os.path.join(BOTSHEPHERD_DIR, "venv")
        cfg_file = os.path.join(venv_dir, "pyvenv.cfg")
        if not os.path.isfile(cfg_file):
            ok = await asyncio.to_thread(_bs_ensure_venv, uv_bin)
            if not ok:
                return {
                    "status": "error",
                    "message": "BS venv 创建失败，请查看服务端日志",
                }
            return {"status": "ok", "message": "BS venv 已重建并同步依赖"}
        env = {**os.environ, "VIRTUAL_ENV": venv_dir, "PYTHONIOENCODING": "utf-8"}
        r = await asyncio.to_thread(
            subprocess.run,
            [uv_bin, "pip", "install", "-q", "-r", req_file],
            capture_output=True,
            text=True,
            cwd=BOTSHEPHERD_DIR,
            env=env,
            timeout=300,
        )
        if r.returncode != 0:
            logger.error("BS sync_deps 失败: %s", r.stderr or r.stdout)
            return {"status": "error", "message": r.stderr or r.stdout}
        logger.info("BS 依赖同步完成")
        return {"status": "ok", "message": "依赖同步完成"}

    def start(self) -> Dict[str, Any]:
        if self.running:
            return {"status": "ok", "message": "already running"}
        if not self.installed:
            return {"status": "error", "message": "not installed"}
        venv_py = _get_venv_python()
        python = venv_py if venv_py else sys.executable
        try:
            kw: Dict[str, Any] = {}
            if sys.platform == "win32":
                kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            # BS WebServer 需要 BOTSHEPHERD_SECRET_KEY 作为 Flask session secret
            if "BOTSHEPHERD_SECRET_KEY" not in env:
                import secrets

                env["BOTSHEPHERD_SECRET_KEY"] = secrets.token_hex(32)
            self._log_buffer.clear()
            self._process = subprocess.Popen(
                [python, "main.py"],
                cwd=BOTSHEPHERD_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                **kw,
            )
            # 后台线程读取进程输出，避免 PIPE 缓冲区满阻塞
            self._log_thread = threading.Thread(
                target=self._reader_worker,
                args=(self._process.stdout,),
                daemon=True,
                name="bs-log-reader",
            )
            self._log_thread.start()
            logger.info("BotShepherd started, PID=%s", self._process.pid)
            self._ensure_api().invalidate()
            return {"status": "ok", "message": f"PID={self._process.pid}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def stop(self) -> Dict[str, Any]:
        if not self.running:
            return {"status": "ok", "message": "not running"}
        try:
            if sys.platform == "win32":
                self._process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self._process.terminate()
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        except Exception as e:
            return {"status": "error", "message": str(e)}
        self._process = None
        if self._log_thread and self._log_thread.is_alive():
            self._log_thread.join(timeout=2)
        self._log_thread = None
        self._ensure_api().invalidate()
        return {"status": "ok", "message": "stopped"}

    def set_auto_start(self, enabled: bool):
        self._auto_start = enabled

    def _read_bs_auth(self) -> Dict[str, str]:
        try:
            cfg = os.path.join(BOTSHEPHERD_DIR, "config", "global_config.json")
            if os.path.isfile(cfg):
                with open(cfg, "r", encoding="utf-8") as f:
                    data = json.load(f)
                auth = data.get("web_auth", {})
                return {
                    "username": auth.get("username", "admin"),
                    "password": auth.get("password", "admin"),
                }
        except Exception:
            pass
        logger.warning(
            "BS global_config.json 缺失 web_auth 配置，使用默认凭据 admin/admin — 请尽快修改"
        )
        return {"username": "admin", "password": "admin"}

    def _read_configs_from_dir(self, subdir: str) -> Dict[str, Any]:
        """从 BS config 子目录读取 JSON 文件（降级用）"""
        result: Dict[str, Any] = {}
        d = os.path.join(BOTSHEPHERD_DIR, "config", subdir)
        if not os.path.isdir(d):
            return result
        for fp in glob.glob(os.path.join(d, "*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    result[os.path.splitext(os.path.basename(fp))[0]] = json.load(f)
            except Exception:
                pass
        return result

    # ---- 连接管理 ----

    async def get_connections(self) -> Dict[str, Any]:
        """获取所有连接配置。

        优先从 API 获取，失败时从文件系统读取。
        如果两者都失败，抛出异常（防止误覆盖用户配置）。
        """
        api = self._ensure_api()
        data = await api.request("GET", "/api/connections")
        if data is not None and not (isinstance(data, dict) and data.get("_error")):
            return {"source": "api", "connections": data}

        # API 失败，尝试从文件读取
        file_configs = self._read_configs_from_dir("connections")
        if file_configs or os.path.isdir(
            os.path.join(BOTSHEPHERD_DIR, "config", "connections")
        ):
            # 如果配置目录存在（即使为空），也认为是有效的
            return {"source": "file", "connections": file_configs}

        # 两者都失败，抛出异常
        raise RuntimeError("无法获取 BS 连接配置：API 不可用且配置目录不存在")

    async def update_connection(self, conn_id: str, config: dict) -> Dict[str, Any]:
        api = self._ensure_api()
        r = await api.request("PUT", f"/api/connections/{conn_id}", json=config)
        if r is None:
            return {"success": False, "error": "BS 未运行或无法连接"}
        return r

    async def copy_connection(self, conn_id: str, body: dict) -> Dict[str, Any]:
        api = self._ensure_api()
        r = await api.request("POST", f"/api/connections/{conn_id}/copy", json=body)
        if r is None:
            return {"success": False, "error": "BS 未运行或无法连接"}
        return r

    async def delete_connection(self, conn_id: str) -> Dict[str, Any]:
        api = self._ensure_api()
        r = await api.request("DELETE", f"/api/connections/{conn_id}")
        if r is None:
            return {"success": False, "error": "BS 未运行或无法连接"}
        return r

    # ---- 账号管理 ----

    async def get_accounts(self) -> Dict[str, Any]:
        api = self._ensure_api()
        data = await api.request("GET", "/api/accounts")
        if data is not None and not (isinstance(data, dict) and data.get("_error")):
            return {"source": "api", "accounts": data}
        return {"source": "file", "accounts": self._read_configs_from_dir("account")}

    async def update_account(self, account_id: str, config: dict) -> Dict[str, Any]:
        api = self._ensure_api()
        r = await api.request("PUT", f"/api/accounts/{account_id}", json=config)
        if r is None:
            return {"success": False, "error": "BS 未运行或无法连接"}
        return r

    async def delete_account(self, account_id: str) -> Dict[str, Any]:
        api = self._ensure_api()
        r = await api.request("DELETE", f"/api/accounts/{account_id}")
        if r is None:
            return {"success": False, "error": "BS 未运行或无法连接"}
        return r

    async def get_account_online(self, account_id: str) -> Dict[str, Any]:
        api = self._ensure_api()
        r = await api.request("GET", f"/api/accounts/{account_id}/online-status")
        if r is None:
            return {"online": False, "error": "BS 未运行或无法连接"}
        return r

    # ---- Bot 框架端点探测 ----

    async def probe_target_endpoint(self, url: str, token: str = "") -> Dict[str, Any]:
        """探测 Bot 框架 WS 端点（AstrBot/NoneBot 等）是否可连。

        携带 OneBot v11 标准头部发起握手（X-Self-ID / X-Client-Role / User-Agent），
        与 BotShepherd 连接时行为一致，避免框架因缺少必要头部而返回 403/400 导致误判。

        返回字段：
          online     — True=在线（握手成功）/ True+note=在线（握手被拒/需认证）/ False=不可达
          latency_ms — 握手耗时（ms），离线时为 None
          note       — 可选补充信息（"handshake_rejected"）
        """
        import time

        t0 = time.time()
        # OneBot v11 标准探测头，与 BotShepherd proxy_connection 保持一致
        headers = {
            "User-Agent": "NapCatManager/1.0 OneBot/11",
            "X-Self-ID": "0",
            "X-Client-Role": "Universal",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            timeout = aiohttp.ClientTimeout(total=3.0, connect=2.0)
            async with aiohttp.ClientSession() as session:
                ws = await session.ws_connect(
                    url, timeout=timeout, heartbeat=None, headers=headers
                )
                await ws.close()
            latency_ms = round((time.time() - t0) * 1000)
            return {"online": True, "latency_ms": latency_ms}
        except aiohttp.WSServerHandshakeError as e:
            # 服务器返回了 HTTP 响应（4xx）→ 端口可达，服务在线，但握手被拒（认证失败等）
            latency_ms = round((time.time() - t0) * 1000)
            if e.status and 400 <= e.status < 500:
                return {
                    "online": True,
                    "latency_ms": latency_ms,
                    "note": "handshake_rejected",
                    "status_code": e.status,
                }
            return {"online": False, "latency_ms": None}
        except Exception:
            return {"online": False, "latency_ms": None}

    # ---- Bot 雷达端点库（持久化） ----

    _RADAR_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "bot_radar_endpoints.json",
    )

    def get_radar_endpoints(self) -> List[Dict[str, Any]]:
        """读取 Bot 雷达端点库（config/bot_radar_endpoints.json）。"""
        try:
            if os.path.isfile(self._RADAR_FILE):
                with open(self._RADAR_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except Exception:
            pass
        return []

    def save_radar_endpoints(self, endpoints: List[Dict[str, Any]]) -> None:
        """覆盖写入 Bot 雷达端点库。字段：alias/url/token。"""
        os.makedirs(os.path.dirname(self._RADAR_FILE), exist_ok=True)
        with open(self._RADAR_FILE, "w", encoding="utf-8") as f:
            json.dump(endpoints, f, indent=2, ensure_ascii=False)

    async def inject_by_alias(
        self,
        alias: str,
        target: str,
        conn_id: str = "",
        container_name: str = "",
        uin: str = "default",
    ) -> Dict[str, Any]:
        """按端点别名执行注入。

        target="bs"  → 将该端点 url 追加到指定 BS 连接的 target_endpoints（热重载生效）
        target="nc"  → 将该端点 url 追加到指定容器 onebot11_{uin}.json 的 websocketClients
        """
        endpoints = self.get_radar_endpoints()
        ep = next((e for e in endpoints if e.get("alias") == alias), None)
        if ep is None:
            return {"success": False, "error": f"别名 '{alias}' 不存在"}

        url = ep.get("url", "")
        token = ep.get("token", "")

        if target == "bs":
            if not conn_id:
                return {"success": False, "error": "注入 BS 需要 conn_id"}
            res = await self.get_connections()
            conn = (res.get("connections") or {}).get(conn_id)
            if not conn:
                return {"success": False, "error": f"BS 连接 '{conn_id}' 不存在"}
            targets: List[str] = list(conn.get("target_endpoints") or [])
            if url in targets:
                return {"success": False, "error": "端点已在 BS 目标列表中"}
            targets.append(url)
            await self.update_connection(conn_id, {**conn, "target_endpoints": targets})
            return {
                "success": True,
                "message": f"已注入 '{alias}' → BS 连接 '{conn_id}'",
            }

        if target == "nc":
            if not container_name:
                return {"success": False, "error": "注入 NC 需要 container_name"}
            from services.config import get_data_dir
            import json as _json

            cfg_path = os.path.join(
                get_data_dir(), container_name, "config", f"onebot11_{uin}.json"
            )
            existing_clients: List[Dict[str, Any]] = []
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        parsed = _json.load(f)
                    wsc = parsed.get("network", {}).get("websocketClients", [])
                    if isinstance(wsc, list):
                        existing_clients = wsc
                except Exception:
                    pass
            if any(c.get("url") == url for c in existing_clients):
                return {"success": False, "error": "端点已在 WS 客户端列表中"}
            new_client = {
                "name": alias or "bot-radar",
                "enable": True,
                "url": url,
                "reportSelfMessage": False,
                "messagePostFormat": "array",
                "token": token or "",
                "debug": False,
                "heartInterval": 30000,
                "reconnectInterval": 30000,
            }
            # 调用现有 inject-network-config 内部逻辑（复用文件写入）
            from routers.container_crud_router import _ALLOWED_NET_KEYS  # noqa
            import json as _json2

            cfg_dir = os.path.join(get_data_dir(), container_name, "config")
            os.makedirs(cfg_dir, exist_ok=True)
            full_cfg: Dict[str, Any] = {}
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        full_cfg = _json2.load(f)
                except Exception:
                    pass
            network = full_cfg.get("network") or {}
            network["websocketClients"] = existing_clients + [new_client]
            full_cfg["network"] = network
            with open(cfg_path, "w", encoding="utf-8") as f:
                _json2.dump(full_cfg, f, indent=2, ensure_ascii=False)
            return {
                "success": True,
                "message": f"已注入 '{alias}' → 容器 '{container_name}' (uin={uin})",
            }

        return {"success": False, "error": f"未知 target: {target}"}

    def _is_known_endpoint(self, endpoint_url: str) -> bool:
        """检查 URL 是否属于管理器自身端点或 Bot 雷达端点库中的已知端点。

        白名单范围：
          1. 管理器自身端点：URL 包含 /ws/napcat/
          2. Bot 雷达端点库：URL 与 bot_radar_endpoints.json 中的某条记录匹配
        """
        if "/ws/napcat/" in endpoint_url:
            return True
        radar_urls = {ep.get("url", "") for ep in self.get_radar_endpoints()}
        return endpoint_url in radar_urls

    async def remove_endpoint_from_bs(
        self, conn_id: str, endpoint_url: str
    ) -> Dict[str, Any]:
        """从指定 BS 连接的 target_endpoints 中移除已知端点。

        安全限制：只允许删除以下两类端点，拒绝删除用户手动配置的未知第三方端点：
          1. 管理器自身端点（URL 包含 /ws/napcat/）
          2. Bot 雷达端点库中的端点（config/bot_radar_endpoints.json）

        Args:
            conn_id: BS 连接 ID
            endpoint_url: 要移除的端点 URL

        Returns:
            {"success": bool, "message": str, "removed": bool}
        """
        # 安全检查：只允许删除管理器自身端点或雷达库中的已知端点
        if not self._is_known_endpoint(endpoint_url):
            return {
                "success": False,
                "error": (
                    "安全限制：只能删除管理器自身端点（/ws/napcat/*）"
                    "或 Bot 雷达端点库中的已知端点，不能删除用户配置的未知第三方端点"
                ),
            }

        res = await self.get_connections()
        conn = (res.get("connections") or {}).get(conn_id)
        if not conn:
            return {"success": False, "error": f"BS 连接 '{conn_id}' 不存在"}

        targets: List[str] = list(conn.get("target_endpoints") or [])
        if endpoint_url not in targets:
            return {
                "success": True,
                "message": "端点不在目标列表中（已是期望状态）",
                "removed": False,
            }

        targets.remove(endpoint_url)
        await self.update_connection(conn_id, {**conn, "target_endpoints": targets})
        return {
            "success": True,
            "message": f"已从 BS 连接 '{conn_id}' 移除管理器端点: {endpoint_url}",
            "removed": True,
        }


botshepherd_manager = BotShepherdManager()
