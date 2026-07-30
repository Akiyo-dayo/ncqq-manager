"""
Bot 心跳 / 在线状态追踪服务

职责：
  - 接收来自 OneBot v11 WS 端点的 meta_event 事件
  - 维护内存表 {str(self_id): {online, last_seen, interval}}
  - 心跳事件同步回写 instance_subsystem（通过 uin 关联容器实例）
  - 提供查询接口供 API / 前端轮询

self_id 统一规范为 str，避免 JSON int 与 HTTP header str 造成的 key 分裂。
掉线判定：超过 max(interval * 3, 90) 秒未收到心跳视为掉线。

WS 连接断开 ≠ Bot 掉线：客户端重连通常只要几秒，期间应依赖超时判定而非连接事件。
"""

import asyncio
import time
from typing import Dict, Optional, Any

from services.log import logger


def _sid(self_id: Any) -> str:
    """将任意类型的 self_id 规范化为 str，避免 int/str 双 key 问题。"""
    return str(self_id)


class BotHeartbeatService:
    """Bot 在线状态内存服务（单例）"""

    # 最大容忍倍率：interval * _MISS_FACTOR 秒无心跳 → 掉线
    _MISS_FACTOR = 3
    # 兜底超时（秒），当 interval 未知时使用
    _FALLBACK_TIMEOUT = 90
    # GC：超过 N 天未收到心跳的条目清除（防内存泄漏）
    _GC_TTL = 7 * 86400
    # 告警防抖冷却（秒）：同一 uin 300s 内最多触发一次，避免双路重复告警
    _ALERT_COOLDOWN = 300

    def __init__(self) -> None:
        # key: str(self_id), value: dict
        self._table: Dict[str, dict] = {}
        # 防抖表：{str(self_id): last_alert_ts}
        self._alert_ts: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # 写入（由 WS 端点调用）
    # ------------------------------------------------------------------

    def on_heartbeat(self, self_id: Any, interval_ms: int, status: dict) -> None:
        """记录一次心跳事件，并同步回写关联的 ContainerInstance。

        Args:
            self_id:     机器人 QQ 号（int 或 str 均可）
            interval_ms: 心跳间隔（毫秒），来自 event.interval
            status:      get_status 数据（包含 online 字段）
        """
        key = _sid(self_id)
        now = time.time()
        online = bool(status.get("online", True))

        # 检测状态翻转：在线 → 离线（心跳本身上报 online=False）
        prev_entry = self._table.get(key)
        was_online = prev_entry.get("online", False) if prev_entry else False

        self._table[key] = {
            "self_id": key,
            "online": online,
            "last_seen": now,
            "interval_ms": interval_ms,
        }

        # 回写 instance_subsystem（通过 uin 关联容器实例）
        self._sync_to_instance(key, online)

        # 若状态从在线变为离线，异步触发告警
        if was_online and not online:
            self._fire_login_lost_alert(key)

    def on_connect(self, self_id: Any) -> None:
        """Bot WS 连接建立时调用（lifecycle meta_event.connect）。"""
        key = _sid(self_id)
        now = time.time()
        entry = self._table.get(key, {})
        entry.update({"self_id": key, "online": True, "last_seen": now})
        self._table[key] = entry
        self._sync_to_instance(key, True)

    def on_ws_lost(self, self_id: Any) -> None:
        """WS 链路断开时调用（管理器 WS 连接关闭）。

        注意：WS 断开 ≠ Bot 掉线。客户端重连通常只要几秒，期间 Bot 实际仍在线。
        此方法仅记录断开时间，不修改 online 字段。
        掉线判定完全依赖 _is_alive() 的心跳超时逻辑。
        """
        key = _sid(self_id)
        if key in self._table:
            self._table[key]["ws_lost_ts"] = time.time()

    # 向后兼容别名（lifecycle disconnect 事件仍调用此名）
    def on_disconnect(self, self_id: Any) -> None:
        """Bot 发送 lifecycle.disconnect 时调用（NapCat 主动断开）。

        与 on_ws_lost 不同：lifecycle.disconnect 表示 NapCat 主动退出，
        此时可以直接置离线，因为 NapCat 已确认断开而非链路抖动。
        """
        key = _sid(self_id)
        if key in self._table:
            prev_online = self._table[key].get("online", False)
            self._table[key]["online"] = False
            self._sync_to_instance(key, False)
            if prev_online:
                self._fire_login_lost_alert(key)

    # ------------------------------------------------------------------
    # 内部：instance_subsystem 回写 & 告警
    # ------------------------------------------------------------------

    def _sync_to_instance(self, key: str, online: bool) -> None:
        """通过 uin 或 name 在 instance_subsystem 中找到对应容器实例并更新 bot_online。

        必须更新**全部**匹配实例：换号或跨节点迁移后多个节点可能残留同一个 uin，
        旧实现只更新第一个匹配项就 return，另一个会永久停在「在线」。
        """
        try:
            from services.instance_subsystem import instance_subsystem
            from services.napcat_ws_service import napcat_ws_service

            def _apply(inst) -> None:
                inst.update_bot_heartbeat(online)
                # 心跳离线时同步标记 logged_in=False，立即触发快速轮询
                if not online and inst.logged_in:
                    inst.update_login(logged_in=False, uin=inst.uin, stage="waiting", reason="bot_offline")

            matched = instance_subsystem.find_by_uin(key)
            for inst in matched:
                _apply(inst)

            if not matched:
                # 刚扫码完还没把 uin 写进实例时，通过 WS 注册表反查容器名。
                # 注册表以容器名为键，必须带上实例自己的 node_id，否则远程容器
                # 会查不到（默认 local），或错误地更新到同名的本地容器上。
                for name, entry in napcat_ws_service._table.items():
                    if entry.uin != key:
                        continue
                    for inst in instance_subsystem.get_all():
                        if inst.name == name:
                            _apply(inst)
                            matched.append(inst)

            if matched:
                # 上线/掉线不产生 Docker 事件，不主动唤醒的话最长要等 4 分钟兜底 tick
                # 才会反映到界面上 —— 这正是「状态刷新不及时」的主要来源。
                from services.container_state import state_engine
                state_engine.notify_change()
        except Exception as e:
            logger.warning("bot_heartbeat 回写 instance_subsystem 异常: %s", e)

    def forget(self, self_id: Any) -> None:
        """删除某个 QQ 号的心跳记录 — 换号后必须调用，否则旧号会一直被判在线。"""
        key = _sid(self_id)
        if self._table.pop(key, None) is not None:
            logger.info("已清除 QQ %s 的心跳缓存", key)
        self._alert_ts.pop(key, None)

    def _fire_login_lost_alert(self, key: str) -> None:
        """异步触发 login_lost 告警（在线 → 离线时），带 300s 防抖冷却。"""
        now = time.time()
        if now - self._alert_ts.get(key, 0) < self._ALERT_COOLDOWN:
            logger.debug("bot_heartbeat 告警冷却中，跳过: self_id=%s", key)
            return
        self._alert_ts[key] = now
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._async_alert(key))
        except RuntimeError:
            pass
        except Exception as e:
            logger.debug("bot_heartbeat 触发告警异常: %s", e)

    async def _async_alert(self, key: str) -> None:
        """实际执行告警推送。通过 uin 反查 instance_subsystem 获得容器名。"""
        try:
            from services.instance_subsystem import instance_subsystem
            from services.alert_manager import alert_manager
            # 尝试找到关联容器获取 name/node_id
            inst_name = ""
            inst_node = "local"
            for inst in instance_subsystem.get_all():
                if inst.uin and inst.uin == key:
                    inst_name = inst.name
                    inst_node = inst.node_id
                    break
            # 无论是否找到容器，都触发告警（self_id 作为兜底名称）
            await alert_manager.notify_login_lost(
                name=inst_name or f"bot_{key}",
                uin=key,
                node_id=inst_node,
            )
        except Exception as e:
            logger.debug("bot_heartbeat 异步告警异常: %s", e)

    # ------------------------------------------------------------------
    # GC：清理长期无心跳的条目
    # ------------------------------------------------------------------

    def gc(self) -> int:
        """清理超过 _GC_TTL 秒未收到心跳的条目，返回清理数量。"""
        now = time.time()
        stale = [
            k for k, v in self._table.items()
            if now - v.get("last_seen", 0) > self._GC_TTL
        ]
        for k in stale:
            del self._table[k]
        if stale:
            logger.debug("bot_heartbeat GC 清理 %d 条过期记录", len(stale))
        return len(stale)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _is_alive(self, entry: dict, now: float) -> bool:
        """判断一条记录是否仍在线（含心跳超时判定）。"""
        if not entry.get("online", False):
            return False
        last_seen: float = entry.get("last_seen", 0.0)
        interval_ms: int = entry.get("interval_ms", 0)
        timeout = (
            max(interval_ms / 1000 * self._MISS_FACTOR, self._FALLBACK_TIMEOUT)
            if interval_ms > 0
            else self._FALLBACK_TIMEOUT
        )
        return (now - last_seen) < timeout

    def is_online(self, self_id: Any) -> bool:
        """判断指定 Bot 当前是否在线。"""
        entry = self._table.get(_sid(self_id))
        if not entry:
            return False
        return self._is_alive(entry, time.time())

    def get_all(self) -> list:
        """返回所有已知 Bot 的在线状态列表（含掉线判定）。"""
        now = time.time()
        return [
            {
                "self_id": entry["self_id"],
                "online": self._is_alive(entry, now),
                "last_seen": entry.get("last_seen", 0.0),
                "last_seen_ago": round(now - entry["last_seen"], 1) if entry.get("last_seen") else None,
                "interval_ms": entry.get("interval_ms", 0),
            }
            for entry in self._table.values()
        ]

    def get_one(self, self_id: Any) -> Optional[dict]:
        """返回单个 Bot 的状态，不存在时返回 None。"""
        key = _sid(self_id)
        entry = self._table.get(key)
        if not entry:
            return None
        now = time.time()
        return {
            "self_id": entry["self_id"],
            "online": self._is_alive(entry, now),
            "last_seen": entry.get("last_seen", 0.0),
            "last_seen_ago": round(now - entry["last_seen"], 1) if entry.get("last_seen") else None,
            "interval_ms": entry.get("interval_ms", 0),
        }


# 全局单例
bot_heartbeat = BotHeartbeatService()

