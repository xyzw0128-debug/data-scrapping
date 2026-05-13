"""Storage helpers for raw responses and normalized OHLCV CSV files."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from src.state import utc_now_iso


OHLCV_FIELDS = ["date", "open", "high", "low", "close", "volume"]


def ensure_data_dirs(data_dir: Path) -> None:
    """Create the simple MVP data directory layout."""
    for relative in ["raw", "ohlcv", "state", "logs"]:
        (data_dir / relative).mkdir(parents=True, exist_ok=True)


def verify_writable(path: Path) -> None:
    """Verify that a path can be written to and renamed within the same directory."""
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write_probe.tmp"
    target = path / ".write_probe.ok"
    probe.write_text("ok\n", encoding="utf-8")
    os.replace(probe, target)
    target.unlink()


def save_raw_json(data_dir: Path, provider: str, symbol: str, payload: dict[str, Any]) -> Path:
    """Save a raw provider response as JSON."""
    safe_symbol = symbol.replace("/", "_")
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    raw_dir = data_dir / "raw" / provider
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{safe_symbol}_{stamp}.json"
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path


def normalize_twelve_data_ohlcv(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize a Twelve Data time_series payload into OHLCV rows."""
    values = payload.get("values")
    if not isinstance(values, list):
        message = payload.get("message") or payload.get("status") or "missing values"
        raise ValueError(f"Twelve Data response has no values: {message}")

    rows: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        row = {
            "date": str(item.get("datetime", "")),
            "open": str(item.get("open", "")),
            "high": str(item.get("high", "")),
            "low": str(item.get("low", "")),
            "close": str(item.get("close", "")),
            "volume": str(item.get("volume", "")),
        }
        if row["date"]:
            rows.append(row)
    rows.sort(key=lambda row: row["date"])
    return rows


def upsert_ohlcv_csv(data_dir: Path, symbol: str, rows: list[dict[str, str]]) -> Path:
    """Write symbol-level OHLCV CSV, replacing duplicate dates."""
    safe_symbol = symbol.replace("/", "_")
    output_dir = data_dir / "ohlcv"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{safe_symbol}.csv"

    merged: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("date"):
                    merged[str(row["date"])] = {field: str(row.get(field, "")) for field in OHLCV_FIELDS}

    for row in rows:
        date = row.get("date", "")
        if date:
            merged[date] = {field: str(row.get(field, "")) for field in OHLCV_FIELDS}

    tmp_path = path.with_suffix(".csv.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OHLCV_FIELDS)
        writer.writeheader()
        for date in sorted(merged):
            writer.writerow(merged[date])
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path
