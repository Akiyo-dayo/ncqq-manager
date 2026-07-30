"""
登录状态检测 Mixin

LoginMixin 提供给 DockerManager 使用，需要 self.client 和 self.resolve_host_port。
全局登录缓存（_login_cache）在本模块维护，由 read_login_cache 公开只读访问。
"""

import json
import os
import time
import urllib.request
import urllib.error
from concurrent.futures import as_completed
from typing import Dict, List

import docker
import docker.errors

from services.log import logger
from services.config import get_data_dir

# ---------------------------------------------------------------------------
# 全局登录缓存（由此模块集中管理）
# ---------------------------------------------------------------------------
_login_cache: Dict[str, Dict] = {}
_LOGIN_CACHE_TTL = 30  # 秒，已登录容器无需频繁检查
_LOGIN_CACHE_TTL_FAIL = 8  # 秒，未登录容器短缓存（快速发现状态变化）


def read_login_cache(name: str) -> Dict:
    """公开接口：只读访问登录状态缓存（供 router 层使用，零阻塞）。"""
    return _login_cache.get(name, {})


def clear_login_cache(name: str) -> bool:
    """公开接口：清理指定容器的登录缓存，返回是否存在并已清理。"""
    return _login_cache.pop(name, None) is not None


def _normalize_uin(raw: str) -> str:
    """归一化 QQ 号：仅保留数字，去除 protocol_ 等前缀。

    过滤 NapCat 内部短 uid，避免把 77/9264 这类内部 ID 当 QQ 号。
    """
    uin = "".join(ch for ch in str(raw) if ch.isdigit())
    return uin if uin and uin != "0" and len(uin) >= 5 else ""


def _host_gateway() -> str:
    return (os.environ.get("HOST_GATEWAY") or "127.0.0.1").strip() or "127.0.0.1"


def _host_url(port: int, path: str) -> str:
    return f"http://{_host_gateway()}:{port}{path}"


# ---------------------------------------------------------------------------
# LoginMixin — 混入 DockerManager，需要 self.client / self.resolve_host_port
# ---------------------------------------------------------------------------


