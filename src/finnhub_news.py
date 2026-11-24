"""Finnhub company news collector with paging and per-symbol resume state.

Finnhub returns at most about 250 articles per request (the newest ones), so a long window
for a busy symbol comes back truncated. When a page looks truncated, the next request
moves ``to`` back to the oldest article date and the window is walked backwards. A symbol
gets at most ``--max-pages`` requests per run; an unfinished window (window_from,
window_to, cursor_to) is saved and resumed next run. ``last_to`` advances only after a
window is fully covered, so no dates are skipped.

Core symbols are requested every run. Other symbols back off while they keep returning no new
articles: after 1, 2, 3, 4+ consecutive empty runs the next request waits 1, 2, 4, 7 days
(``--backoff-days``), and one new article brings the symbol back to every run. A skipped day is
not lost: the next request starts at ``last_to`` (minus one day of overlap), so it covers every
day since the last completed window (up to ``--max-days-back``).
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
from src.data_rules import DataRulesError
from src.env import load_project_env
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.rate_limit import PerMinuteRateLimiter
from src.state import load_state, save_state, utc_now_iso
from src.market_calendar import NEW_YORK
from src.storage import connect_db, ensure_data_dirs, ensure_db_schema, get_db_path, save_raw_latest, upsert_symbol_news
from src.universe import Universe, load_universe


ROOT = Path(__file__).resolve().parents[1]
FINNHUB_COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"
# Finnhub expects plain tickers; entries such as "!OTC/QHUOY" would only waste calls.
VALID_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
# Observed caps were 244-250 articles per response; at or above this a page may be truncated.
TRUNCATION_THRESHOLD = 200
# Days to wait before the next request after 1, 2, 3, 4+ consecutive runs without new articles.
DEFAULT_BACKOFF_DAYS = (1, 2, 4, 7)


def parse_backoff_days(text: str) -> tuple[int, ...]:
    """Parse "1,2,4,7" into waits in days; "1" means every run (no backoff)."""
    try:
        days = tuple(int(part) for part in str(text).split(",") if part.strip())
    except ValueError:
        days = ()
    if not days or any(day < 1 for day in days):
        raise ValueError(f"backoff days must be comma-separated whole numbers >= 1, got {text!r}")
    return days


def _backoff_days_arg(text: str) -> tuple[int, ...]:
    try:
        return parse_backoff_days(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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
    parser.add_argument(
        "--backoff-days",
        type=_backoff_days_arg,
        default=DEFAULT_BACKOFF_DAYS,
        help="Waits in days after 1, 2, 3, ... consecutive runs without new articles (non-core symbols; default 1,2,4,7; 1 = every run)",
    )
    parser.add_argument(
        "--check-date", type=date.fromisoformat, default=None, help="Date YYYY-MM-DD that decides which symbols are due (default: today in New York)"
    )
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


def news_check_date(now: datetime | None = None) -> date:
    """The run's day for backoff decisions: the New York calendar date.

    The news stage starts around 20:00-21:30 New York time, which is close to UTC midnight
    (00:20-01:30 UTC). Two consecutive daily runs could then share a UTC date and a symbol due
    "tomorrow" would be skipped; the New York date changes only hours after the stage ends.
    """
    return (now or datetime.now(UTC)).astimezone(NEW_YORK).date()


def backoff_wait(zero_streak: int, backoff_days: tuple[int, ...] = DEFAULT_BACKOFF_DAYS) -> int:
    """Days until the next request after ``zero_streak`` consecutive runs without new articles."""
    if zero_streak <= 0:
        return 1
    return backoff_days[min(zero_streak, len(backoff_days)) - 1]


def is_news_due(entry: dict | None, check_day: date, backoff_days: tuple[int, ...] = DEFAULT_BACKOFF_DAYS) -> bool:
    """True unless the entry has a next_check_date after ``check_day``.

    A missing or unreadable date (state written before backoff existed) means every run. A date
    further away than the longest wait (clock skew, or a shorter --backoff-days) is also due.
    """
    value = (entry or {}).get("next_check_date")
    if not value:
        return True
    try:
        next_check = date.fromisoformat(str(value))
    except ValueError:
        return True
    return next_check <= check_day or next_check > check_day + timedelta(days=max(backoff_days))


def record_news_result(entry: dict, result: dict, check_day: date, backoff_days: tuple[int, ...] = DEFAULT_BACKOFF_DAYS) -> None:
    """Update the zero-article streak and the next request date after a successful request.

    An unfinished window keeps the symbol on every run until the window is covered. A failed
    request does not call this, so failures keep their every-run retry.
    """
    try:
        previous = max(int(entry.get("zero_streak") or 0), 0)
    except (TypeError, ValueError):  # a hand-edited or damaged value must not fail a stored symbol
        previous = 0
    streak = previous + 1 if result["complete"] and result["new_rows"] == 0 else 0
    wait = backoff_wait(streak, backoff_days)
    entry["zero_streak"] = streak
    entry["next_check_date"] = (check_day + timedelta(days=wait)).isoformat() if wait > 1 else None


def split_due_symbols(
    symbols: list[str],
    news_state: dict,
    check_day: date,
    backoff_days: tuple[int, ...] = DEFAULT_BACKOFF_DAYS,
    always: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Split symbols into (due, waiting); ``always`` symbols (core) are always due."""
    always_set = set(always or [])
    due: list[str] = []
    waiting: list[str] = []
    for symbol in symbols:
        if symbol in always_set or is_news_due(news_state.get(symbol), check_day, backoff_days):
            due.append(symbol)
        else:
            waiting.append(symbol)
    return due, waiting


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


