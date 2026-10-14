#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="ncqq-manager"
SERVICE_USER="${SUDO_USER:-$USER}"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.service"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ -x "${VENV_PYTHON}" ]]; then
  PYTHON_BIN="${VENV_PYTHON}"
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo 运行: sudo bash scripts/install_autostart_ubuntu.sh"
  exit 1
fi

if [[ -z "${UV_BIN}" ]]; then
  echo "未找到 uv，请先安装：https://docs.astral.sh/uv/"
  exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "未找到 Python: ${PYTHON_BIN}"
  exit 1
fi

mkdir -p "${PROJECT_DIR}/config/logs"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=NCQQ Manager
After=network.target docker.service
Wants=network.target docker.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=PYTHONIOENCODING=utf-8
Environment=UV_PROJECT_ENVIRONMENT=${PROJECT_DIR}/.venv
ExecStart=${UV_BIN} run ${PYTHON_BIN} ${PROJECT_DIR}/start.py --skip-build
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "已安装并启动 systemd 服务: ${SERVICE_NAME}"
echo "uv 路径: ${UV_BIN}"
echo "python 路径: ${PYTHON_BIN}"
echo "启动命令: ${UV_BIN} run ${PYTHON_BIN} ${PROJECT_DIR}/start.py --skip-build"
echo "查看状态: sudo systemctl status ${SERVICE_NAME}"
echo "查看日志: sudo journalctl -u ${SERVICE_NAME} -f"

