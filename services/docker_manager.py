"""
Docker 容器管理器

职责：容器生命周期管理（list/create/action/stats/ports/images）。
登录检测逻辑已迁移至 services/docker_login.py（LoginMixin）。
BS 注入 + 登录事件回调已迁移至 services/docker_lifecycle.py（LifecycleMixin）。
热路径另见 docker_async.py(AsyncDockerManager)。
"""
import asyncio
import os
import re
import io
import json
import time
import tarfile
import urllib.request
import urllib.error
import docker
import docker.errors
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import List, Dict, Optional, Any

from services.log import logger
from services.config import get_data_dir
from services.docker_login import LoginMixin, _login_cache, _LOGIN_CACHE_TTL, _LOGIN_CACHE_TTL_FAIL, _normalize_uin, read_login_cache  # noqa: F401
from services.docker_lifecycle import LifecycleMixin

def _host_gateway() -> str:
    return (os.environ.get("HOST_GATEWAY") or "127.0.0.1").strip() or "127.0.0.1"

# 主事件循环引用（由 main.py lifespan 在启动时注入，供线程池回调使用）
_main_event_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """由 lifespan 在主协程中调用，将主事件循环注入本模块，供线程内 fire-and-forget 使用。"""
    global _main_event_loop
    _main_event_loop = loop


# Stats 缓存：{container_name: {stats_dict, ts}}
_stats_cache: Dict[str, Dict] = {}
_STATS_CACHE_TTL = 15  # 秒，stats 采集较慢(1-2s)，缓存 15s 匹配前端轮询周期

# 容器列表全局缓存（消除 WS/HTTP/batchQR 多路重复调用 Docker API）
_containers_cache: Dict[str, Any] = {"data": [], "ts": 0}
_CONTAINERS_CACHE_TTL = 3  # 秒

# Docker API 调用专用线程池（隔离卡死容器，避免阻塞主线程池）
# 60+ 实例场景：每实例 check_login 需 1-2 个线程，64 workers 可同时处理 32-64 实例
_docker_pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="docker-api")
_DOCKER_STATS_TIMEOUT = 3   # 秒，c.stats(stream=False) 超时
_DOCKER_LOGS_TIMEOUT = 2    # 秒，c.logs() 超时
_DOCKER_LIST_TIMEOUT = 20  # 秒，苏州 100+ 容器场景下 5s 偶发不够


