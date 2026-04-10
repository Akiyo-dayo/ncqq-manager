#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="ncqq-manager"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo 运行: sudo bash scripts/uninstall_autostart_ubuntu.sh"
  exit 1
fi

if systemctl list-unit-files | grep -q "^${SERVICE_NAME}\.service"; then
  systemctl disable --now "${SERVICE_NAME}" || true
fi

rm -f "${SERVICE_FILE}"
systemctl daemon-reload
systemctl reset-failed

echo "已卸载 systemd 服务: ${SERVICE_NAME}"
echo "验证: systemctl status ${SERVICE_NAME}"

