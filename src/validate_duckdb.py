"""DuckDB-based data quality checks for collected local CSV files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate collected CSV files with DuckDB.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=None, help="Validation report JSON path")
    parser.add_argument("--fail-on-issues", action="store_true", help="Exit 1 when any issue is found")
    return parser.parse_args()


def _load_duckdb():
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "duckdb is not installed. Install with: pip install duckdb"
        ) from exc
    return duckdb


def _table_exists(con, table_name: str) -> bool:
    row = con.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = 'main' AND table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and int(row[0]) > 0)


def _register_csv_table(con, table_name: str, pattern: str) -> bool:
    sql = f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM read_csv_auto(?, union_by_name=true, filename=true)"
    try:
        con.execute(sql, [pattern])
    except Exception:
        return False
    return _table_exists(con, table_name)


def _append_issue(issues: list[dict[str, object]], dataset: str, issue_type: str, count: int, detail: str) -> None:
    if count <= 0:
        return
    issues.append({"dataset": dataset, "type": issue_type, "count": int(count), "detail": detail})


def validate_with_duckdb(data_dir: Path) -> dict[str, object]:
    duckdb = _load_duckdb()
    con = duckdb.connect(database=":memory:")

    checks: dict[str, bool] = {
        "ohlcv": _register_csv_table(con, "ohlcv", str(data_dir / "ohlcv" / "*.csv")),
        "indicators": _register_csv_table(con, "indicators", str(data_dir / "indicators" / "*.csv")),
        "macro": _register_csv_table(con, "macro", str(data_dir / "macro" / "*.csv")),
        "news": _register_csv_table(con, "news", str(data_dir / "news" / "*.csv")),
    }

    issues: list[dict[str, object]] = []

    if checks["ohlcv"]:
        duplicate_count = con.execute(
            """
            SELECT COUNT(*)
            FROM (
              SELECT filename, date, COUNT(*) AS c
              FROM ohlcv
              GROUP BY filename, date
              HAVING COUNT(*) > 1
            ) t
            """
        ).fetchone()[0]
        _append_issue(issues, "ohlcv", "duplicate_date", duplicate_count, "duplicate date rows per file")

        missing_required_count = con.execute(
            """
            SELECT COUNT(*)
            FROM ohlcv
            WHERE coalesce(date, '') = ''
               OR coalesce(open, '') = ''
               OR coalesce(high, '') = ''
               OR coalesce(low, '') = ''
               OR coalesce(close, '') = ''
               OR coalesce(volume, '') = ''
            """
        ).fetchone()[0]
        _append_issue(issues, "ohlcv", "missing_required", missing_required_count, "required OHLCV field is empty")

        high_below_low_count = con.execute(
            """
            SELECT COUNT(*)
            FROM ohlcv
            WHERE try_cast(high AS DOUBLE) IS NOT NULL
              AND try_cast(low AS DOUBLE) IS NOT NULL
              AND try_cast(high AS DOUBLE) < try_cast(low AS DOUBLE)
            """
        ).fetchone()[0]
        _append_issue(issues, "ohlcv", "high_below_low", high_below_low_count, "high is below low")

        negative_volume_count = con.execute(
            """
            SELECT COUNT(*)
            FROM ohlcv
            WHERE try_cast(volume AS DOUBLE) IS NOT NULL
              AND try_cast(volume AS DOUBLE) < 0
            """
        ).fetchone()[0]
        _append_issue(issues, "ohlcv", "negative_volume", negative_volume_count, "volume is negative")

    if checks["indicators"]:
        missing_cols = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "sma_20",
            "rsi_14",
            "macd_12_26",
        ]
        missing_cols_count = 0
        for col in missing_cols:
            exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='indicators' AND column_name=?",
                [col],
            ).fetchone()[0]
            if int(exists) == 0:
                missing_cols_count += 1
        _append_issue(issues, "indicators", "missing_columns", missing_cols_count, "required indicator columns missing")

    if checks["macro"]:
        missing_cols = ["date", "value", "realtime_start", "realtime_end"]
        missing_cols_count = 0
        for col in missing_cols:
            exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='macro' AND column_name=?",
                [col],
            ).fetchone()[0]
            if int(exists) == 0:
                missing_cols_count += 1
        _append_issue(issues, "macro", "missing_columns", missing_cols_count, "required macro columns missing")

    if checks["news"]:
        missing_cols = ["id", "symbol", "datetime", "headline", "url"]
        missing_cols_count = 0
        for col in missing_cols:
            exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='news' AND column_name=?",
                [col],
            ).fetchone()[0]
            if int(exists) == 0:
                missing_cols_count += 1
        _append_issue(issues, "news", "missing_columns", missing_cols_count, "required news columns missing")

    report = {
        "engine": "duckdb",
        "status": "pass" if not issues else "fail",
        "checked_files": checks,
        "issue_count": len(issues),
        "issues": issues,
    }
    con.close()
    return report


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
    try:
        report = validate_with_duckdb(args.data_dir)
    except RuntimeError as exc:
        error_report = {
            "engine": "duckdb",
            "status": "error",
            "issue_count": 0,
            "issues": [],
            "error": str(exc),
        }
        print(json.dumps(error_report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    output = args.output or args.data_dir / "logs" / "validation" / "latest_duckdb.json"
    report["report_path"] = str(save_report(output, report))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if args.fail_on_issues and report["issue_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
