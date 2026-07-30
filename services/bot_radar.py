"""
Bot 雷达 — Bot 框架 WS 端点的探测、端点库与一键注入。

「雷达」解决的问题：你的 NapCat 实例要把消息推给某个 Bot 框架（AstrBot / NoneBot /
Koishi 等）的 OneBot v11 反向 WS 端点。这些端点散落在各处、是否活着不好确认、
每次给新实例配置又要手抄一遍 URL 和 token。

雷达把它们登记成一个带别名的端点库，随时可以探测在线状态与延迟，
并把任意端点一键注入到任意 NapCat 实例的 onebot11 配置里。
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp

from services.log import logger


_RADAR_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "bot_radar_endpoints.json",
)


class BotRadar:
    """Bot 框架端点雷达（单例）。"""

    # ---- 端点探测 ----

    async def probe_target_endpoint(self, url: str, token: str = "") -> Dict[str, Any]:
        """探测 Bot 框架 WS 端点（AstrBot/NoneBot 等）是否可连。

        携带 OneBot v11 标准头部发起握手（X-Self-ID / X-Client-Role / User-Agent），
        与 NapCat 实际连接时行为一致，避免框架因缺少必要头部而返回 403/400 导致误判。

        返回字段：
          online     — True=在线（握手成功）/ True+note=在线（握手被拒/需认证）/ False=不可达
          latency_ms — 握手耗时（ms），离线时为 None
          note       — 可选补充信息（"handshake_rejected"）
          detail     — 面向用户的可读说明
        """
        t0 = time.time()
        headers = {
            "User-Agent": "NapCatManager/1.0 OneBot/11",
            "X-Self-ID": "0",
            "X-Client-Role": "Universal",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            timeout = aiohttp.ClientTimeout(total=5.0, connect=3.0)
            async with aiohttp.ClientSession() as session:
                ws = await session.ws_connect(url, timeout=timeout, heartbeat=None, headers=headers)
                await ws.close()
            return {
                "online": True,
                "latency_ms": round((time.time() - t0) * 1000),
                "detail": "握手成功，端点可以正常接收 NapCat 连接",
            }
        except aiohttp.WSServerHandshakeError as e:
            latency_ms = round((time.time() - t0) * 1000)
            if e.status and 400 <= e.status < 500:
                # 端口可达、服务在线，但握手被拒 —— 绝大多数是 token 不对。
                return {
                    "online": True,
                    "latency_ms": latency_ms,
                    "note": "handshake_rejected",
                    "status_code": e.status,
                    "detail": f"端点在线但拒绝了握手（HTTP {e.status}），通常是 token 不正确",
                }
            return {"online": False, "latency_ms": None,
                    "detail": f"握手失败：HTTP {e.status}"}
        except aiohttp.ClientConnectorError as e:
            return {"online": False, "latency_ms": None,
                    "detail": f"连不上该地址（对方未监听或被防火墙拦截）：{e}"}
        except Exception as e:
            return {"online": False, "latency_ms": None, "detail": f"探测失败：{e}"}

    # ---- 端点库（持久化到 config/bot_radar_endpoints.json） ----

    def get_endpoints(self) -> List[Dict[str, Any]]:
        """读取端点库。"""
        try:
            if os.path.isfile(_RADAR_FILE):
                with open(_RADAR_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except (OSError, ValueError) as e:
            logger.warning("Bot 雷达端点库读取失败: %s", e)
        return []

    def save_endpoints(self, endpoints: List[Dict[str, Any]]) -> None:
        """覆盖写入端点库。字段：alias/url/token。"""
        os.makedirs(os.path.dirname(_RADAR_FILE), exist_ok=True)
        with open(_RADAR_FILE, "w", encoding="utf-8") as f:
            json.dump(endpoints, f, indent=2, ensure_ascii=False)

    def find_endpoint(self, alias: str) -> Optional[Dict[str, Any]]:
        return next((e for e in self.get_endpoints() if e.get("alias") == alias), None)

    def is_known_endpoint(self, endpoint_url: str) -> bool:
        """URL 是否属于管理器自身端点或端点库中的已知端点。"""
        if not endpoint_url:
            return False
        if "/ws/napcat/" in endpoint_url:
            return True
        return any(e.get("url") == endpoint_url for e in self.get_endpoints())

    # ---- 注入到 NapCat 实例 ----

    def inject_to_container(
        self,
        alias: str,
        container_name: str,
        uin: str = "default",
    ) -> Dict[str, Any]:
        """把端点库里的某个端点追加到容器的 onebot11_{uin}.json 的 websocketClients。"""
        ep = self.find_endpoint(alias)
        if ep is None:
            return {"success": False, "error": f"别名 '{alias}' 不存在"}
        if not container_name:
            return {"success": False, "error": "缺少目标容器名"}

        from services.config import get_data_dir

        url = ep.get("url", "")
        cfg_dir = os.path.join(get_data_dir(), container_name, "config")
        cfg_path = os.path.join(cfg_dir, f"onebot11_{uin}.json")

        full_cfg: Dict[str, Any] = {}
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    full_cfg = json.load(f)
            except (OSError, ValueError) as e:
                logger.warning("读取 %s 失败，将重新生成: %s", cfg_path, e)

        network = full_cfg.get("network") or {}
        clients = network.get("websocketClients")
        clients = clients if isinstance(clients, list) else []
        if any(c.get("url") == url for c in clients if isinstance(c, dict)):
            return {"success": False, "error": "该端点已经在这个实例的 WS 客户端列表里了"}

        clients.append({
            "name": alias or "bot-radar",
            "enable": True,
            "url": url,
            "reportSelfMessage": False,
            "messagePostFormat": "array",
            "token": ep.get("token", "") or "",
            "debug": False,
            "heartInterval": 30000,
            "reconnectInterval": 30000,
        })
        network["websocketClients"] = clients
        full_cfg["network"] = network

        os.makedirs(cfg_dir, exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(full_cfg, f, indent=2, ensure_ascii=False)
        logger.info("Bot 雷达已注入端点 %s → 容器 %s (uin=%s)", alias, container_name, uin)
        return {
            "success": True,
            "message": f"已把「{alias}」注入到实例「{container_name}」，重启该实例后生效",
            "needs_restart": True,
        }


bot_radar = BotRadar()
