"""
集群/节点管理器 — SQLite 持久化，aiohttp 全异步通信（节点状态/代理/操作）。
"""
import json
import os
import time
import asyncio
from typing import List, Dict, Optional, Any, Tuple

import aiohttp

from services.log import logger
from services.config import CONFIG_FILE, APP_VERSION
from services.docker_manager import docker_manager
import services.database as db


_NODES_CACHE_TTL = 10  # 节点状态缓存有效期（秒）


class ClusterManager:
    def __init__(self, config_file: str):
        self.config_file = config_file
        self._session: Optional[aiohttp.ClientSession] = None
        # 节点状态结果级缓存（避免每次请求都重走远程健康检查）
        self._nodes_cache: List[Dict] = []
        self._nodes_cache_ts: float = 0.0

    # ============ aiohttp Session 生命周期 ============

    async def start_session(self):
        """创建共享 aiohttp 连接池 — 在 FastAPI lifespan startup 中调用。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=64, ttl_dns_cache=60),
                headers={"User-Agent": "NapCatManager/1.0"},
            )
            logger.info("集群管理器 aiohttp session 已启动")

    async def stop_session(self):
        """关闭连接池 — 在 FastAPI lifespan shutdown 中调用。"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def init(self):
        """启动后初始化：同步 local 节点 api_key"""
        self._sync_local_node_key()

    def _sync_local_node_key(self):
        """启动时同步 local 节点的 api_key 与 config.json 保持一致"""
        from services.config import app_config
        config_key = app_config.get("api_key") or ""
        node = self._get_node("local")
        if node and node.get("api_key", "") != config_key:
            db.execute("UPDATE nodes SET api_key=? WHERE id='local'", (config_key,))
            db.commit()
            logger.info("已同步 local 节点 api_key 与 config.json")

    @staticmethod
    def _get_node(node_id: str) -> Optional[dict]:
        row = db.fetchone("SELECT * FROM nodes WHERE id=?", (node_id,))
        return db.row_to_dict(row)

    def get_nodes(self) -> List[Dict]:
        rows = db.fetchall("SELECT * FROM nodes")
        return db.rows_to_list(rows)

    def save_nodes(self, nodes: List[Dict]):
        """兼容旧调用：全量覆盖（先删后插）"""
        db.execute("DELETE FROM nodes")
        for n in nodes:
            db.execute(
                "INSERT INTO nodes (id,name,address,api_key) VALUES (?,?,?,?)",
                (n["id"], n["name"], n["address"], n.get("api_key", "")),
            )
        db.commit()

    def _invalidate_cache(self):
        """节点增删改后清除状态缓存。"""
        self._nodes_cache = []
        self._nodes_cache_ts = 0.0

    def add_node(self, node_id: str, name: str, address: str, api_key: str = ""):
        db.execute(
            "INSERT INTO nodes (id,name,address,api_key) VALUES (?,?,?,?)",
            (node_id, name, address, api_key),
        )
        db.commit()
        self._invalidate_cache()

    def update_node(self, node_id: str, name: str, address: str, api_key: str = None):
        if api_key is not None:
            db.execute("UPDATE nodes SET name=?,address=?,api_key=? WHERE id=?",
                       (name, address, api_key, node_id))
        else:
            db.execute("UPDATE nodes SET name=?,address=? WHERE id=?",
                       (name, address, node_id))
        db.commit()
        self._invalidate_cache()

    def delete_node(self, node_id: str) -> bool:
        cur = db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        db.commit()
        self._invalidate_cache()
        return cur.rowcount > 0

    @staticmethod
    def _normalize_address(addr: str) -> str:
        if not addr.startswith("http"):
            addr = "http://" + addr
        return addr.rstrip("/")

    # ============ 快速节点列表（本地完整 + 远程骨架） ============

    async def get_nodes_quick(self) -> List[Dict]:
        """本地节点立即返回完整信息，远程节点仅返回基础字段（status=unknown）。"""
        nodes = self.get_nodes()
        has_local = any(n.get("id") == "local" for n in nodes)
        if not has_local:
            nodes.insert(0, {"id": "local", "name": "本地节点", "address": "127.0.0.1"})

        result = []
        for n in nodes:
            if n.get("id") == "local":
                # 本地节点：直接读内存，零延迟
                result.append(await self._check_node_async(n))
            else:
                # 远程节点：仅骨架
                copy = self._safe_node(n)
                copy["status"] = "unknown"
                copy["ping"] = -1
                copy["system"] = {}
                copy["instances"] = {}
                copy["chart"] = {}
                result.append(copy)
        return result

    # ============ 异步节点状态检查（带结果级缓存） ============

    async def get_nodes_with_status_async(self) -> List[Dict]:
        """异步并发获取所有节点状态 — 带 TTL 缓存，避免重复远程检查。"""
        now = time.monotonic()
        if self._nodes_cache and (now - self._nodes_cache_ts) < _NODES_CACHE_TTL:
            return self._nodes_cache

        nodes = self.get_nodes()
        has_local = any(n.get("id") == "local" for n in nodes)
        if not has_local:
            nodes.insert(0, {"id": "local", "name": "本地节点", "address": "127.0.0.1"})

        tasks = [self._check_node_async(n) for n in nodes]
        result = list(await asyncio.gather(*tasks, return_exceptions=False))
        self._nodes_cache = result
        self._nodes_cache_ts = now
        return result

    @staticmethod
    def _safe_node(node: Dict) -> Dict:
        """返回节点对象的安全副本 — 剔除 api_key，防止密钥通过 API 泄露。"""
        safe = node.copy()
        safe.pop("api_key", None)
        return safe

    async def _check_node_async(self, node: Dict) -> Dict:
        """单节点状态检查 — 本地直接读内存，远程 aiohttp。"""
        node_copy = node.copy()
        if node.get("id") == "local":
            import sys
            from services.config import app_config
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
            node_copy["instances"] = daemon_monitor.get_instance_status()
            node_copy["chart"] = daemon_monitor.get_chart_data()
            return self._safe_node(node_copy)

        status, ping = "offline", -1
        system_info, instances_info, chart_info = {}, {}, {}
        timeout = aiohttp.ClientTimeout(total=2, connect=1)
        try:
            addr = self._normalize_address(node["address"])
            t0 = time.monotonic()
            async with self._get_session().get(
                f"{addr}/api/cluster/status",
                headers={"x-request-api-key": node.get("api_key", "")},
                timeout=timeout,
            ) as resp:
                ping = int((time.monotonic() - t0) * 1000)
                if resp.status == 200:
                    status = "online"
                    data = await resp.json(content_type=None)
                    system_info = data.get("system", {})
                    instances_info = data.get("instances", {})
                    chart_info = data.get("chart", {})
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug("节点 %s 连接失败: %s", node.get("id"), e)

        node_copy["status"] = status
        node_copy["ping"] = ping
        node_copy["system"] = system_info
        node_copy["instances"] = instances_info
        node_copy["chart"] = chart_info
        return self._safe_node(node_copy)

    # ============ 异步远程容器列表 ============

    async def list_remote_containers_async(self) -> tuple[List[Dict], set[str]]:
        """异步获取所有远程节点的容器列表 — 并发 aiohttp，零线程。

        Returns:
            (containers, responded_node_ids): 容器列表 + 成功响应的节点 ID 集合。
        """
        nodes = self.get_nodes()
        remote_nodes = [n for n in nodes if n.get("id") != "local"]
        if not remote_nodes:
            return [], set()

        timeout = aiohttp.ClientTimeout(total=2, connect=1)
        session = self._get_session()

        async def _fetch_one(node: Dict) -> tuple[List[Dict], str | None]:
            try:
                addr = self._normalize_address(node["address"])
                async with session.get(
                    f"{addr}/api/containers",
                    headers={"x-request-api-key": node.get("api_key", "")},
                    timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        containers = data.get("containers", [])
                        for c in containers:
                            c["node_id"] = node["id"]
                        return containers, node["id"]
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.debug("异步: 从节点 %s 获取容器失败: %s", node.get("id"), e)
            return [], None

        results = await asyncio.gather(
            *[_fetch_one(n) for n in remote_nodes],
            return_exceptions=True,
        )
        all_containers: List[Dict] = []
        responded_nodes: set[str] = set()
        for r in results:
            if isinstance(r, tuple):
                containers, node_id = r
                all_containers.extend(containers)
                if node_id is not None:
                    responded_nodes.add(node_id)
        return all_containers, responded_nodes

    # ============ 异步节点代理 ============

    async def proxy_to_node_async(
        self, node_id: str, method: str, path: str,
        timeout: float = 5.0, **kwargs,
    ) -> Tuple[int, Optional[bytes], str]:
        return await self._proxy_to_node_async(
            node_id,
            method,
            path,
            timeout=timeout,
            **kwargs,
        )

    async def proxy_to_node_async(
        self, node_id: str, method: str, path: str,
        timeout: float = 5.0, **kwargs
    ) -> Tuple[int, Optional[bytes], str]:
        """异步通用远程节点代理。返回 (status_code, body_bytes, content_type)。"""
        node = self._get_node(node_id)
        if not node:
            return (404, None, "")
        try:
            addr = self._normalize_address(node["address"])
            t = aiohttp.ClientTimeout(total=timeout, connect=2)
            extra_headers = kwargs.pop("headers", {}) or {}
            headers = {"x-request-api-key": node.get("api_key", ""), **extra_headers}
            async with self._get_session().request(
                method, f"{addr}{path}",
                headers=headers,
                timeout=t,
                **kwargs,
            ) as resp:
                body = await resp.read()
                ct = resp.headers.get("content-type", "application/json")
                return (resp.status, body, ct)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("异步代理到节点 %s 失败: %s", node_id, e)
            return (502, None, "")

    async def action_container_async(self, node_id: str, name: str, action: str) -> bool:
        if node_id == "local" or not node_id:
            return docker_manager.action_container(name, action)
        # Lifecycle actions are accepted asynchronously by the remote node. Keep this
        # short so Japan panel -> Suzhou node does not wait for Docker restart.
        t = 8.0 if action in {"start", "stop", "restart"} else 35.0
        code, _, _ = await self.proxy_to_node_async(
            node_id, "POST", f"/api/containers/{name}/action?action={action}",
            timeout=t)
        return code in {200, 202}

    async def inspect_container_state_async(self, node_id: str, name: str) -> Dict:
        if node_id == "local" or not node_id:
            try:
                from services.docker_async import async_docker_manager
                containers = await async_docker_manager.list_local_containers()
                for c in containers:
                    if c.get("name") == name:
                        status = str(c.get("status") or "unknown")
                        return {
                            "found": True,
                            "status": status,
                            "running": status == "running",
                        }
            except Exception as exc:
                return {"found": False, "status": "unknown", "running": None, "error": str(exc)}
            return {"found": False, "status": "missing", "running": False}
        code, body, _ = await self.proxy_to_node_async(node_id, "GET", "/api/containers", timeout=6.0)
        if code == 200 and body:
            try:
                data = json.loads(body)
                for c in data.get("containers", []):
                    if c.get("name") == name:
                        status = str(c.get("status") or "unknown")
                        return {
                            "found": True,
                            "status": status,
                            "running": status == "running",
                        }
                return {"found": False, "status": "missing", "running": False}
            except Exception as exc:
                return {"found": False, "status": "unknown", "running": None, "error": str(exc)}
        return {"found": False, "status": "unknown", "running": None, "error": f"HTTP {code}"}

    async def get_stats_async(self, node_id: str, name: str) -> Dict:
        if node_id == "local" or not node_id:
            return docker_manager.get_stats(name)
        code, body, _ = await self.proxy_to_node_async(
            node_id, "GET", f"/api/containers/{name}/stats")
        if code == 200 and body:
            stats = json.loads(body)
            stats["node_id"] = node_id
            return stats
        return {}

    async def get_logs_async(self, node_id: str, name: str, lines: int = 100) -> str:
        if node_id == "local" or not node_id:
            return docker_manager.get_logs(name, lines)
        code, body, _ = await self.proxy_to_node_async(
            node_id, "GET", f"/api/containers/{name}/logs?lines={lines}")
        if code == 200 and body:
            return json.loads(body).get("logs", "")
        return ""

    async def get_qr_status_async(self, node_id: str, name: str, bust_cache: bool = True) -> Optional[Dict]:
        if node_id == "local" or not node_id:
            return None
        cache_buster = f"&_t={int(time.time() * 1000)}" if bust_cache else ""
        code, body, _ = await self.proxy_to_node_async(
            node_id,
            "GET",
            f"/api/containers/{name}/qrcode?node_id=local{cache_buster}",
            timeout=8.0,
            headers={
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )
        if code == 200 and body:
            return json.loads(body)
        return None

    # ============ 内部辅助 ============

    def _get_session(self) -> aiohttp.ClientSession:
        """获取共享 session；若已关闭则抛出可恢复异常。"""
        if self._session and not self._session.closed:
            return self._session
        raise RuntimeError("ClusterManager aiohttp session 未初始化或已关闭")


cluster_manager = ClusterManager(config_file=CONFIG_FILE)

