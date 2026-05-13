#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
DRY_RUN="${DRY_RUN:-0}"
TWELVE_MAX_SYMBOLS="${TWELVE_MAX_SYMBOLS:-5}"
FRED_LIMIT="${FRED_LIMIT:-2}"
FINNHUB_LIMIT="${FINNHUB_LIMIT:-2}"
FINNHUB_DAYS_BACK="${FINNHUB_DAYS_BACK:-7}"
RUN_TWELVE="${RUN_TWELVE:-1}"
RUN_INDICATORS="${RUN_INDICATORS:-1}"
RUN_FRED="${RUN_FRED:-1}"
RUN_FINNHUB="${RUN_FINNHUB:-1}"
RUN_SUMMARY="${RUN_SUMMARY:-1}"
SEND_DISCORD="${SEND_DISCORD:-0}"

maybe_dry_run_args=()
if [[ "$DRY_RUN" == "1" ]]; then
  maybe_dry_run_args=(--dry-run)
fi

log_step() {
  printf '\n[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

if [[ "$RUN_TWELVE" == "1" ]]; then
  log_step "Collecting Twelve Data OHLCV"
  "$PYTHON_BIN" -m src.main --provider twelve_data --max-symbols "$TWELVE_MAX_SYMBOLS" "${maybe_dry_run_args[@]}"
fi

if [[ "$RUN_INDICATORS" == "1" ]]; then
  log_step "Calculating local indicators"
  "$PYTHON_BIN" -m src.indicators
fi

if [[ "$RUN_FRED" == "1" ]]; then
  log_step "Collecting FRED macro series"
  "$PYTHON_BIN" -m src.fred --limit "$FRED_LIMIT" "${maybe_dry_run_args[@]}"
fi

if [[ "$RUN_FINNHUB" == "1" ]]; then
  log_step "Collecting Finnhub company news"
  "$PYTHON_BIN" -m src.finnhub_news --limit "$FINNHUB_LIMIT" --days-back "$FINNHUB_DAYS_BACK" "${maybe_dry_run_args[@]}"
fi

if [[ "$RUN_SUMMARY" == "1" ]]; then
  log_step "Generating daily summary"
  summary_args=()
  if [[ "$SEND_DISCORD" == "1" ]]; then
    summary_args=(--send-discord)
  fi
  "$PYTHON_BIN" -m src.summary "${summary_args[@]}"
fi

log_step "Daily run complete"