class DockerManager(LoginMixin, LifecycleMixin):
    def __init__(self):
        try:
            self.client = docker.from_env()
            logger.info("Docker 连接成功")
        except docker.errors.DockerException as e:
            logger.error("Docker 连接失败: %s", e)
            self.client = None

    def list_containers(self, use_cache: bool = True) -> List[Dict]:
        """获取本地 NapCat 容器列表。

        use_cache=True 时使用全局缓存（3s TTL），避免 WS/HTTP/batchQR 多路重复调 Docker API。
        容器操作后应调 invalidate_containers_cache() 立即失效。
        """
        now = time.time()
        if use_cache and now - _containers_cache["ts"] < _CONTAINERS_CACHE_TTL:
            return _containers_cache["data"]

        if not self.client:
            return []
        try:
            future = _docker_pool.submit(self.client.containers.list, all=True)
            containers = future.result(timeout=_DOCKER_LIST_TIMEOUT)
        except FuturesTimeoutError:
            logger.warning("Docker 容器列表获取超时 (%ss)", _DOCKER_LIST_TIMEOUT)
            # 超时返回缓存兜底
            return _containers_cache.get("data", [])
        except docker.errors.DockerException as e:
            logger.error("列举容器失败: %s", e)
            return _containers_cache.get("data", [])

        res = []
        from services.config import get_container_keywords
        keywords = get_container_keywords()
        for c in containers:
            try:
                tags_str = str(c.image.tags).lower()
            except (AttributeError, IndexError):
                tags_str = ""
            if any(kw in tags_str or kw in c.name.lower() for kw in keywords):
                res.append({
                    "id": c.short_id,
                    "name": c.name,
                    "status": c.status,
                    "image": str(c.image.tags[0]) if c.image.tags else "unknown",
                    "created": c.attrs.get("Created", ""),
                })
        _containers_cache["data"] = res
        _containers_cache["ts"] = now
        return res

    @staticmethod
    def invalidate_containers_cache():
        """操作后立即失效容器列表缓存，使下次请求直接查询 Docker。"""
        _containers_cache["ts"] = 0

    def create_container(
        self, name: str,
        volumes: Optional[Dict] = None,
        ports: Optional[Dict] = None,
        docker_image: str = "mlikiowa/napcat-docker:latest",
        **extra_kwargs,
    ) -> Optional[str]:
        if not self.client:
            return None
        try:
            run_kwargs = {
                "name": name,
                "detach": True,
                "environment": extra_kwargs.pop("environment", {"ACCOUNT": ""}),
                "restart_policy": extra_kwargs.pop("restart_policy", {"Name": "always"}),
            }
            if volumes:
                run_kwargs["volumes"] = volumes
            if ports:
                run_kwargs["ports"] = ports
            # 合并高级参数 (mem_limit, network_mode 等)
            run_kwargs.update(extra_kwargs)

            container = self.client.containers.run(docker_image, **run_kwargs)
            logger.info("容器 %s 创建成功 (id=%s)", name, container.short_id)
            return container.short_id
        except docker.errors.ImageNotFound:
            logger.error("镜像 %s 不存在，请先拉取", docker_image)
            return None
        except docker.errors.APIError as e:
            logger.error("创建容器 %s 失败: %s", name, e)
            return None

    def action_container(self, name: str, action: str) -> bool:
        if not self.client:
            return False
        try:
            c = self.client.containers.get(name)
            if action == "start":
                c.start()
            elif action == "stop":
                c.stop()
            elif action == "restart":
                c.restart()
            elif action == "pause":
                c.pause()
            elif action == "unpause":
                c.unpause()
            elif action == "kill":
                c.kill()
            elif action == "delete":
                try:
                    c.stop(timeout=2)
                except docker.errors.APIError:
                    pass
                c.remove(force=True)
            else:
                logger.warning("未知操作: %s", action)
                return False
            logger.info("容器 %s 执行 [%s] 成功", name, action)
            return True
        except docker.errors.NotFound:
            logger.error("容器 %s 不存在", name)
            return False
        except docker.errors.APIError as e:
            logger.error("容器 %s 执行 [%s] 失败: %s", name, action, e)
            return False

    def get_logs(self, name: str, lines: int = 100) -> str:
        if not self.client:
            return ""
        try:
            c = self.client.containers.get(name)
            future = _docker_pool.submit(c.logs, tail=lines)
            raw = future.result(timeout=_DOCKER_LOGS_TIMEOUT + 3)
            return raw.decode("utf-8", errors="replace")
        except docker.errors.NotFound:
            return ""
        except FuturesTimeoutError:
            logger.warning("容器 %s 日志获取超时", name)
            return "[日志获取超时，容器可能无响应]\n"
        except docker.errors.APIError as e:
            logger.error("获取容器 %s 日志失败: %s", name, e)
            return ""

    def get_container_file_binary(self, name: str, path: str) -> Optional[bytes]:
        """通过 docker cp (tar) 从容器内读取文件，带超时保护"""
        result = self.get_container_file_binary_with_mtime(name, path)
        return result[0] if result else None

    def get_container_file_binary_with_mtime(self, name: str, path: str) -> Optional[tuple[bytes, float]]:
        """通过 docker cp (tar) 从容器内读取文件和 mtime，带超时保护。"""
        if not self.client:
            return None
        try:
            c = self.client.containers.get(name)

            def _read_archive():
                bits, _ = c.get_archive(path)
                tar_stream = io.BytesIO()
                for chunk in bits:
                    tar_stream.write(chunk)
                tar_stream.seek(0)
                with tarfile.open(fileobj=tar_stream) as tar:
                    member = tar.next()
                    if member:
                        file_obj = tar.extractfile(member)
                        if file_obj:
                            return file_obj.read(), float(getattr(member, "mtime", 0) or 0)
                return None

            future = _docker_pool.submit(_read_archive)
            return future.result(timeout=5)
        except FuturesTimeoutError:
            logger.warning("容器 %s 文件读取超时: %s", name, path)
            return None
        except docker.errors.NotFound:
            return None
        except (docker.errors.APIError, tarfile.TarError, OSError) as e:
            logger.debug("读取容器文件 %s:%s 失败: %s", name, path, e)
            return None
        return None

    def get_logs_with_timestamps(self, name: str, lines: int = 200) -> str:
        """获取带 Docker 时间戳的容器日志，供 QR 最新 URL 解析使用。"""
        if not self.client:
            return ""
        try:
            c = self.client.containers.get(name)
            future = _docker_pool.submit(c.logs, tail=lines, timestamps=True)
            raw = future.result(timeout=_DOCKER_LOGS_TIMEOUT + 3)
            return raw.decode("utf-8", errors="replace")
        except docker.errors.NotFound:
            return ""
        except FuturesTimeoutError:
            logger.warning("容器 %s 带时间戳日志获取超时", name)
            return ""
        except docker.errors.APIError as e:
            logger.error("获取容器 %s 带时间戳日志失败: %s", name, e)
            return ""

    def get_container_status(self, name: str) -> str:
        """返回容器当前 status，失败返回空串。"""
        if not self.client:
            return ""
        try:
            c = self.client.containers.get(name)
            try:
                c.reload()
            except Exception:
                pass
            return c.status or ""
        except docker.errors.NotFound:
            return ""
        except docker.errors.APIError as e:
            logger.debug("获取容器 %s 状态失败: %s", name, e)
            return ""

    def get_basic_stats(self, name: str) -> Dict:
        """获取容器基础资源统计 (CPU / 内存)，带内存缓存（TTL 8s）。
        Docker stats API 有超时保护，避免卡死容器阻塞线程池。
        """
        now = time.time()
        cached = _stats_cache.get(name)
        if cached and now - cached.get("_ts", 0) < _STATS_CACHE_TTL:
            return {k: v for k, v in cached.items() if k != "_ts"}

        if not self.client:
            return {}
        try:
            c = self.client.containers.get(name)
            if c.status != "running":
                result = {
                    "status": c.status,
                    "created": c.attrs.get("Created", ""),
                    "cpu_percent": 0.0,
                    "mem_usage": 0.0,
                    "mem_limit": 0.0,
                }
                _stats_cache[name] = {**result, "_ts": now}
                return result

            # 用线程池 + 超时包裹 Docker stats API，防止卡死容器阻塞
            future = _docker_pool.submit(c.stats, stream=False)
            try:
                stats = future.result(timeout=_DOCKER_STATS_TIMEOUT)
            except (FuturesTimeoutError, Exception) as e:
                logger.warning("容器 %s stats 超时或异常: %s", name, e)
                # 超时时返回上次缓存或零值
                if cached:
                    return {k: v for k, v in cached.items() if k != "_ts"}
                return {
                    "status": c.status,
                    "created": c.attrs.get("Created", ""),
                    "cpu_percent": 0.0, "mem_usage": 0.0, "mem_limit": 0.0,
                }

            mem_usage = stats.get("memory_stats", {}).get("usage", 0)
            mem_limit = stats.get("memory_stats", {}).get("limit", 0)
            cpu_delta = (
                stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
                - stats.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
            )
            system_delta = (
                stats.get("cpu_stats", {}).get("system_cpu_usage", 0)
                - stats.get("precpu_stats", {}).get("system_cpu_usage", 0)
            )
            cpu_percent = 0.0
            if system_delta > 0 and cpu_delta > 0:
                percpu = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("percpu_usage", [1])
                cpu_percent = (cpu_delta / system_delta) * len(percpu) * 100.0

            result = {
                "status": c.status,
                "created": c.attrs.get("Created", ""),
                "cpu_percent": round(cpu_percent, 2),
                "mem_usage": round(mem_usage / 1024 / 1024, 2),
                "mem_limit": round(mem_limit / 1024 / 1024, 2),
            }
            _stats_cache[name] = {**result, "_ts": now}
            return result
        except docker.errors.NotFound:
            return {}
        except docker.errors.APIError as e:
            logger.error("获取容器 %s 统计失败: %s", name, e)
            return {}

    def get_napcat_info(self, name: str) -> Dict:
        """获取 NapCat 扩展信息 (UIN, 版本, WebUI token 等)"""
        info: Dict = {
            "uin": "未登录 / Not Logged In",
            "version": "Unknown",
            "webui_token": "",
            "webui_port": 0,
            "http_port": 0,
            "platform": "",
            "uptime_formatted": "",
            "network_endpoints": {"http": 0, "ws": 0, "http_client": 0, "ws_client": 0},
        }
        if not self.client:
            return info

        try:
            c = self.client.containers.get(name)
        except docker.errors.NotFound:
            return info

        # 端口解析 — 使用公共方法
        info["webui_port"] = self.resolve_host_port(c, "6099/tcp")
        info["http_port"] = self.resolve_host_port(c, "3000/tcp")

        # WebUI token — 优先从宿主机本地文件读取
        try:
            local_webui = os.path.join(get_data_dir(), name, "config", "webui.json")
            if os.path.exists(local_webui):
                with open(local_webui, "r", encoding="utf-8") as f:
                    w_config = json.loads(f.read())
                    if "token" in w_config:
                        info["webui_token"] = w_config["token"]
        except (json.JSONDecodeError, OSError):
            pass

        # UIN — 只读内存缓存（由 get_stats 定期写入，不在此触发 API 调用）
        cached = _login_cache.get(name, {})
        if cached.get("logged_in") and cached.get("uin"):
            info["uin"] = cached["uin"]
            self._sync_webui_auto_login(name, cached["uin"])

        # NapCat API info
        try:
            if info.get("webui_port"):
                url = f"http://{_host_gateway()}:{info['webui_port']}/plugin/napcat-plugin-builtin/api/public/info"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=1) as response:
                    api_out = json.loads(response.read().decode("utf-8"))
                    if api_out.get("code") == 0 and "data" in api_out:
                        info["uptime_formatted"] = api_out["data"].get("uptimeFormatted", "")
                        info["platform"] = api_out["data"].get("platform", "")
        except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
            pass

        # Network endpoints — 从宿主机本地 onebot11_{uin}.json 文件读取
        if info["uin"] != "未登录 / Not Logged In":
            try:
                cfg_path = os.path.join(get_data_dir(), name, "config", f"onebot11_{info['uin']}.json")
                if os.path.exists(cfg_path):
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        uin_config = json.loads(f.read())
                    net = uin_config.get("network", {})
                    info["network_endpoints"]["http"] = len([s for s in net.get("httpServers", []) if s.get("enable")])
                    info["network_endpoints"]["ws"] = len([s for s in net.get("websocketServers", []) if s.get("enable")])
                    info["network_endpoints"]["http_client"] = len([s for s in net.get("httpClients", []) if s.get("enable")])
                    info["network_endpoints"]["ws_client"] = len([s for s in net.get("websocketClients", []) if s.get("enable")])
            except (json.JSONDecodeError, OSError):
                pass

        # Version from logs — 用线程池 + 超时包裹，防止卡死容器阻塞
        try:
            future = _docker_pool.submit(c.logs, tail=200)
            raw_logs = future.result(timeout=_DOCKER_LOGS_TIMEOUT)
            logs_tail = raw_logs.decode("utf-8", errors="ignore")
            ver_match = re.search(r"NapCat\.Core Version:\s*([\d.]+)", logs_tail)
            if ver_match:
                info["version"] = ver_match.group(1)
        except (FuturesTimeoutError, docker.errors.APIError, Exception):
            pass

        return info

    def get_stats(self, name: str) -> Dict:
        """获取完整统计 (基础资源 + NapCat 信息 + 登录状态)。

        三个子任务并行执行，各自有超时保护，单个子任务失败不阻塞其他。
        """
        # 并行提交三个子任务
        f_basic = _docker_pool.submit(self.get_basic_stats, name)
        f_napcat = _docker_pool.submit(self.get_napcat_info, name)
        f_login = _docker_pool.submit(self.check_login_status, name)

        try:
            basic = f_basic.result(timeout=_DOCKER_STATS_TIMEOUT + 1)
        except Exception:
            basic = {}
        if not basic:
            return {}

        try:
            napcat = f_napcat.result(timeout=_DOCKER_LOGS_TIMEOUT + 2)
        except Exception:
            napcat = {}

        try:
            login = f_login.result(timeout=4)
        except Exception:
            login = {}

        if login.get("logged_in") and login.get("uin"):
            napcat["uin"] = login["uin"]
        return {**basic, **napcat}

    # ============ 端口解析 ============

    def resolve_host_port(self, container, internal_port: str) -> int:
        """从容器对象解析内部端口对应的宿主机映射端口，返回 0 表示未找到"""
        try:
            ports_dict = container.attrs.get("NetworkSettings", {}).get("Ports", {})
            if internal_port in ports_dict and ports_dict[internal_port]:
                return int(ports_dict[internal_port][0]["HostPort"])
        except (KeyError, IndexError, ValueError):
            pass
        try:
            hc_ports = container.attrs.get("HostConfig", {}).get("PortBindings", {})
            if internal_port in hc_ports and hc_ports[internal_port]:
                return int(hc_ports[internal_port][0]["HostPort"])
        except (KeyError, IndexError, ValueError):
            pass
        return 0

    # ============ 端口与镜像管理 ============

    def get_used_ports(self) -> set:
        """扫描所有已使用的宿主机端口（Docker容器 + 系统监听）"""
        used = set()
        # 1. Docker 容器端口
        if self.client:
            try:
                future = _docker_pool.submit(self.client.containers.list, all=True)
                docker_containers = future.result(timeout=_DOCKER_LIST_TIMEOUT)
                for c in docker_containers:
                    ports_dict = c.attrs.get("NetworkSettings", {}).get("Ports", {})
                    for _, bindings in ports_dict.items():
                        if bindings:
                            for b in bindings:
                                try:
                                    used.add(int(b["HostPort"]))
                                except (KeyError, ValueError):
                                    pass
            except FuturesTimeoutError:
                logger.warning("Docker 已用端口列表获取超时 (%ss)", _DOCKER_LIST_TIMEOUT)
            except docker.errors.APIError:
                pass
        # 2. 系统监听端口（用 psutil 快速获取，避免逐端口 socket 扫描）
        try:
            import psutil
            for conn in psutil.net_connections(kind="tcp"):
                if conn.status == "LISTEN" and conn.laddr:
                    used.add(conn.laddr.port)
        except (ImportError, OSError, AttributeError):
            pass
        return used

    def find_available_port(self, base: int, used_ports: set) -> int:
        """从 base 开始找到下一个可用端口（不超过 65535）"""
        port = base
        while port in used_ports:
            port += 1
            if port > 65535:
                raise ValueError(f"没有可用端口（从 {base} 开始，所有端口均被占用）")
        return port

    # ============ 镜像管理 ============

    def list_images(self) -> List[Dict]:
        """列出本地 Docker 镜像"""
        if not self.client:
            return []
        try:
            future = _docker_pool.submit(self.client.images.list)
            images = future.result(timeout=_DOCKER_STATS_TIMEOUT)
            result = []
            for img in images:
                tags = img.tags or []
                size_mb = round(img.attrs.get("Size", 0) / 1024 / 1024, 1)
                created = img.attrs.get("Created", "")
                result.append({
                    "id": img.short_id.replace("sha256:", ""),
                    "tags": tags,
                    "size": size_mb,
                    "created": created,
                })
            return result
        except FuturesTimeoutError:
            logger.warning("Docker 镜像列表获取超时")
            return []
        except docker.errors.DockerException as e:
            logger.error("列举镜像失败: %s", e)
            return []

    def pull_image(self, image_name: str) -> bool:
        """拉取 Docker 镜像"""
        if not self.client:
            return False
        try:
            self.client.images.pull(image_name)
            logger.info("镜像拉取成功: %s", image_name)
            return True
        except docker.errors.DockerException as e:
            logger.error("镜像拉取失败 %s: %s", image_name, e)
            return False

    def delete_image(self, image_id: str, force: bool = False) -> bool:
        """删除 Docker 镜像"""
        if not self.client:
            return False
        try:
            self.client.images.remove(image_id, force=force)
            logger.info("镜像删除成功: %s", image_id)
            return True
        except docker.errors.DockerException as e:
            logger.error("镜像删除失败 %s: %s", image_id, e)
            return False


docker_manager = DockerManager()

