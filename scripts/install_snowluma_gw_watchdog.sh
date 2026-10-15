#!/bin/bash
# Install / re-arm the SnowLuma gateway watchdog + fix-guard on a SnowLuma node.
#
# Run as ROOT on the VM that hosts the `snowluma` container.
#
#   scp snowluma-gw-watchdog.sh install_snowluma_gw_watchdog.sh root@<vm>:/tmp/
#   ssh root@<vm> 'bash /tmp/install_snowluma_gw_watchdog.sh'
#
# Idempotent. What it does:
#   1. install /usr/local/bin/snowluma-gw-watchdog.sh (0755 root:root)
#   2. ARM the guard: stage <host /app/data>/_patches/index.mjs.fixed plus the
#      pristine vendor backup <...>/index.mjs.orig-*. A guard with no restore
#      source is disarmed - that was the exact state of the Suzhou node before
#      2026-10-15 14:10 UTC.
#   3. install the */5 root cron entry
#   4. verify (bash -n, md5 of the staged bundle, node --check, guard no-op run)
#
# Background: docs/SNOWLUMA-GATEWAY-JITTER-FIX.md
set -u

CONTAINER=snowluma
PROGRAM=snowluma
FIXED_MD5=567aa9d453ec13897e0fe7bc2da5a680
ORIG_MD5=6d699508706792d353887bb0aebdc3ab
WATCHDOG_DST=/usr/local/bin/snowluma-gw-watchdog.sh
CRON_LINE='*/5 * * * * /usr/local/bin/snowluma-gw-watchdog.sh >> /var/log/snowluma-gw-watchdog.log 2>&1'
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" = "0" ] || { echo "!! must run as root"; exit 1; }

echo "== 1/4 install watchdog =="
WATCHDOG_SRC="$SRC_DIR/snowluma-gw-watchdog.sh"
[ -f "$WATCHDOG_SRC" ] || WATCHDOG_SRC=/tmp/snowluma-gw-watchdog.sh
[ -f "$WATCHDOG_SRC" ] || { echo "!! watchdog source not found next to this script nor in /tmp"; exit 1; }
install -m 0755 -o root -g root "$WATCHDOG_SRC" "$WATCHDOG_DST" || exit 1
bash -n "$WATCHDOG_DST" || { echo "!! syntax check failed"; exit 1; }
md5sum "$WATCHDOG_DST"

echo "== 2/4 arm the guard =="
DATA_SRC=$(docker inspect "$CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}')
[ -n "$DATA_SRC" ] || { echo "!! container $CONTAINER has no /app/data mount"; exit 1; }
echo "host /app/data -> $DATA_SRC"
mkdir -p "$DATA_SRC/_patches"
chown --reference="$DATA_SRC" "$DATA_SRC/_patches" 2>/dev/null || true

cur=$(docker exec "$CONTAINER" md5sum /app/runtime/index.mjs 2>/dev/null | awk '{print $1}')
echo "container bundle md5: $cur"
if [ "$cur" = "$ORIG_MD5" ]; then
  echo "!! container is running the PRISTINE bundle - apply the fix first:"
  echo "     docker cp apply_snowluma_gateway_fix.js $CONTAINER:/tmp/"
  echo "     docker exec $CONTAINER node /tmp/apply_snowluma_gateway_fix.js"
  echo "     docker exec $CONTAINER supervisorctl restart $PROGRAM"
  exit 1
fi
if [ "$cur" != "$FIXED_MD5" ]; then
  echo "!! bundle md5 is neither the pristine nor the known patched build - refusing to arm"
  exit 1
fi

docker exec "$CONTAINER" cp /app/runtime/index.mjs /app/data/_patches/index.mjs.fixed || exit 1
if ! docker exec "$CONTAINER" sh -c 'ls /app/data/_patches/index.mjs.orig-* >/dev/null 2>&1'; then
  bak=$(docker exec "$CONTAINER" sh -c 'ls -1 /app/runtime/index.mjs.bak-* 2>/dev/null | head -1')
  if [ -n "$bak" ]; then
    echo "staging pristine backup from $bak"
    docker exec "$CONTAINER" cp "$bak" /app/data/_patches/index.mjs.orig-20261014-161714
  else
    echo "!! no pristine backup available (.bak-* and orig-* both absent)"
    echo "!! the guard can restore the patch but cannot roll back to vendor"
  fi
fi
echo "--- staged:"
docker exec "$CONTAINER" sh -c 'md5sum /app/data/_patches/index.mjs.fixed /app/data/_patches/index.mjs.orig-* 2>&1'
staged=$(docker exec "$CONTAINER" md5sum /app/data/_patches/index.mjs.fixed | awk '{print $1}')
[ "$staged" = "$FIXED_MD5" ] || { echo "!! staged fixed bundle md5 mismatch"; exit 1; }
docker exec "$CONTAINER" sh -c 'cp /app/data/_patches/index.mjs.fixed /tmp/_arm_check.mjs && node --check /tmp/_arm_check.mjs && rm -f /tmp/_arm_check.mjs' \
  || { echo "!! staged bundle fails node --check"; exit 1; }

echo "== 3/4 install cron =="
tmpct=$(mktemp)
crontab -l 2>/dev/null > "$tmpct" || true
if grep -q 'snowluma-gw-watchdog' "$tmpct"; then
  echo "cron entry already present"
else
  echo "$CRON_LINE" >> "$tmpct"
  crontab "$tmpct" && echo "cron installed"
fi
rm -f "$tmpct"
crontab -l 2>/dev/null | grep 'snowluma-gw-watchdog' >/dev/null || { echo "!! cron entry missing"; exit 1; }

echo "== 4/4 verify =="
"$WATCHDOG_DST"; echo "guard run exit=$? (0 = armed and nothing to do)"
tail -4 /var/log/snowluma-gw-watchdog.log 2>/dev/null
docker exec "$CONTAINER" md5sum /app/runtime/index.mjs
docker exec "$CONTAINER" supervisorctl status "$PROGRAM"
echo "== OK =="
