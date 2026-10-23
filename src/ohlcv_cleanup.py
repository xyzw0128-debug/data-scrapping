"""Remove stored OHLCV rows that the data rules reject (src.data_rules).

Collection applies the rules to every response, so core symbols are cleaned on their next fetch
and rotation symbols on their next turn. Delisted symbols are never fetched again, and a new
config/history_start.txt line should take effect at once; this CLI applies the same rules to
the rows already in market.db:
- bars on days the NYSE was closed (weekends, holidays, SPECIAL_CLOSURES)
- bars before a symbol's history start
Every indicator row of an affected symbol is deleted in the same transaction, so the next
src.indicators run recalculates the symbol. Patched bars are not inserted here; the next fetch
adds them. Runs under the pipeline lock; --dry-run only counts and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from src.data_rules import DataRules, DataRulesError, load_data_rules
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.market_calendar import trading_days
from src.storage import REBUILD_HINT, SCHEMA_VERSION, SchemaVersionError, connect_db, get_db_path, schema_version


ROOT = Path(__file__).resolve().parents[1]
MEMORY_LIMIT = "1GB"
# Symbols listed in the result, most affected first; the counts are always complete.
TOP_SYMBOLS = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remove stored OHLCV rows on closed days or before a symbol's history start.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config", help="Data rules (src.data_rules)")
    parser.add_argument("--dry-run", action="store_true", help="Count the rows without deleting anything")
    parser.add_argument("--memory-limit", default=MEMORY_LIMIT)
    return parser.parse_args()


def find_targets(con, rules: DataRules) -> list[tuple[str, str, str]]:
    """(symbol, ISO date, reason) of every stored row the rules reject; a closed day wins over before_start."""
    first_day, last_day = con.execute("SELECT min(date), max(date) FROM ohlcv").fetchone()
    if first_day is None:
        return []
    calendar = [day.isoformat() for day in trading_days(first_day, last_day)]
    starts = sorted(rules.history_start.items())
    rows = con.execute(
        """
        WITH cal AS (SELECT unnest(CAST(? AS DATE[])) AS d),
        hs AS (SELECT unnest(CAST(? AS VARCHAR[])) AS symbol, unnest(CAST(? AS DATE[])) AS start_d)
        SELECT o.symbol, CAST(o.date AS VARCHAR), 'closed_day' FROM ohlcv o ANTI JOIN cal c ON c.d = o.date
        UNION ALL
        SELECT o.symbol, CAST(o.date AS VARCHAR), 'before_start'
        FROM ohlcv o JOIN cal c ON c.d = o.date JOIN hs ON hs.symbol = o.symbol AND o.date < hs.start_d
        ORDER BY 1, 2
        """,
        [calendar, [symbol for symbol, _ in starts], [day for _, day in starts]],
    ).fetchall()
    return [(symbol, day, reason) for symbol, day, reason in rows]


def delete_rows(con, targets: list[tuple[str, str, str]]) -> int:
    return con.execute(
        """
        DELETE FROM ohlcv USING (SELECT unnest(CAST(? AS VARCHAR[])) AS symbol, unnest(CAST(? AS DATE[])) AS d) t
        WHERE ohlcv.symbol = t.symbol AND ohlcv.date = t.d
        """,
        [[symbol for symbol, _, _ in targets], [day for _, day, _ in targets]],
    ).fetchone()[0]


def delete_indicators(con, symbols: list[str]) -> int:
    return con.execute("DELETE FROM indicators WHERE symbol IN (SELECT unnest(CAST(? AS VARCHAR[])))", [symbols]).fetchone()[0]


def run(args: argparse.Namespace) -> dict:
    started = time.monotonic()
    rules = load_data_rules(args.config_dir)
    db_path = get_db_path(args.data_dir)
    result: dict = {"dry_run": args.dry_run, "db_path": str(db_path)}
    if not db_path.exists():
        return {**result, "status": "skipped", "reason": "market.db not found"}
    con = connect_db(args.data_dir, read_only=args.dry_run)
    try:
        con.execute(f"SET memory_limit = '{args.memory_limit}'")
        version = schema_version(con)
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(f"{db_path} has schema version {version}, this code expects {SCHEMA_VERSION}. {REBUILD_HINT}")
        targets = find_targets(con, rules)
        per_symbol: dict[str, int] = {}
        for symbol, _, _ in targets:
            per_symbol[symbol] = per_symbol.get(symbol, 0) + 1
        symbols = sorted(per_symbol)
        result.update({
            "closed_day_rows": sum(1 for *_, reason in targets if reason == "closed_day"),
            "before_start_rows": sum(1 for *_, reason in targets if reason == "before_start"),
            "symbols": len(symbols),
            "top_symbols": dict(sorted(per_symbol.items(), key=lambda item: (-item[1], item[0]))[:TOP_SYMBOLS]),
        })
        if args.dry_run or not targets:
            result.update(status="dry_run" if args.dry_run else "nothing_to_do", deleted_rows=0, indicator_rows_deleted=0)
        else:
            con.begin()
            try:
                deleted = delete_rows(con, targets)
                if deleted != len(targets):
                    raise RuntimeError(f"deleted {deleted} ohlcv rows, expected {len(targets)}")
                indicator_rows = delete_indicators(con, symbols)
                con.commit()
            except Exception:
                con.rollback()
                raise
            result.update(status="done", deleted_rows=deleted, indicator_rows_deleted=indicator_rows)
    finally:
        con.close()
    result["elapsed_sec"] = round(time.monotonic() - started, 2)
    return result


def main() -> int:
    args = parse_args()
    try:
        with pipeline_lock(args.data_dir):
            result = run(args)
    except LockHeldError as exc:
        return lock_held_exit(exc)
    except DataRulesError as exc:
        print(json.dumps({"status": "data_rules_invalid", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
