"""SnowLuma 网关心跳接入 —— 给每个账号配置追加指向面板的 wsClient。

背景（2026-10-15）：面板的 `bot_online` 依赖 OneBot `meta_event.heartbeat`，但宁波
71 份 `onebot_<uin>.json` 的 `wsClients` **全部指向外部**（德云 / NA 后台等），
**没有一条指向面板**，于是面板永远收不到心跳：`bot_heartbeat._table` 恒空、
`bot_online` 全 False、登录态只能靠窗口尺寸间接推断。

修法：给每份账号配置的 `wsClients` **追加**一条指向面板的客户端。
OneBot 支持多客户端并行，因此**不影响**已有的德云 / NA 连接。

关键约束：
  * 只追加、绝不删除或改写既有 wsClient；
  * 读不到原文件就**报错退出**，绝不写空文件覆盖账号配置；
  * 写回时保留原属主 / 权限（文件归 akiyo，网关需可写）；
  * 网关不 watch 配置文件，写入后需重启网关或走 `POST /api/config/:uin` 热重载。
"""

import json
import posixpath
from typing import Any, Optional

from services.log import logger

# 面板在 OneBot 反向 WS 上的路径（与 routers/ws_router.py 保持一致）
_PANEL_WS_PATH = "/ws/onebot/v11/ws"
# 面板容器内的账号配置目录（= 宿主机 <data>/gateway/config）
_CONFIG_DIR = "/app/data/config"
# 这条 wsClient 的名字，用于幂等识别（重跑不会重复追加）
_CLIENT_NAME = "ncqq-panel"


def _client(container: str):
    import docker

    return docker.from_env().containers.get(container)


def _read_config(container: str, uin: str) -> dict:
    """读账号配置。读不到就抛异常 —— 绝不返回空 dict 让调用方覆盖原文件。"""
    path = posixpath.join(_CONFIG_DIR, "onebot_%s.json" % uin)
    res = _client(container).exec_run(["cat", path], demux=True)
    if getattr(res, "exit_code", 1) != 0:
        raise FileNotFoundError("read failed: %s" % path)
    out = res.output[0] if isinstance(res.output, tuple) else res.output
    raw = (out or b"").decode("utf-8", "replace").strip()
    if not raw:
        raise ValueError("empty config file: %s" % path)
    return json.loads(raw)


def _write_config(container: str, uin: str, data: dict) -> None:
    """写回账号配置。

    用 `put_archive` 投递单个文件的 tar，并保留原文件的属主 / 权限（文件归 akiyo，
    网关需可写）。比走 shell + stdin 可靠：不受引号 / 编码 / 缓冲影响。
    """
    import io
    import tarfile

    path = posixpath.join(_CONFIG_DIR, "onebot_%s.json" % uin)
    cli = _client(container)
    # 取原属主与权限
    res = cli.exec_run(["stat", "-c", "%u %g %a", path], demux=True)
    out = res.output[0] if isinstance(res.output, tuple) else res.output
    parts = (out or b"").decode().strip().split()
    uid, gid, mode = (parts + ["1001", "1001", "600"])[:3]
    try:
        mode_i = int(mode, 8)
    except ValueError:
        mode_i = 0o600

    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(posixpath.basename(path))
        info.size = len(payload)
        info.mode = mode_i
        info.uid = int(uid)
        info.gid = int(gid)
        tf.addfile(info, io.BytesIO(payload))
    cli.put_archive(_CONFIG_DIR, buf.getvalue())


