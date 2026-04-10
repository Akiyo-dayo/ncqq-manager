"""
生命周期 Mixin

LifecycleMixin 提供给 DockerManager 使用，封装：
  - BS 注入完成标记管理（持久化文件）
  - 登录检测完成回调 _on_login_detected（BS 注入 + 自动加群通知）
需要 self.get_used_ports / self.find_available_port 方法由 DockerManager 提供。
"""

import asyncio
import json
import os
from typing import Any, Dict

from services.log import logger


class LifecycleMixin:
    """BS 注入标记 + 登录事件回调，混入 DockerManager 使用。"""

    @staticmethod
    def _bs_inject_marker_path(data_dir_base: str, name: str, uin: str) -> str:
        """返回 BS 注入完成的持久化标记文件路径。"""
        return os.path.join(data_dir_base, name, ".bs_injected", f"{uin}.done")

    @staticmethod
    def _bs_inject_done(data_dir_base: str, name: str, uin: str) -> bool:
        """检查该实例+uin 是否已完成过 BS 注入（标记文件存在）。"""
        return os.path.isfile(
            LifecycleMixin._bs_inject_marker_path(data_dir_base, name, uin)
        )

    @staticmethod
    def _mark_bs_inject(data_dir_base: str, name: str, uin: str) -> None:
        """写入 BS 注入完成标记文件（幂等）。"""
        marker = LifecycleMixin._bs_inject_marker_path(data_dir_base, name, uin)
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as f:
            f.write(uin)

    @staticmethod
    def _read_injected_bs_url(config_dir: str, uin: str) -> str:
        """从 onebot11_{uin}.json 读取已注入的 BS WS 客户端 URL。

        返回 name=botshepherd 条目的 url，读取失败或不存在时返回空字符串。
        """
        config_file = os.path.join(config_dir, f"onebot11_{uin}.json")
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("network", {}).get("websocketClients", []):
                if isinstance(item, dict) and item.get("name") == "botshepherd":
                    return str(item.get("url", ""))
        except Exception:
            pass
        return ""

    def _on_login_detected(self, name: str, current: Dict, prev: Dict) -> None:
        """登录完成后统一触发：WS 客户端注入 + BS 中间件接管。

        防重复机制（双层）：
          1. 内存层：prev.logged_in + uin 相同则跳过（进程生命周期内有效）
          2. 持久层：data/{name}/.bs_injected/{uin}.done 存在则跳过（重启也有效）
        只有扫码登录（首次出现新 uin）时才真正执行注入。
        """
        uin = str(current.get("uin", ""))
        nickname = current.get("nickname", "")
        if not uin:
            return

        # 层1 - 内存层：本次进程内已检测到相同登录，跳过
        if prev.get("logged_in") and str(prev.get("uin", "")) == uin:
            return

        from services.config import app_config, get_data_dir

        data_dir_base = get_data_dir()
        config_dir = os.path.join(data_dir_base, name, "config")
        ws_enabled = app_config.get("init_ws_client_enabled", False)
        bs_enabled = app_config.get("init_bs_enabled", False)

        if not ws_enabled and not bs_enabled:
            return

        # 层2 - 持久层：该 uin 已注入过（重启后仍有效），跳过步骤①避免重复分配端口
        # ★ 但先验证配置文件是否实际存在：容器重建后 config 目录可能被清空，
        #   此时标记已过期，需清除标记并重新注入
        # ★ P1 修复：即使标记已存在，步骤②（BS connection 同步）仍需执行，
        #   防止 BS 重启后 find_available_port 分配了新端口导致端口漂移失联。
        config_file_exists = os.path.isfile(
            os.path.join(config_dir, f"onebot11_{uin}.json")
        )
        already_injected = self._bs_inject_done(data_dir_base, name, uin)
        if already_injected:
            if config_file_exists:
                # NapCat 侧已注入：跳过步骤①和容器重启，但步骤②（BS 连接同步）仍需执行
                if bs_enabled:
                    injected_url = self._read_injected_bs_url(config_dir, uin)
                    if injected_url:
                        try:
                            from urllib.parse import urlparse

                            parsed = urlparse(injected_url)
                            bs_port = parsed.port
                            if bs_port:
                                bs_bind_url_only = (
                                    f"ws://0.0.0.0:{bs_port}/onebot/v11/ws"
                                )
                                self._sync_bs_connection(
                                    name,
                                    uin,
                                    nickname,
                                    bs_bind_url_only,
                                    injected_url,
                                )
                        except Exception as e:
                            logger.warning("读取已注入 BS URL 失败，跳过同步: %s", e)
                    else:
                        logger.debug(
                            "已注入配置中未找到 botshepherd URL，跳过 BS 同步: %s uin=%s",
                            name,
                            uin,
                        )
                logger.debug(
                    "BS NapCat 注入已跳过（持久标记存在）: %s uin=%s", name, uin
                )
                return
            # 配置文件丢失（容器重建），清除过期标记并重新注入
            logger.info(
                "BS 持久标记存在但 onebot11_%s.json 丢失，清除标记重新注入: %s",
                uin,
                name,
            )
            marker = self._bs_inject_marker_path(data_dir_base, name, uin)
            try:
                os.remove(marker)
            except OSError:
                pass

        # ---- ① WS 客户端注入到 onebot11_{uin}.json ----
        ws_url = ""
        ws_token = str(app_config.get("init_ws_client_token", ""))
        bs_bind_url = ""

        if bs_enabled:
            bs_host = str(app_config.get("init_bs_napcat_host", "172.17.0.1"))
            bs_base_port = int(app_config.get("init_bs_client_base_port", 6100))
            used = self.get_used_ports()  # type: ignore[attr-defined]
            bs_port = self.find_available_port(bs_base_port, used)  # type: ignore[attr-defined]
            bs_bind_url = f"ws://0.0.0.0:{bs_port}/onebot/v11/ws"
            ws_url = f"ws://{bs_host}:{bs_port}/onebot/v11/ws"
        elif ws_enabled:
            ws_url = str(app_config.get("init_ws_client_url", ""))

        if ws_url:
            try:
                from routers.container_crud_router import (
                    _generate_onebot11_config_with_ws_client,
                )

                _generate_onebot11_config_with_ws_client(
                    config_dir, ws_url, ws_token, uin
                )
                self._mark_bs_inject(data_dir_base, name, uin)
                logger.info("BS/WS 注入完成并写入持久标记: %s uin=%s", name, uin)
                # 重启前先写入 webui.json autoLoginAccount，NapCat 重启后可快速登录，无需再扫码。
                try:
                    self._sync_webui_auto_login(name, uin)  # type: ignore[attr-defined]
                    logger.info(
                        "已同步 webui.json autoLoginAccount: %s uin=%s", name, uin
                    )
                except Exception as we:
                    logger.debug(
                        "同步 webui autoLoginAccount 失败（不影响注入）: %s", we
                    )
                # NapCat 不会热重载配置文件，注入后必须重启容器才能生效。
                # 通过 fire-and-forget 异步调度重启，避免阻塞当前线程。
                self._schedule_container_restart(name)
            except Exception as e:
                logger.error("登录后 WS 注入失败 (%s/%s): %s", name, uin, e)

        # ---- ② BS 中间件接管（异步 fire-and-forget）----
        if bs_enabled and ws_url and bs_bind_url:
            self._sync_bs_connection(name, uin, nickname, bs_bind_url, ws_url)

        # ---- ③ 自动加群通知（开关：init_auto_join_groups_enabled）----
        if not app_config.get("init_auto_join_groups_enabled", False):
            return  # 开关关闭，跳过自动加群通知

        raw_groups = app_config.get("init_auto_join_groups", "[]")
        try:
            auto_groups = (
                json.loads(raw_groups) if isinstance(raw_groups, str) else raw_groups
            )
            if not isinstance(auto_groups, list):
                auto_groups = []
        except Exception:
            auto_groups = []

        if auto_groups and uin:
            from services.napcat_ws_service import napcat_ws_service as _ws_svc
            from services.docker_manager import _main_event_loop

            async def _auto_notify_groups(_name: str, _uin: str, _groups: list) -> None:
                """延迟 5s 等待 WS 代理就绪后，向各群发送上线通知。"""
                await asyncio.sleep(5)
                notice = f"✅ Bot [{_name}] QQ:{_uin} 已登录上线，请管理员确认。"
                for gid in _groups:
                    try:
                        await _ws_svc.send_message(_name, "group", str(gid), notice)
                        logger.info("自动加群通知已发送: name=%s group=%s", _name, gid)
                    except Exception as exc:
                        logger.debug(
                            "自动加群通知发送失败: name=%s group=%s: %s",
                            _name,
                            gid,
                            exc,
                        )

            loop = _main_event_loop
            if loop is not None and loop.is_running():  #
                asyncio.run_coroutine_threadsafe(
                    _auto_notify_groups(name, uin, auto_groups), loop
                )
                logger.info("已调度自动加群通知: name=%s groups=%s", name, auto_groups)
            else:
                logger.debug("事件循环未运行，跳过自动加群通知")

    def _sync_bs_connection(
        self,
        name: str,
        uin: str,
        nickname: str,
        bs_bind_url: str,
        ws_url: str,
    ) -> None:
        """（步骤②）将 BS connection 配置同步到 BS 进程（幂等 upsert）。

        无论是首次注入还是 BS 重启后端口漂移补救，都调用此方法。
        fire-and-forget：通过事件循环异步发送，不阻塞调用线程。
        """
        try:
            from services.config import app_config
            from services.docker_manager import _main_event_loop

            raw = app_config.get("init_bs_targets", "[]")
            targets = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(targets, list):
                targets = []
            manager_host = str(app_config.get("manager_host", "127.0.0.1"))
            manager_port = int(app_config.get("manager_port", 8000))
            named_endpoint = f"ws://{manager_host}:{manager_port}/ws/napcat/{name}"
            compat_endpoint = f"ws://{manager_host}:{manager_port}/ws/onebot/v11/ws"

            logger.info("BS 自动注入/更新管理器端点: %s", named_endpoint)

            loop = _main_event_loop
            if loop is not None and loop.is_running():
                from services.botshepherd import botshepherd_manager

                async def _do_sync() -> Dict[str, Any]:
                    # 先获取现有的连接配置，保留用户手动添加的其他端点和其他配置
                    existing_conn = {}
                    existing_targets = []
                    config_loaded = False
                    try:
                        res = await botshepherd_manager.get_connections()
                        conn = (res.get("connections") or {}).get(name)
                        if conn:
                            existing_conn = conn
                            existing_targets = list(conn.get("target_endpoints") or [])
                            config_loaded = True
                        else:
                            # 连接不存在，这是首次创建，允许继续
                            config_loaded = True
                    except Exception as e:
                        logger.warning(
                            "获取现有 BS 连接配置失败（可能 BS 未启动），跳过同步以防覆盖用户配置: %s",
                            e,
                        )
                        return {"success": False, "error": f"无法获取现有配置: {e}"}

                    if not config_loaded:
                        logger.warning("BS 连接配置加载失败，跳过同步以防覆盖用户配置")
                        return {"success": False, "error": "配置加载失败"}

                    # 合并 target_endpoints（保持原有顺序，去重）
                    merged_targets = [named_endpoint]
                    for t in existing_targets + targets:
                        if t not in merged_targets and t != compat_endpoint:
                            merged_targets.append(t)

                    # 基础配置项，确保核心信息正确
                    conn_config = {
                        "name": nickname or name,
                        "description": existing_conn.get(
                            "description", f"Auto [{uin}]"
                        ),
                        "enabled": existing_conn.get("enabled", True),
                        "client_endpoint": bs_bind_url,
                        "target_endpoints": merged_targets,
                        "keep_target_alive": True,
                    }

                    # 合并原有配置，防止用户自己配的 token 等丢失
                    final_config = {**existing_conn, **conn_config}

                    return await botshepherd_manager.update_connection(
                        name, final_config
                    )

                def _on_done(fut: asyncio.Future, _name: str = name) -> None:
                    try:
                        r = fut.result()
                        if isinstance(r, dict) and not r.get("success", True):
                            logger.warning(
                                "BS 连接同步结果: %s → %s", _name, r.get("error", r)
                            )
                        else:
                            logger.info("BS 连接同步完成: %s → %s", _name, r)
                    except Exception as exc:
                        logger.error("BS 连接同步异常: %s → %s", _name, exc)

                future = asyncio.run_coroutine_threadsafe(_do_sync(), loop)
                future.add_done_callback(_on_done)
                logger.info(
                    "已调度 BS 连接同步: %s bind=%s napcat=%s targets=%s",
                    name,
                    bs_bind_url,
                    ws_url,
                    targets,
                )
            else:
                logger.warning("事件循环未运行，跳过 BS 连接同步")
        except Exception as e:
            logger.error("BS 连接同步失败 (%s): %s", name, e)

    def _schedule_container_restart(self, name: str) -> None:
        """注入 WS 配置后异步重启容器，使 NapCat 加载新配置。

        NapCat 不热重载 onebot11_{uin}.json，必须重启才能建立 WS 客户端连接。
        使用 fire-and-forget：5s 延迟后重启，给 BS 注入时间完成，避免竞态。
        """
        from services.docker_manager import _main_event_loop

        async def _do_restart(_name: str) -> None:
            import asyncio as _asyncio

            await _asyncio.sleep(5)  # 等待 BS 注入异步任务完成
            try:
                from services.docker_async import async_docker_manager

                await async_docker_manager.restart_container(_name)
                logger.info("注入后容器重启完成: %s（NapCat 将加载新 WS 配置）", _name)
            except Exception as exc:
                logger.warning(
                    "注入后容器重启失败 %s: %s（手动重启可恢复）", _name, exc
                )

        loop = _main_event_loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(_do_restart(name), loop)
            logger.info("已调度注入后容器重启: %s（5s 后执行）", name)
        else:
            logger.warning("事件循环未运行，跳过注入后重启: %s", name)