def universe_news_selection(
    universe: Universe,
    news_state: dict,
    liquidity: dict[str, float],
    limit: int | None,
    check_day: date | None = None,
    backoff_days: tuple[int, ...] = DEFAULT_BACKOFF_DAYS,
) -> tuple[list[str], list[str]]:
    """The default selection of a run and the symbols left out by backoff.

    Every core symbol and every due active symbol, core symbols first. Without ``check_day``,
    or with a schedule of only 1-day waits, nothing is left out.
    """
    core = universe.core_symbols()
    inactive = universe.inactive_symbols()  # delisted marks and config/core_exclude.txt
    active = [s for s in [*universe.nasdaq, *sorted(universe.former_core)] if s not in inactive]
    waiting: list[str] = []
    if check_day is not None and max(backoff_days) > 1:
        active, waiting = split_due_symbols(active, news_state, check_day, backoff_days, always=core)
    return choose_news_symbols(active, news_state, liquidity, limit, priority=core), waiting


def universe_news_symbols(
    universe: Universe,
    news_state: dict,
    liquidity: dict[str, float],
    limit: int | None,
    check_day: date | None = None,
    backoff_days: tuple[int, ...] = DEFAULT_BACKOFF_DAYS,
) -> list[str]:
    """The default selection of a run (see ``universe_news_selection``)."""
    return universe_news_selection(universe, news_state, liquidity, limit, check_day, backoff_days)[0]


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
    """Collect one symbol's news for a window, resuming a saved unfinished window first.

    Every page is fetched first and the symbol's articles are written with one DB connection
    and transaction. If a page fails nothing is written and the entry is unchanged, so the next
    run requests the same window again. ``new_rows`` counts article ids not stored before: the
    window starts a day before ``last_to``, so returned rows can repeat stored articles.
    """
    fetch = fetch or fetch_json
    if entry.get("window_from") and entry.get("cursor_to"):
        window_from, window_to, cursor = entry["window_from"], entry["window_to"], entry["cursor_to"]
    else:
        window_from, window_to = window
        cursor = window_to
    pages = rows_total = 0
    complete = False
    payload: list[dict] | dict = []
    collected: list[dict[str, str]] = []
    while pages < max_pages:
        payload = fetch(build_company_news_url(symbol, window_from, cursor, api_key), rate_limiter)
        rows = normalize_news(payload, symbol)
        pages += 1
        rows_total += len(rows)
        collected.extend(rows)
        following = next_cursor(rows, window_from, cursor)
        if following is None:
            complete = True
            break
        cursor = following
    new_rows = upsert_symbol_news(data_dir, symbol, collected) if collected else 0
    if payload:  # an empty answer adds nothing worth keeping; the previous raw file stays
        save_raw_latest(data_dir, "finnhub_news", symbol, {"symbol": symbol, "from": window_from, "to": cursor, "items": payload})
    if complete:
        entry.update({"last_to": window_to, "window_from": None, "window_to": None, "cursor_to": None})
    else:
        entry.update({"window_from": window_from, "window_to": window_to, "cursor_to": cursor})
    return {"rows": rows_total, "new_rows": new_rows, "pages": pages, "complete": complete, "from": window_from, "to": window_to}