class LoginMixin:
    """登录状态检测方法集合，混入 DockerManager 使用。"""

    def check_login_via_onebot(self, name: str) -> Dict:
        """方案 A：通过 OneBot 11 HTTP API /get_login_info 检测。
        已登录 → {logged_in: True, uin, nickname, method: 'onebot'}
        未登录 → {logged_in: False}
        """
        if not self.client:  # type: ignore[attr-defined]
            return {"logged_in": False}
        try:
            c = self.client.containers.get(name)  # type: ignore[attr-defined]
            if c.status != "running":
                return {"logged_in": False}
            http_port = self.resolve_host_port(c, "3000/tcp")  # type: ignore[attr-defined]
            if not http_port:
                return {"logged_in": False}
            req = urllib.request.Request(
                _host_url(http_port, "/get_login_info"),
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if result.get("status") == "ok" and result.get("data", {}).get("user_id"):
                uid = str(result["data"]["user_id"])
                if uid and uid != "0":
                    return {
                        "logged_in": True,
                        "uin": uid,
                        "nickname": result["data"].get("nickname", ""),
                        "method": "onebot",
                    }
        except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
            pass
        except docker.errors.NotFound:
            pass
        return {"logged_in": False}

    def check_login_via_webui(self, name: str) -> Dict:
        """方案 B：通过 NapCat WebUI + 本地文件综合检测。

        三重验证（全部满足才确认已登录）：
        1. public/info 正常返回 → NapCat 在运行
        2. qrcode.png 停止刷新（mtime > 30s）→ 不在输出二维码
        3. onebot11_{uin}.json 或 napcat_{uin}.json 存在 → 可提取 uin

        单一否决：
        - /api/qrcode 返回包含 url 的有效数据 → 确认未登录
        """
        if not self.client:  # type: ignore[attr-defined]
            return {"logged_in": False}
        try:
            c = self.client.containers.get(name)  # type: ignore[attr-defined]
            if c.status != "running":
                return {"logged_in": False}
            webui_port = self.resolve_host_port(c, "6099/tcp")  # type: ignore[attr-defined]
            if not webui_port:
                return {"logged_in": False}

            # 检查 1 + 2 并行：qrcode 和 public/info 同时请求
            from services.docker_manager import _docker_pool

            def _fetch_qrcode():
                try:
                    qr_req = urllib.request.Request(
                        _host_url(webui_port, "/api/qrcode"),
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    with urllib.request.urlopen(qr_req, timeout=1) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except (urllib.error.URLError, json.JSONDecodeError, OSError):
                    return None

            def _fetch_public_info():
                try:
                    info_req = urllib.request.Request(
                        _host_url(webui_port, "/plugin/napcat-plugin-builtin/api/public/info"),
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    with urllib.request.urlopen(info_req, timeout=1) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except (urllib.error.URLError, json.JSONDecodeError, OSError):
                    return None

            f_qr = _docker_pool.submit(_fetch_qrcode)
            f_info = _docker_pool.submit(_fetch_public_info)

            try:
                qr_data = f_qr.result(timeout=2)
            except Exception:
                qr_data = None

            if qr_data:
                # 兼容返回结构 {"code": 0, "data": {"bstate": 1, "url": "..."}}
                # 以及直接返回 {"bstate": 1, "url": "..."} 的情况
                data = qr_data.get("data", qr_data) if isinstance(qr_data, dict) else {}
                if isinstance(data, dict):
                    bstate = data.get("bstate")
                    url = data.get("url")
                    # bstate: 1(待扫码), 2(待确认), 3(已失效/到期), 4(登录中?) 等等
                    # 或者有明确的 url，都说明需要扫码，即未登录
                    if url or bstate in (1, 2, 3, 4):
                        return {"logged_in": False}

            # 检查 3：如果 qr_data 为 None（API 请求失败），检查二维码文件状态
            # 如果二维码文件存在且新鲜（< 30s），说明正在待扫码，返回未登录
            if qr_data is None:
                try:
                    qr_path = os.path.join(get_data_dir(), name, "cache", "qrcode.png")
                    if os.path.exists(qr_path):
                        age = time.time() - os.path.getmtime(qr_path)
                        if age < 30:
                            # 二维码文件新鲜，说明正在待扫码
                            return {"logged_in": False}
                except OSError:
                    pass

            # 检查 4：NapCat 进程存活 + 二维码停止刷新 + 有 UIN 配置
            napcat_alive = False
            try:
                info_data = f_info.result(timeout=2)
                if info_data and info_data.get("code") == 0 and "data" in info_data:
                    napcat_alive = True
            except Exception:
                pass

            qr_stale = False
            try:
                qr_path = os.path.join(get_data_dir(), name, "cache", "qrcode.png")
                if os.path.exists(qr_path):
                    age = time.time() - os.path.getmtime(qr_path)
                    qr_stale = age > 30
                else:
                    qr_stale = True
            except OSError:
                pass

            # 曾经这里有一条"NapCat 存活 + 二维码超过 30s 没刷新 + config 目录里有
            # onebot11 文件 ⇒ 判定已登录"的兜底，uin 直接取自文件名。
            # 它是"换号后仍显示旧 QQ"最严重的一环：换号并不会删掉旧账号的
            # onebot11_<旧号>.json，于是旧号被当成当前账号写进缓存，进而触发
            # 用旧号改写 webui.json 的 autoLoginAccount 并自动重启容器 ——
            # 不只是显示陈旧，是主动把状态改错。
            # 登录真值统一由 async_login_checker 的级联检测给出，这里只报告存活。
            _ = (napcat_alive, qr_stale)

        except docker.errors.NotFound:
            pass
        return {"logged_in": False}

    def _get_uin_from_config(self, name: str) -> str:
        """从本地 onebot11_*.json 文件名提取 uin（辅助信息，不用于登录判断）"""
        try:
            config_dir = os.path.join(get_data_dir(), name, "config")
            if not os.path.exists(config_dir):
                return ""
            ob_files = [
                f
                for f in os.listdir(config_dir)
                if f.startswith("onebot11_") and f.endswith(".json")
            ]
            if ob_files:
                latest = max(
                    ob_files,
                    key=lambda f: os.path.getmtime(os.path.join(config_dir, f)),
                )
                raw = latest.replace("onebot11_", "").replace(".json", "")
                return _normalize_uin(raw)
            napcat_files = [
                f
                for f in os.listdir(config_dir)
                if f.startswith("napcat_")
                and f.endswith(".json")
                and not f.startswith("napcat_protocol_")
            ]
            if napcat_files:
                latest = max(
                    napcat_files,
                    key=lambda f: os.path.getmtime(os.path.join(config_dir, f)),
                )
                raw = latest.replace("napcat_", "").replace(".json", "")
                return _normalize_uin(raw)
        except OSError:
            pass
        return ""

    def _sync_webui_auto_login(self, name: str, uin: str) -> None:
        """登录成功后自动同步 webui.json 中的 autoLoginAccount"""
        try:
            local_webui = os.path.join(get_data_dir(), name, "config", "webui.json")
            if not os.path.exists(local_webui):
                return
            with open(local_webui, "r", encoding="utf-8") as wf:
                w_config = json.loads(wf.read())

            modified = False
            if "login" not in w_config or not isinstance(w_config["login"], dict):
                w_config["login"] = {}
                modified = True

            login_cfg = w_config["login"]
            if login_cfg.get("account") != uin:
                login_cfg["account"] = uin
                login_cfg["password"] = ""
                modified = True
            if login_cfg.get("autoLoginAccount") != uin:
                login_cfg["autoLoginAccount"] = uin
                modified = True
            if w_config.get("autoLoginAccount") != uin:
                w_config["autoLoginAccount"] = uin
                modified = True

            if modified:
                with open(local_webui, "w", encoding="utf-8") as wf:
                    json.dump(w_config, wf, indent=4, ensure_ascii=False)
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.debug("同步自动登录配置失败: %s", e)

    def check_login_status(self, name: str, force: bool = False) -> Dict:
        """级联检测登录状态：A(OneBot) → B(WebUI)。

        双层 TTL 缓存：已登录 30s / 未登录 8s。force=True 跳过缓存（用户主动刷新时）。
        返回 {logged_in, uin, nickname, method} 或 {logged_in: False}
        """
        now = time.time()
        prev = _login_cache.get(name, {})

        if not force and prev:
            ttl = _LOGIN_CACHE_TTL if prev.get("logged_in") else _LOGIN_CACHE_TTL_FAIL
            if now - prev.get("ts", 0) < ttl:
                return prev

        result = self.check_login_via_onebot(name)
        if result["logged_in"]:
            result["ts"] = now
            _login_cache[name] = result
            self._on_login_detected(name, result, prev)  # type: ignore[attr-defined]
            return result

        result = self.check_login_via_webui(name)
        if result["logged_in"]:
            result["ts"] = now
            _login_cache[name] = result
            self._on_login_detected(name, result, prev)  # type: ignore[attr-defined]
            return result

        result = {"logged_in": False, "ts": now}
        _login_cache[name] = result
        return result

    def batch_check_login(
        self, names: List[str], timeout: float = 6.0
    ) -> Dict[str, Dict]:
        """批量并行检测多个容器的登录状态。

        双层缓存 TTL：已登录 30s / 未登录 8s。
        缓存命中的直接返回，未命中的并行 API 探测（线程池）。
        """
        from services.docker_manager import _docker_pool

        results: Dict[str, Dict] = {}
        need_check: List[str] = []
        now = time.time()

        for name in names:
            cached = _login_cache.get(name, {})
            ttl = _LOGIN_CACHE_TTL if cached.get("logged_in") else _LOGIN_CACHE_TTL_FAIL
            if now - cached.get("ts", 0) < ttl:
                results[name] = cached
            else:
                need_check.append(name)

        if not need_check:
            return results

        futures = {
            _docker_pool.submit(self.check_login_status, name): name
            for name in need_check
        }
        for future in as_completed(futures, timeout=timeout):
            name = futures[future]
            try:
                results[name] = future.result(timeout=0.1)
            except Exception:
                results[name] = {"logged_in": False}
        for name in need_check:
            if name not in results:
                results[name] = {"logged_in": False}
        return results

    @staticmethod
    def update_login_cache(name: str, event: Dict) -> None:
        """方案 C 预留：插件事件直接更新缓存。
        event 格式: {event: 'login'|'logout', uin, nickname}
        """
        prev = _login_cache.get(name, {})
        if event.get("event") == "login" and event.get("uin"):
            uin = str(event["uin"])
            new_entry = {
                "logged_in": True,
                "uin": uin,
                "nickname": event.get("nickname", ""),
                "method": "plugin",
                "ts": time.time(),
            }
            _login_cache[name] = new_entry
            # 懒加载避免循环导入
            from services.docker_manager import docker_manager as _dm

            _dm._on_login_detected(name, new_entry, prev)
        elif event.get("event") == "logout":
            _login_cache[name] = {
                "logged_in": False,
                "ts": time.time(),
            }
