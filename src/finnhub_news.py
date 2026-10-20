"""Finnhub company news collector with per-symbol resume state."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.config import load_symbols
from src.env import load_project_env
from src.main import PerMinuteRateLimiter
from src.state import load_state, save_state, utc_now_iso
from src.storage import connect_db, ensure_data_dirs, get_db_path, save_raw_json, upsert_news_db


ROOT = Path(__file__).resolve().parents[1]
FINNHUB_COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"
# Finnhub expects plain tickers; entries such as "!OTC/QHUOY" would only waste calls.
VALID_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Finnhub company news for configured symbols.")
    parser.add_argument("--symbols", type=Path, default=ROOT / "config" / "symbols.txt")
    parser.add_argument("--symbol", action="append", help="Collect only this symbol; can be used multiple times")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "state" / "finnhub_news.json", help="Per-symbol news resume state")
    parser.add_argument("--api-key-env", default="FINNHUB_API_KEY")
    parser.add_argument("--dry-run", action="store_true", help="Do not call Finnhub or write news rows")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of symbols to process this run")
    parser.add_argument("--from-date", default=None, help="Fixed start date YYYY-MM-DD for every symbol (disables per-symbol resume)")
    parser.add_argument("--to-date", default=None, help="End date YYYY-MM-DD; default is today UTC")
    parser.add_argument("--days-back", type=int, default=7, help="Lookback for symbols that have never been fetched")
    parser.add_argument("--max-days-back", type=int, default=365, help="Longest catch-up window for previously fetched symbols")
    parser.add_argument("--per-minute-limit", type=int, default=30, help="Finnhub calls per minute (free plan allows 60)")
    return parser.parse_args()


def resolve_date_range(from_date: str | None, to_date: str | None, days_back: int) -> tuple[str, str]:
    """Resolve CLI date arguments into Finnhub YYYY-MM-DD dates."""
    end = date.fromisoformat(to_date) if to_date else datetime.now(UTC).date()
    start = date.fromisoformat(from_date) if from_date else end - timedelta(days=days_back)
    if start > end:
        raise ValueError("from-date must be on or before to-date")
    return start.isoformat(), end.isoformat()


def build_company_news_url(symbol: str, start_date: str, end_date: str, api_key: str) -> str:
    """Build a Finnhub company-news URL."""
    query = urlencode({"symbol": symbol, "from": start_date, "to": end_date, "token": api_key})
    return f"{FINNHUB_COMPANY_NEWS_URL}?{query}"


def resolve_symbol_range(entry: dict, end: date, days_back: int, max_days_back: int) -> tuple[str, str]:
    """Return the window that continues from the symbol's last successful fetch."""
    last_to = entry.get("last_to")
    if last_to:
        start = max(date.fromisoformat(last_to) - timedelta(days=1), end - timedelta(days=max_days_back))
    else:
        start = end - timedelta(days=days_back)
    return min(start, end).isoformat(), end.isoformat()


def load_liquidity(data_dir: Path) -> dict[str, float]:
    """Average dollar volume over each symbol's latest 60 rows; empty if the DB is unavailable."""
    if not get_db_path(data_dir).exists():
        return {}
    try:
        con = connect_db(data_dir, read_only=True)
    except Exception:
        return {}
    try:
        rows = con.execute(
            """
            SELECT symbol, avg(TRY_CAST(close AS DOUBLE) * TRY_CAST(volume AS DOUBLE))
            FROM (SELECT symbol, close, volume, row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn FROM ohlcv)
            WHERE rn <= 60
            GROUP BY symbol
            """
        ).fetchall()
    finally:
        con.close()
    return {symbol: float(value or 0.0) for symbol, value in rows}


def choose_news_symbols(symbols: list[str], news_state: dict, liquidity: dict[str, float], limit: int | None) -> list[str]:
    """Order symbols by least recently attempted, then most liquid, then input order."""
    valid = [symbol for symbol in dict.fromkeys(symbols) if VALID_SYMBOL_PATTERN.match(symbol)]
    position = {symbol: index for index, symbol in enumerate(valid)}
    ranked = sorted(
        valid,
        key=lambda symbol: (
            str(news_state.get(symbol, {}).get("last_attempt_at", "")),
            -liquidity.get(symbol, 0.0),
            position[symbol],
        ),
    )
    return ranked if limit is None else ranked[:limit]


