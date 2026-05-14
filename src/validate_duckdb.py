"""DuckDB-based validation checks for collected local CSV files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.storage import ensure_data_dirs
from src.validate import save_report

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate collected CSV files using DuckDB SQL checks.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=None, help="Validation report JSON path")
    parser.add_argument("--fail-on-issues", action="store_true", help="Exit 1 when any issue is found")
    return parser.parse_args()


def _collect_file_counts(data_dir: Path) -> dict[str, int]:
    return {
        "ohlcv": len(list((data_dir / "ohlcv").glob("*.csv"))),
        "indicators": len(list((data_dir / "indicators").glob("*.csv"))),
        "macro": len(list((data_dir / "macro").glob("*.csv"))),
        "news": len(list((data_dir / "news").glob("*.csv"))),
    }


def validate_with_duckdb(data_dir: Path) -> dict[str, object]:
    ensure_data_dirs(data_dir)

    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError:
        return {
            "status": "error",
            "engine": "duckdb",
            "checked_files": _collect_file_counts(data_dir),
            "issue_count": 1,
            "issues": [
                {
                    "type": "missing_dependency",
                    "dependency": "duckdb",
                    "hint": "Install with: pip install duckdb",
                }
            ],
        }

    con = duckdb.connect(database=":memory:")
    issues: list[dict[str, object]] = []

    ohlcv_glob = str((data_dir / "ohlcv" / "*.csv").as_posix())
    file_count = _collect_file_counts(data_dir)["ohlcv"]
    if file_count > 0:
        duplicates = con.execute(
            """
            SELECT filename, date, COUNT(*) AS duplicate_count
            FROM read_csv_auto(?, filename=true)
            GROUP BY filename, date
            HAVING COUNT(*) > 1
            ORDER BY filename, date
            LIMIT 200
            """,
            [ohlcv_glob],
        ).fetchall()
        for filename, row_date, duplicate_count in duplicates:
            issues.append(
                {
                    "dataset": "ohlcv",
                    "file": str(filename),
                    "type": "duplicate_date",
                    "date": str(row_date),
                    "duplicate_count": int(duplicate_count),
                }
            )

        invalid_ranges = con.execute(
            """
            SELECT filename, date, open, high, low, close, volume
            FROM read_csv_auto(?, filename=true)
            WHERE high < low
               OR open NOT BETWEEN low AND high
               OR close NOT BETWEEN low AND high
               OR volume < 0
            ORDER BY filename, date
            LIMIT 200
            """,
            [ohlcv_glob],
        ).fetchall()
        for filename, row_date, open_value, high_value, low_value, close_value, volume_value in invalid_ranges:
            issues.append(
                {
                    "dataset": "ohlcv",
                    "file": str(filename),
                    "type": "invalid_price_or_volume_range",
                    "date": str(row_date),
                    "open": open_value,
                    "high": high_value,
                    "low": low_value,
                    "close": close_value,
                    "volume": volume_value,
                }
            )

    return {
        "status": "pass" if not issues else "fail",
        "engine": "duckdb",
        "checked_files": _collect_file_counts(data_dir),
        "issue_count": len(issues),
        "issues": issues,
    }


def main() -> int:
    args = parse_args()
    report = validate_with_duckdb(args.data_dir)
    output = args.output or args.data_dir / "logs" / "validation" / "duckdb_latest.json"
    report["report_path"] = str(save_report(output, report))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))

    if report["status"] == "error":
        return 2
    return 1 if args.fail_on_issues and report["issue_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
