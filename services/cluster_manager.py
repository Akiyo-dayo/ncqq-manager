"""
集群/节点管理器 — SQLite 持久化，aiohttp 全异步通信（节点状态/代理/操作）。

所有远程调用统一走 :meth:`ClusterManager.call`：按操作类别取超时、对瞬时连接错误
重试一次、把失败原因归类后写回 nodes 表，UI 因此能显示「密钥不匹配」而不是笼统的
「离线」。本地分支一律走异步 Docker 客户端或线程池，不阻塞事件循环。
"""
import asyncio
import json
import ssl
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp

from services.log import logger
from services.config import CONFIG_FILE, APP_VERSION
from services.docker_manager import docker_manager
import services.database as db


_NODES_CACHE_TTL = 10  # 节点状态缓存有效期（秒）

# 按操作类别分级的超时（秒）。跨境链路上 2 秒是必然抖动的，
# 而拉镜像建容器又可能要几分钟 —— 用一个数字覆盖两者是之前最大的问题。
_TIMEOUTS: Dict[str, float] = {
    "health": 5.0,
    "list": 8.0,
    "read": 10.0,
    "lifecycle": 20.0,
    "heavy": 180.0,
}
_CONNECT_TIMEOUT = 4.0

# 连续失败达到该次数后节点进入 degraded：健康检查降频，其余请求快速失败，
# 避免一个挂掉的节点把每个请求都拖满超时。
_DEGRADED_AFTER_FAILURES = 3
_DEGRADED_PROBE_INTERVAL = 30.0

# 这些失败是"对方明确拒绝"，重试没有意义。
_NON_RETRYABLE = {"dns", "refused", "tls", "unauthorized", "forbidden", "invalid_address"}


@dataclass
class NodeCallResult:
    """一次远程调用的结构化结果 —— 取代过去 (code, body, ct) 三元组丢信息的写法。"""

    ok: bool = False
    status: int = 0
    body: Optional[bytes] = None
    content_type: str = "application/json"
    error_kind: str = ""
    detail: str = ""
    ping_ms: int = -1

    def json(self, default: Any = None) -> Any:
        if not self.ok or not self.body:
            return default
        try:
            return json.loads(self.body)
        except (ValueError, TypeError) as exc:
            logger.warning("节点响应不是合法 JSON: %s", exc)
            return default


@dataclass
class _NodeHealth:
    """进程内的节点健康计数 — 熔断与降频用，不需要持久化。"""

    consecutive_failures: int = 0
    degraded_since: float = 0.0
    last_probe_ts: float = 0.0
    last_error_kind: str = ""
    last_detail: str = ""


