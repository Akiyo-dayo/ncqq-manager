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

Login state: the shared container's X display exposes a 320x460 window for any
account sitting on the login screen. Presence => "waiting", absence =>
"logged_in". The window list is read via the same extractor the QR flow uses.
"""
from __future__ import annotations

import base64
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

_PROBE_SRC = (
    "import runpy,json,sys\n"
    "try:\n"
    "    _m=runpy.run_path('/tmp/dump-qr.py')\n"
    "    print(json.dumps({'windows':sorted(_m['login_wins']().keys()),"
    "'programs':{k:list(v) for k,v in _m['ACCOUNTS'].items()}}))\n"
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
        res = target.exec_run(["test", "-s", _EXTRACTOR_DST])
        if getattr(res, "exit_code", 1) == 0:
            return
        with open(_EXTRACTOR_SRC, "rb") as fh:
            raw = fh.read()
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


def describe() -> list[dict[str, Any]]:
    probe_by_container: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    for name, item in accounts().items():
        uin = item["uin"]
        container = item["container"]
        if container not in probe_by_container:
            probe_by_container[container] = _login_probe(container)
        probe = probe_by_container[container]

        stage = None
        if probe:
            img = (probe.get("programs", {}).get(item["program"]) or [None])[0]
            if img:
                stage = "waiting" if img in (probe.get("windows") or []) else "logged_in"

        entry: dict[str, Any] = {
            "id": f"snowluma:{name}",
            "name": name,
            "status": "running",
            "image": "snowluma-account",
            "created": "",
            "snowluma_account": True,
            "configured_uin": uin,
            "last_uin": uin,
        }
        if stage == "logged_in":
            entry["uin"] = uin
        if stage is not None:
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


def fetch_qr(name: str) -> dict[str, Any]:
    item = accounts().get(name)
    if not item:
        return {"status": "waiting", "source": "snowluma_unknown"}
    fetched = time.time()
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
        archive, stat = target.get_archive("/tmp/" + image_file)
        raw = _extract_png(b"".join(archive))
        if not raw:
            return {"status": "waiting", "source": "snowluma_qr_invalid", "fetched_at": int(fetched)}
        raw_mtime = stat.get("mtime", fetched) if isinstance(stat, dict) else fetched
        try:
            generated = float(raw_mtime)
        except (TypeError, ValueError):
            generated = fetched
        encoded = base64.b64encode(raw).decode("ascii")
        age = max(0, int(fetched - generated))
        return {"status": "ok", "url": "data:image/png;base64," + encoded, "image_base64": encoded, "content_type": "image/png", "type": "image", "source": "snowluma_targeted", "generated_at": int(generated), "fetched_at": int(fetched), "age_seconds": age, "expires_at": int(generated + 120), "expires_in": max(0, 120-age), "max_age_seconds": 120}
    except Exception:
        return {"status": "waiting", "source": "snowluma_qr_error", "fetched_at": int(fetched)}
