"""SnowLuma 权威登录态 —— 直接采信网关自己写下的事实。

窗口探测是从「窗口尺寸」倒推登录态：间接、且**看不见到底登的是哪个号**。
网关日志把两件事都直接写出来了：

    [Hook] login detected: PID=176905 UIN=3939309590
    [Bridge] session started: UIN=3939309590
    [Bridge] session closed:  UIN=3939309590

`supervisorctl status` 给出每个账号程序的**当前 pid**，hook 行给出该 pid 的 uin，
两者一 join，就能回答「这个账号上实际登的是哪个 QQ」。与配置 uin 不一致即
``wrong_account`` —— 这正是 2026-10-15 苏州事故的形态：账号 QQ_1548866093 配置的是
2811969179，实际却以 3228731821 登录，而所有窗口尺寸检查都判它「健康」。

设计约束（安全第一）：
  * **只做正向覆盖**：日志给出明确证据时才改变判定；日志里查不到该 pid 的登录事件
    一律返回 None（交给心跳 / 窗口探测），绝不因为「没查到」就判离线；
  * 零凭据：只读共享容器里的日志文件，与 services/snowluma_logs.py 同一条路；
  * 一次 docker exec 覆盖全部账号，带 TTL 缓存，不放大成每账号一次。
"""
from __future__ import annotations

import re
import time
from typing import Any

try:
    import docker
except ImportError:  # 允许无 Docker SDK 的投影测试
    docker = None

_LOG_DIR = "/app/data/logs"
# 只看最近两个日志文件（按 mtime 升序取末两个），再取末 300 条事件行：
# 足够覆盖「当前进程是在哪次登录里起来的」，又不会把整天的日志读进来。
_SCAN_FILES = 2
_SCAN_LINES = 300
_TTL = 8.0
_cache: dict[str, Any] = {}

_RE_LOGIN = re.compile(r"\[Hook\]\s+login detected:\s*PID=(\d+)\s+UIN=(\d+)")
_RE_SESSION = re.compile(r"\[Bridge\]\s+session (started|closed):\s*UIN=(\d+)")

_DIGEST_CMD = (
    "cd %s 2>/dev/null || exit 0; "
    "ls -1tr snowluma-*.log 2>/dev/null | tail -%d "
    "| xargs -r grep -h -E '\\[Hook\\] login detected|\\[Bridge\\] session (started|closed)' 2>/dev/null "
    "| tail -n %d"
) % (_LOG_DIR, _SCAN_FILES, _SCAN_LINES)


def parse_digest(text: str) -> dict[str, Any]:
    """把事件行解析成 {pids: {pid: uin}, online: {uin: bool}}。

    纯函数，便于离线单测：同一 pid 取**最后一条**登录事件，同一 uin 取**最后一条**
    会话事件（后写的覆盖先写的，与日志时间顺序一致）。
    """
    pids: dict[str, str] = {}
    online: dict[str, bool] = {}
    for line in (text or "").splitlines():
        m = _RE_LOGIN.search(line)
        if m:
            pids[m.group(1)] = m.group(2)
            continue
        m = _RE_SESSION.search(line)
        if m:
            online[m.group(2)] = m.group(1) == "started"
    return {"pids": pids, "online": online}


def digest(container: str, ttl: float = _TTL) -> dict[str, Any] | None:
    """读共享容器日志并解析；失败返回 None（调用方必须回退，不得当成离线）。"""
    if docker is None:
        return None
    now = time.time()
    hit = _cache.get(container)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value: dict[str, Any] | None = None
    try:
        target = docker.from_env().containers.get(container)
        res = target.exec_run(["sh", "-c", _DIGEST_CMD], demux=True)
        out = res.output[0] if isinstance(res.output, tuple) else res.output
        value = parse_digest((out or b"").decode("utf-8", "replace"))
    except Exception:                                  # noqa: BLE001
        value = None
    _cache[container] = (now, value)
    return value


def account_truth(container: str, program: str, pid: str | int | None) -> dict[str, Any] | None:
    """该账号程序**实际**登录的 uin 与在线状态。

    返回 None = 日志里没有关于这个 pid 的登录证据（调用方回退心跳 / 窗口探测）。
    返回 ``{"uin": <uin 或 None>, "online": <bool>, "pid": <pid>}``。

    只在 pid 已知且日志里有该 pid 的登录事件时才给出 uin —— 这样：
      * 账号正常登录自己的号 → uin == 配置 uin；
      * **登错号** → uin != 配置 uin（1548866093 那类事故第一次就能抓到）；
      * 刚 logout / 从未登录 → 查不到该 pid 的登录事件 → None（不误判离线）。
    """
    if not program:
        return None
    pid_s = str(pid or "").strip()
    if not pid_s.isdigit():
        return None
    data = digest(container)
    if not data:
        return None
    uin = (data.get("pids") or {}).get(pid_s)
    if not uin:
        return None
    return {
        "uin": uin,
        "online": bool((data.get("online") or {}).get(uin)),
        "pid": pid_s,
    }


def recent_logins(container: str, limit: int = 20) -> list[dict[str, str]]:
    """诊断用：最近的 pid→uin 登录事件（不做缓存，直接读一次）。"""
    if docker is None:
        return []
    try:
        target = docker.from_env().containers.get(container)
        res = target.exec_run(["sh", "-c", _DIGEST_CMD], demux=True)
        out = res.output[0] if isinstance(res.output, tuple) else res.output
        text = (out or b"").decode("utf-8", "replace")
    except Exception:                                  # noqa: BLE001
        return []
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        m = _RE_LOGIN.search(line)
        if m:
            rows.append({"kind": "login", "pid": m.group(1), "uin": m.group(2)})
        else:
            m = _RE_SESSION.search(line)
            if m:
                rows.append({"kind": m.group(1), "uin": m.group(2)})
    return rows[-limit:]