def _panel_ws_url(container: str, token: str) -> str:
    """算出网关能连到的面板地址。

    面板与网关同在默认 bridge 网络（无 DNS 别名），所以按容器名解析不可靠；
    直接读面板容器在 bridge 上的 IP。可用 NCQQ_PANEL_WS_URL 覆盖。
    """
    import os

    override = os.environ.get("NCQQ_PANEL_WS_URL", "").strip()
    if override:
        base = override
    else:
        import docker

        me = docker.from_env().containers.get(container)
        nets = (me.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        ip = ""
        for net in nets.values():
            ip = (net or {}).get("IPAddress") or ""
            if ip:
                break
        if not ip:
            raise RuntimeError("无法确定面板容器 IP，请设置 NCQQ_PANEL_WS_URL")
        base = "ws://%s:8000" % ip
    url = base.rstrip("/") + _PANEL_WS_PATH
    if token:
        url += "?access_token=" + token
    return url


def _heartbeat_client(url: str, token: str) -> dict:
    """构造要追加的 wsClient（字段与网关既有条目保持一致）。"""
    return {
        "name": _CLIENT_NAME,
        "enabled": True,
        "url": url,
        "role": "Universal",
        "accessToken": token,
        "messageFormat": "array",
        "reportSelfMessage": False,
        "reconnectIntervalMs": 3000,
    }


def _is_ours(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("name") == _CLIENT_NAME:
        return True
    # 名字被改过也要能识别，避免重复追加
    return _PANEL_WS_PATH in str(entry.get("url", ""))


def all_uins() -> list:
    """全部已配置的 SnowLuma 账号 uin（来自 snowluma_accounts.json）。"""
    from services import snowluma

    seen, out = set(), []
    for item in snowluma.accounts().values():
        uin = str(item.get("uin", "")).strip()
        if uin and uin not in seen:
            seen.add(uin)
            out.append(uin)
    return out


def target_container() -> str:
    """共享容器名（承载全部虚拟账号）。"""
    from services import snowluma

    accts = snowluma.accounts()
    for item in accts.values():
        name = item.get("container")
        if name:
            return name
    return snowluma._SHARED_CONTAINER_NAME


def plan(container: str, token: str, uins: list) -> list:
    """只读预演：返回每个 uin 将要发生的动作，不写任何文件。"""
    url = _panel_ws_url(container, token)
    out = []
    for uin in uins:
        try:
            cfg = _read_config(container, uin)
        except Exception as exc:                       # noqa: BLE001
            out.append({"uin": uin, "action": "error", "detail": str(exc)})
            continue
        clients = ((cfg.get("networks") or {}).get("wsClients")) or []
        existing = [c for c in clients if _is_ours(c)]
        if existing:
            same = any(c.get("url") == url for c in existing)
            out.append({
                "uin": uin,
                "action": "unchanged" if same else "update",
                "before": len(clients),
                "ours": len(existing),
            })
        else:
            out.append({"uin": uin, "action": "append", "before": len(clients)})
    return out


def apply(container: str, token: str, uins: list) -> dict:
    """把面板 wsClient 追加进每份账号配置（幂等）。"""
    url = _panel_ws_url(container, token)
    entry = _heartbeat_client(url, token)
    done, skipped, failed = [], [], []
    for uin in uins:
        try:
            cfg = _read_config(container, uin)
            nets = cfg.setdefault("networks", {})
            clients = nets.get("wsClients")
            if not isinstance(clients, list):
                clients = []
            kept = [c for c in clients if not _is_ours(c)]
            kept.append(entry)
            nets["wsClients"] = kept
            _write_config(container, uin, cfg)
            # 回读校验，确认真的落盘且既有客户端没丢
            back = _read_config(container, uin)
            back_clients = ((back.get("networks") or {}).get("wsClients")) or []
            if not any(_is_ours(c) for c in back_clients):
                raise RuntimeError("verify failed: our client not present after write")
            if len(back_clients) != len(kept):
                raise RuntimeError(
                    "verify failed: client count %d != expected %d" % (len(back_clients), len(kept))
                )
            done.append(uin)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("心跳接入失败 uin=%s: %s", uin, exc)
            failed.append({"uin": uin, "detail": str(exc)})
    return {"url": url, "ok": done, "skipped": skipped, "failed": failed,
            "ok_count": len(done), "failed_count": len(failed)}
