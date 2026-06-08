"""
异步 Docker 管理 + 登录检测器

AsyncLoginChecker  — aiohttp 并发登录探测（OneBot HTTP / WebUI 双路）
AsyncDockerManager — aiodocker 替代 docker-py 热路径，零线程池开销
"""
import asyncio
import json
import os
import re
import time
from typing import Dict, List, Optional

import aiohttp
import aiodocker

from services.log import logger
from services.config import get_data_dir


_LOGIN_TIMEOUT = aiohttp.ClientTimeout(total=2, connect=1)
_INFO_TIMEOUT = aiohttp.ClientTimeout(total=1.5, connect=0.8)
_MAX_CONCURRENCY = 30  # 同时最多 30 个 HTTP 探测

_DOCKER_LIST_TIMEOUT = float(os.environ.get("DOCKER_LIST_TIMEOUT", "20"))
_CONTAINER_RESTART_TIMEOUT = int(os.environ.get("CONTAINER_RESTART_TIMEOUT", "60"))
_CONTAINER_STOP_TIMEOUT = int(os.environ.get("CONTAINER_STOP_TIMEOUT", "10"))
_CONTAINER_ACTION_VERIFY_TIMEOUT = float(os.environ.get("CONTAINER_ACTION_VERIFY_TIMEOUT", "20"))
_CONTAINER_ACTION_VERIFY_INTERVAL = float(os.environ.get("CONTAINER_ACTION_VERIFY_INTERVAL", "1"))



def get_host_gateway() -> str:
    """返回 ncqq-manager 容器内访问宿主机的地址。

    场景：ncqq-manager 自己运行在 Docker 容器内时，
    不能用 127.0.0.1 访问宿主机映射端口（那会指向 ncqq-manager 容器自身）。

    优先级：
      1) 环境变量 HOST_GATEWAY
      2) host.docker.internal（部分环境支持）
      3) /proc/net/route 默认网关
    """
    env = (os.environ.get("HOST_GATEWAY") or '').strip()
    if env:
        return env
    try:
        import socket
        return socket.gethostbyname('host.docker.internal')
    except Exception:
        pass
    try:
        with open('/proc/net/route','r',encoding='utf-8') as f:
            # Iface Destination Gateway Flags ...
            for line in f.readlines()[1:]:
                parts=line.strip().split()
                if len(parts) >= 3 and parts[1] == '00000000':
                    gw_hex=parts[2]
                    # little-endian hex to ip
                    b=[str(int(gw_hex[i:i+2],16)) for i in range(6,-2,-2)]
                    return '.'.join(b)
    except Exception:
        pass
    return '127.0.0.1'


def _host_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}{path}"



