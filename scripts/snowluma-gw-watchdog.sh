#!/bin/bash
# SnowLuma gateway watchdog + fix-guard - updated 2026-10-15 (Akiyo ops).
#
# NOTE: this file is intentionally pure ASCII. It is copied between Windows and
# Linux hosts, and non-ASCII bytes do not survive that trip reliably.
#
# Job 0 - ARMING CHECK:
#   Verify the restore source exists. If it is missing the guard is disarmed
#   and says so loudly instead of silently doing nothing.
#
# Job 1 - FIX GUARD:
#   Keep /app/runtime/index.mjs at the PATCHED build so the session-jitter fix
#   survives a container recreate (the file lives in the image layer).
#   Patched bundle is persisted on the host at:
#     <host /app/data>/_patches/index.mjs.fixed   (md5 567aa9d4...)
#   Pristine vendor build backup:
#     <host /app/data>/_patches/index.mjs.orig-*  (md5 6d699508...)
#
# Job 2 - CHURN WATCHDOG:
#   Root cause of the old "session 8-second jitter" (fixed 2026-10-15): a
#   700-800ms Node event-loop stall made the 250ms pipe-liveness probe time out
#   for ALL pipes at once -> livePipes came back empty -> pipe-down for every
#   session -> mass rebuild -> more stalls (feedback loop). Fixed by raising the
#   probe timeout to 2000ms, the poll interval to 3000ms, and never treating an
#   all-empty probe result as authoritative.
#   If churn still appears, restart ONLY the gateway program (QQ clients are
#   never touched).
#
# Hardening note (2026-10-15): the log redirect used to sit directly on the
# restore command (docker exec ... cp ... 2>>"$LOG"). If $LOG was not writable,
# bash failed to open the redirect, the command never ran, and the guard
# silently skipped the restore. All log writes now go through log(), which can
# never gate a corrective action; command output is captured and logged
# separately.
set -u
LOG=/var/log/snowluma-gw-watchdog.log
STATE=/var/run/snowluma-gw-watchdog.last
CONTAINER=snowluma
PROGRAM=snowluma
FIXED_MD5=567aa9d453ec13897e0fe7bc2da5a680
ORIG_MD5=6d699508706792d353887bb0aebdc3ab
THRESHOLD=30        # session restarts within 60s (normal=0, churn~140)
COOLDOWN=600        # min seconds between automatic restarts

ts() { date '+%F %T %Z'; }

LOG_OK=0
if { printf '' >>"$LOG"; } 2>/dev/null; then LOG_OK=1; fi

log() {
  if [ "$LOG_OK" = "1" ]; then
    printf '%s %s\n' "$(ts)" "$*" >>"$LOG" 2>/dev/null && return 0
  fi
  logger -t snowluma-gw-watchdog "$*" 2>/dev/null || true
}

dexec() { docker exec "$CONTAINER" "$@" 2>&1; }

# ---- Job 0: is the guard armed? --------------------------------------------
if ! dexec test -f /app/data/_patches/index.mjs.fixed >/dev/null 2>&1; then
  log "FIX GUARD: *** DISARMED - restore source missing: /app/data/_patches/index.mjs.fixed ***"
fi

# ---- Job 1: fix guard ------------------------------------------------------
cur=$(dexec md5sum /app/runtime/index.mjs 2>/dev/null | awk '{print $1}')
if [ "$cur" = "$ORIG_MD5" ]; then
  log "FIX GUARD: bundle is pristine ($cur) -> restoring patched build"
  out=$(dexec cp /app/data/_patches/index.mjs.fixed /app/runtime/index.mjs); rc=$?
  log "FIX GUARD: cp rc=$rc ${out}"
  if [ "$rc" -eq 0 ] && dexec node --check /app/runtime/index.mjs >/dev/null 2>&1; then
    out=$(dexec supervisorctl restart "$PROGRAM"); rc=$?
    log "FIX GUARD: restart rc=$rc ${out}"
    log "FIX GUARD: patched bundle restored + gateway restarted; QQ clients untouched"
    exit 0
  fi
  log "FIX GUARD: restore FAILED -> reverting to pristine backup"
  out=$(dexec sh -c 'cp /app/data/_patches/index.mjs.orig-* /app/runtime/index.mjs'); rc=$?
  log "FIX GUARD: revert rc=$rc ${out}"
  out=$(dexec supervisorctl restart "$PROGRAM"); rc=$?
  log "FIX GUARD: restart rc=$rc ${out}"
  exit 1
fi

# ---- Job 2: churn watchdog -------------------------------------------------
if ! dexec supervisorctl status "$PROGRAM" 2>/dev/null | grep -q RUNNING; then
  log "gateway NOT RUNNING -> starting"
  out=$(dexec supervisorctl start "$PROGRAM"); rc=$?
  log "start rc=$rc ${out}"
  exit 0
fi

n=$(docker logs "$CONTAINER" --since 60s 2>&1 | grep -c 'session started: UIN=')
n=${n:-0}
if [ "$n" -lt "$THRESHOLD" ]; then
  exit 0
fi

now=$(date +%s)
last=0
[ -f "$STATE" ] && last=$(cat "$STATE" 2>/dev/null || echo 0)
case "$last" in ''|*[!0-9]*) last=0 ;; esac
if [ $((now - last)) -lt "$COOLDOWN" ]; then
  log "churn=$n but only $((now-last))s since last restart (cooldown ${COOLDOWN}s) - skipping"
  exit 0
fi

log "CHURN DETECTED: session-started=$n in 60s (threshold $THRESHOLD) -> restarting gateway"
out=$(dexec supervisorctl restart "$PROGRAM"); rc=$?
log "restart rc=$rc ${out}"
echo "$now" > "$STATE" 2>/dev/null || true
log "gateway restarted; QQ clients untouched"

