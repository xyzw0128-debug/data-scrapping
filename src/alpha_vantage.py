"""Minimal Alpha Vantage daily OHLCV backup collector."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.config import ProviderConfig, load_provider_config, load_symbols
from src.rate_limit import DailyBudget, KeyPool
from src.state import ensure_daily_provider_state, load_state, save_state, utc_now_iso, utc_today
from src.storage import ensure_data_dirs, save_raw_json, upsert_ohlcv_db, verify_writable


ROOT = Path(__file__).resolve().parents[1]
TIME_SERIES_DAILY_KEY = "Time Series (Daily)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect conservative Alpha Vantage daily OHLCV backup data.")
    parser.add_argument("--provider", default="alpha_vantage")
    parser.add_argument("--symbols", type=Path, default=ROOT / "config" / "symbols.txt")
    parser.add_argument("--providers", type=Path, default=ROOT / "config" / "providers.yaml")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "state" / "state.json")
    parser.add_argument("--dry-run", action="store_true", help="Do not call Alpha Vantage or write OHLCV CSV files")
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Collect symbols even if Alpha Vantage already succeeded today")
    return parser.parse_args()


def build_alpha_vantage_url(config: ProviderConfig, symbol: str, api_key: str) -> str:
    """Build Alpha Vantage TIME_SERIES_DAILY URL."""
    query = urlencode(
        {
            "function": "TIME_SERIES_DAILY",
            "symbol": symbol,
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


def fetch_json(url: str, rate_limiter: PerMinuteRateLimiter | None = None) -> dict:
    """Fetch JSON from Alpha Vantage."""
    if rate_limiter is not None:
        rate_limiter.wait()
    request = Request(url, headers={"User-Agent": "data-scrapping-alpha-vantage-mvp/0.1"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_alpha_vantage_ohlcv(payload: dict) -> list[dict[str, str]]:
    """Normalize Alpha Vantage TIME_SERIES_DAILY response into OHLCV rows."""
    if "Note" in payload:
        raise ValueError(f"Alpha Vantage rate limit note: {payload['Note']}")
    if "Information" in payload:
        raise ValueError(f"Alpha Vantage information response: {payload['Information']}")
    if "Error Message" in payload:
        raise ValueError(f"Alpha Vantage error: {payload['Error Message']}")

    series = payload.get(TIME_SERIES_DAILY_KEY)
    if not isinstance(series, dict):
        raise ValueError("Alpha Vantage response missing Time Series (Daily)")

    rows: list[dict[str, str]] = []
    for date_value, values in series.items():
        if not isinstance(values, dict):
            continue
        rows.append(
            {
                "date": str(date_value),
                "open": str(values.get("1. open", "")),
                "high": str(values.get("2. high", "")),
                "low": str(values.get("3. low", "")),
                "close": str(values.get("4. close", "")),
                "volume": str(values.get("5. volume", "")),
            }
        )
    rows.sort(key=lambda row: row["date"])
    return rows


def choose_pending_symbols(symbols: list[str], state: dict, provider: str, max_symbols: int, force: bool) -> list[str]:
    """Choose symbols not yet completed today, retrying failures first."""
    symbol_state = state.setdefault("symbols", {})
    pending: list[tuple[int, str]] = []
    for symbol in symbols:
        provider_details = symbol_state.setdefault(symbol, {}).setdefault(provider, {})
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
    """Record per-symbol Alpha Vantage status."""
    provider_details = state.setdefault("symbols", {}).setdefault(symbol, {}).setdefault(provider, {})
    update = {"status": status, "updated_at": utc_now_iso(), **extra}
    if status == "done":
        update["last_success_date"] = utc_today()
        update["last_success_at"] = utc_now_iso()
    provider_details.update(update)


def collect_symbol(
    config: ProviderConfig,
    data_dir: Path,
    symbol: str,
    api_key: str,
    dry_run: bool,
    rate_limiter: PerMinuteRateLimiter | None = None,
) -> tuple[str, int, str | None]:
    """Collect one symbol from Alpha Vantage."""
    if dry_run:
        return "dry_run", 0, None
    payload = fetch_json(build_alpha_vantage_url(config, symbol, api_key), rate_limiter=rate_limiter)
    raw_path = save_raw_json(data_dir, config.name, symbol, payload)
    rows = normalize_alpha_vantage_ohlcv(payload)
    if not rows:
        raise ValueError("No Alpha Vantage OHLCV rows found in response")
    db_path = upsert_ohlcv_db(data_dir, symbol, rows)
    return "done", len(rows), f"raw={raw_path} db={db_path}"


def main() -> int:
    args = parse_args()
    ensure_data_dirs(args.data_dir)
    verify_writable(args.data_dir / "state")

    config = load_provider_config(args.providers, args.provider)
    symbols = load_symbols(args.symbols)
    max_symbols = args.max_symbols if args.max_symbols is not None else config.max_symbols_per_run
    state = load_state(args.state)

    # Build KeyPool for sequential key rotation.
    key_pool = KeyPool.from_env(config, state)

    if not args.dry_run and key_pool.current_slot() is None:
        summary = {
            "started_at": utc_now_iso(),
            "finished_at": utc_now_iso(),
            "provider": config.name,
            "dry_run": False,
            "status": "all_keys_exhausted",
            "key_pool": key_pool.summary(),
            "processed": 0,
            "failed": 0,
            "skipped_rate_limit": 0,
            "symbols": [],
        }
        save_state(args.state, state)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    rate_limiter = PerMinuteRateLimiter(config.per_minute_limit)
    summary = {
        "started_at": utc_now_iso(),
        "provider": config.name,
        "dry_run": args.dry_run,
        "processed": 0,
        "failed": 0,
        "skipped_rate_limit": 0,
        "total_remaining_start": key_pool.total_remaining(),
        "key_pool_start": key_pool.summary(),
        "symbols": [],
    }

    for symbol in choose_pending_symbols(symbols, state, config.name, max_symbols, args.force):
        slot = key_pool.current_slot()
        if not args.dry_run and slot is None:
            record_symbol_status(state, config.name, symbol, "skipped_rate_limit", reason="all_keys_exhausted")
            summary["skipped_rate_limit"] += 1
            break

        if args.dry_run:
            api_key = ""
            budget: DailyBudget | None = None
        else:
            api_key, budget = slot  # type: ignore[misc]
            decision = budget.can_spend(1)
            if not decision.allowed:
                slot = key_pool.current_slot()
                if slot is None:
                    record_symbol_status(state, config.name, symbol, "skipped_rate_limit", reason="all_keys_exhausted")
                    summary["skipped_rate_limit"] += 1
                    break
                api_key, budget = slot

        try:
            status, row_count, note = collect_symbol(
                config, args.data_dir, symbol, api_key, args.dry_run, rate_limiter,
            )
            if budget is not None:
                budget.spend(1)
            record_symbol_status(state, config.name, symbol, status, rows=row_count, note=note)
            summary["processed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": status, "rows": row_count})
        except Exception as exc:
            if budget is not None:
                budget.spend(1)
            record_symbol_status(state, config.name, symbol, "failed", error=str(exc))
            summary["failed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": "failed", "error": str(exc)})

    summary["finished_at"] = utc_now_iso()
    summary["total_remaining_end"] = key_pool.total_remaining()
    summary["key_pool_end"] = key_pool.summary()
    state.setdefault("runs", []).append(summary)
    state["runs"] = state["runs"][-20:]
    save_state(args.state, state)

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())