class AsyncLoginChecker:
    """异步登录状态检测器 — 替代 docker_manager 中的同步 urllib 探测。"""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        """创建共享 HTTP 连接池。"""
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=60),
            headers={"User-Agent": "NapCatManager/1.0"},
        )
        logger.info("异步登录检测器已启动")

    async def stop(self):
        """关闭连接池。"""
        if self._session:
            await self._session.close()
            self._session = None

    async def _exec_in_container(self, name: str, sh_cmd: str, timeout: int = 3) -> str:
        """通过 Docker exec 在目标容器内执行命令并返回 stdout。"""
        def _run():
            import docker
            client = docker.from_env()
            c = client.containers.get(name)
            r = c.exec_run(["sh", "-lc", sh_cmd])
            out = r.output or b""
            try:
                return out.decode("utf-8", errors="ignore")
            except Exception:
                return str(out)

        try:
            return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
        except Exception:
            return ""

    async def _get_uin_via_container_fs(self, name: str) -> str:
        """从容器内 /app/napcat/config 的文件名提取 uin（无宿主机目录挂载时使用）。"""
        out = await self._exec_in_container(
            name,
            'ls -t /app/napcat/config/onebot11_*.json 2>/dev/null | head -n 1 || true',
            timeout=2,
        )
        path = (out or '').strip().split("\n")[0].strip()
        m = re.search(r"onebot11_(\d+)\.json", path)
        if m:
            return m.group(1)

        out = await self._exec_in_container(
            name,
            'ls -t /app/napcat/config/napcat_*.json 2>/dev/null | head -n 1 || true',
            timeout=2,
        )
        path = (out or '').strip().split("\n")[0].strip()
        m = re.search(r"napcat_(\d+)\.json", path)
        return m.group(1) if m else ""

    async def _qr_stale_via_container_fs(self, name: str, max_age: int = 60) -> bool:
        """判断容器内 qrcode.png 是否停止刷新（无文件/过旧=stale）。"""
        # busybox 可能没有 stat -c，优先用 python3；没有 python3 时直接认为 stale
        cmd = (
            "python3 -c \"import os,time; p='/app/napcat/cache/qrcode.png'; print('missing' if not os.path.exists(p) else int(time.time()-os.path.getmtime(p)))\" 2>/dev/null || echo missing"
        )
        out = await self._exec_in_container(name, cmd, timeout=2)
        v = (out or '').strip().split("\n")[0].strip()
        if v == 'missing' or not v:
            # 无文件或无输出 → 未知状态，默认 not stale（不倒向"已登录"方向）
            return False
        try:
            return int(v) > max_age
        except Exception:
            return False

    async def _get_container_started_at(self, name: str) -> float:
        """获取容器的 Docker StartedAt 时间戳（Unix seconds），失败返回 0。"""
        def _run():
            import docker
            from datetime import datetime, timezone
            client = docker.from_env()
            c = client.containers.get(name)
            started_at_str = c.attrs.get("State", {}).get("StartedAt", "")
            if not started_at_str:
                return 0.0
            # Docker ISO 8601: "2026-04-11T12:30:00.123456789Z" 或 "2026-04-11T12:30:00Z"
            dt_str = started_at_str.split(".")[0].rstrip("Z")
            dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.timestamp()
        try:
            return await asyncio.wait_for(asyncio.to_thread(_run), timeout=3)
        except Exception:
            return 0.0

    async def _qr_from_this_session_via_container_fs(self, name: str) -> bool:
        """判断容器内 qrcode.png 是否在本次容器会话中生成（mtime > 容器启动时间）。"""
        cmd = (
            "python3 -c \""
            "import os,time; p='/app/napcat/cache/qrcode.png'; "
            "up=float(open('/proc/uptime').read().split()[0]); "
            "start=time.time()-up; "
            "print('yes' if os.path.exists(p) and os.path.getmtime(p)>start else 'no')"
            "\" 2>/dev/null || echo no"
        )
        out = await self._exec_in_container(name, cmd, timeout=2)
        return (out or '').strip().split("\n")[0].strip() == 'yes'

    async def check_login_via_container_exec(self, name: str) -> Dict:
        """Container login check with adaptive fallback."""
        cmd = (
            "python3 -c \""
            "import urllib.request,json; "
            "r=urllib.request.urlopen(urllib.request.Request("
            "'http://127.0.0.1:3000/get_login_info',"
            "data=b'{}',headers={'Content-Type':'application/json'}),"
            "timeout=2); "
            "d=json.loads(r.read()); "
            "uid=str(d.get('data',{}).get('user_id','')); "
            "print(uid if d.get('status')=='ok' and uid and uid!='0' else '')"
            "\" 2>/dev/null || echo ''"
        )
        out = await self._exec_in_container(name, cmd, timeout=4)
        uid = (out or '').strip().split("\n")[0].strip()
        if uid and uid.isdigit() and uid != "0":
            return {
                "logged_in": True,
                "uin": uid,
                "nickname": "",
                "method": "container_exec",
                "stage": "logged_in",
                "reason": "onebot_via_container_exec",
            }

        try:
            logs = await self._exec_in_container(
                name,
                "docker logs --tail 200 {} 2>/dev/null || true".format(name),
                timeout=4,
            )
            text = logs or ""
            if ("接收 <-" in text) or ("发送 ->" in text):
                uin = await self._get_uin_via_container_fs(name)
                if not uin:
                    try:
                        uin = await asyncio.to_thread(self._get_uin_from_config, name)
                    except Exception:
                        uin = ""
                return {
                    "logged_in": True,
                    "uin": uin or "",
                    "nickname": "",
                    "method": "container_log_signal",
                    "stage": "logged_in",
                    "reason": "message_flow_detected_in_container_logs",
                }
        except Exception:
            pass

        return {"logged_in": False, "stage": "waiting"}
    # ============ 单容器检测 ============

    async def check_login_onebot(self, http_port: int) -> Dict:
        """方案 A：OneBot HTTP API /get_login_info"""
        if not http_port or not self._session:
            return {"logged_in": False, "stage": "waiting"}
        try:
            async with self._session.post(
                 _host_url(get_host_gateway(), http_port, "/get_login_info"),
                json={},
                timeout=_LOGIN_TIMEOUT,
            ) as resp:
                result = await resp.json(content_type=None)
            if result.get("status") == "ok" and result.get("data", {}).get("user_id"):
                uid = str(result["data"]["user_id"])
                if uid and uid != "0":
                    return {
                        "logged_in": True,
                        "uin": uid,
                        "nickname": result["data"].get("nickname", ""),
                        "method": "onebot",
                        "stage": "logged_in",
                        "reason": "onebot_http_ready",
                    }
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError,
                ValueError, KeyError):
            pass
        return {"logged_in": False, "stage": "waiting"}

    async def _check_login_via_filesystem(self, name: str, webui_port: int) -> Dict:
        """文件系统辅助检测（第5级）— 仅作为辅助信号，不再作为强确认依据。

        改进历史：
          v1: qr_stale + uin + webui_alive → logged_in (误判：QR 过期也算 stale)
          v2: qr_from_this_session 硬否决 → logged_in: False (误判：扫码登录后也永远否决)
          v3 (当前): filesystem 不再直接判定 logged_in，仅提供辅助信号。
              真正的登录确认由 Level 3.5 (container_exec) 完成。
              filesystem 仅在 WebUI 活跃 + 无本次 QR + 有 uin 时作为弱信号判定已登录。
        """
        from services.config import get_data_dir
        # 文件系统判定不再依赖 WebUI 可达性，避免网络/端口映射导致误判
        try:
            # 1. 从文件名发现 uin
            uin = await asyncio.to_thread(self._get_uin_from_config, name)
            if not uin:
                uin = await self._get_uin_via_container_fs(name)
            if not uin:
                return {"logged_in": False, "stage": "waiting"}

            # 3. 检查本次容器会话是否产生过二维码（仅作为辅助状态字段）
            qr_from_this_session = False
            try:
                container_started_at = await self._get_container_started_at(name)
                qr_path = os.path.join(get_data_dir(), name, "cache", "qrcode.png")
                exists = await asyncio.to_thread(os.path.exists, qr_path)
                if exists:
                    if container_started_at > 0:
                        mtime = await asyncio.to_thread(os.path.getmtime, qr_path)
                        qr_from_this_session = mtime > container_started_at
                    else:
                        qr_from_this_session = await self._qr_from_this_session_via_container_fs(name)
                else:
                    qr_from_this_session = await self._qr_from_this_session_via_container_fs(name)
            except Exception:
                pass

            if qr_from_this_session:
                # 仅当二维码是“近期刷新”的情况下才判定为待登录；
                # 旧二维码文件（历史残留）不应阻止已登录判定。
                qr_recent = False
                try:
                    qr_path = os.path.join(get_data_dir(), name, "cache", "qrcode.png")
                    if await asyncio.to_thread(os.path.exists, qr_path):
                        mtime = await asyncio.to_thread(os.path.getmtime, qr_path)
                        qr_recent = (time.time() - mtime) < 180
                except Exception:
                    qr_recent = False

                if qr_recent:
                    logger.debug(
                        "登录检测[%s] 文件系统: 检测到近期二维码刷新，判定待登录",
                        name,
                    )
                    return {"logged_in": False, "stage": "ambiguous",
                            "reason": "filesystem_recent_qr",
                            "uin": uin}

            # 配置文件中的 uin 只能作为 last_uin 线索；不能确认当前在线。
            return {"logged_in": False, "stage": "waiting", "uin": uin, "reason": "filesystem_last_uin_only"}
        except Exception:
            pass
        return {"logged_in": False, "stage": "waiting"}

    async def check_login_status(self, name: str,
                                  http_port: int, webui_port: int) -> Dict:
        """五级级联检测：SDK WS → BS API → HTTP 兜底 → 容器内exec → 文件系统辅助。

        优先级：
          1. napcat_ws_service（零网络开销，WS 已连接时直接返回）
          2. BS 账号 API（BS 运行时辅助检测，10s TTL 缓存）
          3. OneBot HTTP /get_login_info（兜底，仅 WS/BS 均无结果时请求）
          3.5. 容器内 docker exec 请求 127.0.0.1:3000（绕过端口映射 / WS 403）
          4. 文件系统辅助（弱信号：无本次 QR + WebUI 活跃 + 有 uin → token 自动登录）
        """
        from services.napcat_ws_service import napcat_ws_service

        # 1. SDK WS 仅作为辅助信号，不直接作为登录真值源（避免残留假在线）
        r1 = napcat_ws_service.get_login_result(name)

        # 2. BS 账号 API 辅助（次路径）
        r2 = await napcat_ws_service.check_via_bs(name)
        if r2["logged_in"]:
            logger.debug("登录检测[%s] BS辅助命中 uin=%s", name, r2.get("uin"))
            return r2

        # 3. OneBot HTTP 兜底（仅 WS 未连接 + BS 无信号时）
        if http_port:
            r3 = await self.check_login_onebot(http_port)
            if r3["logged_in"]:
                r3_uin = r3.get("uin", "")
                if r3_uin:
                    napcat_ws_service.ensure_uin(name, r3_uin)
                logger.debug("登录检测[%s] HTTP兜底命中 uin=%s", name, r3.get("uin"))
                return r3

        # 3.5. 容器内 OneBot 检测 — 绕过端口映射，直连容器内 127.0.0.1:3000
        # 适用于 http_port=0（未映射到宿主机）或宿主机网络不通的场景
        r35 = await self.check_login_via_container_exec(name)
        if r35["logged_in"]:
            r35_uin = r35.get("uin", "")
            if r35_uin:
                napcat_ws_service.ensure_uin(name, r35_uin)
            logger.debug("登录检测[%s] 容器内exec命中 uin=%s", name, r35.get("uin"))
            return r35

        # 4. 文件系统仅用于辅助信息，不作为已登录真值源（避免历史文件误判）
        r4 = {"logged_in": False, "stage": "waiting"}

        # 均无信号：保留最佳 stage（优先取有 uin 的结果）
        stage = r1.get("stage") or r2.get("stage") or r4.get("stage", "") or "waiting"
        return {"logged_in": False, "stage": stage}

    # ============ 批量检测 ============

    async def batch_check_login(
        self, instances: list, concurrency: int = _MAX_CONCURRENCY,
    ) -> Dict[str, Dict]:
        """批量并发检测登录状态。

        Args:
            instances: ContainerInstance 列表（需有 name, http_port, webui_port）
            concurrency: 最大并发数
        Returns:
            {name: {logged_in, uin?, ...}}
        """
        sem = asyncio.Semaphore(concurrency)
        results: Dict[str, Dict] = {}

        async def _check_one(inst):
            async with sem:
                try:
                    r = await asyncio.wait_for(
                        self.check_login_status(
                            inst.name, inst.http_port, inst.webui_port),
                        timeout=4,
                    )
                    results[inst.name] = r
                except (asyncio.TimeoutError, Exception):
                    results[inst.name] = {"logged_in": False, "stage": "unknown"}

        await asyncio.gather(*[_check_one(i) for i in instances])
        return results

    # ============ 内部辅助 ============

    async def _fetch_json(self, url: str, timeout: aiohttp.ClientTimeout) -> Optional[Dict]:
        """通用 GET JSON 请求，异常返回 None。"""
        if not self._session:
            return None
        try:
            async with self._session.get(url, timeout=timeout) as resp:
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError,
                json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _extract_uin_from_dir(config_dir: str) -> str:
        """从指定目录的 onebot11_*.json / napcat_*.json 文件名提取 uin。"""
        try:
            if not os.path.exists(config_dir):
                return ""
            ob_files = [
                f for f in os.listdir(config_dir)
                if f.startswith("onebot11_") and f.endswith(".json")
            ]
            if ob_files:
                latest = max(
                    ob_files,
                    key=lambda fn: os.path.getmtime(os.path.join(config_dir, fn)),
                )
                raw = latest.replace("onebot11_", "").replace(".json", "")
                return ''.join(ch for ch in str(raw) if ch.isdigit())
            napcat_files = [
                f for f in os.listdir(config_dir)
                if f.startswith("napcat_") and f.endswith(".json")
                and not f.startswith("napcat_protocol_")
            ]
            if napcat_files:
                latest = max(
                    napcat_files,
                    key=lambda fn: os.path.getmtime(os.path.join(config_dir, fn)),
                )
                raw = latest.replace("napcat_", "").replace(".json", "")
                return ''.join(ch for ch in str(raw) if ch.isdigit())
        except OSError:
            pass
        return ""

    @staticmethod
    def _get_uin_from_config(name: str) -> str:
        """兼容多种数据布局提取 uin。

        优先顺序：
          1) ncqq-manager 默认数据目录 /app/data/<name>/config
          2) 通过 Docker Mounts 推断的宿主机目录（如 /NEKRO_*/napcat_data/napcat）
        """
        default_dir = os.path.join(get_data_dir(), name, "config")
        uin = AsyncLoginChecker._extract_uin_from_dir(default_dir)
        if uin:
            return uin

        try:
            import docker
            client = docker.from_env()
            c = client.containers.get(name)
            mounts = c.attrs.get("Mounts", []) or []
            candidates = []
            for m in mounts:
                src = (m or {}).get("Source", "")
                dst = (m or {}).get("Destination", "")
                if not src or not dst:
                    continue
                dst_l = dst.lower()
                if "napcat/config" in dst_l or dst_l.endswith("/napcat") or "/napcat/" in dst_l:
                    candidates.append(src)
                if "qq" in dst_l and os.path.isdir(src):
                    candidates.append(os.path.join(os.path.dirname(src), "napcat"))
            seen = set()
            for cdir in candidates:
                if not cdir or cdir in seen:
                    continue
                seen.add(cdir)
                uin = AsyncLoginChecker._extract_uin_from_dir(cdir)
                if uin:
                    return uin
        except Exception:
            pass
        return ""


