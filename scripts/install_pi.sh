#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Error: $PYTHON_BIN not found. Python 3.11 or newer is required." >&2
  exit 1
fi

PYTHON_VERSION="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! $PYTHON_BIN -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Error: Python 3.11 or newer is required. Found ${PYTHON_VERSION}." >&2
  exit 1
fi

echo "Using repository at: $ROOT_DIR"

"$PYTHON_BIN" -m pip install -r "$ROOT_DIR/requirements.txt" --break-system-packages

install_service_with_root() {
  local src="$1"
  local dest="$2"
  sudo sed "s|/workspace/data-scrapping|$ROOT_DIR|g" "$src" > "$dest"
}

install_timer_file() {
  local src="$1"
  local dest="$2"
  sudo cp "$src" "$dest"
}

install_service_with_root "$ROOT_DIR/systemd/data-scrapping.service" "/etc/systemd/system/data-scrapping.service"
install_service_with_root "$ROOT_DIR/systemd/data-scrapping-backup.service" "/etc/systemd/system/data-scrapping-backup.service"
install_timer_file "$ROOT_DIR/systemd/data-scrapping.timer" "/etc/systemd/system/data-scrapping.timer"
install_timer_file "$ROOT_DIR/systemd/data-scrapping-backup.timer" "/etc/systemd/system/data-scrapping-backup.timer"

if [[ ! -f /etc/data-scrapping.env ]]; then
  sudo cp "$ROOT_DIR/systemd/data-scrapping.env.example" /etc/data-scrapping.env
  echo "Created /etc/data-scrapping.env from example template."
else
  echo "Keeping existing /etc/data-scrapping.env (not overwritten)."
fi

sudo systemctl daemon-reload
sudo systemctl enable data-scrapping.timer
sudo systemctl enable data-scrapping-backup.timer

echo
echo "Install complete."
echo "Repository path: $ROOT_DIR"
echo "Set real API keys in /etc/data-scrapping.env before production runs."
echo "rclone is not installed by this script. Install and configure it separately if backup timers are used."
