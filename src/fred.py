"""FRED macro series collector.

Besides observations it stores each series' metadata in ``series_meta``. Series whose FRED
notes carry copyright or reproduction restrictions (e.g. BAA10Y from Moody's, VIXCLS from
Cboe) are flagged ``personal_use_only``; dataset exports leave them out by default.
Per-series results go to data/state/fred.json, which the snapshot step uses to decide
whether a run's macro data is complete.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.env import load_project_env
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.state import load_state, save_state, utc_now_iso
from src.storage import ensure_data_dirs, save_raw_latest, upsert_macro_db, upsert_series_meta


ROOT = Path(__file__).resolve().parents[1]
FRED_API_ROOT = "https://api.stlouisfed.org/fred"
FRED_BASE_URL = f"{FRED_API_ROOT}/series/observations"
MACRO_FIELDS = ["date", "value", "realtime_start", "realtime_end"]
RIGHTS_PATTERN = re.compile(r"copyright|reproduc|permission|prohibit|redistribut|licens", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect FRED macro series.")
    parser.add_argument("--series-file", type=Path, default=ROOT / "config" / "fred_series.txt")
    parser.add_argument("--series", action="append", help="Collect only this FRED series; can be used multiple times")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--api-key-env", default="FRED_API_KEY")
    parser.add_argument("--dry-run", action="store_true", help="Do not call FRED or write anything")
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


def build_fred_url(series_id: str, api_key: str, endpoint: str = "series/observations", **extra: str) -> str:
    """Build a FRED API URL for one series."""
    params = {"series_id": series_id, "api_key": api_key, "file_type": "json", **extra}
    if endpoint == "series/observations":
        params.setdefault("sort_order", "asc")
    return f"{FRED_API_ROOT}/{endpoint}?{urlencode(params)}"


def fetch_json(url: str) -> dict:
    """Fetch JSON from FRED."""
    request = Request(url, headers={"User-Agent": "data-scrapping-fred/0.2"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_observations(payload: dict) -> list[dict[str, str]]:
    """Normalize FRED observations into simple rows."""
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


def rights_note(notes: str) -> str | None:
    """Sentences of FRED notes that restrict copying or storage, or None when there are none."""
    text = " ".join(notes.split())
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if RIGHTS_PATTERN.search(s)]
    return " ".join(sentences)[:1000] if sentences else None


def series_metadata(series_id: str, api_key: str) -> dict:
    """Title, frequency, units, release, and rights note of one series."""
    meta = fetch_json(build_fred_url(series_id, api_key, "series"))["seriess"][0]
    releases = fetch_json(build_fred_url(series_id, api_key, "series/release")).get("releases") or [{}]
    note = rights_note(str(meta.get("notes") or ""))
    return {
        "series_id": series_id,
        "title": meta.get("title"),
        "frequency": meta.get("frequency_short"),
        "units": meta.get("units_short"),
        "release": releases[0].get("name"),
        "license_note": note,
        "personal_use_only": note is not None,
    }


def collect_series(data_dir: Path, series_id: str, api_key: str, dry_run: bool) -> tuple[str, int, str | None]:
    """Collect one FRED series and return status, row count, and output note."""
    if dry_run:
        return "dry_run", 0, None

    payload = fetch_json(build_fred_url(series_id, api_key))
    raw_path = save_raw_latest(data_dir, "fred", series_id, payload)
    rows = normalize_observations(payload)
    if not rows:
        raise ValueError("No FRED observation rows found in response")
    db_path = upsert_macro_db(data_dir, series_id, rows)
    upsert_series_meta(data_dir, [series_metadata(series_id, api_key)])
    return "done", len(rows), f"raw={raw_path} db={db_path}"


def run(args: argparse.Namespace) -> dict:
    ensure_data_dirs(args.data_dir)
    series_ids = args.series if args.series else load_series(args.series_file)
    if args.limit is not None:
        series_ids = series_ids[: args.limit]

    api_key = os.environ.get(args.api_key_env, "")
    if not args.dry_run and not api_key:
        raise RuntimeError(f"Missing FRED API key environment variable: {args.api_key_env}")

    state_path = args.data_dir / "state" / "fred.json"
    fred_state = load_state(state_path) if state_path.exists() else {"schema_version": 1}
    entries = fred_state.setdefault("series", {})
    summary = {"started_at": utc_now_iso(), "dry_run": args.dry_run, "processed": 0, "failed": 0, "series": []}
    for series_id in series_ids:
        entry = entries.setdefault(series_id, {})
        try:
            status, row_count, note = collect_series(args.data_dir, series_id, api_key, args.dry_run)
            summary["processed"] += 1
            summary["series"].append({"series_id": series_id, "status": status, "rows": row_count, "note": note})
            entry.update({"status": status, "rows": row_count, "last_success_at": utc_now_iso(), "error": None})
        except Exception as exc:
            summary["failed"] += 1
            summary["series"].append({"series_id": series_id, "status": "failed", "error": str(exc)})
            entry.update({"status": "failed", "last_attempt_at": utc_now_iso(), "error": str(exc)})
    if not args.dry_run:
        save_state(state_path, fred_state)
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
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
