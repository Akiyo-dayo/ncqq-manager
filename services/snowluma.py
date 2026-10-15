"""Narrow SnowLuma account adapter.

Only configured virtual QQ accounts are exposed as Manager instances. The
underlying SnowLuma supervisor/container is deliberately never listed as a
managed instance and is not accepted by this adapter's lifecycle methods.

Mapping source, first hit wins:
  1. /app/config/snowluma_accounts.json  (bind-mounted, survives recreation)
  2. NCQQ_SNOWLUMA_ACCOUNTS env var      (legacy)
  3. built-in default

Name filter: NCQQ_SNOWLUMA_PREFIX (default ""). An empty prefix lets a virtual
account reuse the ORIGINAL NapCat container name, so existing Manager bindings
(which key on (node_id, container_name)) keep matching after migration.

Login state is a POSITIVE determination: an account is reported "logged_in"
only when its supervisor program is RUNNING and it exposes a window that is
NOT one of the known login windows (320x460 scan window / 680x440 one-click
window). Anything the probe cannot classify fails closed to "unknown" — the
old "no login window => logged_in" rule mislabeled accounts parked on the
one-click screen (and unrepainted windows) as online. The window list is read
via the same extractor the QR flow uses.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
import time
from typing import Any

try:
    import docker
except ImportError:  # allows config/projection tests without Docker SDK installed
    docker = None

_PREFIX = os.environ.get("NCQQ_SNOWLUMA_PREFIX", "")
_ACCOUNTS_FILE = os.environ.get("NCQQ_SNOWLUMA_ACCOUNTS_FILE", "/app/config/snowluma_accounts.json")
_EXTRACTOR_SRC = "/app/config/snowluma-qr-extractor.py"
_EXTRACTOR_DST = "/tmp/dump-qr.py"

_DEFAULT = {"snowluma_3284733247": {"container": "snowluma", "program": "qq-extra-1", "uin": "3284733247"}}

_WIN_TTL = 8.0
_win_cache: dict[str, Any] = {"ts": 0.0, "container": "", "value": None}

# 已知登录窗尺寸（2026-10-15 宁波实测）：
#   320x460 = 扫码登录窗；680x440 = 一键登录窗。
# 两者都表示「未登录，停在登录界面」。旧逻辑只认 320x460，导致停在
# 680x440 一键登录界面的账号被误判为 logged_in（假在线）。
_LOGIN_WINDOW_SIZES = ((320, 460), (680, 440))
# 主窗口判定下限（实测登录后会出现 960x640 / 720x640 / 800x600 主窗口）。
# 低于该尺寸的窗口（1x1 / 10x10 等辅助窗）不作为登录证据。
_MAIN_MIN_W = 600
_MAIN_MIN_H = 400
# 一键登录界面「QQ 蓝」像素阈值（标定：一键登录界面 10024 / 自动恢复过渡窗 0）。
_BLUE_BAND_THRESHOLD = 1500
# 二维码最大有效期（秒）— 与状态引擎的 QR 保鲜窗口保持一致。
_QR_MAX_AGE = 120
# 二维码可接受的最小边长（像素）；小于此值视为残图 / 坏图。
_QR_MIN_DIM = 64

_PROBE_SRC = (
    "import runpy,json,sys\n"
    "try:\n"
    "    _m=runpy.run_path('/tmp/dump-qr.py')\n"
    "    _h2i=_m.get('HOME2IMG') or {}\n"
    "    _sz={}\n"
    "    for _row in _m['win_list']():\n"
    "        _wid,_x,_y,_w,_h=_row\n"
    "        _p=_m['pid_of'](_wid)\n"
    "        _img=_h2i.get(_m['home_of'](_p) if _p else '')\n"
    "        if _img:\n"
    "            _sz.setdefault(_img,[]).append([_w,_h])\n"
    "    print(json.dumps({'windows':sorted(_m['login_wins']().keys()),"
    "'programs':{k:list(v) for k,v in _m['ACCOUNTS'].items()},"
    "'sizes':_sz}))\n"
    "except Exception:\n"
    "    sys.exit(3)\n"
)


def _raw_mapping() -> dict:
    try:
        if _ACCOUNTS_FILE and os.path.isfile(_ACCOUNTS_FILE):
            with open(_ACCOUNTS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                inner = data.get("accounts")
                return inner if isinstance(inner, dict) else data
    except Exception:
        pass
    raw = os.environ.get("NCQQ_SNOWLUMA_ACCOUNTS", "")
    if not raw:
        return _DEFAULT
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def accounts() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for name, item in _raw_mapping().items():
        if not isinstance(name, str) or not isinstance(item, dict):
            continue
        if _PREFIX and not name.startswith(_PREFIX):
            continue
        if item.get("enabled") is False:
            continue
        container, program, uin = (str(item.get(k, "")).strip() for k in ("container", "program", "uin"))
        if container and program and uin.isdigit():
            out[name] = {"container": container, "program": program, "uin": uin}
    return out


def is_virtual(name: str) -> bool:
    return name in accounts()


_SHARED_CONTAINER_NAME = os.environ.get("NCQQ_SNOWLUMA_CONTAINER", "snowluma")


def is_shared_container(name: str) -> bool:
    """name 是否为承载全部虚拟账号的共享容器本体（如 snowluma）。

    这类名字不是虚拟账号。对它做 Docker 生命周期操作 = 所有账号一起掉线，
    必须显式拒绝。
    """
    if name in accounts():
        return False
    try:
        return name in {item["container"] for item in accounts().values()}
    except Exception:
        return name == _SHARED_CONTAINER_NAME


def _supervisorctl(container: str, *args: str):
    """在共享容器里跑 supervisorctl，返回 (exit_code, 合并输出)。"""
    try:
        target = _container_client(container)
        res = target.exec_run(["supervisorctl", *args], demux=True)
        out = res.output[0] if isinstance(res.output, tuple) else res.output
        err = res.output[1] if isinstance(res.output, tuple) and len(res.output) > 1 else b""
        txt = ((out or b"") + (err or b"")).decode("utf-8", "replace")
        return getattr(res, "exit_code", 1), txt
    except Exception as exc:                      # noqa: BLE001
        return 1, "supervisorctl failed: %s" % exc


_STATES_TTL = 3.0
_states_cache: dict = {}


def program_states(container: str) -> dict:
    """-> {program: RUNNING|STOPPED|...}（一次 status 覆盖全部程序）

    带 3 秒 TTL：describe() 每次都会调它，而它是一次 docker exec，
    面板轮询时不做缓存会放大成每秒若干次 exec。
    """
    import time as _t

    hit = _states_cache.get(container)
    if hit and _t.time() - hit[0] < _STATES_TTL:
        return hit[1]
    _code, txt = _supervisorctl(container, "status")
    states: dict = {}
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    _states_cache[container] = (_t.time(), states)
    return states


def account_action(name: str, action: str):
    """按账号粒度 start/stop/restart：只动它自己的 supervisor 程序，绝不碰共享容器。"""
    item = accounts().get(name)
    if not item:
        return False, "unknown snowluma account: %s" % name
    if action not in ("start", "stop", "restart", "logout"):
        return False, "unsupported action for snowluma account: %s" % action
    if action == "logout":
        return logout_account(name)
    code, txt = _supervisorctl(item["container"], action, item["program"])
    return code == 0, (txt.strip() or ("exit=%s" % code))


def logout_account(name: str):
    """退出登录：清掉该账号盘上的登录凭据并重启，使其回到扫码登录界面。

    用于「登错账号」——清完用户重新扫正确的号即可。
    """
    item = accounts().get(name)
    if not item:
        return False, "unknown snowluma account: %s" % name
    try:
        target = _container_client(item["container"])
        _ensure_extractor(item["container"])
        _ensure_oneclick(item["container"])
        res = target.exec_run(
            ["python3", _ONECLICK_DST, "logout", item["program"]], demux=True
        )
        out = res.output[0] if isinstance(res.output, tuple) else res.output
        err = res.output[1] if isinstance(res.output, tuple) and len(res.output) > 1 else b""
        txt = ((out or b"") + (err or b"")).decode("utf-8", "replace").strip()
        return getattr(res, "exit_code", 1) == 0, txt[-600:]
    except Exception as exc:                      # noqa: BLE001
        return False, str(exc)


_ONECLICK_SRC = "/app/config/snowluma-oneclick.py"
_ONECLICK_DST = "/tmp/snowluma-oneclick.py"


def _ensure_oneclick(container: str) -> None:
    """把一键登录助手送进共享容器（/tmp 会被容器重建清掉，故按需重送）。"""
    if not os.path.isfile(_ONECLICK_SRC):
        return
    try:
        target = _container_client(container)
        with open(_ONECLICK_SRC, "rb") as fh:
            raw = fh.read()
        want = hashlib.md5(raw).hexdigest()
        res = target.exec_run(["md5sum", _ONECLICK_DST])
        have = res.output[0] if isinstance(res.output, tuple) else res.output
        if want.encode() in (have or b""):
            return
        _put_bytes(target, "snowluma-oneclick.py", raw)
    except Exception:
        pass


def classify_png(container: str, png_path: str) -> int:
    """该 PNG 在 y=305..350 的「QQ 蓝」像素数；-1 = 无法判定。

    标定值：一键登录界面 10024 / 自动恢复过渡窗 0。阈值 1500。
    """
    try:
        target = _container_client(container)
        _ensure_extractor(container)
        _ensure_oneclick(container)
        res = target.exec_run(["python3", _ONECLICK_DST, "classify", png_path], demux=True)
        out = res.output[0] if isinstance(res.output, tuple) else res.output
        for line in (out or b"").decode("utf-8", "replace").splitlines():
            if line.startswith("BLUEBAND="):
                return int(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return -1


def oneclick_login(name: str) -> dict:
    """对「已记住账号」的一键登录界面执行：勾自动登录 + 点登录。"""
    item = accounts().get(name)
    if not item:
        return {"ok": False, "reason": "unknown snowluma account: %s" % name}
    try:
        target = _container_client(item["container"])
        _ensure_extractor(item["container"])
        _ensure_oneclick(item["container"])
        res = target.exec_run(
            ["python3", _ONECLICK_DST, "login", item["program"]], demux=True
        )
        out = res.output[0] if isinstance(res.output, tuple) else res.output
        err = res.output[1] if isinstance(res.output, tuple) and len(res.output) > 1 else b""
        txt = ((out or b"") + (err or b"")).decode("utf-8", "replace").strip()
        return {"ok": getattr(res, "exit_code", 1) == 0, "log": txt[-800:]}
    except Exception as exc:                      # noqa: BLE001
        return {"ok": False, "reason": str(exc)}


def _container_client(container: str):
    if docker is None:
        raise RuntimeError("docker sdk unavailable")
    return docker.from_env().containers.get(container)


def _put_bytes(target, filename: str, raw: bytes) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(filename)
        info.size = len(raw)
        tf.addfile(info, io.BytesIO(raw))
    target.put_archive("/tmp", buf.getvalue())


def _ensure_extractor(container: str) -> None:
    """Ship the QR / login-window extractor into the shared container.

    It is kept in the Manager's persistent config dir because /tmp inside the
    SnowLuma container is wiped whenever that container is recreated.
    """
    if not os.path.isfile(_EXTRACTOR_SRC):
        return
    try:
        target = _container_client(container)
        with open(_EXTRACTOR_SRC, "rb") as fh:
            raw = fh.read()
        want = hashlib.md5(raw).hexdigest()
        # 旧实现是「目标文件存在就早退」，导致改了面板侧提取器后容器里那份永远不更新
        # （2026-10-13 FBnekro 旧提取器事故、2026-10-14 弹窗假图事故的同一个坑）。
        # 改为比对 md5，内容变了就重送。
        res = target.exec_run(["md5sum", _EXTRACTOR_DST])
        have = res.output[0] if isinstance(res.output, tuple) else res.output
        if want.encode() in (have or b""):
            return
        _put_bytes(target, "dump-qr.py", raw)
    except Exception:
        pass


def _login_probe(container: str) -> dict[str, Any] | None:
    """-> {'windows': [...], 'programs': {program: [img, home]}} or None on failure."""
    now = time.time()
    if _win_cache["container"] == container and now - _win_cache["ts"] < _WIN_TTL:
        return _win_cache["value"]
    value: dict[str, Any] | None = None
    try:
        target = _container_client(container)
        _ensure_extractor(container)
        _put_bytes(target, "strict-probe-login.py", _PROBE_SRC.encode())
        res = target.exec_run(["python3", "/tmp/strict-probe-login.py"], demux=True)
        if getattr(res, "exit_code", 1) != 0:
            raise RuntimeError("probe exit %s" % getattr(res, "exit_code", "?"))
        out = res.output[0] if isinstance(res.output, tuple) else res.output
        lines = (out or b"").decode("utf-8", "replace").strip().splitlines()
        value = json.loads(lines[-1]) if lines else None
    except Exception:
        value = None
    _win_cache.update(ts=now, container=container, value=value)
    return value


def _normalize_sizes(sizes: Any) -> list[tuple[int, int]]:
    """把探针返回的 [[w,h],...] 归一化为 [(w,h),...]；非法项丢弃。"""
    out: list[tuple[int, int]] = []
    for pair in sizes or []:
        try:
            w, h = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        out.append((w, h))
    return out


def _classify_login_stage(
    program: str,
    running: bool,
    probe: dict[str, Any] | None,
    blue_band: int | None = None,
) -> str:
    """正向判定单账号登录态：stopped / waiting / logged_in / unknown。

    证据链（2026-10-15 宁波实测，52 个账号窗口尺寸全量核对）：
      * 停在扫码登录界面 → 该账号存在 320x460 窗口；
      * 停在一键登录界面 → 该账号存在 680x440 窗口（旧逻辑漏判 ⇒ 假在线）；
      * 已登录           → 该账号存在 960x640 / 720x640 / 800x600 主窗口；
      * 只剩 1x1 / 10x10 辅助窗或无窗口 → 拿不准，报 unknown。

    任何「拿不准」一律 unknown，绝不默认 logged_in。
    """
    if not running:
        return "stopped"
    if not probe:
        return "unknown"
    img = ((probe.get("programs") or {}).get(program) or [None])[0]
    if not img:
        return "unknown"
    # 提取器原生只收录 320x460 扫码窗，命中即 waiting（无需 sizes）
    if img in (probe.get("windows") or []):
        return "waiting"

    sizes = (probe.get("sizes") or {}).get(img)
    if sizes is None:
        # 旧版提取器不返回 sizes：退回 blue_band 内容证据，拿不准则 unknown
        if blue_band is None:
            return "unknown"
        return "waiting" if blue_band >= _BLUE_BAND_THRESHOLD else "logged_in"

    normalized = _normalize_sizes(sizes)
    # 任一登录窗（扫码 320x460 / 一键 680x440）⇒ 未登录
    if any(sz in _LOGIN_WINDOW_SIZES for sz in normalized):
        return "waiting"
    # 存在主窗口 ⇒ 已登录
    if any(w >= _MAIN_MIN_W and h >= _MAIN_MIN_H for (w, h) in normalized):
        return "logged_in"
    # 只有辅助小窗（1x1 / 10x10）或完全没有窗口 ⇒ 拿不准
    return "unknown"


def _account_login_stage(item: dict[str, str]) -> str:
    """账号级登录态 — describe() 与 fetch_qr() 共用，保证两者判定一致。"""
    container = item["container"]
    running = program_states(container).get(item["program"]) == "RUNNING"
    probe = _login_probe(container) if running else None
    return _classify_login_stage(item["program"], running, probe)


def describe() -> list[dict[str, Any]]:
    """合成虚拟账号列表。

    登录态改为正向判定（2026-10-15 实测）：
      * status 取真实 supervisor 运行态；
      * 程序未 RUNNING 时 login_stage = "stopped"；
      * 只有「RUNNING 且窗口不是登录窗」才报 logged_in，拿不准一律 unknown，
        不再用「没有登录窗 ⇒ 已登录」的假在线判定；
      * login_stage 恒有值，避免状态引擎保留上一轮的陈旧登录态；
      * uin / last_uin 仅在确认登录时输出。
    """
    probe_by_container: dict[str, Any] = {}
    states_by_container: dict[str, dict] = {}
    out: list[dict[str, Any]] = []
    for name, item in accounts().items():
        uin = item["uin"]
        container = item["container"]
        if container not in states_by_container:
            states_by_container[container] = program_states(container)
        states = states_by_container[container]
        running = states.get(item["program"]) == "RUNNING"

        if running and container not in probe_by_container:
            probe_by_container[container] = _login_probe(container)
        probe = probe_by_container.get(container)

        stage = _classify_login_stage(item["program"], running, probe)

        entry: dict[str, Any] = {
            "id": f"snowluma:{name}",
            "name": name,
            "status": "running" if running else "exited",
            "image": "snowluma-account",
            "created": "",
            "snowluma_account": True,
            "supervisor_program": item["program"],
            "supervisor_state": states.get(item["program"], "UNKNOWN"),
            "configured_uin": uin,
        }
        if stage == "logged_in":
            entry["uin"] = uin
            entry["last_uin"] = uin
        entry["login_stage"] = stage
        entry["login_method"] = "snowluma_probe"
        out.append(entry)
    return out


def _extract_png(data: bytes) -> bytes | None:
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data))
        for member in tf.getmembers():
            if member.isfile():
                f = tf.extractfile(member)
                raw = f.read() if f else b""
                if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    return raw
    except (OSError, tarfile.TarError):
        return None
    return None


def _png_dimensions(raw: bytes | None) -> tuple[int, int]:
    """解析 PNG 的 IHDR 宽高（纯标准库）。非 PNG / 结构损坏返回 (0, 0)。"""
    if not raw or len(raw) < 24 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return 0, 0
    if raw[12:16] != b"IHDR":
        return 0, 0
    try:
        width = int.from_bytes(raw[16:20], "big")
        height = int.from_bytes(raw[20:24], "big")
    except Exception:                              # noqa: BLE001
        return 0, 0
    return width, height


def fetch_qr(name: str) -> dict[str, Any]:
    item = accounts().get(name)
    if not item:
        return {"status": "waiting", "source": "snowluma_unknown"}
    fetched = time.time()
    # 已确认登录 → 立即告知前端停止轮询。旧实现永远只返回 waiting/ok，
    # 已登录账号会一直卡在「正在获取二维码」。
    try:
        if _account_login_stage(item) == "logged_in":
            return {
                "status": "logged_in",
                "uin": item["uin"],
                "source": "snowluma_login",
                "fetched_at": int(fetched),
            }
    except Exception:                              # noqa: BLE001
        pass
    try:
        if docker is None:
            return {"status": "waiting", "source": "snowluma_docker_unavailable", "fetched_at": int(fetched)}
        client = docker.from_env()
        target = client.containers.get(item["container"])
        _ensure_extractor(item["container"])
        # Install the fail-closed wrapper for this invocation only. It never
        # restarts supervisor programs and refuses unproven/ambiguous windows.
        wrapper = ("import runpy,sys\n" "_m=runpy.run_path('/tmp/dump-qr.py')\n" "_run=_m['run']\n" "_run.__globals__['spawn_fresh']=lambda *a,**k:None\n" "_run(sys.argv[1],False)\n")
        import io as _io, tarfile as _tarfile
        buf = _io.BytesIO()
        with _tarfile.open(fileobj=buf, mode="w") as tf:
            info = _tarfile.TarInfo("strict-dump-qr.py"); raw = wrapper.encode(); info.size = len(raw)
            tf.addfile(info, _io.BytesIO(raw))
        target.put_archive("/tmp", buf.getvalue())
        result = target.exec_run(["python3", "/tmp/strict-dump-qr.py", item["program"]], demux=True)
        if getattr(result, "exit_code", 1) != 0:
            return {"status": "waiting", "source": "snowluma_qr_unavailable", "fetched_at": int(fetched)}
        # The extractor prints the exact file it wrote; its per-program name
        # mapping is NOT always "qq-<uin>" (the default "qq" program writes
        # qr-app.png). Trust stdout, fall back to the legacy convention.
        _out = result.output[0] if isinstance(result.output, tuple) else result.output
        _txt = (_out or b"").decode("utf-8", "replace")
        _names = [ln.strip() for ln in _txt.splitlines()
                  if ln.strip().startswith("qr-") and ln.strip().endswith(".png")]
        image_file = _names[-1] if _names else "qr-qq-" + item["uin"] + ".png"
        # 判别：QQ 已记住账号时，这个「320x460 登录窗」其实是一键登录界面
        # （头像 + ○自动登录 + 蓝色登录按钮），不是二维码。用 y=305..350 的
        # 「QQ 蓝」像素数区分（标定：一键登录界面 10024 / 自动恢复过渡窗 0）。
        # 若不判别，就会把这张没法扫的图当二维码回给面板 —— 即「无法扫码」的根因。
        blue_band = classify_png(item["container"], "/tmp/" + image_file)
        if blue_band >= _BLUE_BAND_THRESHOLD:
            return {
                "status": "waiting",
                "source": "snowluma_oneclick",
                "type": "oneclick",
                "oneclick": True,
                "blue_band": blue_band,
                "fetched_at": int(fetched),
                "message": "该号已记住账号，QQ 显示的是一键登录界面而非二维码；"
                           "请点「一键登录」（或等待 autologin 守护自动完成）。",
            }
        archive, stat = target.get_archive("/tmp/" + image_file)
        raw = _extract_png(b"".join(archive))
        # 旧实现用 len(raw) < 8000 猜「残图」，会误杀合法的紧凑二维码。
        # 改为解析 PNG IHDR 宽高：只有真正过小 / 结构损坏的图才判为不可用。
        width, height = _png_dimensions(raw)
        if width < _QR_MIN_DIM or height < _QR_MIN_DIM:
            return {"status": "waiting", "source": "snowluma_qr_invalid", "fetched_at": int(fetched)}
        raw_mtime = stat.get("mtime", fetched) if isinstance(stat, dict) else fetched
        try:
            generated = float(raw_mtime)
        except (TypeError, ValueError):
            generated = fetched
        encoded = base64.b64encode(raw).decode("ascii")
        age = max(0, int(fetched - generated))
        return {"status": "ok", "url": "data:image/png;base64," + encoded, "image_base64": encoded, "content_type": "image/png", "type": "image", "source": "snowluma_targeted", "generated_at": int(generated), "fetched_at": int(fetched), "age_seconds": age, "expires_at": int(generated + _QR_MAX_AGE), "expires_in": max(0, _QR_MAX_AGE-age), "max_age_seconds": _QR_MAX_AGE}
    except Exception:
        return {"status": "waiting", "source": "snowluma_qr_error", "fetched_at": int(fetched)}
