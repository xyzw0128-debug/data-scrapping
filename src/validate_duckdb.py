"""DuckDB-based validation report for local datasets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.storage import get_db_path
from src.validate import save_report

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DuckDB SQL validations for OHLCV data.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--fail-on-issues", action="store_true")
    return parser.parse_args()


def validate_with_duckdb(data_dir: Path) -> dict[str, object]:
    try:
        import duckdb  # type: ignore
    except Exception as exc:
        return {"status": "error", "engine": "duckdb", "issue_count": 1, "issues": [{"type": "dependency_missing", "error": str(exc)}]}

    db_path = get_db_path(data_dir)
    if not db_path.exists():
        return {"status": "pass", "engine": "duckdb", "issue_count": 0, "checked_tables": {}, "issues": [], "note": "market.db not found"}

    con = duckdb.connect(str(db_path), read_only=True)
    issues: list[dict[str, object]] = []
    try:
        tables = {
            "ohlcv": con.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0],
            "indicators": con.execute("SELECT COUNT(*) FROM indicators").fetchone()[0],
            "macro": con.execute("SELECT COUNT(*) FROM macro").fetchone()[0],
            "news": con.execute("SELECT COUNT(*) FROM news").fetchone()[0],
        }

        dupes = con.execute("SELECT symbol, date, COUNT(*) AS cnt FROM ohlcv GROUP BY symbol, date HAVING COUNT(*) > 1").fetchall()
        for sym, dt, cnt in dupes:
            issues.append({"dataset": "ohlcv", "type": "duplicate_date", "symbol": sym, "date": dt, "count": cnt})

        invalid = con.execute(
            """
            SELECT symbol, date, open, high, low, close, volume
            FROM ohlcv
            WHERE TRY_CAST(high AS DOUBLE) < TRY_CAST(low AS DOUBLE)
               OR TRY_CAST(volume AS DOUBLE) < 0
               OR TRY_CAST(open AS DOUBLE) IS NULL
               OR TRY_CAST(high AS DOUBLE) IS NULL
               OR TRY_CAST(low AS DOUBLE) IS NULL
               OR TRY_CAST(close AS DOUBLE) IS NULL
               OR TRY_CAST(volume AS DOUBLE) IS NULL
            """
        ).fetchall()
        for row in invalid:
            issues.append({"dataset": "ohlcv", "type": "invalid_numeric_or_range", "symbol": row[0], "date": row[1]})
    finally:
        con.close()

    return {"status": "pass" if not issues else "fail", "engine": "duckdb", "db_path": str(db_path), "checked_tables": tables, "issue_count": len(issues), "issues": issues}


def main() -> int:
    args = parse_args()
    report = validate_with_duckdb(args.data_dir)
    output = args.output or args.data_dir / "logs" / "validation" / "duckdb_latest.json"
    report["report_path"] = str(save_report(output, report))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if args.fail_on_issues and report.get("issue_count") else 0


if __name__ == "__main__":
    raise SystemExit(main())