def run(args: argparse.Namespace) -> dict:
    ensure_data_dirs(args.data_dir)
    if not args.dry_run:
        ensure_db_schema(args.data_dir)  # fail once on an unconverted schema, not per symbol
    news_state = load_state(args.state).get("symbols", {}) if args.state.exists() else {}
    if args.dry_run:
        news_state = copy.deepcopy(news_state)
    backoff_days = tuple(getattr(args, "backoff_days", None) or DEFAULT_BACKOFF_DAYS)
    check_day = getattr(args, "check_date", None) or news_check_date()
    waiting: list[str] = []
    if args.symbol:  # explicitly named symbols are always requested
        symbols = args.symbol[: args.limit] if args.limit is not None else args.symbol
    else:
        # Liquidity only breaks ties in the order; it matters only when --limit cuts the list.
        liquidity = load_liquidity(args.data_dir) if args.limit is not None else {}
        if args.symbols is not None:
            candidates = load_symbols(args.symbols)
            if max(backoff_days) > 1:
                candidates, waiting = split_due_symbols(candidates, news_state, check_day, backoff_days)
            symbols = choose_news_symbols(candidates, news_state, liquidity, args.limit)
        else:
            universe = load_universe(args.data_dir, args.config_dir)
            symbols, waiting = universe_news_selection(universe, news_state, liquidity, args.limit, check_day, backoff_days)
    end = date.fromisoformat(args.to_date) if args.to_date else datetime.now(UTC).date()
    fixed_range = resolve_date_range(args.from_date, args.to_date, args.days_back) if args.from_date else None

    api_key = os.environ.get(args.api_key_env, "")
    if not args.dry_run and not api_key:
        raise RuntimeError(f"Missing Finnhub API key environment variable: {args.api_key_env}")

    rate_limiter = PerMinuteRateLimiter(args.per_minute_limit)
    summary = {"started_at": utc_now_iso(), "dry_run": args.dry_run, "to": end.isoformat(), "planned": len(symbols),
               "check_date": check_day.isoformat(), "backoff_days": list(backoff_days), "waiting_backoff": len(waiting),
               "processed": 0, "failed": 0, "rows": 0, "new_rows": 0, "pages": 0, "unfinished_windows": 0, "symbols": []}

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
            summary["new_rows"] += result["new_rows"]
            summary["pages"] += result["pages"]
            summary["unfinished_windows"] += 0 if result["complete"] else 1
            summary["symbols"].append({"symbol": symbol, "status": "done", **result})
            record_news_result(entry, result, check_day, backoff_days)
            entry.update({"status": "done", "last_attempt_at": utc_now_iso(), "rows": result["rows"], "new_rows": result["new_rows"], "error": None})
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
    except DataRulesError as exc:  # config/core_exclude.txt, read with the universe before any call
        print(json.dumps({"status": "data_rules_invalid", "error": str(exc)}, ensure_ascii=False))
        return 1
    printable = {**summary, "symbols": [s for s in summary["symbols"] if s["status"] == "failed"][:50]}
    print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
