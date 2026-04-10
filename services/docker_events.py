"""
Docker Events 监听器 — 后台线程订阅 Docker daemon 容器生命周期事件，
收到事件后：
  1. 通知 StateEngine 立即刷新（替代纯定时轮询）。
  2. 按容器名分发到 SSE 订阅队列（供 GET /api/containers/{name}/events 使用）。
断线自动重连，5s 间隔。
"""
import asyncio
import threading
import time
from collections import defaultdict
from typing import Optional, Callable

import docker
import docker.errors

from services.log import logger

# 只关注容器生命周期事件
_EVENT_FILTERS = {
    "type": ["container"],
    "event": ["start", "stop", "die", "destroy", "create", "restart", "pause", "unpause"],
}
_RECONNECT_INTERVAL = 5   # 断线重连间隔（秒）
_QUEUE_MAXSIZE = 64        # 每条订阅队列最大积压事件数（防慢消费者撑爆内存）


class DockerEventWatcher:
    """Docker 事件监听器 — 后台线程，事件驱动通知 StateEngine + SSE 订阅分发。"""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._notify_fn: Optional[Callable] = None
        # name -> list[asyncio.Queue]；由各 SSE handler 注册
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._sub_lock = threading.Lock()   # 保护 _subscribers / _last_event 写操作
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # name -> {action, time}；供 stats 接口附加 last_event 字段
        self._last_event: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    #  公开：查询最近事件                                                   #
    # ------------------------------------------------------------------ #

    def get_last_event(self, name: str) -> Optional[dict]:
        """返回指定容器最近一次 Docker 事件 {action, time}，无记录时返回 None。"""
        with self._sub_lock:
            return self._last_event.get(name)

    # ------------------------------------------------------------------ #
    #  公开：订阅 / 取消订阅                                               #
    # ------------------------------------------------------------------ #

    def subscribe(self, name: str, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        """为指定容器名注册一个事件队列，返回该队列供 SSE handler 消费。

        Args:
            name: 容器名（精确匹配）。
            loop: SSE handler 所在的事件循环，用于线程安全投递。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        with self._sub_lock:
            self._subscribers[name].append(q)
            if self._loop is None:
                self._loop = loop
        logger.debug("EventWatcher: 订阅容器事件 name=%s total=%d", name, len(self._subscribers[name]))
        return q

    def unsubscribe(self, name: str, q: asyncio.Queue) -> None:
        """移除指定队列。"""
        with self._sub_lock:
            lst = self._subscribers.get(name, [])
            try:
                lst.remove(q)
            except ValueError:
                pass
            if not lst:
                self._subscribers.pop(name, None)
        logger.debug("EventWatcher: 取消订阅 name=%s", name)

    # ------------------------------------------------------------------ #
    #  生命周期                                                            #
    # ------------------------------------------------------------------ #

    def start(self, notify_fn: Callable):
        """启动事件监听线程。

        Args:
            notify_fn: 收到事件时的回调（通常是 state_engine.notify_change）
        """
        if self._running:
            return
        self._notify_fn = notify_fn
        self._running = True
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="docker-events",
            daemon=True,
        )
        self._thread.start()
        logger.info("Docker 事件监听器已启动")

    def stop(self):
        """停止监听。"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    # ------------------------------------------------------------------ #
    #  内部                                                                #
    # ------------------------------------------------------------------ #

    def _watch_loop(self):
        """后台线程主循环 — 断线自动重连。"""
        while self._running:
            client = None
            try:
                client = docker.from_env(timeout=10)
                logger.info("Docker Events 已连接")
                self._consume_events(client)
            except docker.errors.DockerException as e:
                logger.debug("Docker Events 连接失败: %s", e)
            except Exception as e:
                logger.debug("Docker Events 异常: %s", e)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

            if self._running:
                time.sleep(_RECONNECT_INTERVAL)

    def _consume_events(self, client):
        """消费事件流（阻塞，直到断线或 stop）。"""
        for event in client.events(decode=True, filters=_EVENT_FILTERS):
            if not self._running:
                break
            name = event.get("Actor", {}).get("Attributes", {}).get("name", "?")
            action = event.get("Action", "?")
            logger.debug("Docker event: %s %s", action, name)

            if self._notify_fn:
                self._notify_fn()

            self._dispatch(name, action, event)

    def _dispatch(self, name: str, action: str, raw: dict) -> None:
        """将事件投递到订阅该容器名的所有队列（线程安全），并更新 last_event 缓存。"""
        payload = {
            "name": name,
            "action": action,
            "time": raw.get("time", int(time.time())),
            "status": raw.get("status", action),
            "exit_code": raw.get("Actor", {}).get("Attributes", {}).get("exitCode"),
        }
        with self._sub_lock:
            # 更新 last_event 缓存（仅保留 action + time，轻量）
            self._last_event[name] = {"action": action, "time": payload["time"]}
            queues = list(self._subscribers.get(name, []))

        if not queues or self._loop is None:
            return

        def _put_all():
            for q in queues:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    logger.debug("EventWatcher: 队列满，丢弃事件 name=%s action=%s", name, action)

        self._loop.call_soon_threadsafe(_put_all)


# ============ 单例 ============
docker_event_watcher = DockerEventWatcher()