def fetch_json(url: str, rate_limiter: PerMinuteRateLimiter | None = None) -> list[dict] | dict:
    """Fetch JSON from Finnhub, waiting once on an HTTP 429 rate-limit response."""
    for attempt in range(2):
        if rate_limiter is not None:
            rate_limiter.wait()
        request = Request(url, headers={"User-Agent": "data-scrapping-finnhub-news-mvp/0.1"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code != 429 or attempt == 1:
                raise
            time.sleep(60)
    raise RuntimeError("unreachable")


def _format_datetime(value: object) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(timestamp, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_news(payload: list[dict] | dict, symbol: str) -> list[dict[str, str]]:
    """Normalize Finnhub company news into simple CSV rows."""
    if isinstance(payload, dict):
        message = payload.get("error") or payload.get("message") or "unexpected object response"
        raise ValueError(f"Finnhub response is not a news list: {message}")
    if not isinstance(payload, list):
        raise ValueError("Finnhub response is not a list")

    rows: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        news_id = str(item.get("id", ""))
        timestamp_iso = _format_datetime(item.get("datetime"))
        rows.append(
            {
                "id": news_id,
                "symbol": symbol,
                "datetime": timestamp_iso,
                "date": timestamp_iso[:10] if timestamp_iso else "",
                "headline": str(item.get("headline", "")),
                "source": str(item.get("source", "")),
                "summary": str(item.get("summary", "")),
                "url": str(item.get("url", "")),
                "image": str(item.get("image", "")),
                "category": str(item.get("category", "")),
            }
        )
    rows.sort(key=lambda row: (row["datetime"], row["id"], row["url"]))
    return rows


def collect_symbol_news(
    data_dir: Path,
    symbol: str,
    start_date: str,
    end_date: str,
    api_key: str,
    dry_run: bool,
    rate_limiter: PerMinuteRateLimiter | None = None,
) -> tuple[str, int, str | None]:
    """Collect Finnhub company news for one symbol."""
    if dry_run:
        return "dry_run", 0, None

    payload = fetch_json(build_company_news_url(symbol, start_date, end_date, api_key), rate_limiter)
    raw_path = save_raw_json(data_dir, "finnhub_news", symbol, {"symbol": symbol, "from": start_date, "to": end_date, "items": payload})
    rows = normalize_news(payload, symbol)
    db_path = upsert_news_db(data_dir, symbol, rows)
    return "done", len(rows), f"raw={raw_path} db={db_path}"


def main() -> int:
    load_project_env(ROOT)
    args = parse_args()
    ensure_data_dirs(args.data_dir)
    news_state = load_state(args.state).get("symbols", {}) if args.state.exists() else {}
    if args.symbol:
        symbols = args.symbol[: args.limit] if args.limit is not None else args.symbol
    else:
        symbols = choose_news_symbols(load_symbols(args.symbols), news_state, load_liquidity(args.data_dir), args.limit)
    end = date.fromisoformat(args.to_date) if args.to_date else datetime.now(UTC).date()
    fixed_range = resolve_date_range(args.from_date, args.to_date, args.days_back) if args.from_date else None

    api_key = os.environ.get(args.api_key_env, "")
    if not args.dry_run and not api_key:
        raise RuntimeError(f"Missing Finnhub API key environment variable: {args.api_key_env}")

    rate_limiter = PerMinuteRateLimiter(args.per_minute_limit)
    summary = {
        "started_at": utc_now_iso(),
        "dry_run": args.dry_run,
        "to": end.isoformat(),
        "processed": 0,
        "failed": 0,
        "rows": 0,
        "symbols": [],
    }

    def persist() -> None:
        if not args.dry_run:
            save_state(args.state, {"schema_version": 1, "symbols": news_state})

    for index, symbol in enumerate(symbols, start=1):
        entry = news_state.setdefault(symbol, {})
        start_date, end_date = fixed_range or resolve_symbol_range(entry, end, args.days_back, args.max_days_back)
        try:
            status, row_count, note = collect_symbol_news(args.data_dir, symbol, start_date, end_date, api_key, args.dry_run, rate_limiter)
            summary["processed"] += 1
            summary["rows"] += row_count
            summary["symbols"].append({"symbol": symbol, "status": status, "from": start_date, "rows": row_count, "note": note})
            entry.update({"status": "done", "last_attempt_at": utc_now_iso(), "last_to": end_date, "rows": row_count, "error": None})
        except Exception as exc:
            summary["failed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": "failed", "from": start_date, "error": str(exc)})
            entry.update({"status": "failed", "last_attempt_at": utc_now_iso(), "error": str(exc)})
        if index % 10 == 0:
            persist()
    persist()
    summary["finished_at"] = utc_now_iso()
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
