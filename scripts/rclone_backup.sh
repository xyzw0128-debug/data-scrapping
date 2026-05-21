#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RCLONE_BIN="${RCLONE_BIN:-rclone}"
RCLONE_DEST="${RCLONE_DEST:-}"
RCLONE_DRY_RUN="${RCLONE_DRY_RUN:-0}"
BACKUP_DATA_DIR="${BACKUP_DATA_DIR:-data}"
BACKUP_MODE="${BACKUP_MODE:-copy}"

if [[ -z "$RCLONE_DEST" ]]; then
  echo "RCLONE_DEST is required, e.g. remote:data-scrapping" >&2
  exit 2
fi

if ! command -v "$RCLONE_BIN" >/dev/null 2>&1; then
  echo "rclone binary not found: $RCLONE_BIN" >&2
  exit 127
fi

args=(
  --fast-list
  --transfers "${RCLONE_TRANSFERS:-4}"
  --checkers "${RCLONE_CHECKERS:-8}"
  --exclude "state/collector.lock"
  --exclude "logs/collector.log"
)

if [[ "$RCLONE_DRY_RUN" == "1" ]]; then
  args+=(--dry-run)
fi

case "$BACKUP_MODE" in
  copy)
    "$RCLONE_BIN" copy "$BACKUP_DATA_DIR" "$RCLONE_DEST" "${args[@]}"
    ;;
  sync)
    "$RCLONE_BIN" sync "$BACKUP_DATA_DIR" "$RCLONE_DEST" "${args[@]}"
    ;;
  *)
    echo "Unsupported BACKUP_MODE=$BACKUP_MODE. Use copy or sync." >&2
    exit 2
    ;;
esac
