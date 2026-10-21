"""Rebuild market.db into a new file: the one-off schema v4 conversion and periodic compaction.

Replacing every row of a symbol each day leaves the deleted rows' space inside the file and
DuckDB's VACUUM does not return it; only copying into a new file does (operating DB copy,
2026-10-21: 2,273 MB -> 2,504 MB -> 2,581 MB over two replays of the core run). Both jobs share
one procedure, run under the pipeline lock:

1. CHECKPOINT market.db and close it; stop if market.db.wal is still there.
2. Attach market.db READ_ONLY and build a temporary file in the same directory
   (``--to-v4``: copy the schema, recreate ohlcv/indicators with typed columns after checking
   that every non-empty value converts exactly; otherwise ``COPY FROM DATABASE``).
3. Close the temporary file, reopen it READ_ONLY and verify it against the source: tables,
   row counts, constraints (primary keys), the version 4 table layout, view columns and types,
   and row-hash checksums of every table and view (``prices_daily`` included).
4. Hard-link the current file as market.db.bak-<UTC date> and ``os.replace`` the new file over
   market.db. Any failure before the replace removes only the temporary file; market.db is
   never moved aside. Only after a successful replace are older backups removed, keeping the
   newest ``--keep-backups`` (2).

The size right after a rebuild is the baseline in data/state/db_maintenance.json.
``--if-needed`` (run_daily.sh, just before the full snapshot) rebuilds only when market.db has
grown past ``--growth-factor`` (2) times that baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.atomic_io import atomic_write_json
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.storage import REBUILD_HINT, SCHEMA_VERSION, TABLE_SQL, create_schema, create_views, get_db_path, schema_version


ROOT = Path(__file__).resolve().parents[1]
STATE_NAME = "db_maintenance.json"
DEFAULT_GROWTH_FACTOR = 2.0
# A version 3 rebuild needs more than 2 GB (VARCHAR primary keys); typed tables fit in 2 GB.
CONVERT_MEMORY_LIMIT = "3GB"
REBUILD_MEMORY_LIMIT = "2GB"
DEFAULT_THREADS = 4
HISTORY_KEEP = 20
# Backups (market.db.bak-*) left after a successful rebuild, newest first; the one just made counts.
KEEP_BACKUPS = 2
# The new file is never larger than the source; keep that much free plus a margin.
FREE_SPACE_FACTOR = 1.1
# Tables whose version 3 VARCHAR columns become DATE/DOUBLE/BIGINT in version 4.
CONVERTED_TABLES = ("ohlcv", "indicators")
CONVERT_FROM_VERSION = 3


class RebuildError(RuntimeError):
    """The rebuild stopped before replacing market.db (the original file is untouched)."""


def maintenance_path(data_dir: Path) -> Path:
    return data_dir / "state" / STATE_NAME


def load_maintenance(data_dir: Path) -> dict[str, Any]:
    path = maintenance_path(data_dir)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def temp_db_path(db_path: Path) -> Path:
    return db_path.with_name(f".{db_path.name}.rebuild.tmp")


def spill_dir_path(db_path: Path) -> Path:
    return db_path.with_name(f".{db_path.name}.rebuild-spill.tmp")


def wal_path(path: Path) -> Path:
    return path.with_name(path.name + ".wal")


def backup_path(db_path: Path, now: datetime) -> Path:
    path = db_path.with_name(f"{db_path.name}.bak-{now:%Y-%m-%d}")
    if path.exists():  # a second rebuild on the same UTC day keeps the earlier backup too
        path = db_path.with_name(f"{db_path.name}.bak-{now:%Y-%m-%dT%H%M%SZ}")
    return path


def prune_backups(db_path: Path, keep: int) -> list[str]:
    """Delete all but the newest ``keep`` market.db.bak-* files; names sort by rebuild time."""
    backups = sorted(path for path in db_path.parent.glob(f"{db_path.name}.bak-*") if path.is_file())
    removed = backups[: max(0, len(backups) - keep)]
    for path in removed:
        path.unlink()
    return [str(path) for path in removed]


def _utc_iso(now: datetime) -> str:
    return now.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _connect(path: Path | str, memory_limit: str, threads: int, spill_dir: Path, read_only: bool = False):
    import duckdb  # type: ignore

    config = {"memory_limit": memory_limit, "threads": threads, "temp_directory": str(spill_dir)}
    return duckdb.connect(str(path), read_only=read_only, config=config)


def rebuild_check(data_dir: Path, growth_factor: float = DEFAULT_GROWTH_FACTOR) -> dict[str, Any]:
    """Whether market.db has grown past growth_factor x the size right after the last rebuild."""
    db_path = get_db_path(data_dir)
    size = db_path.stat().st_size if db_path.exists() else None
    baseline = load_maintenance(data_dir).get("baseline_bytes")
    threshold = int(baseline * growth_factor) if baseline else None
    return {
        "size_bytes": size,
        "baseline_bytes": baseline,
        "growth_factor": growth_factor,
        "threshold_bytes": threshold,
        "due": size is not None and threshold is not None and size > threshold,
    }


def initialize_baseline(data_dir: Path, size: int, now: datetime) -> None:
    """Without any rebuild yet, the current size becomes the baseline (no rebuild is done)."""
    state = load_maintenance(data_dir)
    state.update({"baseline_bytes": size, "baseline_initialized_at": _utc_iso(now)})
    atomic_write_json(maintenance_path(data_dir), state)


def record_rebuild(data_dir: Path, result: dict[str, Any]) -> None:
    keys = ["mode", "finished_at", "size_before_bytes", "size_after_bytes", "backup_path", "pruned_backups", "schema_version", "duration_sec"]
    entry = {key: result.get(key) for key in keys}
    state = load_maintenance(data_dir)
    state.update({"baseline_bytes": result["size_after_bytes"], "last_rebuild": entry})
    state["history"] = [*state.get("history", []), entry][-HISTORY_KEEP:]
    atomic_write_json(maintenance_path(data_dir), state)


# ---- build -------------------------------------------------------------------------------


def _v4_layout(memory_limit: str, threads: int, spill_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """(column, type) lists of every version 4 table, from an empty in-memory database."""
    con = _connect(":memory:", memory_limit, threads, spill_dir)
    try:
        create_schema(con)
        return {table: [(row[0], row[1]) for row in con.execute(f"DESCRIBE {table}").fetchall()] for table in TABLE_SQL}
    finally:
        con.close()


def _convert_expr(column: str, column_type: str) -> tuple[str, str]:
    """(copy expression, invalid-value condition) for a version 3 VARCHAR column.

    "" becomes NULL. Any other value must convert exactly: CAST alone would round '1.5' to a
    BIGINT 2 and accept 'nan', so whole numbers and finite doubles are checked first.
    """
    if column_type == "VARCHAR":
        return column, "false"
    if column_type == "DATE":
        return f"CAST({column} AS DATE)", f"TRY_CAST({column} AS DATE) IS NULL"
    if column_type == "DOUBLE":
        return (
            f"CAST(NULLIF({column}, '') AS DOUBLE)",
            f"NULLIF({column}, '') IS NOT NULL AND NOT coalesce(isfinite(TRY_CAST({column} AS DOUBLE)), false)",
        )
    if column_type == "BIGINT":
        as_int, as_double = f"TRY_CAST({column} AS BIGINT)", f"TRY_CAST({column} AS DOUBLE)"
        return (
            f"CAST(NULLIF({column}, '') AS BIGINT)",
            f"NULLIF({column}, '') IS NOT NULL AND ({as_int} IS NULL OR {as_double} <> {as_int} OR {as_int} < 0)",
        )
    raise RebuildError(f"no version 3 conversion for column type {column_type}")


def _source_columns(con, table: str) -> dict[str, str]:
    return {row[0]: row[1] for row in con.execute(f"DESCRIBE src.main.{table}").fetchall()}


def _check_convertible(con, layout: dict[str, list[tuple[str, str]]]) -> dict[str, dict[str, int]]:
    """Count non-empty source values that would not convert exactly; any count stops the rebuild."""
    invalid: dict[str, dict[str, int]] = {}
    for table in CONVERTED_TABLES:
        source = _source_columns(con, table)
        missing = [column for column, _ in layout[table] if column not in source]
        typed = [column for column, _ in layout[table] if source.get(column) not in (None, "VARCHAR")]
        if missing or typed:
            raise RebuildError(f"{table} is not a version 3 table (missing columns {missing}, non-VARCHAR columns {typed})")
        conditions = {column: _convert_expr(column, column_type)[1] for column, column_type in layout[table] if column_type != "VARCHAR"}
        select = ", ".join(f"count(*) FILTER (WHERE {condition})" for condition in conditions.values())
        counts = con.execute(f"SELECT {select} FROM src.main.{table}").fetchone()
        invalid[table] = {column: int(count) for column, count in zip(conditions, counts) if count}
    if any(invalid.values()):
        raise RebuildError(f"values that do not convert to version 4 types: {invalid}")
    return invalid


def _build_v4(con, layout: dict[str, list[tuple[str, str]]]) -> None:
    """Copy the source schema and data into dst, with ohlcv/indicators recreated as typed tables."""
    _check_convertible(con, layout)
    con.execute("COPY FROM DATABASE src TO dst (SCHEMA)")
    con.execute("USE dst")
    try:
        for table in CONVERTED_TABLES:
            con.execute(f"DROP TABLE {table}")
            con.execute(TABLE_SQL[table])
        source_tables = [row[0] for row in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'src' AND schema_name = 'main'"
        ).fetchall()]
        for table in source_tables:
            if table in CONVERTED_TABLES:
                columns = [column for column, _ in layout[table]]
                exprs = [_convert_expr(column, column_type)[0] for column, column_type in layout[table]]
                con.execute(f"INSERT INTO {table} ({', '.join(columns)}) SELECT {', '.join(exprs)} FROM src.main.{table}")
            else:
                con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM src.main.{table}")
        create_views(con)
        con.execute("INSERT OR REPLACE INTO schema_meta VALUES ('version', ?)", [str(SCHEMA_VERSION)])
    finally:
        con.execute("USE memory")


# ---- verify ------------------------------------------------------------------------------


def _names(con, catalog: str, kind: str) -> list[str]:
    if kind == "table":
        sql = "SELECT table_name FROM duckdb_tables() WHERE database_name = ? AND schema_name = 'main' ORDER BY 1"
    else:
        sql = "SELECT view_name FROM duckdb_views() WHERE database_name = ? AND schema_name = 'main' AND NOT internal ORDER BY 1"
    return [row[0] for row in con.execute(sql, [catalog]).fetchall()]


def _describe(con, catalog: str, name: str) -> list[tuple[str, str]]:
    return [(row[0], row[1]) for row in con.execute(f"DESCRIBE {catalog}.main.{name}").fetchall()]


def _constraints(con, catalog: str) -> set[tuple[str, str, tuple[str, ...]]]:
    rows = con.execute(
        "SELECT table_name, constraint_type, constraint_column_names FROM duckdb_constraints() WHERE database_name = ?",
        [catalog],
    ).fetchall()
    return {(table, kind, tuple(columns)) for table, kind, columns in rows}


def _checksum(con, sql: str) -> tuple[int, int]:
    count, total = con.execute(sql).fetchone()
    return int(count), int(total or 0)


def _version_of(con, catalog: str) -> str | None:
    row = con.execute(f"SELECT value FROM {catalog}.main.schema_meta WHERE key = 'version'").fetchone()
    return row[0] if row else None


def verify_rebuild(con, to_v4: bool, layout: dict[str, list[tuple[str, str]]]) -> dict[str, Any]:
    """Compare the new file (catalog ``new``) with the source (catalog ``src``); raise on any difference."""
    problems: list[str] = []
    tables = _names(con, "src", "table")
    if _names(con, "new", "table") != tables:
        raise RebuildError(f"tables differ: {tables} vs {_names(con, 'new', 'table')}")
    for table, columns in layout.items():
        if table in tables and _describe(con, "new", table) != columns:
            problems.append(f"{table} layout differs from schema version {SCHEMA_VERSION}")
    if _constraints(con, "new") != _constraints(con, "src"):
        problems.append(f"constraints differ: {sorted(_constraints(con, 'src') ^ _constraints(con, 'new'))}")
    for table in CONVERTED_TABLES:
        if (table, "PRIMARY KEY", ("symbol", "date")) not in _constraints(con, "new"):
            problems.append(f"{table} has no PRIMARY KEY (symbol, date)")

    table_rows: dict[str, int] = {}
    for table in tables:
        where = " WHERE key <> 'version'" if table == "schema_meta" else ""
        if to_v4 and table in CONVERTED_TABLES:
            columns = [column for column, _ in layout[table]]
            source_exprs = ", ".join(_convert_expr(column, column_type)[0] for column, column_type in layout[table])
            source = _checksum(con, f"SELECT count(*), sum(hash({source_exprs})) FROM src.main.{table}")
            new = _checksum(con, f"SELECT count(*), sum(hash({', '.join(columns)})) FROM new.main.{table}")
        else:
            source = _checksum(con, f"SELECT count(*), sum(hash(t)) FROM src.main.{table} AS t{where}")
            new = _checksum(con, f"SELECT count(*), sum(hash(t)) FROM new.main.{table} AS t{where}")
        if source[0] != new[0]:
            problems.append(f"{table}: {source[0]} rows in source, {new[0]} in new file")
        elif source[1] != new[1]:
            problems.append(f"{table}: row checksum differs")
        table_rows[table] = con.execute(f"SELECT count(*) FROM new.main.{table}").fetchone()[0] if where else new[0]

    views = _names(con, "src", "view")
    if _names(con, "new", "view") != views:
        problems.append(f"views differ: {views} vs {_names(con, 'new', 'view')}")
    view_rows: dict[str, int] = {}
    checksums: dict[str, str] = {}
    for view in sorted(set(views) & set(_names(con, "new", "view"))):
        if _describe(con, "new", view) != _describe(con, "src", view):
            problems.append(f"view {view}: columns or types differ")
            continue
        source = _checksum(con, f"SELECT count(*), sum(hash(v)) FROM src.main.{view} AS v")
        new = _checksum(con, f"SELECT count(*), sum(hash(v)) FROM new.main.{view} AS v")
        if source != new:
            problems.append(f"view {view}: rows {source[0]} vs {new[0]} or checksum differs")
        view_rows[view] = new[0]
        checksums[view] = str(new[1])

    expected_version = str(SCHEMA_VERSION) if to_v4 else _version_of(con, "src")
    if _version_of(con, "new") != expected_version:
        problems.append(f"schema version {_version_of(con, 'new')} (expected {expected_version})")
    if problems:
        raise RebuildError("verification failed: " + "; ".join(problems))
    return {"tables": table_rows, "views": view_rows, "view_checksums": checksums}


# ---- procedure ---------------------------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rebuild_database(
    data_dir: Path,
    to_v4: bool = False,
    memory_limit: str | None = None,
    threads: int = DEFAULT_THREADS,
    now: datetime | None = None,
    keep_backups: int = KEEP_BACKUPS,
) -> dict[str, Any]:
    """Rebuild market.db (caller holds the pipeline lock). Returns a result dict; raises RebuildError."""
    if keep_backups < 1:
        raise RebuildError("keep_backups must be at least 1 (the backup of this rebuild)")
    started = time.monotonic()
    now = now or datetime.now(UTC)
    memory_limit = memory_limit or (CONVERT_MEMORY_LIMIT if to_v4 else REBUILD_MEMORY_LIMIT)
    mode = "to_v4" if to_v4 else "rebuild"
    db_path = get_db_path(data_dir)
    tmp, spill = temp_db_path(db_path), spill_dir_path(db_path)
    if not db_path.exists():
        raise RebuildError(f"{db_path} does not exist")
    for leftover in (tmp, wal_path(tmp)):  # from a run killed mid-rebuild
        leftover.unlink(missing_ok=True)
    shutil.rmtree(spill, ignore_errors=True)
    result: dict[str, Any] = {"mode": mode, "db_path": str(db_path), "memory_limit": memory_limit, "threads": threads}

    try:
        # 1. Fold the WAL into the file and close every connection to it.
        con = _connect(db_path, memory_limit, threads, spill)
        try:
            version = schema_version(con)
            if to_v4 and version == SCHEMA_VERSION:
                return {**result, "status": "already_v4", "schema_version": version, "size_bytes": db_path.stat().st_size}
            expected = CONVERT_FROM_VERSION if to_v4 else SCHEMA_VERSION
            if version != expected:
                hint = f" (convert first: {REBUILD_HINT})" if not to_v4 and version is not None and version < SCHEMA_VERSION else ""
                raise RebuildError(f"{db_path} has schema version {version}; {mode} needs version {expected}{hint}")
            con.execute("CHECKPOINT")
        finally:
            con.close()
        if wal_path(db_path).exists():
            raise RebuildError(f"{wal_path(db_path)} still exists after CHECKPOINT; another process may have market.db open")
        size_before = db_path.stat().st_size
        free = shutil.disk_usage(db_path.parent).free
        if free < size_before * FREE_SPACE_FACTOR:
            raise RebuildError(f"not enough free space: {free} bytes free, {int(size_before * FREE_SPACE_FACTOR)} needed")
        result.update({"size_before_bytes": size_before, "source_schema_version": version})

        # 2. Build the new file next to market.db.
        layout = _v4_layout(memory_limit, threads, spill)
        con = _connect(":memory:", memory_limit, threads, spill)
        try:
            con.execute(f"ATTACH {_quote(db_path)} AS src (READ_ONLY)")
            con.execute(f"ATTACH {_quote(tmp)} AS dst")
            if to_v4:
                _build_v4(con, layout)
            else:
                con.execute("COPY FROM DATABASE src TO dst")
            con.execute("CHECKPOINT dst")
            con.execute("DETACH dst")
        finally:
            con.close()
        result["build_sec"] = round(time.monotonic() - started, 1)

        # 3. Verify the closed file READ_ONLY against the source.
        verify_started = time.monotonic()
        con = _connect(":memory:", memory_limit, threads, spill)
        try:
            con.execute(f"ATTACH {_quote(db_path)} AS src (READ_ONLY)")
            con.execute(f"ATTACH {_quote(tmp)} AS new (READ_ONLY)")
            result["checks"] = verify_rebuild(con, to_v4, layout)
        finally:
            con.close()
        result["verify_sec"] = round(time.monotonic() - verify_started, 1)
        if wal_path(tmp).exists():
            raise RebuildError(f"{wal_path(tmp)} exists after closing the new file")

        # 4. Keep the current file under a second name, then swap the new one in.
        backup = backup_path(db_path, now)
        os.link(db_path, backup)
        try:
            os.replace(tmp, db_path)
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        _fsync_dir(db_path.parent)
    finally:
        tmp.unlink(missing_ok=True)  # no-op once replaced
        wal_path(tmp).unlink(missing_ok=True)
        shutil.rmtree(spill, ignore_errors=True)

    result.update({
        "status": "converted" if to_v4 else "rebuilt",
        "schema_version": SCHEMA_VERSION,
        "size_after_bytes": db_path.stat().st_size,
        "backup_path": str(backup),
        "finished_at": _utc_iso(datetime.now(UTC)),
        "duration_sec": round(time.monotonic() - started, 1),
    })
    try:
        result["pruned_backups"] = prune_backups(db_path, keep_backups)
    except OSError as exc:  # market.db is already replaced; leftover backups are only disk space
        result["prune_error"] = f"{type(exc).__name__}: {exc}"
    try:
        record_rebuild(data_dir, result)
    except OSError as exc:  # market.db is already replaced; the next check keeps the old baseline
        result["state_error"] = f"{type(exc).__name__}: {exc}"
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    db_path = get_db_path(args.data_dir)
    if args.if_needed:
        check = rebuild_check(args.data_dir, args.growth_factor)
        if args.dry_run:
            return {"status": "dry_run", "would_rebuild": check["due"], **check}
        if check["size_bytes"] is None:
            return {"status": "skipped", "reason": "no_database", **check}
        if check["baseline_bytes"] is None:
            initialize_baseline(args.data_dir, check["size_bytes"], datetime.now(UTC))
            return {"status": "baseline_initialized", **check, "baseline_bytes": check["size_bytes"]}
        if not check["due"]:
            return {"status": "skipped", "reason": "below_threshold", **check}
        return {**rebuild_database(args.data_dir, memory_limit=args.memory_limit, threads=args.threads, keep_backups=args.keep_backups), "check": check}
    if args.dry_run:
        plan: dict[str, Any] = {"status": "dry_run", "mode": "to_v4" if args.to_v4 else "rebuild", "db_path": str(db_path)}
        if db_path.exists():
            memory_limit = args.memory_limit or (CONVERT_MEMORY_LIMIT if args.to_v4 else REBUILD_MEMORY_LIMIT)
            con = _connect(db_path, memory_limit, args.threads, spill_dir_path(db_path), read_only=True)
            try:
                plan["schema_version"] = schema_version(con)
            finally:
                con.close()
            plan.update({"size_bytes": db_path.stat().st_size, "free_bytes": shutil.disk_usage(db_path.parent).free})
        return plan
    return rebuild_database(args.data_dir, to_v4=args.to_v4, memory_limit=args.memory_limit, threads=args.threads, keep_backups=args.keep_backups)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild market.db into a new file (schema v4 conversion or compaction).")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--to-v4", action="store_true", help="Convert a schema version 3 market.db to version 4 (once, at deployment)")
    mode.add_argument("--if-needed", action="store_true", help="Rebuild only when market.db exceeds --growth-factor x its size right after the last rebuild")
    parser.add_argument("--growth-factor", type=float, default=DEFAULT_GROWTH_FACTOR)
    parser.add_argument("--memory-limit", default=None, help=f"DuckDB memory_limit (default: {CONVERT_MEMORY_LIMIT} with --to-v4, else {REBUILD_MEMORY_LIMIT})")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    parser.add_argument("--keep-backups", type=int, default=KEEP_BACKUPS, help="market.db.bak-* files kept after a successful rebuild, this one included")
    parser.add_argument("--dry-run", action="store_true", help="Report the decision or plan without writing anything")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with pipeline_lock(args.data_dir):
            result = run(args)
    except LockHeldError as exc:
        return lock_held_exit(exc)
    except Exception as exc:  # market.db is untouched; report and let run_daily.sh continue
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