class ClusterManager:
    def __init__(self, config_file: str):
        self.config_file = config_file
        self._session: Optional[aiohttp.ClientSession] = None
        # 节点状态结果级缓存（避免每次请求都重走远程健康检查）
        self._nodes_cache: List[Dict] = []
        self._nodes_cache_ts: float = 0.0
        self._nodes_cache_lock = asyncio.Lock()
        self._health: Dict[str, _NodeHealth] = {}

    # ============ aiohttp Session 生命周期 ============

    async def start_session(self):
        """创建共享 aiohttp 连接池 — 在 FastAPI lifespan startup 中调用。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=64, ttl_dns_cache=60),
                headers={"User-Agent": f"NapCatManager/{APP_VERSION}"},
            )
            logger.info("集群管理器 aiohttp session 已启动")

    async def stop_session(self):
        """关闭连接池 — 在 FastAPI lifespan shutdown 中调用。"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def init(self):
        """启动后初始化：修复被写坏的集群密钥，并同步 local 节点 api_key。"""
        self._repair_masked_key()
        self._sync_local_node_key()

    @staticmethod
    def _repair_masked_key() -> None:
        """集群设置页曾把 GET 返回的掩码 "***" 原样回传，把真密钥覆写成了字面量。

        受影响的部署一旦保存过一次设置就整个集群 401，且没有任何提示。
        这里在启动时自愈：发现掩码值就重新生成一把，并在日志里说清楚后果。
        """
        import uuid
        from services.config import app_config

        current = app_config.get("api_key") or ""
        if current not in ("***", '"***"'):
            return
        new_key = uuid.uuid4().hex
        app_config.set("api_key", new_key)
        logger.warning(
            "检测到集群密钥被写坏（值为掩码 ***），已自动重新生成。"
            "如果本机被其它面板添加为节点，请到集群设置页复制新密钥并更新过去。"
        )

    def _sync_local_node_key(self):
        """启动时同步 local 节点的 api_key 与运行时配置保持一致。"""
        from services.config import app_config
        config_key = app_config.get("api_key") or ""
        node = self._get_node("local")
        if node and node.get("api_key", "") != config_key:
            db.execute("UPDATE nodes SET api_key=? WHERE id='local'", (config_key,))
            db.commit()
            logger.info("已同步 local 节点 api_key")

    # ============ 节点 CRUD ============

    @staticmethod
    def _get_node(node_id: str) -> Optional[dict]:
        row = db.fetchone("SELECT * FROM nodes WHERE id=?", (node_id,))
        return db.row_to_dict(row)

    def get_nodes(self, include_disabled: bool = False) -> List[Dict]:
        if include_disabled:
            rows = db.fetchall("SELECT * FROM nodes")
        else:
            rows = db.fetchall("SELECT * FROM nodes WHERE COALESCE(enabled,1)=1")
        return db.rows_to_list(rows)

    def _invalidate_cache(self):
        """节点增删改后清除状态缓存。"""
        self._nodes_cache = []
        self._nodes_cache_ts = 0.0

    def find_node_by_address(self, address: str, include_disabled: bool = True) -> Optional[Dict]:
        """按规范化地址查找已存在的节点 — 用于去重与软删除后复活。"""
        target = self._normalize_address(address)
        for node in self.get_nodes(include_disabled=include_disabled):
            if self._normalize_address(node.get("address", "")) == target:
                return node
        return None

    def add_node(self, node_id: str, name: str, address: str, api_key: str = "",
                 insecure_tls: bool = False):
        db.execute(
            "INSERT INTO nodes (id,name,address,api_key,created_at,enabled,insecure_tls) "
            "VALUES (?,?,?,?,?,1,?)",
            (node_id, name, address, api_key, time.time(), 1 if insecure_tls else 0),
        )
        db.commit()
        self._invalidate_cache()

    def revive_node(self, node_id: str, name: str, address: str, api_key: str = "",
                    insecure_tls: bool = False):
        """复活一个软删除的节点，保留原 ID —— 用户对它的实例授权因此不会失效。"""
        db.execute(
            "UPDATE nodes SET name=?,address=?,api_key=?,enabled=1,insecure_tls=?,"
            "last_error='',last_error_kind='',last_status='' WHERE id=?",
            (name, address, api_key, 1 if insecure_tls else 0, node_id),
        )
        db.commit()
        self._health.pop(node_id, None)
        self._invalidate_cache()

    def update_node(self, node_id: str, name: str, address: str, api_key: str = None,
                    insecure_tls: Optional[bool] = None) -> bool:
        sets = ["name=?", "address=?"]
        params: List[Any] = [name, address]
        if api_key is not None:
            sets.append("api_key=?")
            params.append(api_key)
        if insecure_tls is not None:
            sets.append("insecure_tls=?")
            params.append(1 if insecure_tls else 0)
        params.append(node_id)
        cur = db.execute(f"UPDATE nodes SET {','.join(sets)} WHERE id=?", tuple(params))
        db.commit()
        self._invalidate_cache()
        return cur.rowcount > 0

    def delete_node(self, node_id: str) -> bool:
        """软删除节点 —— 保留 ID，重新添加同地址时复活，用户授权不丢。"""
        cur = db.execute(
            "UPDATE nodes SET enabled=0,last_status='removed' WHERE id=? AND COALESCE(enabled,1)=1",
            (node_id,),
        )
        db.commit()
        self._health.pop(node_id, None)
        self._invalidate_cache()
        return cur.rowcount > 0

    def purge_node(self, node_id: str) -> bool:
        """彻底删除节点记录（软删除之后的二次清理）。"""
        cur = db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        db.commit()
        self._health.pop(node_id, None)
        self._invalidate_cache()
        return cur.rowcount > 0

    # ============ 地址规范化与健康状态 ============

    @staticmethod
    def _normalize_address(addr: str) -> str:
        """补全 scheme 并去掉尾斜杠。

        旧实现用 ``startswith("http")`` 判断，主机名叫 ``httpnode`` 就会被误判成
        已带协议，随后 aiohttp 抛 InvalidURL 被吞成「离线」。
        """
        addr = (addr or "").strip()
        if not addr:
            return ""
        if "://" not in addr:
            addr = "http://" + addr
        return addr.rstrip("/")

    @staticmethod
    def validate_address(addr: str) -> Tuple[bool, str]:
        """返回 (是否合法, 错误说明) — 供添加节点时给出可读的校验失败原因。"""
        normalized = ClusterManager._normalize_address(addr)
        if not normalized:
            return False, "地址不能为空"
        parts = urlsplit(normalized)
        if parts.scheme not in ("http", "https"):
            return False, f"不支持的协议 {parts.scheme}://，只支持 http 或 https"
        if not parts.hostname:
            return False, "地址中缺少主机名"
        try:
            port = parts.port
        except ValueError:
            return False, "端口号不是合法数字"
        if port is not None and not (1 <= port <= 65535):
            return False, "端口号必须在 1-65535 之间"
        return True, ""

    def _health_of(self, node_id: str) -> _NodeHealth:
        health = self._health.get(node_id)
        if health is None:
            health = _NodeHealth()
            self._health[node_id] = health
        return health

    def _record_success(self, node_id: str) -> None:
        health = self._health_of(node_id)
        health.consecutive_failures = 0
        health.degraded_since = 0.0
        health.last_probe_ts = time.monotonic()
        health.last_error_kind = ""
        health.last_detail = ""
        db.execute(
            "UPDATE nodes SET last_ok_ts=?,last_status='online',last_error='',last_error_kind='' WHERE id=?",
            (time.time(), node_id),
        )
        db.commit()

    def _record_failure(self, node_id: str, kind: str, detail: str) -> None:
        health = self._health_of(node_id)
        health.consecutive_failures += 1
        health.last_probe_ts = time.monotonic()
        health.last_error_kind = kind
        health.last_detail = detail
        if health.consecutive_failures >= _DEGRADED_AFTER_FAILURES and not health.degraded_since:
            health.degraded_since = time.monotonic()
        db.execute(
            "UPDATE nodes SET last_status='offline',last_error=?,last_error_kind=? WHERE id=?",
            (detail[:500], kind, node_id),
        )
        db.commit()

    def _should_skip_degraded(self, node_id: str) -> bool:
        """degraded 节点在探测间隔内快速失败，不再每个请求都等满超时。"""
        health = self._health_of(node_id)
        if not health.degraded_since:
            return False
        return (time.monotonic() - health.last_probe_ts) < _DEGRADED_PROBE_INTERVAL

    @staticmethod
    def _classify(exc: BaseException) -> Tuple[str, str]:
        """把 aiohttp 异常归类成用户能看懂的原因。"""
        if isinstance(exc, asyncio.TimeoutError):
            return "timeout", "连接超时，节点未在限定时间内响应"
        if isinstance(exc, aiohttp.ClientConnectorCertificateError):
            return "tls", f"TLS 证书校验失败：{exc}。自签证书请在节点上勾选「跳过证书校验」"
        if isinstance(exc, aiohttp.ClientConnectorSSLError):
            return "tls", f"TLS 握手失败：{exc}"
        if isinstance(exc, aiohttp.ClientConnectorError):
            os_err = getattr(exc, "os_error", None)
            if os_err is not None and getattr(os_err, "errno", None) in (-2, -3, -5, 11001):
                return "dns", f"域名解析失败：{exc}"
            return "refused", f"无法建立连接（对方未监听或被防火墙拦截）：{exc}"
        if isinstance(exc, aiohttp.InvalidURL):
            return "invalid_address", f"地址格式不合法：{exc}"
        if isinstance(exc, aiohttp.ClientError):
            return "network", f"网络错误：{exc}"
        return "unknown", str(exc)

    @staticmethod
    def _classify_status(status: int) -> Tuple[str, str]:
        if status in (401, 403):
            return "unauthorized", "集群密钥不匹配（对方返回未授权）。请核对两端的集群密钥"
        if status == 404:
            return "http_404", "对方没有这个接口，可能版本过旧"
        if status >= 500:
            return f"http_{status}", f"节点内部错误（HTTP {status}）"
        return f"http_{status}", f"节点返回 HTTP {status}"

    # ============ 统一远程调用入口 ============

    def _ssl_for(self, node: Dict) -> Any:
        if not self._normalize_address(node.get("address", "")).startswith("https://"):
            return None
        if node.get("insecure_tls"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return None

    async def call(
        self,
        node_id: str,
        method: str,
        path: str,
        *,
        kind: str = "read",
        timeout: Optional[float] = None,
        retries: int = 1,
        node: Optional[Dict] = None,
        **kwargs,
    ) -> NodeCallResult:
        """对远程节点发起一次调用，返回结构化结果。"""
        node = node or self._get_node(node_id)
        if not node:
            return NodeCallResult(error_kind="unknown_node", detail=f"节点 {node_id} 不存在")
        if not node.get("api_key"):
            return NodeCallResult(
                error_kind="no_key",
                detail="该节点没有配置集群密钥，无法通信。请在节点设置里填写对方的集群密钥",
            )
        if self._should_skip_degraded(node_id):
            health = self._health_of(node_id)
            # 保留真实失败原因（DNS/拒绝/密钥不匹配…），否则界面上只会看到
            # 「熔断中」，用户依然不知道到底哪里配错了。
            return NodeCallResult(
                error_kind=health.last_error_kind or "degraded",
                detail=(f"{health.last_detail}（已连续失败 {health.consecutive_failures} 次，"
                        f"{int(_DEGRADED_PROBE_INTERVAL)}s 后重试）" if health.last_detail
                        else f"节点连续 {health.consecutive_failures} 次通信失败，暂时跳过"),
            )

        valid, reason = self.validate_address(node.get("address", ""))
        if not valid:
            self._record_failure(node_id, "invalid_address", reason)
            return NodeCallResult(error_kind="invalid_address", detail=reason)

        addr = self._normalize_address(node["address"])
        total = timeout if timeout is not None else _TIMEOUTS.get(kind, _TIMEOUTS["read"])
        client_timeout = aiohttp.ClientTimeout(total=total, connect=min(_CONNECT_TIMEOUT, total))
        extra_headers = kwargs.pop("headers", {}) or {}
        headers = {"x-request-api-key": node.get("api_key", ""), **extra_headers}
        ssl_ctx = self._ssl_for(node)

        attempt = 0
        last: NodeCallResult = NodeCallResult(error_kind="unknown", detail="未执行")
        while attempt <= retries:
            attempt += 1
            t0 = time.monotonic()
            try:
                async with self._get_session().request(
                    method, f"{addr}{path}",
                    headers=headers,
                    timeout=client_timeout,
                    ssl=ssl_ctx,
                    **kwargs,
                ) as resp:
                    body = await resp.read()
                    ping = int((time.monotonic() - t0) * 1000)
                    ct = resp.headers.get("content-type", "application/json")
                    if 200 <= resp.status < 300:
                        self._record_success(node_id)
                        return NodeCallResult(True, resp.status, body, ct, ping_ms=ping)
                    kind_, detail = self._classify_status(resp.status)
                    last = NodeCallResult(False, resp.status, body, ct, kind_, detail, ping)
                    # 应用层错误码不重试：对方已经明确回复了。
                    self._record_failure(node_id, kind_, detail)
                    return last
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                kind_, detail = self._classify(exc)
                last = NodeCallResult(error_kind=kind_, detail=detail)
                if kind_ in _NON_RETRYABLE or attempt > retries:
                    break
                logger.debug("节点 %s 第 %d 次调用失败(%s)，重试: %s", node_id, attempt, kind_, detail)
                await asyncio.sleep(0.3)

        logger.warning("节点 %s 通信失败 [%s %s]: %s (%s)",
                       node_id, method, path, last.detail, last.error_kind)
        self._record_failure(node_id, last.error_kind, last.detail)
        return last

    async def probe_node(self, address: str, api_key: str, insecure_tls: bool = False) -> Dict:
        """握手探测 — 添加节点前先验证，返回结构化的失败原因。不落库。"""
        valid, reason = self.validate_address(address)
        if not valid:
            return {"ok": False, "error_kind": "invalid_address", "detail": reason}
        if not api_key:
            return {"ok": False, "error_kind": "no_key", "detail": "请填写对方节点的集群密钥"}

        pseudo_node = {
            "id": "__probe__",
            "address": address,
            "api_key": api_key,
            "insecure_tls": 1 if insecure_tls else 0,
        }
        addr = self._normalize_address(address)
        client_timeout = aiohttp.ClientTimeout(total=_TIMEOUTS["health"], connect=_CONNECT_TIMEOUT)
        t0 = time.monotonic()
        try:
            async with self._get_session().get(
                f"{addr}/api/cluster/status",
                headers={"x-request-api-key": api_key},
                timeout=client_timeout,
                ssl=self._ssl_for(pseudo_node),
            ) as resp:
                ping = int((time.monotonic() - t0) * 1000)
                body = await resp.read()
                if 200 <= resp.status < 300:
                    try:
                        data = json.loads(body)
                    except (ValueError, TypeError):
                        return {
                            "ok": False, "error_kind": "not_a_node", "ping_ms": ping,
                            "detail": "该地址有服务在响应，但返回的不是 NapCat 面板的数据。请确认地址指向的是面板端口",
                        }
                    return {
                        "ok": True,
                        "ping_ms": ping,
                        "detail": "连接成功",
                        "remote_app_version": (data.get("system") or {}).get("app_version", ""),
                        "remote_instances": data.get("instances", {}),
                    }
                kind_, detail = self._classify_status(resp.status)
                return {"ok": False, "error_kind": kind_, "detail": detail, "ping_ms": ping}
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            kind_, detail = self._classify(exc)
            return {"ok": False, "error_kind": kind_, "detail": detail}

    # ============ 兼容旧调用的代理封装 ============

    async def proxy_to_node_async(
        self, node_id: str, method: str, path: str,
        timeout: float = None, kind: str = "read", **kwargs,
    ) -> Tuple[int, Optional[bytes], str]:
        """异步通用远程节点代理。返回 (status_code, body_bytes, content_type)。

        保留给已有调用方；新代码请直接用 :meth:`call` 以拿到失败原因。
        """
        result = await self.call(node_id, method, path, kind=kind, timeout=timeout, **kwargs)
        if result.ok:
            return (result.status, result.body, result.content_type)
        return (result.status or 502, result.body, result.content_type)

    # ============ 快速节点列表（本地完整 + 远程骨架） ============

    async def get_nodes_quick(self) -> List[Dict]:
        """本地节点立即返回完整信息，远程节点仅返回基础字段（status=unknown）。"""
        result = []
        for n in self.get_nodes():
            if n.get("id") == "local":
                result.append(await self._check_node_async(n))
            else:
                copy = self._safe_node(n)
                copy["status"] = "unknown"
                copy["ping"] = -1
                copy["system"] = {}
                copy["instances"] = {}
                copy["chart"] = {}
                result.append(copy)
        return result

    # ============ 异步节点状态检查（带结果级缓存） ============

    async def get_nodes_with_status_async(self, force: bool = False) -> List[Dict]:
        """异步并发获取所有节点状态 — 带 TTL 缓存，避免重复远程检查。"""
        async with self._nodes_cache_lock:
            now = time.monotonic()
            if not force and self._nodes_cache and (now - self._nodes_cache_ts) < _NODES_CACHE_TTL:
                return [dict(n) for n in self._nodes_cache]

            nodes = self.get_nodes()
            result = list(await asyncio.gather(*[self._check_node_async(n) for n in nodes]))
            self._nodes_cache = result
            self._nodes_cache_ts = now
            return [dict(n) for n in result]

    @staticmethod
    def _safe_node(node: Dict) -> Dict:
        """返回节点对象的安全副本 — 剔除 api_key，防止密钥通过 API 泄露。"""
        safe = node.copy()
        safe.pop("api_key", None)
        safe["has_key"] = bool(node.get("api_key"))
        return safe

    @staticmethod
    async def _notify_node_status_change(node_id: str, node_name: str, result: NodeCallResult) -> None:
        """节点在线状态发生翻转时推送告警。告警失败不能影响节点检查本身。"""
        try:
            from services.alert_manager import alert_manager
            await alert_manager.notify_node_status(
                node_id, node_name, online=result.ok, reason=result.detail,
            )
        except Exception as exc:
            logger.debug("节点状态告警推送失败 [%s]: %s", node_id, exc)

    async def _check_node_async(self, node: Dict) -> Dict:
        """单节点状态检查 — 本地直接读内存，远程走统一调用层。"""
        node_copy = node.copy()
        node_id = node.get("id")
        if node_id == "local":
            import sys
            from services.daemon_monitor import daemon_monitor
            node_copy["status"] = "online"
            node_copy["ping"] = 0
            node_copy["system"] = {
                "cpu_percent": daemon_monitor.current_cpu,
                "mem_percent": daemon_monitor.current_mem,
                "platform": sys.platform,
                "python_version": sys.version.split()[0],
                "app_version": APP_VERSION,
            }
            node_copy["instances"] = daemon_monitor.get_instance_status(node_id="local")
            node_copy["chart"] = daemon_monitor.get_chart_data()
            node_copy["last_error"] = ""
            node_copy["last_error_kind"] = ""
            return self._safe_node(node_copy)

        was_online = str(node.get("last_status") or "") == "online"
        result = await self.call(node_id, "GET", "/api/cluster/status", kind="health", node=node)
        data = result.json({}) or {}
        if was_online != result.ok:
            await self._notify_node_status_change(node_id, node.get("name", node_id), result)
        node_copy["status"] = "online" if result.ok else "offline"
        node_copy["ping"] = result.ping_ms
        node_copy["system"] = data.get("system", {})
        node_copy["instances"] = data.get("instances", {})
        node_copy["chart"] = data.get("chart", {})
        node_copy["last_error"] = "" if result.ok else result.detail
        node_copy["last_error_kind"] = "" if result.ok else result.error_kind
        node_copy["degraded"] = bool(self._health_of(node_id).degraded_since)
        return self._safe_node(node_copy)

    # ============ 异步远程容器列表 ============

    async def list_remote_containers_async(self) -> Tuple[List[Dict], set]:
        """异步获取所有远程节点的容器列表 — 并发 aiohttp，零线程。

        Returns:
            (containers, responded_node_ids): 容器列表 + 成功响应的节点 ID 集合。
        """
        remote_nodes = [n for n in self.get_nodes() if n.get("id") != "local"]
        if not remote_nodes:
            return [], set()

        async def _fetch_one(node: Dict) -> Tuple[List[Dict], Optional[str]]:
            result = await self.call(node["id"], "GET", "/api/containers", kind="list", node=node)
            if not result.ok:
                return [], None
            containers = (result.json({}) or {}).get("containers", [])
            for c in containers:
                # 链式集群里对方可能回传它自己的下级节点容器，那些不归本面板直接管辖。
                if c.get("node_id") not in (None, "", "local", node["id"]):
                    continue
                c["node_id"] = node["id"]
            return [c for c in containers if c.get("node_id") == node["id"]], node["id"]

        results = await asyncio.gather(*[_fetch_one(n) for n in remote_nodes], return_exceptions=True)
        all_containers: List[Dict] = []
        responded_nodes: set = set()
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("远程容器列表拉取异常: %s", r)
                continue
            containers, node_id = r
            all_containers.extend(containers)
            if node_id is not None:
                responded_nodes.add(node_id)
        return all_containers, responded_nodes

    # ============ 容器操作 ============

    async def action_container_async(self, node_id: str, name: str, action: str,
                                     delete_data: bool = False) -> bool:
        if node_id == "local" or not node_id:
            from services.docker_async import async_docker_manager
            return await async_docker_manager.action_container(name, action, node_id="local")
        query = f"?action={action}&node_id=local"
        if delete_data:
            query += "&delete_data=true"
        kind = "lifecycle" if action in {"start", "stop", "restart"} else "heavy"
        result = await self.call(node_id, "POST", f"/api/containers/{name}/action{query}", kind=kind)
        return result.ok

    async def inspect_container_state_async(self, node_id: str, name: str) -> Dict:
        """查询单个容器的实时状态（含 State.StartedAt，用于判定重启是否已生效）。"""
        if node_id == "local" or not node_id:
            from services.docker_async import async_docker_manager
            return await async_docker_manager.inspect_state(name)

        result = await self.call(node_id, "GET", f"/api/containers/{name}/state?node_id=local", kind="read")
        if result.ok:
            data = result.json({}) or {}
            if data.get("status") == "ok" and "state" in data:
                return data["state"]

        # 老版本节点没有 /state 端点，退回容器列表（拿不到 started_at）。
        listing = await self.call(node_id, "GET", "/api/containers", kind="list")
        if not listing.ok:
            return {"found": False, "status": "unknown", "running": None, "started_at": "",
                    "error": listing.detail or f"HTTP {listing.status}"}
        for c in (listing.json({}) or {}).get("containers", []):
            if c.get("name") == name:
                status = str(c.get("status") or "unknown")
                return {"found": True, "status": status, "running": status == "running",
                        "started_at": str(c.get("started_at") or "")}
        return {"found": False, "status": "missing", "running": False, "started_at": ""}

    async def get_stats_async(self, node_id: str, name: str) -> Dict:
        if node_id == "local" or not node_id:
            # docker_manager.get_stats 是同步 docker-py，直接 await 会阻塞整个事件循环
            # 长达数秒，期间所有远程节点的健康检查都会误判超时。
            return await asyncio.to_thread(docker_manager.get_stats, name)
        result = await self.call(node_id, "GET", f"/api/containers/{name}/stats", kind="read")
        stats = result.json({})
        if not isinstance(stats, dict):
            return {}
        stats["node_id"] = node_id
        return stats

    async def get_logs_async(self, node_id: str, name: str, lines: int = 100) -> str:
        if node_id == "local" or not node_id:
            from services.docker_async import async_docker_manager
            return await async_docker_manager.get_logs(name, lines)
        result = await self.call(node_id, "GET", f"/api/containers/{name}/logs?lines={lines}", kind="read")
        return (result.json({}) or {}).get("logs", "")

    async def get_qr_status_async(self, node_id: str, name: str, bust_cache: bool = True) -> Optional[Dict]:
        if node_id == "local" or not node_id:
            return None
        cache_buster = f"&_t={int(time.time() * 1000)}" if bust_cache else ""
        result = await self.call(
            node_id, "GET",
            f"/api/containers/{name}/qrcode?node_id=local{cache_buster}",
            kind="read",
            headers={"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"},
        )
        return result.json(None)

    # ============ 内部辅助 ============

    def _get_session(self) -> aiohttp.ClientSession:
        """获取共享 session；若已关闭则抛出可恢复异常。"""
        if self._session and not self._session.closed:
            return self._session
        raise RuntimeError("ClusterManager aiohttp session 未初始化或已关闭")


cluster_manager = ClusterManager(config_file=CONFIG_FILE)
