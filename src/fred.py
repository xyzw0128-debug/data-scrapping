"""Minimal FRED macro series collector."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.state import utc_now_iso
from src.storage import ensure_data_dirs, save_raw_json


ROOT = Path(__file__).resolve().parents[1]
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
MACRO_FIELDS = ["date", "value", "realtime_start", "realtime_end"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect a small set of FRED macro series.")
    parser.add_argument("--series-file", type=Path, default=ROOT / "config" / "fred_series.txt")
    parser.add_argument("--series", action="append", help="Collect only this FRED series; can be used multiple times")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--api-key-env", default="FRED_API_KEY")
    parser.add_argument("--dry-run", action="store_true", help="Do not call FRED or write macro CSV files")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of series to process this run")
    return parser.parse_args()


def load_series(path: Path) -> list[str]:
    """Load FRED series IDs from a one-series-per-line file."""
    series: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        series.append(value)
    return series


def build_fred_url(series_id: str, api_key: str) -> str:
    """Build a FRED observations API URL for one series."""
    query = urlencode(
        {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "asc",
        }
    )
    return f"{FRED_BASE_URL}?{query}"


def fetch_json(url: str) -> dict:
    """Fetch JSON from FRED."""
    request = Request(url, headers={"User-Agent": "data-scrapping-fred-mvp/0.1"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_observations(payload: dict) -> list[dict[str, str]]:
    """Normalize FRED observations into simple CSV rows."""
    observations = payload.get("observations")
    if not isinstance(observations, list):
        message = payload.get("error_message") or payload.get("message") or "missing observations"
        raise ValueError(f"FRED response has no observations: {message}")

    rows: list[dict[str, str]] = []
    for item in observations:
        if not isinstance(item, dict):
            continue
        date = str(item.get("date", ""))
        if not date:
            continue
        rows.append(
            {
                "date": date,
                "value": str(item.get("value", "")),
                "realtime_start": str(item.get("realtime_start", "")),
                "realtime_end": str(item.get("realtime_end", "")),
            }
        )
    rows.sort(key=lambda row: row["date"])
    return rows


def write_macro_csv(data_dir: Path, series_id: str, rows: list[dict[str, str]]) -> Path:
    """Write one FRED macro series CSV."""
    output_dir = data_dir / "macro"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"FRED_{series_id}.csv"
    tmp_path = path.with_suffix(".csv.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MACRO_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path


def collect_series(data_dir: Path, series_id: str, api_key: str, dry_run: bool) -> tuple[str, int, str | None]:
    """Collect one FRED series and return status, row count, and output note."""
    if dry_run:
        return "dry_run", 0, None

    payload = fetch_json(build_fred_url(series_id, api_key))
    raw_path = save_raw_json(data_dir, "fred", series_id, payload)
    rows = normalize_observations(payload)
    if not rows:
        raise ValueError("No FRED observation rows found in response")
    csv_path = write_macro_csv(data_dir, series_id, rows)
    return "done", len(rows), f"raw={raw_path} csv={csv_path}"


def main() -> int:
    args = parse_args()
    ensure_data_dirs(args.data_dir)
    (args.data_dir / "macro").mkdir(parents=True, exist_ok=True)

    series_ids = args.series if args.series else load_series(args.series_file)
    if args.limit is not None:
        series_ids = series_ids[: args.limit]

    api_key = os.environ.get(args.api_key_env, "")
    if not args.dry_run and not api_key:
        raise RuntimeError(f"Missing FRED API key environment variable: {args.api_key_env}")

    summary = {
        "started_at": utc_now_iso(),
        "dry_run": args.dry_run,
        "processed": 0,
        "failed": 0,
        "series": [],
    }
    for series_id in series_ids:
        try:
            status, row_count, note = collect_series(args.data_dir, series_id, api_key, args.dry_run)
            summary["processed"] += 1
            summary["series"].append({"series_id": series_id, "status": status, "rows": row_count, "note": note})
        except Exception as exc:
            summary["failed"] += 1
            summary["series"].append({"series_id": series_id, "status": "failed", "error": str(exc)})
    summary["finished_at"] = utc_now_iso()
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
