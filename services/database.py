"""
SQLite 数据库层 — 替代所有 JSON 文件存储
单一共享连接 + autocommit + 线程锁，彻底解决并发写入 "database is locked" 问题
"""
import os
import json
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from services.log import logger
from services.config import CONFIG_DIR

DB_PATH = os.path.join(CONFIG_DIR, "app.db")

# 单一共享连接 + 全局锁（SQLite 本身单写入者，Python 层也需串行化）
_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(
            DB_PATH,
            timeout=30,
            check_same_thread=False,   # 允许跨线程使用同一连接
            isolation_level=None,      # autocommit: 每条 DML 自动提交，不持有隐式事务锁
        )
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.execute("PRAGMA busy_timeout=10000")
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db():
    """创建所有表（幂等）+ 执行增量迁移"""
    with _lock:
        conn = _get_conn()
        conn.executescript(_SCHEMA)
    _run_migrations()
    logger.info("SQLite 数据库已就绪: %s", DB_PATH)


def _run_migrations():
    """基于 schema_version 的增量迁移"""
    version = get_setting("schema_version", 0)
    if not isinstance(version, int):
        version = int(version)

    migrations = [
        # version 1: operation_logs 增加结构化操作者字段
        "operation_logs_operator_columns",
        # version 2: operation_logs 增加查询索引
        "operation_logs_query_indexes",
        # version 3: scheduled_tasks 增加执行结果字段
        "scheduled_tasks_result_fields",
    ]

    target = len(migrations)
    if version >= target:
        return

    for i in range(version, target):
        try:
            with _lock:
                conn = _get_conn()
                if migrations[i] == "operation_logs_operator_columns":
                    columns = {
                        row["name"] for row in conn.execute("PRAGMA table_info(operation_logs)").fetchall()
                    }
                    if "operator_name" not in columns:
                        conn.execute("ALTER TABLE operation_logs ADD COLUMN operator_name TEXT")
                    if "operator_uuid" not in columns:
                        conn.execute("ALTER TABLE operation_logs ADD COLUMN operator_uuid TEXT")
                    if "operator_ip" not in columns:
                        conn.execute("ALTER TABLE operation_logs ADD COLUMN operator_ip TEXT")
                if migrations[i] == "operation_logs_query_indexes":
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_oplogs_operator_uuid_ts "
                        "ON operation_logs(operator_uuid, timestamp DESC)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_oplogs_type_ts "
                        "ON operation_logs(type, timestamp DESC)"
                    )
                if migrations[i] == "scheduled_tasks_result_fields":
                    tables = {
                        row["name"] for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if "scheduled_tasks" in tables:
                        columns = {
                            row["name"] for row in conn.execute(
                                "PRAGMA table_info(scheduled_tasks)"
                            ).fetchall()
                        }
                        if "last_result" not in columns:
                            conn.execute(
                                "ALTER TABLE scheduled_tasks ADD COLUMN last_result TEXT DEFAULT ''"
                            )
                        if "last_error" not in columns:
                            conn.execute(
                                "ALTER TABLE scheduled_tasks ADD COLUMN last_error TEXT DEFAULT ''"
                            )
                        if "run_count" not in columns:
                            conn.execute(
                                "ALTER TABLE scheduled_tasks ADD COLUMN run_count INTEGER DEFAULT 0"
                            )
            logger.info("数据库迁移 v%d → v%d 完成", i, i + 1)
        except Exception as e:
            logger.error("数据库迁移 v%d 失败: %s", i + 1, e)
            return

    set_setting("schema_version", target)
    commit()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uuid       TEXT PRIMARY KEY,
    userName   TEXT UNIQUE NOT NULL,
    passWord   TEXT NOT NULL,
    permission INTEGER NOT NULL DEFAULT 1,
    registerTime TEXT DEFAULT '',
    loginTime  TEXT DEFAULT '',
    apiKey     TEXT DEFAULT '',
    instances  TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_users_apiKey ON users(apiKey);
CREATE INDEX IF NOT EXISTS idx_users_userName ON users(userName);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    uuid       TEXT NOT NULL,
    userName   TEXT NOT NULL,
    permission INTEGER NOT NULL,
    loginTime  TEXT DEFAULT '',
    created_at REAL NOT NULL,
    last_active REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions(uuid);

CREATE TABLE IF NOT EXISTS nodes (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    address TEXT NOT NULL,
    api_key TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS operation_logs (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    level         TEXT NOT NULL DEFAULT 'info',
    time          TEXT NOT NULL,
    timestamp     INTEGER NOT NULL,
    operator_name TEXT,
    operator_uuid TEXT,
    operator_ip   TEXT,
    payload       TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_oplogs_ts ON operation_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_oplogs_operator_uuid_ts ON operation_logs(operator_uuid, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_oplogs_type_ts ON operation_logs(type, timestamp DESC);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    cron_expr TEXT DEFAULT '',
    interval_seconds INTEGER DEFAULT 3600,
    config TEXT DEFAULT '{}',
    last_run REAL DEFAULT 0,
    last_result TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    run_count INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_failures (
    ip         TEXT PRIMARY KEY,
    count      INTEGER NOT NULL DEFAULT 0,
    first_fail REAL NOT NULL,
    last_fail  REAL NOT NULL
);
"""

# ──────────── settings 读写助手 ────────────

def get_setting(key: str, default: Any = None) -> Any:
    """从 settings 表读取一个配置值（JSON 反序列化）"""
    row = fetchone("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_setting(key: str, value: Any):
    """写入一个配置值（JSON 序列化）"""
    execute(
        "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_all_settings() -> Dict[str, Any]:
    """读取所有 settings 返回字典"""
    rows = fetchall("SELECT key,value FROM settings")
    result: Dict[str, Any] = {}
    for r in rows:
        try:
            result[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            result[r["key"]] = r["value"]
    return result


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        return _get_conn().execute(sql, params)


def executemany(sql: str, params_list: list) -> sqlite3.Cursor:
    with _lock:
        return _get_conn().executemany(sql, params_list)


def commit():
    """显式提交 — autocommit (isolation_level=None) 模式下为 no-op。

    当前 SQLite 连接使用 autocommit 模式（每条 DML 自动提交），
    因此调用 commit() 不产生实际效果。保留此函数是为了：
    1. 保持调用方代码的语义一致性（若未来切回显式事务模型）
    2. 避免移除后大量调用点报错

    已知行为：重启后所有 DML 均已自动持久化，无需担心丢失。
    """
    pass


def close_db():
    """关闭数据库连接（应用关闭时调用）"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
            logger.info("数据库连接已关闭")


def fetchone(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with _lock:
        return _get_conn().execute(sql, params).fetchone()


def fetchall(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    with _lock:
        return _get_conn().execute(sql, params).fetchall()


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows: List[sqlite3.Row]) -> List[dict]:
    return [dict(r) for r in rows]


