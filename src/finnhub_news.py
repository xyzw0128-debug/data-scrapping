"""Minimal Finnhub company news collector."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.config import load_symbols
from src.state import utc_now_iso
from src.storage import ensure_data_dirs, save_raw_json, upsert_news_db


ROOT = Path(__file__).resolve().parents[1]
FINNHUB_COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"
NEWS_FIELDS = ["id", "symbol", "datetime", "date", "headline", "source", "summary", "url", "image", "category"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Finnhub company news for configured symbols.")
    parser.add_argument("--symbols", type=Path, default=ROOT / "config" / "symbols.txt")
    parser.add_argument("--symbol", action="append", help="Collect only this symbol; can be used multiple times")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--api-key-env", default="FINNHUB_API_KEY")
    parser.add_argument("--dry-run", action="store_true", help="Do not call Finnhub or write news CSV files")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of symbols to process this run")
    parser.add_argument("--from-date", default=None, help="Start date YYYY-MM-DD; default is today minus --days-back")
    parser.add_argument("--to-date", default=None, help="End date YYYY-MM-DD; default is today UTC")
    parser.add_argument("--days-back", type=int, default=7, help="Lookback window when --from-date is omitted")
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


def fetch_json(url: str) -> list[dict] | dict:
    """Fetch JSON from Finnhub."""
    request = Request(url, headers={"User-Agent": "data-scrapping-finnhub-news-mvp/0.1"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


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


def _dedupe_key(row: dict[str, str]) -> str:
    return row.get("id") or row.get("url") or f"{row.get('datetime')}|{row.get('headline')}"


def upsert_news_csv(data_dir: Path, symbol: str, rows: list[dict[str, str]]) -> Path:
    """Write symbol-level Finnhub news CSV, replacing duplicate IDs/URLs."""
    output_dir = data_dir / "news"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"FINNHUB_{symbol.replace('/', '_')}.csv"

    merged: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                key = _dedupe_key(row)
                if key:
                    merged[key] = {field: str(row.get(field, "")) for field in NEWS_FIELDS}

    for row in rows:
        key = _dedupe_key(row)
        if key:
            merged[key] = {field: str(row.get(field, "")) for field in NEWS_FIELDS}

    tmp_path = path.with_suffix(".csv.tmp")
    ordered_rows = sorted(merged.values(), key=lambda row: (row["datetime"], row["id"], row["url"]))
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=NEWS_FIELDS)
        writer.writeheader()
        writer.writerows(ordered_rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path


def collect_symbol_news(
    data_dir: Path,
    symbol: str,
    start_date: str,
    end_date: str,
    api_key: str,
    dry_run: bool,
) -> tuple[str, int, str | None]:
    """Collect Finnhub company news for one symbol."""
    if dry_run:
        return "dry_run", 0, None

    payload = fetch_json(build_company_news_url(symbol, start_date, end_date, api_key))
    raw_path = save_raw_json(data_dir, "finnhub_news", symbol, {"symbol": symbol, "from": start_date, "to": end_date, "items": payload})
    rows = normalize_news(payload, symbol)
    db_path = upsert_news_db(data_dir, symbol, rows)
    return "done", len(rows), f"raw={raw_path} db={db_path}"


def main() -> int:
    args = parse_args()
    ensure_data_dirs(args.data_dir)
    symbols = args.symbol if args.symbol else load_symbols(args.symbols)
    if args.limit is not None:
        symbols = symbols[: args.limit]
    start_date, end_date = resolve_date_range(args.from_date, args.to_date, args.days_back)

    api_key = os.environ.get(args.api_key_env, "")
    if not args.dry_run and not api_key:
        raise RuntimeError(f"Missing Finnhub API key environment variable: {args.api_key_env}")

    summary = {
        "started_at": utc_now_iso(),
        "dry_run": args.dry_run,
        "from": start_date,
        "to": end_date,
        "processed": 0,
        "failed": 0,
        "symbols": [],
    }
    for symbol in symbols:
        try:
            status, row_count, note = collect_symbol_news(args.data_dir, symbol, start_date, end_date, api_key, args.dry_run)
            summary["processed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": status, "rows": row_count, "note": note})
        except Exception as exc:
            summary["failed"] += 1
            summary["symbols"].append({"symbol": symbol, "status": "failed", "error": str(exc)})
    summary["finished_at"] = utc_now_iso()
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
