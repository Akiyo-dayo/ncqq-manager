"""
告警管理服务 - 规则配置 + Webhook 推送
支持容器状态变化、CPU/内存超限、实例离线等告警场景
"""
import json
import time
import socket
import asyncio
import threading
import ipaddress
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

import aiohttp

from services.log import logger
import services.database as db


def _validate_webhook_url(url: str, allow_local: bool = False) -> str:
    """校验 Webhook URL，防止 SSRF 攻击。返回清洗后的 URL 或抛出 ValueError。
    当 allow_local=True 时，允许指向本地/内网地址（适用于通知插件与管理器同机部署场景）。
    """
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Webhook URL 仅支持 http/https 协议，收到: {parsed.scheme}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Webhook URL 缺少主机名")
    if not allow_local:
        # 解析主机名为 IP 并检查是否为内网地址
        try:
            addrs = socket.getaddrinfo(hostname, None)
            for _, _, _, _, sockaddr in addrs:
                ip = ipaddress.ip_address(sockaddr[0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise ValueError(f"Webhook URL 不允许指向内网地址: {hostname} -> {ip}")
        except socket.gaierror:
            raise ValueError(f"Webhook URL 主机名无法解析: {hostname}")
    return url


class AlertManager:
    """告警规则管理与触发器"""

    def __init__(self):
        self._init_table()

    def _init_table(self):
        """确保告警表存在"""
        db.execute("""
            CREATE TABLE IF NOT EXISTS alert_rules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                config TEXT DEFAULT '{}',
                webhook_url TEXT DEFAULT '',
                created_at REAL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT,
                message TEXT,
                level TEXT DEFAULT 'info',
                created_at REAL DEFAULT 0
            )
        """)
        db.commit()

    def list_rules(self) -> List[Dict]:
        rows = db.fetchall("SELECT * FROM alert_rules ORDER BY created_at DESC")
        return [self._parse_rule(r) for r in rows]

    def get_rule(self, rule_id: str) -> Optional[Dict]:
        row = db.fetchone("SELECT * FROM alert_rules WHERE id=?", (rule_id,))
        return self._parse_rule(row) if row else None

    def get_history(self, limit: int = 50) -> List[Dict]:
        """返回最近 limit 条告警历史记录。"""
        rows = db.fetchall(
            "SELECT * FROM alert_history ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def _is_local_allowed(self) -> bool:
        """读取 settings 中的 allow_local_webhook 开关"""
        return bool(db.get_setting("allow_local_webhook", False))

    def create_rule(self, rule_id: str, name: str, rule_type: str,
                    config: Dict, webhook_url: str = "") -> bool:
        try:
            webhook_url = _validate_webhook_url(webhook_url, allow_local=self._is_local_allowed())
            db.execute(
                "INSERT INTO alert_rules (id,name,type,config,webhook_url,created_at) VALUES (?,?,?,?,?,?)",
                (rule_id, name, rule_type, json.dumps(config), webhook_url, time.time()),
            )
            db.commit()
            return True
        except ValueError as e:
            logger.warning("告警规则创建被拒绝: %s", e)
            raise
        except Exception as e:
            logger.error("创建告警规则失败: %s", e)
            return False

    def update_rule(self, rule_id: str, name: str = None,
                    enabled: bool = None, config: Dict = None,
                    webhook_url: str = None) -> bool:
        updates, params = [], []
        if name is not None:
            updates.append("name=?"); params.append(name)
        if enabled is not None:
            updates.append("enabled=?"); params.append(1 if enabled else 0)
        if config is not None:
            updates.append("config=?"); params.append(json.dumps(config))
        if webhook_url is not None:
            webhook_url = _validate_webhook_url(webhook_url, allow_local=self._is_local_allowed())
            updates.append("webhook_url=?"); params.append(webhook_url)
        if not updates:
            return False
        params.append(rule_id)
        db.execute(f"UPDATE alert_rules SET {','.join(updates)} WHERE id=?", params)
        db.commit()
        return True

    def delete_rule(self, rule_id: str) -> bool:
        db.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))
        db.execute("DELETE FROM alert_history WHERE rule_id=?", (rule_id,))
        db.commit()
        return True

    def trigger_alert(self, rule_id: str, message: str, level: str = "warning"):
        """触发告警：写入历史 + 发送 Webhook"""
        db.execute(
            "INSERT INTO alert_history (rule_id,message,level,created_at) VALUES (?,?,?,?)",
            (rule_id, message, level, time.time()),
        )
        db.commit()
        rule = self.get_rule(rule_id)
        if rule and rule.get("webhook_url"):
            self._fire_webhook_task(rule["webhook_url"], message, level)

    async def trigger_alert_async(
        self, rule_id: str, message: str, level: str = "warning",
        extra: Optional[Dict] = None,
    ):
        """异步触发告警：写入历史 + 异步 Webhook 推送。extra 中的字段会合并到 payload。"""
        db.execute(
            "INSERT INTO alert_history (rule_id,message,level,created_at) VALUES (?,?,?,?)",
            (rule_id, message, level, time.time()),
        )
        db.commit()
        rule = self.get_rule(rule_id)
        if rule and rule.get("webhook_url"):
            await self._send_webhook_async(rule["webhook_url"], message, level, extra)

    async def notify_instance_offline(self, name: str, node_id: str = "local", uin: str = ""):
        """实例离线通知 — 查找所有 instance_offline 类型且 enabled 的规则并触发。
        同时通过 qq_bot 哨兵渠道发 QQ 消息（群/私聊均支持）。
        """
        rules = self.list_rules()
        offline_time = time.strftime("%Y-%m-%d %H:%M:%S")
        uin_display = uin or "未知"
        message = (
            f"⚠️ 实例停止: {name}\n"
            f"📋 QQ 账号: {uin_display}\n"
            f"🖥️ 节点: {node_id}\n"
            f"⏰ 停止时间: {offline_time}"
        )
        extra = {
            "event": "instance_offline",
            "instance": name,
            "node_id": node_id,
            "uin": uin_display,
            "offline_time": offline_time,
        }
        for rule in rules:
            if rule.get("type") == "instance_offline" and rule.get("enabled"):
                # 检查 config 中是否配置了过滤条件
                cfg = rule.get("config", {})
                target = cfg.get("instance_name", "")
                target_node = cfg.get("node_id", "")
                if target and target != name:
                    continue
                if target_node and target_node != node_id:
                    continue
                await self.trigger_alert_async(rule["id"], message, "critical", extra)
        # qq_bot 哨兵渠道：容器停止也推 QQ 消息
        await self._dispatch_qq_bot_rules(message, extra)

    async def notify_node_status(self, node_id: str, node_name: str, online: bool,
                                 reason: str = "") -> None:
        """节点级上线/离线通知 —— 整台节点失联时过去没有任何告警，
        只能靠管理员自己发现面板上一片容器都不动了。
        """
        rules = self.list_rules()
        moment = time.strftime("%Y-%m-%d %H:%M:%S")
        if online:
            message = f"✅ 节点恢复: {node_name}（{node_id}）\n⏰ 时间: {moment}"
            level = "info"
        else:
            message = (
                f"🔌 节点失联: {node_name}（{node_id}）\n"
                f"📋 原因: {reason or '未知'}\n"
                f"⏰ 时间: {moment}\n"
                f"该节点上的实例状态已标记为不可信。"
            )
            level = "critical"
        extra = {
            "event": "node_online" if online else "node_offline",
            "node_id": node_id,
            "node_name": node_name,
            "reason": reason,
            "time": moment,
        }
        wanted = "node_online" if online else "node_offline"
        for rule in rules:
            if rule.get("type") != wanted or not rule.get("enabled"):
                continue
            target_node = (rule.get("config", {}) or {}).get("node_id", "")
            if target_node and target_node != node_id:
                continue
            await self.trigger_alert_async(rule["id"], message, level, extra)
        await self._dispatch_qq_bot_rules(message, extra)

    async def notify_instance_online(self, name: str, node_id: str = "local", uin: str = ""):
        """实例上线通知 — 容器从非 running 变为 running 时触发。
        触发所有 instance_online 类型且 enabled 的 webhook 规则，
        同时通过 qq_bot 哨兵渠道发 QQ 消息。
        """
        rules = self.list_rules()
        online_time = time.strftime("%Y-%m-%d %H:%M:%S")
        uin_display = uin or "未知"
        message = (
            f"✅ 实例上线: {name}\n"
            f"📋 QQ 账号: {uin_display}\n"
            f"🖥️ 节点: {node_id}\n"
            f"⏰ 上线时间: {online_time}"
        )
        extra = {
            "event": "instance_online",
            "instance": name,
            "node_id": node_id,
            "uin": uin_display,
            "online_time": online_time,
        }
        for rule in rules:
            if rule.get("type") == "instance_online" and rule.get("enabled"):
                cfg = rule.get("config", {})
                target = cfg.get("instance_name", "")
                target_node = cfg.get("node_id", "")
                if target and target != name:
                    continue
                if target_node and target_node != node_id:
                    continue
                await self.trigger_alert_async(rule["id"], message, "info", extra)
        # qq_bot 哨兵渠道：实例上线也推 QQ 消息
        await self._dispatch_qq_bot_rules(message, extra)

    async def notify_login_lost(
        self, name: str, uin: str = "", node_id: str = "local",
    ):
        """掉线扫码通知 — 登录态丢失时触发，附带 QR 扫码链接。

        查找所有 login_lost 类型且 enabled 的规则，构建含 QR 链接的通知推送。
        QR 链接指向面板的公开接口 /api/containers/{name}/qrcode（无需认证）。
        """
        rules = self.list_rules()
        base_url = (db.get_setting("webhook_base_url", "") or "").rstrip("/")
        lost_time = time.strftime("%Y-%m-%d %H:%M:%S")
        uin_display = uin or "未知"

        # 构建 QR 链接（仅在配置了 base_url 时生成）
        qr_url = ""
        dashboard_url = ""
        if base_url:
            qr_url = f"{base_url}/api/containers/{name}/qrcode?node_id={node_id}"
            dashboard_url = base_url

        # 人类可读消息
        lines = [
            f"🔑 实例掉线需重新登录: {name}",
            f"📋 QQ 账号: {uin_display}",
            f"🖥️ 节点: {node_id}",
            f"⏰ 掉线时间: {lost_time}",
        ]
        if qr_url:
            lines.append(f"📱 扫码链接: {qr_url}")
        message = "\n".join(lines)

        extra = {
            "event": "login_lost",
            "instance": name,
            "node_id": node_id,
            "uin": uin_display,
            "lost_time": lost_time,
        }
        if qr_url:
            extra["qr_url"] = qr_url
            extra["dashboard_url"] = dashboard_url

        for rule in rules:
            if rule.get("type") == "login_lost" and rule.get("enabled"):
                cfg = rule.get("config", {})
                target = cfg.get("instance_name", "")
                target_node = cfg.get("node_id", "")
                if target and target != name:
                    continue
                if target_node and target_node != node_id:
                    continue
                await self.trigger_alert_async(rule["id"], message, "critical", extra)

        # qq_bot 通知渠道：通过哨兵 Bot 发 QQ 消息
        await self._dispatch_qq_bot_rules(message, extra)

    async def notify_via_bot(
        self,
        sender_name: str,
        msg_type: str,
        target_id: str,
        message: str,
    ) -> bool:
        """
        通过指定 Bot（sender_name 容器）发送 QQ 消息通知。
        适用于"用哨兵 Bot 通知某个账号/群掉线"场景。

        sender_name: 用来发消息的 Bot 容器名（必须当前在线）
        msg_type: "private"（私聊）| "group"（群聊）
        target_id: 接收通知的 QQ 号或群号
        返回是否发送成功。
        """
        try:
            from services.napcat_ws_service import napcat_ws_service
            msg_id = await napcat_ws_service.send_message(
                sender_name, msg_type, target_id, message
            )
            if msg_id:
                logger.info("QQ Bot 通知发送成功: sender=%s type=%s target=%s msg_id=%s",
                            sender_name, msg_type, target_id, msg_id)
                return True
            logger.warning("QQ Bot 通知发送失败（无 message_id）: sender=%s target=%s",
                           sender_name, target_id)
            return False
        except Exception as exc:
            logger.warning("QQ Bot 通知异常: sender=%s target=%s: %s",
                           sender_name, target_id, exc)
            return False

    async def _dispatch_qq_bot_rules(self, message: str, extra: dict) -> None:
        """查找 qq_bot 类型告警规则并通过对应 Bot 发送 QQ 消息（多哨兵轮询 + 多目标并发）。

        新配置结构（向后兼容旧单 sender/target）：
        {
          "sender_bots": ["mili2", "mili3"],   # 哨兵数组，依次尝试直到一个成功
          "sender_bot": "mili2",               # 旧字段（单个），兼容保留
          "targets": [
            {"msg_type": "private", "target_id": "12345678"},
            {"msg_type": "group",   "target_id": "87654321"}
          ],
          "msg_type": "private",               # 旧字段，兼容保留
          "target_id": "12345678"              # 旧字段，兼容保留
        }
        """
        from services.napcat_ws_service import napcat_ws_service
        rules = self.list_rules()
        for rule in rules:
            if not (rule.get("type") == "qq_bot" and rule.get("enabled")):
                continue
            cfg = rule.get("config", {})

            # ---- 解析哨兵列表（新字段 sender_bots 优先；旧字段 sender_bot 兼容）----
            raw_senders = cfg.get("sender_bots")
            if not raw_senders:
                raw_senders = [cfg.get("sender_bot", "")]
            senders = [s for s in raw_senders if s]
            if not senders:
                logger.debug("qq_bot 规则缺少 sender_bots/sender_bot，跳过: rule_id=%s", rule.get("id"))
                continue

            # ---- 解析目标列表（新字段 targets 优先；旧字段 msg_type+target_id 兼容）----
            raw_targets = cfg.get("targets")
            if not raw_targets:
                msg_type_old = cfg.get("msg_type", "private")
                target_id_old = cfg.get("target_id", "")
                raw_targets = ([{"msg_type": msg_type_old, "target_id": target_id_old}]
                               if target_id_old else [])
            targets = [t for t in raw_targets if t.get("target_id")]
            if not targets:
                logger.debug("qq_bot 规则缺少有效 targets，跳过: rule_id=%s", rule.get("id"))
                continue

            # ---- 从哨兵数组中选取第一个在线的 Bot（sender_bots 存容器名）----
            sender = None
            for s in senders:
                if napcat_ws_service.is_connected(s):
                    sender = s
                    break
            if not sender:
                logger.debug("qq_bot 通知跳过: 所有候选哨兵 Bot %s 均未连接", senders)
                continue

            # ---- 并发发送所有目标 ----
            import asyncio as _asyncio
            tasks = [
                self.notify_via_bot(sender, t["msg_type"], t["target_id"], message)
                for t in targets
            ]
            results = await _asyncio.gather(*tasks, return_exceptions=True)
            sent = sum(1 for r in results if r is True)
            logger.info("qq_bot 通知完成: rule_id=%s sender=%s targets=%d sent=%d",
                        rule.get("id"), sender, len(targets), sent)

    def _fire_webhook_task(self, url: str, message: str, level: str):
        """在当前事件循环中创建异步 webhook 发送任务；无事件循环时回退到后台线程执行。"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_webhook_async(url, message, level))
            return
        except RuntimeError:
            pass

        def _runner():
            try:
                asyncio.run(self._send_webhook_async(url, message, level))
            except Exception as e:
                logger.debug("Webhook 后台发送失败: %s", e)

        threading.Thread(target=_runner, name="alert-webhook", daemon=True).start()

    async def _send_webhook_async(
        self, url: str, message: str, level: str,
        extra: Optional[Dict] = None,
    ):
        """异步发送 Webhook 通知 — aiohttp，零线程。extra 字段合并到 payload。"""
        payload: Dict[str, Any] = {
            "text": message,
            "level": level,
            "timestamp": int(time.time()),
            "source": "NapCat Manager",
        }
        if extra:
            payload.update(extra)
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await session.post(url, json=payload)
        except Exception as e:
            logger.debug("Webhook 异步发送失败: %s", e)

    def _parse_rule(self, row) -> Dict:
        d = dict(row)
        d["config"] = json.loads(d.get("config", "{}"))
        d["enabled"] = bool(d.get("enabled", 0))
        return d


alert_manager = AlertManager()

