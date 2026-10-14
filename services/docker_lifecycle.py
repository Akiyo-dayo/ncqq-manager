"""
生命周期 Mixin

LifecycleMixin 提供给 DockerManager 使用，封装：
  - WS 客户端注入完成标记管理（持久化文件）
  - 登录检测完成回调 _on_login_detected（WS 注入 + 自动加群通知）
需要 self.get_used_ports / self.find_available_port 方法由 DockerManager 提供。
"""

import asyncio
import json
import os
from typing import Any, Dict

from services.log import logger


class LifecycleMixin:
    """WS 注入标记 + 登录事件回调，混入 DockerManager 使用。"""

    # 标记目录名沿用历史值：改名会让所有存量实例被判成"未注入"，
    # 从而重新注入并重启一遍容器。
    _INJECT_MARKER_DIR = ".bs_injected"

    @staticmethod
    def _inject_marker_path(data_dir_base: str, name: str, uin: str) -> str:
        """返回 WS 注入完成的持久化标记文件路径。"""
        return os.path.join(data_dir_base, name, LifecycleMixin._INJECT_MARKER_DIR, f"{uin}.done")

    @staticmethod
    def _inject_done(data_dir_base: str, name: str, uin: str) -> bool:
        """检查该实例+uin 是否已完成过注入（标记文件存在）。"""
        return os.path.isfile(LifecycleMixin._inject_marker_path(data_dir_base, name, uin))

    @staticmethod
    def _mark_inject(data_dir_base: str, name: str, uin: str) -> None:
        """写入注入完成标记文件（幂等）。"""
        marker = LifecycleMixin._inject_marker_path(data_dir_base, name, uin)
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as f:
            f.write(uin)

    def _on_login_detected(self, name: str, current: Dict, prev: Dict) -> None:
        """登录完成后统一触发：WS 客户端注入 + 自动加群通知。

        防重复机制（双层）：
          1. 内存层：prev.logged_in + uin 相同则跳过（进程生命周期内有效）
          2. 持久层：data/{name}/.bs_injected/{uin}.done 存在则跳过（重启也有效）
        只有扫码登录（首次出现新 uin）时才真正执行注入。
        """
        uin = str(current.get("uin", ""))
        if not uin:
            return

        # 层1 - 内存层：本次进程内已检测到相同登录，跳过
        if prev.get("logged_in") and str(prev.get("uin", "")) == uin:
            return

        from services.config import app_config, get_data_dir

        data_dir_base = get_data_dir()
        config_dir = os.path.join(data_dir_base, name, "config")

        if app_config.get("init_ws_client_enabled", False):
            self._inject_ws_client(name, uin, data_dir_base, config_dir)

        # ---- 自动加群通知（开关：init_auto_join_groups_enabled）----
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

    def _inject_ws_client(self, name: str, uin: str, data_dir_base: str, config_dir: str) -> None:
        """把配置好的 OneBot WS 客户端写进 onebot11_{uin}.json，并重启容器生效。"""
        from services.config import app_config

        ws_url = str(app_config.get("init_ws_client_url", ""))
        if not ws_url:
            return

        # 持久标记存在且配置文件确实还在 → 已注入过，跳过（避免重复重启容器）。
        # 容器重建后 config 会被清空，此时标记已过期，必须清掉重新注入。
        if self._inject_done(data_dir_base, name, uin):
            if os.path.isfile(os.path.join(config_dir, f"onebot11_{uin}.json")):
                logger.debug("WS 注入已跳过（持久标记存在）: %s uin=%s", name, uin)
                return
            logger.info("持久标记存在但 onebot11_%s.json 丢失，清除标记重新注入: %s", uin, name)
            try:
                os.remove(self._inject_marker_path(data_dir_base, name, uin))
            except OSError as e:
                logger.debug("清除过期注入标记失败: %s", e)

        try:
            from routers.container_crud_router import _generate_onebot11_config_with_ws_client

            _generate_onebot11_config_with_ws_client(
                config_dir, ws_url, str(app_config.get("init_ws_client_token", "")), uin,
            )
            self._mark_inject(data_dir_base, name, uin)
            logger.info("WS 客户端注入完成并写入持久标记: %s uin=%s", name, uin)
            # 重启前先写入 webui.json autoLoginAccount，NapCat 重启后可快速登录，无需再扫码。
            try:
                self._sync_webui_auto_login(name, uin)  # type: ignore[attr-defined]
                logger.info("已同步 webui.json autoLoginAccount: %s uin=%s", name, uin)
            except Exception as we:
                logger.debug("同步 webui autoLoginAccount 失败（不影响注入）: %s", we)
            # NapCat 不会热重载配置文件，注入后必须重启容器才能生效。
            self._schedule_container_restart(name)
        except Exception as e:
            logger.error("登录后 WS 注入失败 (%s/%s): %s", name, uin, e)

    def _schedule_container_restart(self, name: str) -> None:
        """注入 WS 配置后异步重启容器，使 NapCat 加载新配置。

        NapCat 不热重载 onebot11_{uin}.json，必须重启才能建立 WS 客户端连接。
        使用 fire-and-forget：5s 延迟后重启，给配置写入留出时间，避免竞态。
        """
        from services.docker_manager import _main_event_loop

        async def _do_restart(_name: str) -> None:
            import asyncio as _asyncio

            await _asyncio.sleep(5)  # 等待配置写入落盘
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
