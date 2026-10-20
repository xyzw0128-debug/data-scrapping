"""Minimal Alpha Vantage daily OHLCV backup collector.

Disabled in run_daily.sh by default: it upserts into the same ohlcv table as Twelve Data,
and TIME_SERIES_DAILY prices are not split-adjusted, so enabling it mixes price bases.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.config import ProviderConfig, load_provider_config, load_symbols
from src.env import load_project_env
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.rate_limit import KeyPool, PerMinuteRateLimiter
from src.scheduling import choose_rotation_symbols, provider_entry, record_failure, record_skip, record_success
from src.state import load_state, save_state, utc_now_iso, utc_today
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


def run(args: argparse.Namespace) -> dict:
    ensure_data_dirs(args.data_dir)
    verify_writable(args.data_dir / "state")

    config = load_provider_config(args.providers, args.provider)
    symbols = load_symbols(args.symbols)
    max_symbols = args.max_symbols if args.max_symbols is not None else config.max_symbols_per_run
    state = load_state(args.state)
    if args.dry_run:
        state = copy.deepcopy(state)  # dry-run never writes state
    today = utc_today()

    key_pool = KeyPool.from_env(config, state)
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

    for symbol in choose_rotation_symbols(symbols, state, config.name, max_symbols, today, args.force):
        entry = provider_entry(state, symbol, config.name)
        slot = None if args.dry_run else key_pool.current_slot()
        if not args.dry_run and slot is None:
            record_skip(entry, "all_keys_exhausted")
            summary["skipped_rate_limit"] += 1
            break
        try:
            status, row_count, note = collect_symbol(
                config, args.data_dir, symbol, slot.api_key if slot else "", args.dry_run, rate_limiter,
            )
            if slot is not None:
                slot.budget.spend(1)
            record_success(entry, today, rows=row_count, note=note)
            summary["processed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": status, "rows": row_count})
        except Exception as exc:
            if slot is not None:
                slot.budget.spend(1)
            record_failure(entry, today, str(exc))
            summary["failed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": "failed", "error": str(exc)})

    summary["finished_at"] = utc_now_iso()
    summary["total_remaining_end"] = key_pool.total_remaining()
    summary["key_pool_end"] = key_pool.summary()
    if not args.dry_run:
        state.setdefault("runs", []).append({k: v for k, v in summary.items() if k != "symbols"})
        state["runs"] = state["runs"][-20:]
        save_state(args.state, state)
    return summary


def main() -> int:
    load_project_env(ROOT)
    args = parse_args()
    try:
        with pipeline_lock(args.data_dir):
            summary = run(args)
    except LockHeldError as exc:
        return lock_held_exit(exc)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