# ============ 单例 — 登录检测 ============
async_login_checker = AsyncLoginChecker()


# ============================================================
#  AsyncDockerManager — aiodocker 替代 docker-py 热路径
# ============================================================


class AsyncDockerManager:
    """异步 Docker 管理器 — 零线程 aiodocker 替代 docker-py。

    热路径方法（Phase 1 — 状态引擎用）：
      - list_local_containers()  → 替代 docker_manager.list_containers()
      - resolve_ports(names)     → 替代 _resolve_ports()

    CRUD 方法（后续优化 — 路由层用）：
      - action_container(name, action)   → 替代 docker_manager.action_container()
      - create_container(name, ...)      → 替代 docker_manager.create_container()
      - get_logs(name, tail)             → 替代 cluster_manager.get_logs() 本地分支
      - get_used_ports()                 → 替代 docker_manager.get_used_ports()
    """

    def __init__(self):
        self._docker: Optional[aiodocker.Docker] = None
        self._action_locks: Dict[str, asyncio.Lock] = {}

    async def start(self):
        """创建 aiodocker 连接（自动探测 Windows npipe / Linux socket）。"""
        self._docker = aiodocker.Docker()
        logger.info("异步Docker管理器已启动")

    async def stop(self):
        """关闭 aiodocker 连接。"""
        if self._docker:
            await self._docker.close()
            self._docker = None

    @property
    def connected(self) -> bool:
        return self._docker is not None

    # ---- 1. 容器列表（替代 docker_manager.list_containers） ----

    async def list_local_containers(self) -> List[Dict]:
        """异步获取本地 NapCat 容器列表。

        返回格式与 docker_manager.list_containers() 一致：
        [{id, name, status, image, created}, ...]
        """
        if not self._docker:
            return []
        try:
            raw_list = await asyncio.wait_for(
                self._docker.containers.list(all=True), timeout=_DOCKER_LIST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("异步容器列表获取超时 %.1fs", _DOCKER_LIST_TIMEOUT)
            return []
        except aiodocker.exceptions.DockerError as e:
            logger.debug("异步容器列表获取失败: %s", e)
            return []

        results: List[Dict] = []
        from services.config import get_container_keywords
        keywords = get_container_keywords()
        for c in raw_list:
            d = c._container
            names = d.get("Names", [])
            name = names[0].lstrip("/") if names else ""
            image = d.get("Image", "")
            if not any(kw in image.lower() or kw in name.lower() for kw in keywords):
                continue
            results.append({
                "id": d.get("Id", "")[:12],
                "name": name,
                "status": d.get("State", "created"),
                "image": image,
                "created": d.get("Created", ""),
            })
        return results

    # ---- 2. 端口解析（替代 _resolve_ports） ----

    async def resolve_ports(self, names: List[str]) -> Dict[str, Dict]:
        """异步批量解析容器端口映射（inspect → NetworkSettings.Ports）。"""
        if not self._docker:
            return {n: {"http_port": 0, "webui_port": 0} for n in names}

        async def _resolve_one(name: str) -> tuple:
            try:
                container = await self._docker.containers.get(name)
                info = await container.show()
                ports = info.get("NetworkSettings", {}).get("Ports", {}) or {}
                return name, {
                    "http_port": self._extract_host_port(ports, "3000/tcp"),
                    "webui_port": self._extract_host_port(ports, "6099/tcp"),
                }
            except Exception as e:
                logger.debug("端口解析失败 [%s]: %s", name, e)
                return name, {"http_port": 0, "webui_port": 0}

        pairs = await asyncio.gather(*[_resolve_one(n) for n in names])
        return dict(pairs)

    # ---- 3. 容器操作（CRUD 异步化 — 替代 docker_manager.action_container） ----

    def _get_action_lock(self, key: str) -> asyncio.Lock:
        """按节点/容器串行化生命周期操作，避免连续点击互相打架。"""
        lock = self._action_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._action_locks[key] = lock
        return lock

    async def _wait_container_state(self, name: str, desired_running: bool, timeout: float = _CONTAINER_ACTION_VERIFY_TIMEOUT) -> bool:
        """有限等待 Docker inspect 状态；只确认 running，不等待 QQ 登录。"""
        if not self._docker:
            return False
        deadline = time.monotonic() + timeout
        last_state = "unknown"
        while time.monotonic() < deadline:
            try:
                container = await self._docker.containers.get(name)
                info = await container.show()
                state = info.get("State", {}) or {}
                running = bool(state.get("Running"))
                last_state = str(state.get("Status") or ("running" if running else "stopped"))
                if running == desired_running:
                    return True
            except aiodocker.exceptions.DockerError as e:
                last_state = f"docker-error:{e}"
                if not desired_running:
                    # delete 或 stop 后容器不存在也视为不在运行。
                    status = getattr(e, "status", None)
                    if status == 404:
                        return True
            await asyncio.sleep(_CONTAINER_ACTION_VERIFY_INTERVAL)
        logger.warning(
            "容器 %s 状态确认超时: desired_running=%s timeout=%.1fs last_state=%s",
            name, desired_running, timeout, last_state,
        )
        return False

    async def action_container(self, name: str, action: str, node_id: str = "local") -> bool:
        """异步执行容器操作（start/stop/restart/pause/unpause/kill/delete）。"""
        if not self._docker:
            return False
        lock_key = f"{node_id or 'local'}:{name}"
        lock = self._get_action_lock(lock_key)
        start_ts = time.monotonic()
        async with lock:
            try:
                container = await self._docker.containers.get(name)
                if action == "start":
                    await container.start()
                elif action == "stop":
                    await container.stop(t=_CONTAINER_STOP_TIMEOUT, timeout=_CONTAINER_STOP_TIMEOUT + 10)
                elif action == "restart":
                    try:
                        await container.stop(t=_CONTAINER_RESTART_TIMEOUT, timeout=_CONTAINER_RESTART_TIMEOUT + 10)
                    except aiodocker.exceptions.DockerError as stop_e:
                        # Docker may report "already stopped"; start below will surface real failures.
                        logger.debug("容器 %s restart stop 阶段返回: %s", name, stop_e)
                    await container.start()
                elif action == "pause":
                    await container.pause()
                elif action == "unpause":
                    await container.unpause()
                elif action == "kill":
                    await container.kill()
                elif action == "delete":
                    try:
                        await container.stop(t=2, timeout=10)
                    except aiodocker.exceptions.DockerError:
                        pass
                    await container.delete(force=True)
                else:
                    logger.warning("未知操作: %s", action)
                    return False
                elapsed = time.monotonic() - start_ts
                logger.info("容器 %s 异步执行 [%s] 成功 (elapsed=%.2fs)", name, action, elapsed)
                return True
            except asyncio.TimeoutError as e:
                elapsed = time.monotonic() - start_ts
                logger.error("容器 %s 异步执行 [%s] 超时 (elapsed=%.2fs): %s", name, action, elapsed, e)
                return False
            except aiodocker.exceptions.DockerError as e:
                elapsed = time.monotonic() - start_ts
                logger.error("容器 %s 异步执行 [%s] Docker失败 (elapsed=%.2fs): %s", name, action, elapsed, e)
                return False
            except Exception as e:
                elapsed = time.monotonic() - start_ts
                logger.exception("容器 %s 异步执行 [%s] 异常 (elapsed=%.2fs): %s", name, action, elapsed, e)
                return False

    async def restart_container(self, name: str, timeout: int = 30) -> None:
        """异步重启容器（注入后刷新 NapCat 配置使用）。
        timeout: 等待容器停止的秒数，超时后强制 kill。
        """
        if not self._docker:
            raise RuntimeError("AsyncDockerManager 未连接 Docker daemon")
        container = await self._docker.containers.get(name)
        await container.restart(t=timeout, timeout=timeout + 15)
        logger.info("容器 %s 重启完成（timeout=%ds）", name, timeout)

    # ---- 5. 容器创建（CRUD 异步化 — 替代 docker_manager.create_container） ----

    async def create_container(
        self, name: str, image: str,
        volumes: Optional[Dict] = None,
        ports: Optional[Dict] = None,
        environment: Optional[Dict] = None,
        restart_policy: Optional[Dict] = None,
        mem_limit: Optional[str] = None,
        network_mode: Optional[str] = None,
    ) -> Optional[str]:
        """异步创建并启动容器（aiodocker API 格式）。"""
        if not self._docker:
            return None
        try:
            # aiodocker 使用 Docker Engine API 原始格式
            host_config: Dict = {}
            if volumes:
                binds = [
                    f"{host_path}:{mount['bind']}:{mount.get('mode', 'rw')}"
                    for host_path, mount in volumes.items()
                ]
                host_config["Binds"] = binds
            if ports:
                # ports 格式: {"6099/tcp": 6001, "3000/tcp": 3001}
                exposed = {}
                port_bindings = {}
                for container_port, host_port in ports.items():
                    exposed[container_port] = {}
                    port_bindings[container_port] = [{"HostPort": str(host_port)}]
                host_config["PortBindings"] = port_bindings
            if restart_policy:
                host_config["RestartPolicy"] = restart_policy
            if mem_limit:
                # "512m" → bytes
                val = mem_limit.rstrip("m")
                host_config["Memory"] = int(val) * 1024 * 1024
            if network_mode and network_mode != "bridge":
                host_config["NetworkMode"] = network_mode

            config: Dict = {
                "Image": image,
                "Env": [f"{k}={v}" for k, v in (environment or {}).items()],
                "HostConfig": host_config,
            }
            if ports:
                config["ExposedPorts"] = {p: {} for p in ports}

            try:
                container = await self._docker.containers.create_or_replace(
                    name=name, config=config,
                )
            except aiodocker.exceptions.DockerError as pull_e:
                if pull_e.status == 404:
                    # 镜像本地不存在，自动拉取后重试
                    logger.info("镜像 %s 本地不存在，自动拉取中（首次部署可能需要数分钟）...", image)
                    await self._docker.images.pull(image)
                    logger.info("镜像 %s 拉取完成，重试创建容器...", image)
                    container = await self._docker.containers.create_or_replace(
                        name=name, config=config,
                    )
                else:
                    raise
            await container.start()
            info = await container.show()
            short_id = info.get("Id", "")[:12]
            logger.info("容器 %s 异步创建成功 (id=%s)", name, short_id)
            return short_id
        except aiodocker.exceptions.DockerError as e:
            logger.error("异步创建容器 %s 失败: %s", name, e)
            return None

    # ---- 6. 容器日志（CRUD 异步化 — 替代 cluster_manager.get_logs） ----

    async def get_logs(self, name: str, tail: int = 100) -> str:
        """异步获取容器日志。"""
        if not self._docker:
            return ""
        try:
            container = await self._docker.containers.get(name)
            log_lines = await container.log(
                stdout=True, stderr=True, tail=tail,
            )
            return "\n".join(log_lines)
        except aiodocker.exceptions.DockerError as e:
            logger.debug("异步获取容器 %s 日志失败: %s", name, e)
            return ""

    # ---- 7. 已用端口查询（CRUD 异步化） ----

    async def get_used_ports(self) -> set:
        """异步获取所有容器已用的宿主机端口。"""
        if not self._docker:
            return set()
        used = set()
        try:
            containers = await asyncio.wait_for(
                self._docker.containers.list(all=True), timeout=_DOCKER_LIST_TIMEOUT,
            )
            for c in containers:
                info = c._container
                ports = info.get("Ports", [])
                for p in ports:
                    if isinstance(p, dict) and p.get("PublicPort"):
                        used.add(p["PublicPort"])
        except asyncio.TimeoutError:
            logger.warning("异步获取 Docker 已用端口超时 %.1fs", _DOCKER_LIST_TIMEOUT)
        except aiodocker.exceptions.DockerError as e:
            logger.debug("异步获取 Docker 已用端口失败: %s", e)
        return used

    # ---- 8. 镜像管理（替代 docker_manager 同步版） ----

    async def list_images(self) -> List[Dict]:
        """异步列出本地 Docker 镜像。"""
        if not self._docker:
            return []
        try:
            images = await self._docker.images.list()
            result = []
            for img in images:
                tags = img.get("RepoTags") or []
                size_mb = round(img.get("Size", 0) / 1024 / 1024, 1)
                created = img.get("Created", "")
                img_id = img.get("Id", "")
                if img_id.startswith("sha256:"):
                    img_id = img_id[7:19]
                else:
                    img_id = img_id[:12]
                result.append({
                    "id": img_id,
                    "tags": tags,
                    "size": size_mb,
                    "created": created,
                })
            return result
        except aiodocker.exceptions.DockerError as e:
            logger.error("异步列举镜像失败: %s", e)
            return []

    async def pull_image(self, image_name: str) -> bool:
        """异步拉取 Docker 镜像。"""
        if not self._docker:
            return False
        try:
            await self._docker.images.pull(image_name)
            logger.info("异步镜像拉取成功: %s", image_name)
            return True
        except aiodocker.exceptions.DockerError as e:
            logger.error("异步镜像拉取失败 %s: %s", image_name, e)
            return False

    async def delete_image(self, image_id: str, force: bool = False) -> bool:
        """异步删除 Docker 镜像。"""
        if not self._docker:
            return False
        try:
            await self._docker.images.delete(image_id, force=force)
            logger.info("异步镜像删除成功: %s", image_id)
            return True
        except aiodocker.exceptions.DockerError as e:
            logger.error("异步镜像删除失败 %s: %s", image_id, e)
            return False

    @staticmethod
    def find_available_port(base: int, used_ports: set) -> int:
        """从 base 开始找到下一个可用端口（纯计算，不涉及 Docker API）。"""
        port = base
        while port in used_ports:
            port += 1
            if port > 65535:
                raise ValueError(f"没有可用端口（从 {base} 开始，所有端口均被占用）")
        return port

    # ---- 内部辅助 ----

    @staticmethod
    def _extract_host_port(ports: Dict, internal: str) -> int:
        """从 NetworkSettings.Ports 提取宿主机映射端口。"""
        try:
            bindings = ports.get(internal)
            if bindings and isinstance(bindings, list):
                return int(bindings[0]["HostPort"])
        except (KeyError, IndexError, ValueError, TypeError):
            pass
        return 0

    @staticmethod
    def _parse_stats(s: Dict) -> Dict:
        """解析 Docker stats JSON → {cpu_percent, mem_usage, mem_limit}。

        CPU 公式：(cpu_delta / system_delta) * num_cpus * 100
        """
        mem_usage = s.get("memory_stats", {}).get("usage", 0)
        mem_limit = s.get("memory_stats", {}).get("limit", 0)
        cpu_delta = (
            s.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
            - s.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
        )
        system_delta = (
            s.get("cpu_stats", {}).get("system_cpu_usage", 0)
            - s.get("precpu_stats", {}).get("system_cpu_usage", 0)
        )
        cpu_percent = 0.0
        if system_delta > 0 and cpu_delta > 0:
            percpu = s.get("cpu_stats", {}).get(
                "cpu_usage", {}).get("percpu_usage") or [1]
            cpu_percent = (cpu_delta / system_delta) * len(percpu) * 100.0
        return {
            "cpu_percent": round(cpu_percent, 2),
            "mem_usage": round(mem_usage / 1024 / 1024, 2),
            "mem_limit": round(mem_limit / 1024 / 1024, 2),
        }


# ============ 单例 — Docker 管理 ============
async_docker_manager = AsyncDockerManager()

