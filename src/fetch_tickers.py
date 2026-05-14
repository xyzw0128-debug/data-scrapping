"""Fetch exchange ticker lists from the Twelve Data stocks endpoint."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.config import load_provider_config, load_symbols


ROOT = Path(__file__).resolve().parents[1]
STOCKS_URL = "https://api.twelvedata.com/stocks"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch a Twelve Data exchange ticker list into config/symbols.txt.")
    parser.add_argument("--exchange", default="NASDAQ", help="Exchange code to request from Twelve Data stocks endpoint")
    parser.add_argument("--symbols", type=Path, default=ROOT / "config" / "symbols.txt")
    parser.add_argument("--providers", type=Path, default=ROOT / "config" / "providers.yaml")
    parser.add_argument("--provider", default="twelve_data", help="Provider config section used for the API key environment variable")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print ticker count without writing symbols.txt")
    parser.add_argument("--append", action="store_true", help="Merge fetched tickers with existing symbols.txt instead of overwriting")
    return parser.parse_args()


def build_stocks_url(exchange: str, api_key: str) -> str:
    query = urlencode({"exchange": exchange, "apikey": api_key})
    return f"{STOCKS_URL}?{query}"


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "data-scrapping-fetch-tickers/0.1"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_symbols(payload: dict) -> list[str]:
    if payload.get("status") == "error":
        message = payload.get("message") or payload.get("code") or "unknown Twelve Data error"
        raise ValueError(f"Twelve Data stocks endpoint error: {message}")

    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("Twelve Data stocks response missing data list")

    symbols: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_symbol = item.get("symbol")
        if not raw_symbol:
            continue
        symbol = str(raw_symbol).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    symbols.sort()
    return symbols


def atomic_write_symbols(path: Path, symbols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for symbol in symbols:
            handle.write(f"{symbol}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def merge_symbols(existing: list[str], fetched: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for symbol in [*existing, *fetched]:
        normalized = symbol.strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


def main() -> int:
    args = parse_args()
    config = load_provider_config(args.providers, args.provider)
    api_key = os.environ.get(config.api_key_env, "")
    if not api_key and not args.dry_run:
        raise RuntimeError(f"Missing API key environment variable: {config.api_key_env}")

    fetched_symbols: list[str] = []
    source = "twelve_data_stocks"
    if api_key:
        payload = fetch_json(build_stocks_url(args.exchange, api_key))
        fetched_symbols = extract_symbols(payload)
    elif args.symbols.exists():
        source = "existing_symbols_no_api_key"
        fetched_symbols = load_symbols(args.symbols)
    else:
        source = "no_api_key_no_existing_symbols"

    output_symbols = fetched_symbols
    existing_count = 0

    if args.append and args.symbols.exists():
        existing_symbols = load_symbols(args.symbols)
        existing_count = len(existing_symbols)
        output_symbols = merge_symbols(existing_symbols, fetched_symbols)

    if not args.dry_run:
        atomic_write_symbols(args.symbols, output_symbols)

    result = {
        "append": args.append,
        "dry_run": args.dry_run,
        "exchange": args.exchange,
        "existing_count": existing_count,
        "fetched_count": len(fetched_symbols),
        "output_count": len(output_symbols),
        "source": source,
        "symbols_path": str(args.symbols),
        "written": not args.dry_run,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
