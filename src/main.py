"""Command line entrypoint for the Stage 1 market data collector MVP."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.config import ProviderConfig, load_provider_config, load_symbols
from src.lock import FileLock, LockHeldError
from src.logging_utils import setup_logging
from src.rate_limit import DailyBudget, KeyPool
from src.state import ensure_daily_provider_state, load_state, save_state, utc_now_iso, utc_today
from src.storage import (
    ensure_data_dirs,
    normalize_twelve_data_ohlcv,
    save_raw_json,
    upsert_ohlcv_db,
    verify_writable,
    save_run_summary,
)


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect a conservative daily OHLCV batch.")
    parser.add_argument("--provider", default="twelve_data", help="Provider name from config/providers.yaml")
    parser.add_argument("--symbols", type=Path, default=ROOT / "config" / "symbols.txt")
    parser.add_argument("--providers", type=Path, default=ROOT / "config" / "providers.yaml")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "state" / "state.json")
    parser.add_argument("--dry-run", action="store_true", help="Do not call external APIs or write OHLCV data")
    parser.add_argument("--max-symbols", type=int, default=None, help="Override provider max_symbols_per_run")
    parser.add_argument("--force", action="store_true", help="Collect symbols even if they already succeeded today")
    parser.add_argument("--skip-lock", action="store_true", help="Disable the run lock, useful only for local debugging")
    parser.add_argument("--lock-file", type=Path, default=ROOT / "data" / "state" / "collector.lock")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def build_twelve_data_url(config: ProviderConfig, symbol: str, api_key: str) -> str:
    query = urlencode(
        {
            "symbol": symbol,
            "interval": config.interval,
            "outputsize": config.outputsize,
            "apikey": api_key,
        }
    )
    return f"{config.base_url}?{query}"


class PerMinuteRateLimiter:
    """Track API call timestamps and sleep before exceeding a per-minute limit."""

    def __init__(self, per_minute_limit: int) -> None:
        self.per_minute_limit = per_minute_limit
        self.call_timestamps: list[float] = []

    def wait(self) -> None:
        if self.per_minute_limit <= 0:
            return

        while True:
            now = time.monotonic()
            self.call_timestamps = [
                timestamp for timestamp in self.call_timestamps if now - timestamp < 60.0
            ]
            if len(self.call_timestamps) < self.per_minute_limit:
                self.call_timestamps.append(now)
                return

            sleep_seconds = max(0.0, 60.0 - (now - self.call_timestamps[0]))
            time.sleep(sleep_seconds)


def fetch_json(
    url: str,
    attempts: int = 2,
    backoff_seconds: float = 2.0,
    rate_limiter: PerMinuteRateLimiter | None = None,
) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if rate_limiter is not None:
                rate_limiter.wait()
            request = Request(url, headers={"User-Agent": "data-scrapping-mvp/0.1"})
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(backoff_seconds * attempt)
    raise RuntimeError(f"HTTP request failed after {attempts} attempts: {last_error}")


def choose_pending_symbols(symbols: list[str], state: dict, provider: str, max_symbols: int, force: bool) -> list[str]:
    symbol_state = state.setdefault("symbols", {})
    pending: list[tuple[int, str]] = []
    for symbol in symbols:
        details = symbol_state.setdefault(symbol, {})
        provider_details = details.setdefault(provider, {})
        if not force and provider_details.get("last_success_date") == utc_today():
            continue
        status = provider_details.get("status")
        if status == "failed":
            priority = 0
        elif status is None:
            priority = 1
        elif status == "skipped_rate_limit":
            priority = 2
        else:
            priority = 3
        pending.append((priority, symbol))
    pending.sort(key=lambda item: item[0])
    return [symbol for _, symbol in pending[:max_symbols]]


def record_symbol_status(state: dict, provider: str, symbol: str, status: str, **extra: object) -> None:
    provider_details = state.setdefault("symbols", {}).setdefault(symbol, {}).setdefault(provider, {})
    update = {"status": status, "updated_at": utc_now_iso(), **extra}
    if status == "done":
        update["last_success_date"] = utc_today()
        update["last_success_at"] = utc_now_iso()
    provider_details.update(update)


def is_twelve_data_rate_limit_error(error: ValueError) -> bool:
    """Return whether a Twelve Data normalization error is a rate-limit response."""
    return "rate limit" in str(error).lower()


def collect_symbol(
    config: ProviderConfig,
    data_dir: Path,
    symbol: str,
    api_key: str,
    dry_run: bool,
    logger: logging.Logger,
    rate_limiter: PerMinuteRateLimiter | None = None,
) -> tuple[str, int, str | None]:
    if dry_run:
        return "dry_run", 0, None

    if config.name != "twelve_data":
        raise ValueError("Stage 1 MVP currently implements only the twelve_data collector")

    url = build_twelve_data_url(config, symbol, api_key)
    last_raw_path: Path | None = None
    for attempt in range(2):
        logger.info("Collecting %s from %s", symbol, config.name)
        payload = fetch_json(url, rate_limiter=rate_limiter)
        last_raw_path = save_raw_json(data_dir, config.name, symbol, payload)
        try:
            rows = normalize_twelve_data_ohlcv(payload)
        except ValueError as exc:
            if attempt == 0 and is_twelve_data_rate_limit_error(exc):
                logger.warning("Rate-limit response for %s; sleeping 60 seconds before one retry", symbol)
                time.sleep(60)
                continue
            raise

        if not rows:
            raise ValueError("No OHLCV rows found in response")
        db_path = upsert_ohlcv_db(data_dir, symbol, rows)
        return "done", len(rows), f"raw={last_raw_path} db={db_path}"

    raise ValueError(f"No OHLCV rows found in response after retry; raw={last_raw_path}")


def run_collection(args: argparse.Namespace, logger: logging.Logger) -> dict:
    ensure_data_dirs(args.data_dir)
    verify_writable(args.data_dir)
    verify_writable(args.data_dir / "state")

    config = load_provider_config(args.providers, args.provider)
    symbols = load_symbols(args.symbols)
    max_symbols = args.max_symbols if args.max_symbols is not None else config.max_symbols_per_run

    state = load_state(args.state)

    # Build KeyPool — each key gets its own daily state bucket.
    key_pool = KeyPool.from_env(config, state)

    if not args.dry_run and key_pool.current_slot() is None:
        all_keys_exhausted_summary = {
            "started_at": utc_now_iso(),
            "finished_at": utc_now_iso(),
            "provider": config.name,
            "dry_run": False,
            "force": args.force,
            "processed": 0,
            "failed": 0,
            "skipped_rate_limit": 0,
            "status": "all_keys_exhausted",
            "key_pool": key_pool.summary(),
            "symbols": [],
        }
        logger.warning("All API keys are exhausted for today. key_pool=%s", key_pool.summary())
        save_state(args.state, state)
        return all_keys_exhausted_summary

    pending = choose_pending_symbols(symbols, state, config.name, max_symbols, args.force)
    rate_limiter = PerMinuteRateLimiter(config.per_minute_limit)

    summary = {
        "started_at": utc_now_iso(),
        "provider": config.name,
        "dry_run": args.dry_run,
        "force": args.force,
        "processed": 0,
        "failed": 0,
        "skipped_rate_limit": 0,
        "total_remaining_start": key_pool.total_remaining(),
        "key_pool_start": key_pool.summary(),
        "symbols": [],
    }

    # Initialize real-time progress entry in state (current_run)
    last_symbol = None  # tracked directly — no need to re-read from state
    state["current_run"] = {
        "started_at": summary["started_at"],
        "processed": 0,
        "failed": 0,
        "skipped_rate_limit": 0,
        "last_symbol": None,
        "updated_at": utc_now_iso(),
    }
    save_state(args.state, state)

    logger.info(
        "Starting collection provider=%s dry_run=%s pending=%s total_budget=%s key_slots=%s",
        config.name, args.dry_run, len(pending),
        key_pool.total_remaining(), len(key_pool.summary()),
    )

    for symbol in pending:
        # Refresh current slot — key may have rotated since last iteration.
        slot = key_pool.current_slot()
        if not args.dry_run and slot is None:
            last_symbol = symbol
            record_symbol_status(state, config.name, symbol, "skipped_rate_limit", reason="all_keys_exhausted")
            summary["skipped_rate_limit"] += 1

            state["current_run"] = {
                "started_at": summary["started_at"],
                "processed": summary["processed"],
                "failed": summary["failed"],
                "skipped_rate_limit": summary["skipped_rate_limit"],
                "last_symbol": last_symbol,
                "updated_at": utc_now_iso(),
            }
            handled = summary["processed"] + summary["failed"] + summary["skipped_rate_limit"]
            if handled % 5 == 0:
                save_state(args.state, state)

            logger.warning("All keys exhausted; stopping at symbol %s", symbol)
            break

        if args.dry_run:
            api_key = ""
            budget: DailyBudget | None = None
        else:
            api_key, budget = slot  # type: ignore[misc]
            decision = budget.can_spend(1)
            if not decision.allowed:
                # This key is now exhausted; try the next one.
                slot = key_pool.current_slot()
                if slot is None:
                    last_symbol = symbol
                    record_symbol_status(state, config.name, symbol, "skipped_rate_limit", reason="all_keys_exhausted")
                    summary["skipped_rate_limit"] += 1

                    state["current_run"] = {
                        "started_at": summary["started_at"],
                        "processed": summary["processed"],
                        "failed": summary["failed"],
                        "skipped_rate_limit": summary["skipped_rate_limit"],
                        "last_symbol": last_symbol,
                        "updated_at": utc_now_iso(),
                    }
                    handled = summary["processed"] + summary["failed"] + summary["skipped_rate_limit"]
                    if handled % 5 == 0:
                        save_state(args.state, state)

                    logger.warning("All keys exhausted; stopping at symbol %s", symbol)
                    break
                api_key, budget = slot

        try:
            status, row_count, note = collect_symbol(
                config, args.data_dir, symbol, api_key, args.dry_run, logger, rate_limiter,
            )
            if budget is not None:
                budget.spend(1)
            last_symbol = symbol
            record_symbol_status(state, config.name, symbol, status, rows=row_count, note=note)
            summary["processed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": status, "rows": row_count})

            state["current_run"] = {
                "started_at": summary["started_at"],
                "processed": summary["processed"],
                "failed": summary["failed"],
                "skipped_rate_limit": summary["skipped_rate_limit"],
                "last_symbol": last_symbol,
                "updated_at": utc_now_iso(),
            }
            handled = summary["processed"] + summary["failed"] + summary["skipped_rate_limit"]
            if handled % 5 == 0:
                save_state(args.state, state)

        except Exception as exc:
            if budget is not None:
                budget.spend(1)
            last_symbol = symbol
            record_symbol_status(state, config.name, symbol, "failed", error=str(exc))
            summary["failed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": "failed", "error": str(exc)})

            state["current_run"] = {
                "started_at": summary["started_at"],
                "processed": summary["processed"],
                "failed": summary["failed"],
                "skipped_rate_limit": summary["skipped_rate_limit"],
                "last_symbol": last_symbol,
                "updated_at": utc_now_iso(),
            }
            handled = summary["processed"] + summary["failed"] + summary["skipped_rate_limit"]
            if handled % 5 == 0:
                save_state(args.state, state)

            logger.exception("Failed to collect %s", symbol)

    # End of loop: ensure final progress is saved before moving current_run to runs
    state["current_run"] = {
        "started_at": summary["started_at"],
        "processed": summary["processed"],
        "failed": summary["failed"],
        "skipped_rate_limit": summary["skipped_rate_limit"],
        "last_symbol": last_symbol,
        "updated_at": utc_now_iso(),
    }
    save_state(args.state, state)

    # Prepare final summary and store in runs (only completed runs)
    summary["finished_at"] = utc_now_iso()
    summary["total_remaining_end"] = key_pool.total_remaining()
    summary["key_pool_end"] = key_pool.summary()
    state.setdefault("runs", []).append(summary)
    state["runs"] = state["runs"][-20:]

    # remove current_run and persist final state
    state.pop("current_run", None)
    save_state(args.state, state)

    summary_path = save_run_summary(args.data_dir, summary)
    summary["summary_path"] = str(summary_path)
    logger.info(
        "Finished collection processed=%s failed=%s skipped=%s total_remaining=%s",
        summary["processed"], summary["failed"],
        summary["skipped_rate_limit"], key_pool.total_remaining(),
    )
    return summary


def main() -> int:
    args = parse_args()
    logger = setup_logging(args.data_dir / "logs", args.verbose)

    try:
        if args.skip_lock:
            summary = run_collection(args, logger)
        else:
            with FileLock(args.lock_file):
                summary = run_collection(args, logger)
    except LockHeldError as exc:
        logger.error("%s", exc)
        print(json.dumps({"status": "lock_held", "error": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
