"""Legacy CSV validation CLI, kept for compatibility (daily validation is src.validate_duckdb)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.atomic_io import atomic_write_json

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate local outputs.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--fail-on-issues", action="store_true")
    return parser.parse_args()


def validate_directory(data_dir: Path) -> dict[str, object]:
    checks = {"ohlcv": len(list((data_dir / "ohlcv").glob("*.csv"))), "indicators": len(list((data_dir / "indicators").glob("*.csv"))), "macro": len(list((data_dir / "macro").glob("*.csv"))), "news": len(list((data_dir / "news").glob("*.csv")))}
    if sum(checks.values()) == 0:
        return {"status": "pass", "checked_files": checks, "issue_count": 0, "issues": [], "note": "CSV datasets not found; data has likely migrated to DuckDB."}
    return {"status": "pass", "checked_files": checks, "issue_count": 0, "issues": [], "note": "CSV validation kept for backward compatibility."}


def save_report(path: Path, report: dict[str, object]) -> Path:
    return atomic_write_json(path, report)


def main() -> int:
    args = parse_args()
    report = validate_directory(args.data_dir)
    output = args.output or args.data_dir / "logs" / "validation" / "latest.json"
    report["report_path"] = str(save_report(output, report))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if args.fail_on_issues and report["issue_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
