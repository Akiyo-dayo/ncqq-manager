"""
用户管理子系统 - SQLite 持久化
支持多用户、角色权限、实例绑定、bcrypt 密码哈希（兼容旧 SHA256 格式，登录时静默升级）
"""
import json
import uuid
import hashlib
import time
from typing import Optional, Dict
import bcrypt

from services.log import logger
import services.database as db


# 权限等级
class ROLE:
    ADMIN = 10   # 管理员 - 全部权限
    USER = 1     # 普通用户 - 仅操作分配的实例
    GUEST = 0    # 访客
    BAN = -1     # 封禁


class UserManager:
    _BAN_THRESHOLD = 10   # 失败次数上限
    _BAN_TTL = 1800       # 30 分钟自动解封

    def __init__(self):
        pass

    def ensure_default_admin(self):
        """确保至少存在一个管理员（仅 fallback，正常流程应通过 /api/setup/init 创建）"""
        row = db.fetchone("SELECT 1 FROM users LIMIT 1")
        if not row:
            import secrets
            random_pwd = secrets.token_urlsafe(12)
            self.create_user(username="admin", password=random_pwd, permission=ROLE.ADMIN)
            logger.warning("=" * 60)
            logger.warning("未检测到任何用户，已创建应急管理员账号")
            logger.warning("  用户名: admin")
            logger.warning("  密码:   %s", random_pwd)
            logger.warning("  ⚠️  请立即登录后修改密码！此密码仅显示一次。")
            logger.warning("=" * 60)

    @staticmethod
    def _hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    @staticmethod
    def _verify_password(password: str, stored_hash: str) -> bool:
        # bcrypt 哈希（$2b$、$2a$、$2y$ 开头）
        if stored_hash.startswith("$2"):
            try:
                return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
            except (ValueError, TypeError):
                return False
        # 旧 SHA256+Salt 格式 (salt$hex)
        if "$" not in stored_hash:
            return password == stored_hash
        salt, hashed = stored_hash.split("$", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == hashed

    @staticmethod
    def _needs_rehash(stored_hash: str) -> bool:
        """旧格式（SHA256 或纯文本）需要升级到 bcrypt"""
        return not stored_hash.startswith("$2")

    # ============ 用户 CRUD ============

    def create_user(self, username: str, password: str, permission: int = ROLE.USER) -> Optional[dict]:
        if self.get_user_by_username(username):
            return None
        user_uuid = uuid.uuid4().hex[:24]
        api_key = uuid.uuid4().hex
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO users (uuid,userName,passWord,permission,registerTime,loginTime,apiKey,instances) VALUES (?,?,?,?,?,?,?,?)",
            (user_uuid, username, self._hash_password(password), permission, now, "", api_key, "[]"),
        )
        db.commit()
        return {"uuid": user_uuid, "userName": username, "permission": permission,
                "registerTime": now, "loginTime": "", "apiKey": api_key, "instances": []}

    def edit_user(self, user_uuid: str, **kwargs) -> bool:
        user = self.get_user_by_uuid(user_uuid)
        if not user:
            return False
        sets, params = [], []
        if "userName" in kwargs and kwargs["userName"]:
            existing = self.get_user_by_username(kwargs["userName"])
            if existing and existing["uuid"] != user_uuid:
                return False
            sets.append("userName=?"); params.append(kwargs["userName"])
        if "permission" in kwargs and kwargs["permission"] is not None:
            sets.append("permission=?"); params.append(kwargs["permission"])
        if "passWord" in kwargs and kwargs["passWord"]:
            sets.append("passWord=?"); params.append(self._hash_password(kwargs["passWord"]))
        if "instances" in kwargs:
            sets.append("instances=?"); params.append(json.dumps(kwargs["instances"]))
        if "apiKey" in kwargs:
            sets.append("apiKey=?"); params.append(kwargs["apiKey"])
        if not sets:
            return True
        params.append(user_uuid)
        db.execute(f"UPDATE users SET {','.join(sets)} WHERE uuid=?", tuple(params))
        db.commit()
        return True

    def delete_user(self, user_uuid: str) -> bool:
        cur = db.execute("DELETE FROM users WHERE uuid=?", (user_uuid,))
        db.commit()
        return cur.rowcount > 0

    # ============ 查询 ============

    def get_user_by_uuid(self, user_uuid: str) -> Optional[dict]:
        row = db.fetchone("SELECT * FROM users WHERE uuid=?", (user_uuid,))
        return self._row_to_user(row)

    def get_user_by_username(self, username: str) -> Optional[dict]:
        row = db.fetchone("SELECT * FROM users WHERE userName=?", (username,))
        return self._row_to_user(row)

    def get_user_by_api_key(self, api_key: str) -> Optional[dict]:
        row = db.fetchone("SELECT * FROM users WHERE apiKey=?", (api_key,))
        return self._row_to_user(row)

    @staticmethod
    def _row_to_user(row) -> Optional[dict]:
        if row is None:
            return None
        u = dict(row)
        u["instances"] = json.loads(u.get("instances", "[]"))
        return u

    def list_users(self, page: int = 1, page_size: int = 20, search: str = "") -> dict:
        offset = (page - 1) * page_size
        if search:
            like_param = f"%{search}%"
            total_row = db.fetchone(
                "SELECT COUNT(*) as cnt FROM users WHERE userName LIKE ?",
                (like_param,),
            )
            total = total_row["cnt"] if total_row else 0
            rows = db.fetchall(
                "SELECT * FROM users WHERE userName LIKE ? ORDER BY registerTime DESC LIMIT ? OFFSET ?",
                (like_param, page_size, offset),
            )
        else:
            total_row = db.fetchone("SELECT COUNT(*) as cnt FROM users")
            total = total_row["cnt"] if total_row else 0
            rows = db.fetchall(
                "SELECT * FROM users ORDER BY registerTime DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            )
        safe_data = []
        for r in rows:
            u = self._row_to_user(r)
            safe_data.append({
                "uuid": u["uuid"],
                "userName": u["userName"],
                "permission": u["permission"],
                "registerTime": u.get("registerTime", ""),
                "loginTime": u.get("loginTime", ""),
                "hasApiKey": bool(u.get("apiKey", "")),
                "instances": u.get("instances", []),
                "instanceCount": len(u.get("instances", [])),
            })
        return {"total": total, "page": page, "pageSize": page_size, "data": safe_data}

    # ============ 认证 ============

    def check_login(self, username: str, password: str) -> Optional[dict]:
        user = self.get_user_by_username(username)
        if not user:
            return None
        if user["permission"] == ROLE.BAN:
            return None
        if self._verify_password(password, user["passWord"]):
            # 静默升级旧哈希格式到 bcrypt
            if self._needs_rehash(user["passWord"]):
                new_hash = self._hash_password(password)
                db.execute("UPDATE users SET passWord=? WHERE uuid=?", (new_hash, user["uuid"]))
                db.commit()
                logger.info("用户 %s 密码哈希已升级至 bcrypt", username)
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            db.execute("UPDATE users SET loginTime=? WHERE uuid=?", (now, user["uuid"]))
            db.commit()
            user["loginTime"] = now
            return user
        return None

    def check_ban_ip(self, ip: str) -> bool:
        """检查 IP 是否被封禁。True=允许登录，False=已封禁"""
        row = db.fetchone("SELECT count, last_fail FROM login_failures WHERE ip=?", (ip,))
        if not row:
            return True
        r = dict(row)
        # TTL 自动解封
        if time.time() - r["last_fail"] > self._BAN_TTL:
            db.execute("DELETE FROM login_failures WHERE ip=?", (ip,))
            db.commit()
            return True
        return r["count"] <= self._BAN_THRESHOLD

    def record_login_failure(self, ip: str):
        now = time.time()
        row = db.fetchone("SELECT count FROM login_failures WHERE ip=?", (ip,))
        if row:
            db.execute(
                "UPDATE login_failures SET count=count+1, last_fail=? WHERE ip=?",
                (now, ip),
            )
        else:
            db.execute(
                "INSERT INTO login_failures (ip, count, first_fail, last_fail) VALUES (?,1,?,?)",
                (ip, now, now),
            )
        db.commit()

    def clear_login_failure(self, ip: str):
        db.execute("DELETE FROM login_failures WHERE ip=?", (ip,))
        db.commit()

    # ============ 权限 & 实例绑定 ============

    def is_admin(self, user: dict) -> bool:
        return user.get("permission", 0) >= ROLE.ADMIN

    def has_instance(self, user: dict, node_id: str, container_name: str) -> bool:
        if self.is_admin(user):
            return True
        for inst in user.get("instances", []):
            if inst.get("node_id") == node_id and inst.get("container_name") == container_name:
                return True
        return False

    def assign_instances(self, user_uuid: str, instances: list) -> bool:
        user = self.get_user_by_uuid(user_uuid)
        if not user:
            return False
        db.execute("UPDATE users SET instances=? WHERE uuid=?",
                   (json.dumps(instances), user_uuid))
        db.commit()
        return True

    def remove_instances(self, user_uuid: str, instances: list) -> bool:
        user = self.get_user_by_uuid(user_uuid)
        if not user:
            return False
        remove_set = {(i["node_id"], i["container_name"]) for i in instances}
        current = user.get("instances", [])
        updated = [i for i in current if (i["node_id"], i["container_name"]) not in remove_set]
        db.execute("UPDATE users SET instances=? WHERE uuid=?",
                   (json.dumps(updated), user_uuid))
        db.commit()
        return True

    def count(self) -> dict:
        rows = db.fetchall("SELECT permission FROM users")
        total = len(rows)
        admins = sum(1 for r in rows if r["permission"] >= ROLE.ADMIN)
        users = sum(1 for r in rows if r["permission"] == ROLE.USER)
        banned = sum(1 for r in rows if r["permission"] == ROLE.BAN)
        return {"total": total, "admins": admins, "users": users, "banned": banned}


# 全局单例
user_manager = UserManager()

