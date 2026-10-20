"""Publish a read-only copy of market.db for stockpred, with a completion record.

DuckDB lets no other process open a database file while one process has it open for
writing (not even read-only), so stockpred reads data/db/market_snapshot.db instead.
Under the pipeline lock this module records symbol_status and a collection_runs row in
market.db, checkpoints, copies the file, verifies the copy, and swaps it in atomically.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.env import load_project_env
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.market_calendar import expected_session
from src.scheduling import peek_entry
from src.state import load_state
from src.storage import connect_db, ensure_db_schema, get_db_path, get_snapshot_path
from src.universe import MIN_SP500_MEMBERS, Universe, load_universe, yahoo_symbol


ROOT = Path(__file__).resolve().parents[1]
PROVIDER = "twelve_data"
RUN_ID_ENV = "DATA_SCRAPPING_RUN_ID"
RUN_STARTED_ENV = "DATA_SCRAPPING_RUN_STARTED_AT"
# Without a run start time, FRED series count as fresh when fetched within this window.
DEFAULT_FRED_WINDOW = timedelta(hours=20)


def fred_state_path(data_dir: Path) -> Path:
    return data_dir / "state" / "fred.json"


def load_series_ids(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]


def splits_complete(entry: dict) -> bool:
    """Split history was derived and has not been invalidated by a later split."""
    return bool(entry.get("splits_checked_at")) and not entry.get("splits_stale")


def evaluate_completion(
    universe: Universe,
    state: dict,
    fred_state: dict,
    series: list[str],
    session: str,
    since: str,
    min_sp500: int = MIN_SP500_MEMBERS,
) -> dict:
    """Decide complete / partial / failed for the core universe.

    complete: the S&P 500 list is loaded, every core symbol has the session bar, adj_close,
    and its split history, and every FRED series succeeded since ``since``.
    failed: the universe is not initialized, or no core symbol has the session bar.
    """
    core = universe.core_symbols()
    entries = {symbol: peek_entry(state, symbol, PROVIDER) for symbol in core}
    missing = [symbol for symbol in core if entries[symbol].get("core_session") != session]
    missing_splits = [symbol for symbol in core if not splits_complete(entries[symbol])]
    fred_entries = fred_state.get("series", {})
    missing_series = [sid for sid in series if str(fred_entries.get(sid, {}).get("last_success_at") or "") < since]
    universe_issue = None
    if len(universe.sp500) < min_sp500:
        universe_issue = f"universe_not_initialized: {len(universe.sp500)} S&P 500 members (< {min_sp500})"
    core_ok = len(core) - len(missing)
    if universe_issue or (core and core_ok == 0):
        status = "failed"
    elif missing or missing_splits or missing_series:
        status = "partial"
    else:
        status = "complete"
    return {
        "status": status,
        "session_date": session,
        "core_expected": len(core),
        "core_ok": core_ok,
        "missing_symbols": missing,
        "splits_ok": len(core) - len(missing_splits),
        "missing_splits": missing_splits,
        "fred_expected": len(series),
        "fred_ok": len(series) - len(missing_series),
        "missing_series": missing_series,
        "universe_issue": universe_issue,
    }


def symbol_status_rows(universe: Universe, state: dict, last_bars: dict[str, object], now: str) -> list[list[object]]:
    rows = []
    for symbol in universe.all_symbols():
        entry = peek_entry(state, symbol, PROVIDER)
        delisted = universe.delisted.get(symbol)
        rows.append([
            symbol,
            yahoo_symbol(symbol),
            universe.group_of(symbol),
            delisted is None,
            delisted.get("marked_at") if delisted else None,
            last_bars.get(symbol),
            entry.get("last_success_at"),
            entry.get("core_session"),
            splits_complete(entry),
            now,
        ])
    return rows


def _copy_verified(source: Path, target: Path, run_id: str, phase: str) -> None:
    import duckdb  # type: ignore

    tmp = target.with_name(f".{target.name}.tmp")
    tmp.unlink(missing_ok=True)
    shutil.copyfile(source, tmp)
    with tmp.open("rb") as handle:
        os.fsync(handle.fileno())
    con = duckdb.connect(str(tmp), read_only=True)
    try:
        found = con.execute("SELECT count(*) FROM collection_runs WHERE run_id = ? AND phase = ?", [run_id, phase]).fetchone()[0]
    finally:
        con.close()
    if found != 1:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("snapshot copy is missing its collection_runs row")
    os.replace(tmp, target)


def create_snapshot(
    data_dir: Path,
    config_dir: Path,
    state_path: Path,
    phase: str,
    run_id: str,
    started_at: str | None,
    note: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    min_sp500: int = MIN_SP500_MEMBERS,
) -> dict:
    now = now or datetime.now(UTC)
    now_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    since = started_at or (now - DEFAULT_FRED_WINDOW).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    universe = load_universe(data_dir, config_dir)
    state = load_state(state_path)
    fred_path = fred_state_path(data_dir)
    fred_state = json.loads(fred_path.read_text(encoding="utf-8")) if fred_path.exists() else {}
    session = expected_session(now).isoformat()
    result = evaluate_completion(universe, state, fred_state, load_series_ids(config_dir / "fred_series.txt"), session, since, min_sp500)
    result.update({"run_id": run_id, "phase": phase, "started_at": started_at, "finished_at": now_iso, "note": note})
    if dry_run:
        return result

    ensure_db_schema(data_dir)
    con = connect_db(data_dir)
    try:
        last_bars = dict(con.execute("SELECT symbol, max(date) FROM ohlcv GROUP BY symbol").fetchall())
        con.begin()
        con.execute("DELETE FROM symbol_status")
        con.executemany(
            "INSERT INTO symbol_status VALUES (?, ?, ?, ?, ?, CAST(? AS DATE), ?, CAST(? AS DATE), ?, ?)",
            symbol_status_rows(universe, state, last_bars, now_iso),
        )
        con.execute(
            """INSERT OR REPLACE INTO collection_runs
               (run_id, phase, started_at, finished_at, session_date, status, core_expected, core_ok, missing_symbols,
                fred_expected, fred_ok, missing_series, note, splits_ok, missing_splits, universe_issue)
               VALUES (?, ?, ?, ?, CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                run_id, phase, started_at, now_iso, session, result["status"],
                result["core_expected"], result["core_ok"], json.dumps(result["missing_symbols"]),
                result["fred_expected"], result["fred_ok"], json.dumps(result["missing_series"]), note,
                result["splits_ok"], json.dumps(result["missing_splits"]), result["universe_issue"],
            ],
        )
        con.commit()
        con.execute("CHECKPOINT")
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    _copy_verified(get_db_path(data_dir), get_snapshot_path(data_dir), run_id, phase)
    result["snapshot_path"] = str(get_snapshot_path(data_dir))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record collection status and publish market_snapshot.db.")
    parser.add_argument("--phase", choices=["core", "full"], default="full")
    parser.add_argument("--run-id", default=None, help=f"Default: ${RUN_ID_ENV} or a new id")
    parser.add_argument("--run-started-at", default=None, help=f"FRED freshness cutoff; default: ${RUN_STARTED_ENV} or now-20h")
    parser.add_argument("--note", default=None, help="Free text stored with the run (e.g. failed stages)")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "state" / "state.json")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate completion without writing anything")
    return parser.parse_args()


def main() -> int:
    load_project_env(ROOT)
    args = parse_args()
    run_id = args.run_id or os.environ.get(RUN_ID_ENV) or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started_at = args.run_started_at or os.environ.get(RUN_STARTED_ENV) or None
    try:
        with pipeline_lock(args.data_dir):
            result = create_snapshot(args.data_dir, args.config_dir, args.state, args.phase, run_id, started_at, args.note, dry_run=args.dry_run)
    except LockHeldError as exc:
        return lock_held_exit(exc)
    printable = {
        **result,
        "missing_symbols": result["missing_symbols"][:50],
        "missing_symbol_count": len(result["missing_symbols"]),
        "missing_splits": result["missing_splits"][:50],
        "missing_split_count": len(result["missing_splits"]),
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result["status"] == "failed" and not args.dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
