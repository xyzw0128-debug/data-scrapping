#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
DRY_RUN="${DRY_RUN:-0}"
TWELVE_MAX_SYMBOLS="${TWELVE_MAX_SYMBOLS:-700}"
# Empty FRED_LIMIT collects every series in config/fred_series.txt.
FRED_LIMIT="${FRED_LIMIT:-}"
# Finnhub rotates through symbols (least recently fetched first), so a few hundred per run covers the list over time.
FINNHUB_LIMIT="${FINNHUB_LIMIT:-300}"
FINNHUB_DAYS_BACK="${FINNHUB_DAYS_BACK:-30}"
ALPHA_VANTAGE_MAX_SYMBOLS="${ALPHA_VANTAGE_MAX_SYMBOLS:-2}"
RUN_FETCH_TICKERS="${RUN_FETCH_TICKERS:-0}"
RUN_TWELVE="${RUN_TWELVE:-1}"
RUN_ALPHA_VANTAGE="${RUN_ALPHA_VANTAGE:-0}"
RUN_INDICATORS="${RUN_INDICATORS:-1}"
RUN_FRED="${RUN_FRED:-1}"
RUN_FINNHUB="${RUN_FINNHUB:-1}"
RUN_SUMMARY="${RUN_SUMMARY:-1}"
RUN_DUCKDB_VALIDATE="${RUN_DUCKDB_VALIDATE:-1}"
SEND_DISCORD="${SEND_DISCORD:-0}"
DISCORD_STATUS_REFRESH_SEC="${DISCORD_STATUS_REFRESH_SEC:-15}"
RUN_BACKUP="${RUN_BACKUP:-0}"

maybe_dry_run_args=()
if [[ "$DRY_RUN" == "1" ]]; then
  maybe_dry_run_args=(--dry-run)
fi

log_step() {
  printf '\n[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

# Stages are independent: a failure (for example one news symbol erroring) is recorded
# and the remaining stages still run. The script exits non-zero at the end if any failed.
failed_stages=()
run_stage() {
  local name="$1"
  shift
  local status=0
  "$@" || status=$?
  if [[ "$status" -ne 0 ]]; then
    printf '[%s] stage "%s" failed with exit code %s; continuing\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$name" "$status" >&2
    failed_stages+=("$name")
  fi
}

update_discord_status() {
  if [[ "$SEND_DISCORD" == "1" ]]; then
    "$PYTHON_BIN" -m src.discord_status "$@"
  fi
}

status_monitor_pid=""
start_status_monitor() {
  if [[ "$SEND_DISCORD" != "1" ]]; then
    return
  fi
  (
    while true; do
      "$PYTHON_BIN" -m src.discord_status --refresh-progress >/dev/null 2>&1 || true
      sleep "$DISCORD_STATUS_REFRESH_SEC"
    done
  ) &
  status_monitor_pid="$!"
}

stop_status_monitor() {
  if [[ -n "$status_monitor_pid" ]]; then
    kill "$status_monitor_pid" >/dev/null 2>&1 || true
    wait "$status_monitor_pid" >/dev/null 2>&1 || true
    status_monitor_pid=""
  fi
}

trap stop_status_monitor EXIT

update_discord_status --status "대기중" --set-start --current-symbol "없음"

if [[ "$RUN_FETCH_TICKERS" == "1" ]]; then
  log_step "Fetching Twelve Data ticker list"
  run_stage fetch_tickers "$PYTHON_BIN" -m src.fetch_tickers "${maybe_dry_run_args[@]}"
fi

if [[ "$RUN_TWELVE" == "1" ]]; then
  log_step "Collecting Twelve Data OHLCV"
  update_discord_status --status "수집 중" --current-symbol "twelve_data"
  start_status_monitor
  run_stage twelve_data "$PYTHON_BIN" -m src.main --provider twelve_data --max-symbols "$TWELVE_MAX_SYMBOLS" "${maybe_dry_run_args[@]}"
  stop_status_monitor
  update_discord_status --status "수집 후처리 중" --current-symbol "없음" --refresh-progress
fi

if [[ "$RUN_ALPHA_VANTAGE" == "1" ]]; then
  log_step "Collecting Alpha Vantage OHLCV backup"
  run_stage alpha_vantage "$PYTHON_BIN" -m src.alpha_vantage --max-symbols "$ALPHA_VANTAGE_MAX_SYMBOLS" "${maybe_dry_run_args[@]}"
fi

if [[ "$RUN_INDICATORS" == "1" ]]; then
  log_step "Calculating local indicators (symbols whose OHLCV changed)"
  run_stage indicators "$PYTHON_BIN" -m src.indicators
fi

if [[ "$RUN_FRED" == "1" ]]; then
  log_step "Collecting FRED macro series"
  fred_args=()
  if [[ -n "$FRED_LIMIT" ]]; then
    fred_args=(--limit "$FRED_LIMIT")
  fi
  run_stage fred "$PYTHON_BIN" -m src.fred "${fred_args[@]}" "${maybe_dry_run_args[@]}"
fi

if [[ "$RUN_FINNHUB" == "1" ]]; then
  log_step "Collecting Finnhub company news"
  run_stage finnhub_news "$PYTHON_BIN" -m src.finnhub_news --limit "$FINNHUB_LIMIT" --days-back "$FINNHUB_DAYS_BACK" "${maybe_dry_run_args[@]}"
fi

if [[ "$RUN_SUMMARY" == "1" ]]; then
  log_step "Generating daily summary"
  update_discord_status --status "요약 생성 중" --current-symbol "summary"
  summary_args=()
  if [[ "$SEND_DISCORD" == "1" ]]; then
    summary_args=(--send-discord)
  fi
  run_stage summary "$PYTHON_BIN" -m src.summary "${summary_args[@]}"
fi

if [[ "$RUN_DUCKDB_VALIDATE" == "1" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    log_step "Skipping DuckDB validation (DRY_RUN=1)"
  else
    log_step "Running DuckDB validation"
    update_discord_status --status "검증 중" --current-symbol "validate_duckdb"
    run_stage validate_duckdb "$PYTHON_BIN" -m src.validate_duckdb
  fi
fi

if [[ "$RUN_BACKUP" == "1" ]]; then
  update_discord_status --status "백업 중" --backup-start --current-symbol "rclone_backup"
  if [[ "$DRY_RUN" == "1" ]]; then
    scripts/rclone_backup.sh >/dev/null 2>&1 || true
  else
    scripts/rclone_backup.sh || update_discord_status --error "rclone 백업 실패"
  fi
  update_discord_status --status "완료" --backup-finish --current-symbol "없음" --refresh-progress
else
  update_discord_status --status "완료" --current-symbol "없음" --refresh-progress
fi

if (( ${#failed_stages[@]} > 0 )); then
  update_discord_status --error "실패 단계: ${failed_stages[*]}"
  log_step "Daily run complete with failed stages: ${failed_stages[*]}"
  exit 1
fi

log_step "Daily run complete"
