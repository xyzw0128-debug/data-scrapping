#!/usr/bin/env bash
# Install the collector on this machine as systemd *user* units (no sudo).
#
#   scripts/install_local.sh                 # venv + packages + units, enable the collection timer
#   ENABLE_TIMER=0 scripts/install_local.sh  # install units without enabling them
#   ENABLE_BACKUP=1 scripts/install_local.sh # also enable the rclone backup timer (configure rclone first)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ENABLE_TIMER="${ENABLE_TIMER:-1}"
ENABLE_BACKUP="${ENABLE_BACKUP:-0}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
VENV_PY="$ROOT_DIR/.venv/bin/python"

cd "$ROOT_DIR"
echo "Repository: $ROOT_DIR"

if [[ ! -x "$VENV_PY" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 "$ROOT_DIR/.venv"
  else
    "$PYTHON_BIN" -m venv "$ROOT_DIR/.venv"
  fi
fi
if ! "$VENV_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Error: .venv needs Python 3.11 or newer." >&2
  exit 1
fi

if "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  "$VENV_PY" -m pip install -r requirements.txt
elif command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV_PY" -r requirements.txt
else
  "$VENV_PY" -m ensurepip --upgrade
  "$VENV_PY" -m pip install -r requirements.txt
fi

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  chmod 600 "$ROOT_DIR/.env"
  echo "Created .env from .env.example; add the real API keys before the first run."
fi

mkdir -p "$UNIT_DIR"
for unit in data-scrapping.service data-scrapping.timer data-scrapping-backup.service data-scrapping-backup.timer; do
  sed "s|@ROOT@|$ROOT_DIR|g" "$ROOT_DIR/systemd/$unit" > "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload

if [[ "$ENABLE_TIMER" == "1" ]]; then
  systemctl --user enable --now data-scrapping.timer
fi
if [[ "$ENABLE_BACKUP" == "1" ]]; then
  systemctl --user enable --now data-scrapping-backup.timer
fi

if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]]; then
  echo "Note: user lingering is off, so timers run only while you are logged in."
  echo "      To run them without a login session: sudo loginctl enable-linger $USER"
fi

echo
echo "Installed units in $UNIT_DIR"
systemctl --user list-timers 'data-scrapping*' --all --no-pager || true
