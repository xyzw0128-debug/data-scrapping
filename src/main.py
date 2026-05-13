"""Command line entrypoint for the Stage 1 market data collector MVP."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.config import ProviderConfig, load_provider_config, load_symbols
from src.rate_limit import DailyBudget
from src.state import ensure_daily_provider_state, load_state, save_state, utc_now_iso
from src.storage import (
    ensure_data_dirs,
    normalize_twelve_data_ohlcv,
    save_raw_json,
    upsert_ohlcv_csv,
    verify_writable,
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


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "data-scrapping-mvp/0.1"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def choose_pending_symbols(symbols: list[str], state: dict, provider: str, max_symbols: int) -> list[str]:
    symbol_state = state.setdefault("symbols", {})
    pending = []
    for symbol in symbols:
        details = symbol_state.setdefault(symbol, {})
        provider_details = details.setdefault(provider, {})
        if provider_details.get("status") == "done":
            continue
        pending.append(symbol)
    return pending[:max_symbols]


def record_symbol_status(state: dict, provider: str, symbol: str, status: str, **extra: object) -> None:
    provider_details = state.setdefault("symbols", {}).setdefault(symbol, {}).setdefault(provider, {})
    provider_details.update({"status": status, "updated_at": utc_now_iso(), **extra})


def collect_symbol(
    config: ProviderConfig,
    data_dir: Path,
    symbol: str,
    api_key: str,
    dry_run: bool,
) -> tuple[str, int, str | None]:
    if dry_run:
        return "dry_run", 0, None

    if config.name != "twelve_data":
        raise ValueError("Stage 1 MVP currently implements only the twelve_data collector")

    payload = fetch_json(build_twelve_data_url(config, symbol, api_key))
    raw_path = save_raw_json(data_dir, config.name, symbol, payload)
    rows = normalize_twelve_data_ohlcv(payload)
    if not rows:
        raise ValueError("No OHLCV rows found in response")
    csv_path = upsert_ohlcv_csv(data_dir, symbol, rows)
    return "done", len(rows), f"raw={raw_path} csv={csv_path}"


def main() -> int:
    args = parse_args()
    ensure_data_dirs(args.data_dir)
    verify_writable(args.data_dir / "state")

    config = load_provider_config(args.providers, args.provider)
    symbols = load_symbols(args.symbols)
    max_symbols = args.max_symbols if args.max_symbols is not None else config.max_symbols_per_run

    state = load_state(args.state)
    provider_state = ensure_daily_provider_state(state, config.name, config.daily_limit, config.daily_reserve)
    budget = DailyBudget(config, provider_state)

    api_key = os.environ.get(config.api_key_env, "")
    if not args.dry_run and not api_key:
        raise RuntimeError(f"Missing API key environment variable: {config.api_key_env}")

    pending = choose_pending_symbols(symbols, state, config.name, max_symbols)
    summary = {
        "started_at": utc_now_iso(),
        "provider": config.name,
        "dry_run": args.dry_run,
        "processed": 0,
        "failed": 0,
        "skipped_rate_limit": 0,
        "remaining_before_reserve_start": budget.remaining_before_reserve,
        "symbols": [],
    }

    for symbol in pending:
        decision = budget.can_spend(1)
        if not args.dry_run and not decision.allowed:
            record_symbol_status(state, config.name, symbol, "skipped_rate_limit", reason=decision.reason)
            summary["skipped_rate_limit"] += 1
            continue

        try:
            status, row_count, note = collect_symbol(config, args.data_dir, symbol, api_key, args.dry_run)
            if not args.dry_run:
                budget.spend(1)
            record_symbol_status(state, config.name, symbol, status, rows=row_count, note=note)
            summary["processed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": status, "rows": row_count})
        except Exception as exc:
            if not args.dry_run:
                budget.spend(1)
            record_symbol_status(state, config.name, symbol, "failed", error=str(exc))
            summary["failed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": "failed", "error": str(exc)})

    summary["finished_at"] = utc_now_iso()
    summary["calls_used_today"] = provider_state.get("calls_used_today", 0)
    summary["remaining_before_reserve_end"] = budget.remaining_before_reserve
    state.setdefault("runs", []).append(summary)
    state["runs"] = state["runs"][-20:]
    save_state(args.state, state)

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
