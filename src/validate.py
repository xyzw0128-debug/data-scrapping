"""CSV data quality checks for collected local files."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path

from src.storage import OHLCV_FIELDS, ensure_data_dirs


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate collected CSV files without external dependencies.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=None, help="Validation report JSON path")
    parser.add_argument("--fail-on-issues", action="store_true", help="Exit 1 when any issue is found")
    return parser.parse_args()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [{key: str(value or "") for key, value in row.items()} for row in csv.DictReader(handle)]


def _parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def validate_ohlcv_file(path: Path) -> list[dict[str, object]]:
    """Validate one OHLCV CSV file."""
    issues: list[dict[str, object]] = []
    rows = _read_csv(path)
    seen_dates: set[str] = set()
    previous_date: date | None = None

    for index, row in enumerate(rows, start=2):
        missing = [field for field in OHLCV_FIELDS if not row.get(field)]
        if missing:
            issues.append({"file": str(path), "line": index, "type": "missing_fields", "fields": missing})

        row_date = row.get("date", "")
        parsed_date = _parse_date(row_date)
        if not parsed_date:
            issues.append({"file": str(path), "line": index, "type": "invalid_date", "date": row_date})
        elif previous_date and parsed_date < previous_date:
            issues.append({"file": str(path), "line": index, "type": "date_out_of_order", "date": row_date})
        if parsed_date:
            previous_date = parsed_date

        if row_date in seen_dates:
            issues.append({"file": str(path), "line": index, "type": "duplicate_date", "date": row_date})
        seen_dates.add(row_date)

        open_value = _parse_float(row.get("open", ""))
        high_value = _parse_float(row.get("high", ""))
        low_value = _parse_float(row.get("low", ""))
        close_value = _parse_float(row.get("close", ""))
        volume_value = _parse_float(row.get("volume", ""))

        numeric_values = {
            "open": open_value,
            "high": high_value,
            "low": low_value,
            "close": close_value,
            "volume": volume_value,
        }
        invalid_numeric = [field for field, value in numeric_values.items() if value is None]
        if invalid_numeric:
            issues.append({"file": str(path), "line": index, "type": "invalid_numeric", "fields": invalid_numeric})
            continue

        if high_value is not None and low_value is not None and high_value < low_value:
            issues.append({"file": str(path), "line": index, "type": "high_below_low", "high": high_value, "low": low_value})
        if all(value is not None for value in [open_value, high_value, low_value, close_value]):
            if not low_value <= open_value <= high_value:
                issues.append({"file": str(path), "line": index, "type": "open_outside_range"})
            if not low_value <= close_value <= high_value:
                issues.append({"file": str(path), "line": index, "type": "close_outside_range"})
        if volume_value is not None and volume_value < 0:
            issues.append({"file": str(path), "line": index, "type": "negative_volume", "volume": volume_value})

    return issues


def validate_required_columns(path: Path, required_fields: list[str], dataset: str) -> list[dict[str, object]]:
    """Validate required columns for a generic CSV file."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
    missing = [field for field in required_fields if field not in fieldnames]
    if missing:
        return [{"file": str(path), "dataset": dataset, "type": "missing_columns", "fields": missing}]
    return []


def validate_directory(data_dir: Path) -> dict[str, object]:
    """Validate known CSV outputs and return a report."""
    ensure_data_dirs(data_dir)
    checks = {
        "ohlcv": sorted((data_dir / "ohlcv").glob("*.csv")),
        "indicators": sorted((data_dir / "indicators").glob("*.csv")),
        "macro": sorted((data_dir / "macro").glob("*.csv")),
        "news": sorted((data_dir / "news").glob("*.csv")),
    }
    issues: list[dict[str, object]] = []

    for path in checks["ohlcv"]:
        issues.extend(validate_ohlcv_file(path))
    for path in checks["indicators"]:
        issues.extend(validate_required_columns(path, [*OHLCV_FIELDS, "sma_20", "rsi_14", "macd_12_26"], "indicators"))
    for path in checks["macro"]:
        issues.extend(validate_required_columns(path, ["date", "value", "realtime_start", "realtime_end"], "macro"))
    for path in checks["news"]:
        issues.extend(validate_required_columns(path, ["id", "symbol", "datetime", "headline", "url"], "news"))

    return {
        "status": "pass" if not issues else "fail",
        "checked_files": {name: len(paths) for name, paths in checks.items()},
        "issue_count": len(issues),
        "issues": issues,
    }


def save_report(path: Path, report: dict[str, object]) -> Path:
    """Save validation report JSON atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)
    return path


def main() -> int:
    args = parse_args()
    report = validate_directory(args.data_dir)
    output = args.output or args.data_dir / "logs" / "validation" / "latest.json"
    report["report_path"] = str(save_report(output, report))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if args.fail_on_issues and report["issue_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
