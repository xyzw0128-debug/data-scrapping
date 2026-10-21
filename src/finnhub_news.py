"""Finnhub company news collector with paging and per-symbol resume state.

Finnhub returns at most about 250 articles per request (the newest ones), so a long window
for a busy symbol comes back truncated. When a page looks truncated, the next request
moves ``to`` back to the oldest article date and the window is walked backwards. A symbol
gets at most ``--max-pages`` requests per run; an unfinished window (window_from,
window_to, cursor_to) is saved and resumed next run. ``last_to`` advances only after a
window is fully covered, so no dates are skipped.
"""

from __future__ import annotations

import argparse
import copy
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
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.rate_limit import PerMinuteRateLimiter
from src.state import load_state, save_state, utc_now_iso
from src.storage import connect_db, ensure_data_dirs, get_db_path, save_raw_latest, upsert_news_db
from src.universe import Universe, load_universe


ROOT = Path(__file__).resolve().parents[1]
FINNHUB_COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"
# Finnhub expects plain tickers; entries such as "!OTC/QHUOY" would only waste calls.
VALID_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
# Observed caps were 244-250 articles per response; at or above this a page may be truncated.
TRUNCATION_THRESHOLD = 200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Finnhub company news, core symbols first.")
    parser.add_argument("--symbols", type=Path, default=None, help="Symbol file to use instead of the core/rotation universe")
    parser.add_argument("--symbol", action="append", help="Collect only this symbol; can be used multiple times")
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "state" / "finnhub_news.json", help="Per-symbol news resume state")
    parser.add_argument("--api-key-env", default="FINNHUB_API_KEY")
    parser.add_argument("--dry-run", action="store_true", help="Do not call Finnhub or write anything")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of symbols to process this run (default: all)")
    parser.add_argument("--from-date", default=None, help="Fixed start date YYYY-MM-DD for every symbol (disables per-symbol resume)")
    parser.add_argument("--to-date", default=None, help="End date YYYY-MM-DD; default is today UTC")
    parser.add_argument("--days-back", type=int, default=7, help="Lookback for symbols that have never been fetched")
    parser.add_argument("--max-days-back", type=int, default=365, help="Longest catch-up window for previously fetched symbols")
    parser.add_argument("--max-pages", type=int, default=10, help="Requests per symbol per run when responses are truncated")
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


def choose_news_symbols(
    symbols: list[str],
    news_state: dict,
    liquidity: dict[str, float],
    limit: int | None,
    priority: list[str] | None = None,
) -> list[str]:
    """Priority (core) symbols first; within each group least recently attempted, then most liquid, then input order."""
    priority_set = set(priority or [])
    valid = [symbol for symbol in dict.fromkeys([*(priority or []), *symbols]) if VALID_SYMBOL_PATTERN.match(symbol)]
    position = {symbol: index for index, symbol in enumerate(valid)}
    ranked = sorted(
        valid,
        key=lambda symbol: (
            0 if symbol in priority_set else 1,
            str(news_state.get(symbol, {}).get("last_attempt_at", "")),
            -liquidity.get(symbol, 0.0),
            position[symbol],
        ),
    )
    return ranked if limit is None else ranked[:limit]


def universe_news_symbols(universe: Universe, news_state: dict, liquidity: dict[str, float], limit: int | None) -> list[str]:
    """The default selection of a run: every active symbol, core symbols first."""
    active = [s for s in [*universe.nasdaq, *sorted(universe.former_core)] if s not in universe.delisted]
    return choose_news_symbols(active, news_state, liquidity, limit, priority=universe.core_symbols())


def fetch_json(url: str, rate_limiter: PerMinuteRateLimiter | None = None) -> list[dict] | dict:
    """Fetch JSON from Finnhub, waiting once on an HTTP 429 rate-limit response."""
    for attempt in range(2):
        if rate_limiter is not None:
            rate_limiter.wait()
        request = Request(url, headers={"User-Agent": "data-scrapping-finnhub-news/0.2"})
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
    """Normalize Finnhub company news into flat rows (datetime is UTC with seconds)."""
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


def next_cursor(rows: list[dict[str, str]], window_from: str, cursor_to: str) -> str | None:
    """Return the next ``to`` date for a truncated page, or None when the window is covered."""
    if len(rows) < TRUNCATION_THRESHOLD:
        return None
    dates = [row["date"] for row in rows if row["date"]]
    if not dates:
        return None
    oldest = min(dates)
    if oldest <= window_from:
        return None
    # More than a page of articles on one day: step back a day instead of looping forever.
    cursor = oldest if oldest < cursor_to else (date.fromisoformat(cursor_to) - timedelta(days=1)).isoformat()
    return cursor if cursor >= window_from else None


