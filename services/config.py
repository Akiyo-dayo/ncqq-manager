"""
配置管理单例 — 双源架构：JSON（启动引导字段）+ SQLite settings 表（运行时字段），支持热更新。
"""
import os
import json
import uuid
from typing import Any, Dict
from services.log import logger

APP_VERSION = "1.0.0"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
FRONTEND_DIST = os.path.join(BASE_DIR, "frontend", "dist")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)


class AppConfig:
    """应用配置单例 - 双源架构：JSON(启动引导) + SQLite(运行时设置)"""

    # 启动引导字段（必须在 JSON 中，DB 初始化前需要）
    _BOOTSTRAP_KEYS = {"initialized", "host", "port"}

    # 运行时字段（存储在 SQLite settings 表）
    _RUNTIME_KEYS = {"webui_base_port", "http_base_port", "ws_base_port",
                     "docker_image", "api_key", "data_dir",
                     "container_keywords",
                     "init_ws_client_enabled", "init_ws_client_url", "init_ws_client_token",
                     "init_auto_join_groups_enabled", "init_auto_join_groups"}

    _BOOTSTRAP_DEFAULT = {
        "initialized": False,
        "host": "0.0.0.0",
        "port": 8000,
    }

    _RUNTIME_DEFAULT = {
        "webui_base_port": 6000,
        "http_base_port": 3000,
        "ws_base_port": 3001,
        "docker_image": "mlikiowa/napcat-docker:latest",
        "api_key": "",
        "data_dir": os.path.join(BASE_DIR, "data"),
        "container_keywords": '["napcat"]',  # JSON 字符串，容器名/镜像名匹配关键词
        "init_ws_client_enabled": False,
        "init_ws_client_url": "ws://127.0.0.1:5100/onebot/v11/ws",
        "init_ws_client_token": "",
        "init_auto_join_groups_enabled": False,  # 登录后自动发群通知开关
        "init_auto_join_groups": "[]",  # 登录后自动发通知的群号列表 JSON（如 ["123456"]）
    }

    def __init__(self):
        self._data: Dict[str, Any] = {}
        self._runtime_loaded = False
        self._load_bootstrap()

    def _load_bootstrap(self):
        """加载启动引导配置（仅 JSON）"""
        if not os.path.exists(CONFIG_FILE):
            self._data = {**self._BOOTSTRAP_DEFAULT}
            self._save_bootstrap()
            logger.info("已生成默认配置文件: %s", CONFIG_FILE)
            return

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            self._data = json.load(f)

        # 合并缺失的启动引导字段
        changed = False
        for k, v in self._BOOTSTRAP_DEFAULT.items():
            if k not in self._data:
                self._data[k] = v
                changed = True
        if changed:
            self._save_bootstrap()
            logger.info("配置文件已补充缺失字段")

    def _save_bootstrap(self):
        """保存启动引导配置（仅 JSON）"""
        bootstrap_data = {k: v for k, v in self._data.items() if k in self._BOOTSTRAP_KEYS}
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(bootstrap_data, f, indent=4, ensure_ascii=False)

    def _load_runtime_from_db(self, *, persist_missing_defaults: bool):
        import services.database as db

        settings = db.get_all_settings()
        for k, default_v in self._RUNTIME_DEFAULT.items():
            if k in settings:
                self._data[k] = settings[k]
                continue
            val = default_v if default_v else (uuid.uuid4().hex if k == "api_key" else default_v)
            self._data[k] = val
            if persist_missing_defaults:
                db.set_setting(k, val)

    def load_runtime_once(self):
        """首次加载运行时配置（从 SQLite settings 表）- 必须在 init_db() 后调用"""
        if self._runtime_loaded:
            return
        self._load_runtime_from_db(persist_missing_defaults=True)
        self._runtime_loaded = True
        logger.info("运行时配置已从 SQLite 加载")

    def reload_runtime(self):
        """强制从 SQLite 重新读取运行时配置到内存。"""
        self._load_runtime_from_db(persist_missing_defaults=False)
        self._runtime_loaded = True
        logger.info("运行时配置已从 SQLite 刷新")

    def load_runtime(self):
        """兼容旧调用：等价于 load_runtime_once()."""
        self.load_runtime_once()

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any):
        """设置配置值 - 自动判断存储位置"""
        self._data[key] = value
        if key in self._BOOTSTRAP_KEYS:
            self._save_bootstrap()
        elif key in self._RUNTIME_KEYS:
            import services.database as db
            db.set_setting(key, value)

    def update(self, updates: dict):
        """批量更新配置"""
        self._data.update(updates)

        # 分离启动引导和运行时字段
        bootstrap_updates = {k: v for k, v in updates.items() if k in self._BOOTSTRAP_KEYS}
        runtime_updates = {k: v for k, v in updates.items() if k in self._RUNTIME_KEYS}

        if bootstrap_updates:
            self._save_bootstrap()

        if runtime_updates:
            import services.database as db
            for k, v in runtime_updates.items():
                db.set_setting(k, v)

    def bootstrap_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._BOOTSTRAP_KEYS))

    @property
    def runtime_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._RUNTIME_KEYS))

    @property
    def source_matrix(self) -> dict[str, tuple[str, ...]]:
        return {
            "bootstrap": self.bootstrap_keys,
            "runtime": self.runtime_keys,
        }

    def reload(self):
        """重新加载所有配置：JSON 引导字段 + SQLite 运行时字段。"""
        self._load_bootstrap()
        self.reload_runtime()

def get_data_dir() -> str:
    """获取实例数据挂载目录，支持用户在设置中修改"""
    d = app_config.get("data_dir")
    if not d:
        d = DATA_DIR
    os.makedirs(d, exist_ok=True)
    return d


def get_container_keywords() -> list[str]:
    """获取容器识别关键词列表。容器名或镜像名包含任一关键词即视为托管容器。"""
    raw = app_config.get("container_keywords", '["napcat"]')
    try:
        kw = json.loads(raw) if isinstance(raw, str) else raw
        return [w.lower() for w in kw if isinstance(w, str) and w.strip()]
    except (json.JSONDecodeError, TypeError):
        return ["napcat"]


# 全局单例
app_config = AppConfig()

