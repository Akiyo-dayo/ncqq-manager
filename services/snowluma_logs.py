"""Recent log lines for SnowLuma virtual accounts.

A SnowLuma account has no container of its own: all accounts share one
container. The panel's generic log path therefore queries Docker for the OLD
NapCat container of the same name, which is normally stopped - yet
``docker logs`` still serves its history, so the UI shows a dead container's
old lines instead of the account's live activity.

SnowLuma's gateway writes a structured per-day log
(``/app/data/logs/snowluma-YYYY-MM-DD.log``) in which account-tagged lines carry
the UIN, so a single account's stream can be reconstructed by filtering. This
module reads that file inside the shared container and returns the last N
matching lines.

``get_account_logs`` returns ``None`` for anything that is not a virtual
account, so callers can fall back to the generic Docker path unchanged.
"""
from __future__ import annotations

try:
    import docker
except ImportError:  # allows projection tests without the Docker SDK
    docker = None

_LOG_DIR = "/app/data/logs"
_SCAN_MIN = 3000
_SCAN_MAX = 60000
_SCAN_FACTOR = 30


def _scan_window(lines: int) -> int:
    """How many raw shared-log lines to sift through for ``lines`` results."""
    try:
        n = int(lines)
    except (TypeError, ValueError):
        n = 100
    if n < 1:
        n = 1
    return max(_SCAN_MIN, min(_SCAN_MAX, n * _SCAN_FACTOR))


def _build_command(lines: int, uin: str) -> str:
    """Shell snippet executed inside the shared container.

    Scans the tail of the two most recent daily logs (oldest first, so the final
    ``tail`` keeps the newest lines) and keeps only lines tagged with this UIN.
    """
    return (
        "cd %s 2>/dev/null || exit 0; "
        "ls -1tr snowluma-*.log 2>/dev/null | tail -2 "
        "| xargs -r -I{} tail -n %d {} 2>/dev/null "
        "| grep -F -h -e '[%s]' -e 'uin=%s' 2>/dev/null "
        "| tail -n %d"
    ) % (_LOG_DIR, _scan_window(lines), uin, uin, int(lines))


def get_account_logs(name: str, lines: int = 100):
    """Return recent log text for virtual account ``name``.

    ``None`` means "not a virtual account" - the caller must use the normal
    Docker log path. Any string (including an empty one) means the virtual path
    was taken, so the caller must not fall back.
    """
    try:
        from services.snowluma import accounts
    except Exception:
        return None

    try:
        entry = accounts().get(name)
    except Exception:
        return None
    if not entry:
        return None

    uin = str(entry.get("uin") or "").strip()
    container = str(entry.get("container") or "").strip()
    if not uin.isdigit() or not container:
        return None
    if docker is None:
        return ""

    try:
        target = docker.from_env().containers.get(container)
        result = target.exec_run(["sh", "-c", _build_command(lines, uin)])
        if result.exit_code != 0:
            return ""
        raw = result.output or b""
        if isinstance(raw, tuple):  # demux=True would return (stdout, stderr)
            raw = raw[0] or b""
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""