def collect_symbol_news(
    data_dir: Path,
    symbol: str,
    entry: dict,
    window: tuple[str, str],
    api_key: str,
    max_pages: int,
    rate_limiter: PerMinuteRateLimiter | None = None,
    fetch=None,
) -> dict:
    """Collect one symbol's news for a window, resuming a saved unfinished window first."""
    fetch = fetch or fetch_json
    if entry.get("window_from") and entry.get("cursor_to"):
        window_from, window_to, cursor = entry["window_from"], entry["window_to"], entry["cursor_to"]
    else:
        window_from, window_to = window
        cursor = window_to
    pages = rows_total = 0
    complete = False
    payload: list[dict] | dict = []
    while pages < max_pages:
        payload = fetch(build_company_news_url(symbol, window_from, cursor, api_key), rate_limiter)
        rows = normalize_news(payload, symbol)
        pages += 1
        rows_total += len(rows)
        if rows:
            upsert_news_db(data_dir, symbol, rows)
        following = next_cursor(rows, window_from, cursor)
        if following is None:
            complete = True
            break
        cursor = following
    save_raw_latest(data_dir, "finnhub_news", symbol, {"symbol": symbol, "from": window_from, "to": cursor, "items": payload})
    if complete:
        entry.update({"last_to": window_to, "window_from": None, "window_to": None, "cursor_to": None})
    else:
        entry.update({"window_from": window_from, "window_to": window_to, "cursor_to": cursor})
    return {"rows": rows_total, "pages": pages, "complete": complete, "from": window_from, "to": window_to}


def run(args: argparse.Namespace) -> dict:
    ensure_data_dirs(args.data_dir)
    news_state = load_state(args.state).get("symbols", {}) if args.state.exists() else {}
    if args.dry_run:
        news_state = copy.deepcopy(news_state)
    if args.symbol:
        symbols = args.symbol[: args.limit] if args.limit is not None else args.symbol
    elif args.symbols is not None:
        symbols = choose_news_symbols(load_symbols(args.symbols), news_state, load_liquidity(args.data_dir), args.limit)
    else:
        universe = load_universe(args.data_dir, args.config_dir)
        symbols = universe_news_symbols(universe, news_state, load_liquidity(args.data_dir), args.limit)
    end = date.fromisoformat(args.to_date) if args.to_date else datetime.now(UTC).date()
    fixed_range = resolve_date_range(args.from_date, args.to_date, args.days_back) if args.from_date else None

    api_key = os.environ.get(args.api_key_env, "")
    if not args.dry_run and not api_key:
        raise RuntimeError(f"Missing Finnhub API key environment variable: {args.api_key_env}")

    rate_limiter = PerMinuteRateLimiter(args.per_minute_limit)
    summary = {"started_at": utc_now_iso(), "dry_run": args.dry_run, "to": end.isoformat(), "planned": len(symbols),
               "processed": 0, "failed": 0, "rows": 0, "pages": 0, "unfinished_windows": 0, "symbols": []}

    def persist() -> None:
        if not args.dry_run:
            save_state(args.state, {"schema_version": 2, "symbols": news_state})

    for index, symbol in enumerate(symbols, start=1):
        entry = news_state.setdefault(symbol, {})
        window = fixed_range or resolve_symbol_range(entry, end, args.days_back, args.max_days_back)
        if args.dry_run:
            summary["processed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": "dry_run", "from": window[0], "to": window[1]})
            continue
        try:
            result = collect_symbol_news(args.data_dir, symbol, entry, window, api_key, args.max_pages, rate_limiter)
            summary["processed"] += 1
            summary["rows"] += result["rows"]
            summary["pages"] += result["pages"]
            summary["unfinished_windows"] += 0 if result["complete"] else 1
            summary["symbols"].append({"symbol": symbol, "status": "done", **result})
            entry.update({"status": "done", "last_attempt_at": utc_now_iso(), "rows": result["rows"], "error": None})
        except Exception as exc:
            summary["failed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": "failed", "from": window[0], "error": str(exc)})
            entry.update({"status": "failed", "last_attempt_at": utc_now_iso(), "error": str(exc)})
        if index % 10 == 0:
            persist()
    persist()
    summary["finished_at"] = utc_now_iso()
    return summary


def main() -> int:
    load_project_env(ROOT)
    args = parse_args()
    try:
        with pipeline_lock(args.data_dir):
            summary = run(args)
    except LockHeldError as exc:
        return lock_held_exit(exc)
    printable = {**summary, "symbols": [s for s in summary["symbols"] if s["status"] == "failed"][:50]}
    print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
