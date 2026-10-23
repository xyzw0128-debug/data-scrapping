"""DuckDB-based validation report for local datasets.

``issues`` (duplicate dates, invalid values) make the status ``fail``. ``warnings`` are the
data rules' checks (src.data_rules) and leave the status alone:
- core_gap: a trading day without a bar between a core symbol's first bar (or its history
  start) and its last bar (at most the latest collected session), unless the day is in
  config/known_gaps.txt or config/bar_patches.csv. Gaps that were never stored count too.
- core_stale: a core symbol whose last bar is older than the latest collected session, or
  that has no bars at all.
- closed_day_bars: bars on days the NYSE was closed (core symbols listed).
- before_history_start: bars older than config/history_start.txt allows.
- patch_missing: a config/bar_patches.csv bar that is not in ohlcv (skipped or not fetched yet).
The report carries generated_at and the pipeline run id, so src.summary shows only this run's
result.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from src.atomic_io import atomic_write_json, json_default
from src.data_rules import DataRules, DataRulesError, load_data_rules
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.market_calendar import expected_session, trading_days
from src.state import utc_now_iso
from src.storage import get_db_path

ROOT = Path(__file__).resolve().parents[1]
RUN_ID_ENV = "DATA_SCRAPPING_RUN_ID"
CORE_GROUPS = ["core_etf", "core_sp500", "core_extra"]
MEMORY_LIMIT = "1GB"
# Items listed per warning type; the count is always complete.
MAX_ITEMS = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DuckDB SQL validations for OHLCV data.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config", help="Data rules (src.data_rules)")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--fail-on-issues", action="store_true")
    return parser.parse_args()


def _warning(kind: str, items: list[dict[str, object]], **extra: object) -> dict[str, object]:
    return {"type": kind, "count": len(items), **extra, "items": items[:MAX_ITEMS]}


def data_rule_warnings(con, rules: DataRules) -> list[dict[str, object]]:
    """Warnings for the data rules on an open market.db connection (see the module docstring)."""
    tables = {row[0] for row in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    if "ohlcv" not in tables:
        return []
    first_day, last_day = con.execute("SELECT min(date), max(date) FROM ohlcv").fetchone()
    if first_day is None:
        return []
    calendar = [day.isoformat() for day in trading_days(first_day, last_day)]
    starts = sorted(rules.history_start.items())
    start_args = [[symbol for symbol, _ in starts], [day for _, day in starts]]
    patch_days = sorted({(p.symbol, p.date) for items in rules.patches.values() for p in items})
    skip_days = sorted(rules.known_gaps | set(patch_days))
    core: list[str] = []
    if "symbol_status" in tables:
        core = [row[0] for row in con.execute(
            "SELECT symbol FROM symbol_status WHERE active AND group_name IN (SELECT unnest(CAST(? AS VARCHAR[]))) ORDER BY symbol", [CORE_GROUPS]
        ).fetchall()]
    session = None
    if "collection_runs" in tables:
        session = con.execute("SELECT CAST(max(session_date) AS VARCHAR) FROM collection_runs").fetchone()[0]
    session = session or expected_session(datetime.now(UTC)).isoformat()

    warnings: list[dict[str, object]] = []
    gaps = con.execute(
        """
        WITH cal AS (SELECT unnest(CAST(? AS DATE[])) AS d),
        core AS (SELECT unnest(CAST(? AS VARCHAR[])) AS symbol),
        hs AS (SELECT unnest(CAST(? AS VARCHAR[])) AS symbol, unnest(CAST(? AS DATE[])) AS start_d),
        skip AS (SELECT unnest(CAST(? AS VARCHAR[])) AS symbol, unnest(CAST(? AS DATE[])) AS d),
        ranges AS (
            SELECT o.symbol, greatest(min(o.date), coalesce(any_value(hs.start_d), min(o.date))) AS start_d,
                   least(max(o.date), CAST(? AS DATE)) AS end_d
            FROM ohlcv o JOIN core USING (symbol) LEFT JOIN hs USING (symbol)
            GROUP BY o.symbol
        )
        SELECT r.symbol, CAST(c.d AS VARCHAR)
        FROM ranges r JOIN cal c ON c.d BETWEEN r.start_d AND r.end_d
        ANTI JOIN ohlcv o ON o.symbol = r.symbol AND o.date = c.d
        ANTI JOIN skip s ON s.symbol = r.symbol AND s.d = c.d
        ORDER BY 1, 2
        """,
        [calendar, core, *start_args, [s for s, _ in skip_days], [d for _, d in skip_days], session],
    ).fetchall()
    if gaps:
        warnings.append(_warning("core_gap", [{"symbol": s, "date": d} for s, d in gaps]))

    stale = con.execute(
        """
        WITH core AS (SELECT unnest(CAST(? AS VARCHAR[])) AS symbol)
        SELECT c.symbol, CAST(max(o.date) AS VARCHAR) FROM core c LEFT JOIN ohlcv o USING (symbol)
        GROUP BY c.symbol HAVING max(o.date) IS NULL OR max(o.date) < CAST(? AS DATE) ORDER BY 1
        """,
        [core, session],
    ).fetchall()
    if stale:
        warnings.append(_warning("core_stale", [{"symbol": s, "last_bar_date": d} for s, d in stale], session_date=session))

    closed = con.execute(
        """
        WITH cal AS (SELECT unnest(CAST(? AS DATE[])) AS d)
        SELECT o.symbol, CAST(o.date AS VARCHAR) FROM ohlcv o ANTI JOIN cal c ON c.d = o.date ORDER BY 1, 2
        """,
        [calendar],
    ).fetchall()
    if closed:
        core_set = set(core)
        core_items = [{"symbol": s, "date": d} for s, d in closed if s in core_set]
        warnings.append({
            "type": "closed_day_bars", "count": len(closed), "symbols": len({s for s, _ in closed}),
            "core_count": len(core_items), "items": core_items[:MAX_ITEMS],
        })

    early = con.execute(
        """
        WITH hs AS (SELECT unnest(CAST(? AS VARCHAR[])) AS symbol, unnest(CAST(? AS DATE[])) AS start_d)
        SELECT o.symbol, count(*), CAST(min(o.date) AS VARCHAR) FROM ohlcv o JOIN hs ON hs.symbol = o.symbol AND o.date < hs.start_d
        GROUP BY 1 ORDER BY 1
        """,
        start_args,
    ).fetchall()
    if early:
        items = [{"symbol": s, "rows": n, "first_date": d} for s, n, d in early]
        warnings.append({"type": "before_history_start", "count": sum(n for _, n, _ in early), "items": items[:MAX_ITEMS]})

    missing = con.execute(
        """
        WITH p AS (SELECT unnest(CAST(? AS VARCHAR[])) AS symbol, unnest(CAST(? AS DATE[])) AS d)
        SELECT p.symbol, CAST(p.d AS VARCHAR) FROM p ANTI JOIN ohlcv o ON o.symbol = p.symbol AND o.date = p.d ORDER BY 1, 2
        """,
        [[s for s, _ in patch_days], [d for _, d in patch_days]],
    ).fetchall()
    if missing:
        warnings.append(_warning("patch_missing", [{"symbol": s, "date": d} for s, d in missing]))
    return warnings


def validate_with_duckdb(data_dir: Path, rules: DataRules | None = None) -> dict[str, object]:
    try:
        import duckdb  # type: ignore
    except Exception as exc:
        return {"status": "error", "engine": "duckdb", "issue_count": 1, "issues": [{"type": "dependency_missing", "error": str(exc)}], "warning_count": 0, "warnings": []}

    db_path = get_db_path(data_dir)
    if not db_path.exists():
        return {"status": "pass", "engine": "duckdb", "issue_count": 0, "checked_tables": {}, "issues": [], "warning_count": 0, "warnings": [], "note": "market.db not found"}

    con = duckdb.connect(str(db_path), read_only=True)
    issues: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    try:
        con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
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

        if rules is not None:
            warnings = data_rule_warnings(con, rules)
    finally:
        con.close()

    return {
        "status": "pass" if not issues else "fail", "engine": "duckdb", "db_path": str(db_path), "checked_tables": tables,
        "issue_count": len(issues), "issues": issues, "warning_count": sum(int(w["count"]) for w in warnings), "warnings": warnings,
    }


def main() -> int:
    args = parse_args()
    rules: DataRules | None = None
    rules_error = None
    try:
        rules = load_data_rules(args.config_dir)
    except DataRulesError as exc:
        rules_error = str(exc)
    try:
        with pipeline_lock(args.data_dir):
            report = validate_with_duckdb(args.data_dir, rules)
    except LockHeldError as exc:
        return lock_held_exit(exc)
    if rules_error:
        report["issues"].append({"type": "data_rules_invalid", "error": rules_error})
        report["issue_count"] = len(report["issues"])
        report["status"] = "fail"
    report["generated_at"] = utc_now_iso()
    report["run_id"] = os.environ.get(RUN_ID_ENV) or None
    output = args.output or args.data_dir / "logs" / "validation" / "duckdb_latest.json"
    report["report_path"] = str(atomic_write_json(output, report))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=json_default))
    return 1 if args.fail_on_issues and report.get("issue_count") else 0


if __name__ == "__main__":
    raise SystemExit(main())
