"""market.db rebuild: schema v3 -> v4 conversion, periodic compaction, failure safety, deployment guard."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import os
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from src.lock import PIPELINE_LOCK_ENV, pipeline_lock

HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None

# Schema version 3 as the operating market.db has it (VARCHAR ohlcv/indicators, views casting them).
V3_TABLE_SQL = [
    "CREATE TABLE ohlcv (symbol VARCHAR NOT NULL, date VARCHAR NOT NULL, open VARCHAR, high VARCHAR, low VARCHAR, close VARCHAR, volume VARCHAR, updated_at VARCHAR NOT NULL, adj_close VARCHAR, PRIMARY KEY(symbol, date))",
    "CREATE TABLE indicators (symbol VARCHAR NOT NULL, date VARCHAR NOT NULL, open VARCHAR, high VARCHAR, low VARCHAR, close VARCHAR, volume VARCHAR, sma_20 VARCHAR, sma_50 VARCHAR, sma_200 VARCHAR, rsi_14 VARCHAR, macd_12_26 VARCHAR, macd_signal_9 VARCHAR, macd_hist VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, date))",
    "CREATE TABLE macro (series_id VARCHAR NOT NULL, date VARCHAR NOT NULL, value VARCHAR, realtime_start VARCHAR, realtime_end VARCHAR, updated_at VARCHAR NOT NULL, first_seen_at VARCHAR, PRIMARY KEY(series_id, date))",
    "CREATE TABLE news (symbol VARCHAR NOT NULL, id VARCHAR NOT NULL, datetime VARCHAR, date VARCHAR, headline VARCHAR, source VARCHAR, summary VARCHAR, url VARCHAR, image VARCHAR, category VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, id))",
    "CREATE TABLE schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR)",
    "CREATE TABLE corporate_actions (symbol VARCHAR NOT NULL, date DATE NOT NULL, type VARCHAR NOT NULL, value DOUBLE NOT NULL, source VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, date, type))",
    "CREATE TABLE symbol_status (symbol VARCHAR PRIMARY KEY, yahoo_symbol VARCHAR, group_name VARCHAR, active BOOLEAN, delisted_at VARCHAR, last_bar_date DATE, last_success_at VARCHAR, core_session DATE, splits_complete BOOLEAN, updated_at VARCHAR)",
    "CREATE TABLE collection_runs (run_id VARCHAR NOT NULL, phase VARCHAR NOT NULL, started_at VARCHAR, finished_at VARCHAR, session_date DATE, status VARCHAR, core_expected INTEGER, core_ok INTEGER, missing_symbols VARCHAR, fred_expected INTEGER, fred_ok INTEGER, missing_series VARCHAR, note VARCHAR, splits_ok INTEGER, missing_splits VARCHAR, universe_issue VARCHAR, PRIMARY KEY(run_id, phase))",
    "CREATE TABLE series_meta (series_id VARCHAR PRIMARY KEY, title VARCHAR, frequency VARCHAR, units VARCHAR, release VARCHAR, license_note VARCHAR, personal_use_only BOOLEAN, updated_at VARCHAR)",
]
V3_VIEW_SQL = {
    "ohlcv_typed": """SELECT symbol, TRY_CAST(date AS DATE) AS date, TRY_CAST(open AS DOUBLE) AS open, TRY_CAST(high AS DOUBLE) AS high,
        TRY_CAST(low AS DOUBLE) AS low, TRY_CAST(close AS DOUBLE) AS close, TRY_CAST(volume AS DOUBLE) AS volume,
        TRY_CAST(adj_close AS DOUBLE) AS adj_close, updated_at FROM ohlcv""",
    "indicators_typed": """SELECT symbol, TRY_CAST(date AS DATE) AS date, TRY_CAST(sma_20 AS DOUBLE) AS sma_20, TRY_CAST(sma_50 AS DOUBLE) AS sma_50,
        TRY_CAST(sma_200 AS DOUBLE) AS sma_200, TRY_CAST(rsi_14 AS DOUBLE) AS rsi_14, TRY_CAST(macd_12_26 AS DOUBLE) AS macd_12_26,
        TRY_CAST(macd_signal_9 AS DOUBLE) AS macd_signal_9, TRY_CAST(macd_hist AS DOUBLE) AS macd_hist, updated_at FROM indicators""",
    "macro_typed": """SELECT series_id, TRY_CAST(date AS DATE) AS date, TRY_CAST(value AS DOUBLE) AS value,
        realtime_start, realtime_end, first_seen_at, updated_at FROM macro""",
    "news_typed": """SELECT symbol, id, TRY_CAST(replace(datetime, 'Z', '') AS TIMESTAMP) AS datetime_utc,
        TRY_CAST(date AS DATE) AS date, headline, source, summary, url, image, category, updated_at FROM news""",
    "prices_daily": """SELECT o.symbol, TRY_CAST(o.date AS DATE) AS date, TRY_CAST(o.open AS DOUBLE) AS open, TRY_CAST(o.high AS DOUBLE) AS high,
        TRY_CAST(o.low AS DOUBLE) AS low, TRY_CAST(o.close AS DOUBLE) AS close, TRY_CAST(o.adj_close AS DOUBLE) AS adj_close,
        TRY_CAST(o.volume AS DOUBLE) AS volume, 0.0 AS dividends, coalesce(s.value, 0.0) AS splits
        FROM ohlcv o LEFT JOIN corporate_actions s ON s.symbol = o.symbol AND s.type = 'split' AND s.date = TRY_CAST(o.date AS DATE)""",
}
# NVDA's 10:1 split on 2024-06-10, in split-adjusted closes (raw closes were 10x before it).
NVDA_CLOSES = {"2024-06-04": "116.437", "2024-06-05": "122.44", "2024-06-06": "120.998", "2024-06-07": "120.888", "2024-06-10": "121.79", "2024-06-11": "120.91"}


def _make_v3_db(data_dir: Path, volume: str = "0") -> Path:
    import duckdb  # type: ignore

    db_path = data_dir / "db" / "market.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        for sql in V3_TABLE_SQL:
            con.execute(sql)
        for name, sql in V3_VIEW_SQL.items():
            con.execute(f"CREATE VIEW {name} AS {sql}")
        con.execute("INSERT INTO schema_meta VALUES ('version', '3')")
        for index, (day, close) in enumerate(NVDA_CLOSES.items()):
            con.execute(
                "INSERT INTO ohlcv VALUES ('NVDA', ?, ?, ?, ?, ?, ?, '2026-10-21T00:00:00Z', ?)",
                [day, close, close, close, close, volume if index == 0 else str(1000 + index), f"{float(close) * 0.997:.5f}"],
            )
        con.execute("INSERT INTO ohlcv VALUES ('ROT', '2024-06-11', '4.00000', '4.5', '3.9', '4.2', '', '2026-10-21T00:00:00Z', NULL)")
        con.execute("INSERT INTO indicators VALUES ('NVDA', '2024-06-10', '1', '1', '1', '1', '1', '', '', '', '55.123456', '', '', '', '2026-10-21T00:00:01Z')")
        con.execute("INSERT INTO indicators VALUES ('NVDA', '2024-06-11', '1', '1', '1', '1', '1', '120.5', '', '', '54.1', '0.25', '0.2', '0.05', '2026-10-21T00:00:01Z')")
        con.execute("INSERT INTO macro VALUES ('DGS10', '2024-06-10', '.', '', '', '2026-10-21T00:00:00Z', NULL), ('DGS10', '2024-06-11', '4.40', '', '', '2026-10-21T00:00:00Z', '2026-10-21T00:00:00Z')")
        con.execute("INSERT INTO news VALUES ('NVDA', '1', '2024-06-11T14:30:00Z', '2024-06-11', 'h, \"q\"', 's', 'a\nb', 'u', '', 'company', '2026-10-21T00:00:00Z')")
        con.execute("INSERT INTO corporate_actions VALUES ('NVDA', DATE '2024-06-10', 'split', 10.0, 'twelve_data_adjust_ratio', '2026-10-21T00:00:00Z')")
        con.execute("INSERT INTO symbol_status VALUES ('NVDA', 'NVDA', 'core_sp500', true, NULL, DATE '2024-06-11', '2026-10-21T00:00:00Z', DATE '2024-06-11', true, '2026-10-21T00:00:00Z')")
        con.execute("INSERT INTO collection_runs VALUES ('r1', 'full', 's', 'f', DATE '2024-06-11', 'complete', 1, 1, '[]', 1, 1, '[]', NULL, 1, '[]', NULL)")
        con.execute("INSERT INTO series_meta VALUES ('DGS10', 't', 'Daily', 'Percent', 'H.15', NULL, false, '2026-10-21T00:00:00Z')")
    finally:
        con.close()
    return db_path


def _query(db_path: Path, sql: str) -> list[tuple]:
    import duckdb  # type: ignore

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rebuild_main(data_dir: Path, *flags: str) -> tuple[int, dict]:
    from src import rebuild

    out = io.StringIO()
    with patch("sys.argv", ["src.rebuild", "--data-dir", str(data_dir), *flags]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = rebuild.main()
    return code, json.loads(out.getvalue())


NOW = datetime(2026, 10, 21, 22, 0, tzinfo=UTC)


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
@patch.dict(os.environ, {PIPELINE_LOCK_ENV: ""})
class ConvertToV4Test(unittest.TestCase):
    def test_v3_is_converted_with_identical_views_and_a_hardlink_backup(self) -> None:
        from src import rebuild, storage
        from src.corporate_actions import split_events

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            db_path = _make_v3_db(data_dir)
            before = {view: _query(db_path, f"SELECT * FROM {view} ORDER BY ALL") for view in V3_VIEW_SQL}
            original_inode, original_hash = db_path.stat().st_ino, _sha256(db_path)

            result = rebuild.rebuild_database(data_dir, to_v4=True, memory_limit="256MB", threads=1, now=NOW)

            backup = Path(result["backup_path"])
            self.assertEqual((result["status"], result["source_schema_version"], result["schema_version"]), ("converted", 3, 4))
            self.assertEqual(backup.name, "market.db.bak-2026-10-21")
            self.assertEqual((backup.stat().st_ino, _sha256(backup)), (original_inode, original_hash), "backup is the untouched v3 file")
            self.assertNotEqual(db_path.stat().st_ino, original_inode)
            self.assertEqual(sorted(p.name for p in db_path.parent.iterdir()), ["market.db", "market.db.bak-2026-10-21"], "no temporary files left")
            for view, rows in before.items():
                self.assertEqual(_query(db_path, f"SELECT * FROM {view} ORDER BY ALL"), rows, view)
            types = dict(_query(db_path, "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = 'ohlcv'"))
            self.assertEqual((types["date"], types["close"], types["volume"], types["adj_close"]), ("DATE", "DOUBLE", "BIGINT", "DOUBLE"))
            self.assertEqual(_query(db_path, "SELECT volume FROM ohlcv WHERE symbol = 'NVDA' AND date = '2024-06-04'"), [(0,)], "zero volume is kept")
            self.assertEqual(_query(db_path, "SELECT volume, adj_close FROM ohlcv WHERE symbol = 'ROT'"), [(None, None)], "'' becomes NULL")
            self.assertEqual(_query(db_path, "SELECT sma_20, rsi_14 FROM indicators ORDER BY date"), [(None, 55.123456), (120.5, 54.1)])
            self.assertEqual(result["checks"]["views"]["prices_daily"], 7)
            state = rebuild.load_maintenance(data_dir)
            self.assertEqual(state["baseline_bytes"], db_path.stat().st_size)
            self.assertEqual(state["last_rebuild"]["mode"], "to_v4")

            # The converted file works with the version 4 code paths.
            storage._SCHEMA_READY.clear()
            storage.ensure_db_schema(data_dir)
            closes = storage.load_closes(data_dir, "NVDA")
            raw = {day: close * (10 if day < "2024-06-10" else 1) for day, close in closes.items()}
            self.assertEqual(split_events(raw, closes), [("2024-06-10", 10.0)])
            storage.replace_symbol_prices(data_dir, "ROT", [{"date": "2024-06-12", "open": "4", "high": "4", "low": "4", "close": "4.1", "volume": "0"}])
            self.assertEqual(_query(db_path, "SELECT CAST(date AS VARCHAR), close, volume FROM prices_daily WHERE symbol = 'ROT'"), [("2024-06-12", 4.1, 0.0)])
            again = rebuild.rebuild_database(data_dir, to_v4=True, memory_limit="256MB", threads=1, now=NOW)
            self.assertEqual(again["status"], "already_v4")

    def test_unconvertible_value_stops_before_anything_changes(self) -> None:
        from src import rebuild

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            db_path = _make_v3_db(data_dir, volume="1.5")  # CAST would silently round it to 2
            original = _sha256(db_path)
            code, printed = _rebuild_main(data_dir, "--to-v4", "--memory-limit", "256MB", "--threads", "1")
            self.assertEqual(code, 1)
            self.assertEqual(printed["status"], "failed")
            self.assertIn("'volume': 1", printed["error"])
            self.assertEqual(_sha256(db_path), original)
            self.assertEqual(sorted(p.name for p in db_path.parent.iterdir()), ["market.db"])
            self.assertFalse(rebuild.maintenance_path(data_dir).exists())

    def test_failed_verification_removes_only_the_temporary_file(self) -> None:
        from src import rebuild

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            db_path = _make_v3_db(data_dir)
            original = _sha256(db_path)
            rebuild.temp_db_path(db_path).write_bytes(b"left by a killed run")
            with patch.object(rebuild, "verify_rebuild", side_effect=rebuild.RebuildError("checksum differs")):
                with self.assertRaisesRegex(rebuild.RebuildError, "checksum differs"):
                    rebuild.rebuild_database(data_dir, to_v4=True, memory_limit="256MB", threads=1, now=NOW)
            with patch.object(rebuild.os, "replace", side_effect=OSError("disk gone")):
                with self.assertRaisesRegex(OSError, "disk gone"):
                    rebuild.rebuild_database(data_dir, to_v4=True, memory_limit="256MB", threads=1, now=NOW)
            self.assertEqual(_sha256(db_path), original)
            self.assertEqual(sorted(p.name for p in db_path.parent.iterdir()), ["market.db"], "no temp file, spill dir, or backup link left")
            self.assertEqual(_query(db_path, "SELECT value FROM schema_meta"), [("3",)])

    def test_v3_database_stops_a_collection_run_before_any_api_call(self) -> None:
        from src import main, storage

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_v3_db(root / "data")
            args = argparse.Namespace(data_dir=root / "data", dry_run=False)
            with patch.object(main, "load_provider_config", side_effect=AssertionError("must stop before loading providers")):
                with self.assertRaisesRegex(storage.SchemaVersionError, "src.rebuild --to-v4"):
                    main.run_collection(args, logging.getLogger("test"))


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
@patch.dict(os.environ, {PIPELINE_LOCK_ENV: ""})
class PeriodicRebuildTest(unittest.TestCase):
    def _v4_db(self, data_dir: Path) -> Path:
        from src import storage

        rows = [{"date": f"2024-{m:02d}-{d:02d}", "open": "1", "high": "1", "low": "1", "close": str(m + d / 100), "volume": str(d)} for m in range(1, 13) for d in range(1, 28)]
        for symbol in ["AAA", "BBB", "CCC"]:
            storage.replace_symbol_prices(data_dir, symbol, rows, {rows[-1]["date"]: "1.0"})
        storage.replace_split_events(data_dir, "AAA", [("2024-03-01", 2.0)], "test")
        return storage.get_db_path(data_dir)

    def test_if_needed_initializes_baseline_then_rebuilds_past_twice_the_size(self) -> None:
        from src import rebuild

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            db_path = self._v4_db(data_dir)
            size = db_path.stat().st_size

            code, dry = _rebuild_main(data_dir, "--if-needed", "--dry-run")
            self.assertEqual((code, dry["status"], dry["would_rebuild"], dry["baseline_bytes"]), (0, "dry_run", False, None))
            self.assertFalse(rebuild.maintenance_path(data_dir).exists(), "dry-run writes nothing")

            code, first = _rebuild_main(data_dir, "--if-needed")
            self.assertEqual((code, first["status"], first["baseline_bytes"]), (0, "baseline_initialized", size))
            code, second = _rebuild_main(data_dir, "--if-needed")
            self.assertEqual((second["status"], second["reason"], second["threshold_bytes"]), ("skipped", "below_threshold", 2 * size))

            rebuild.atomic_write_json(rebuild.maintenance_path(data_dir), {"baseline_bytes": size // 3})
            code, dry = _rebuild_main(data_dir, "--if-needed", "--dry-run")
            self.assertEqual((dry["status"], dry["would_rebuild"]), ("dry_run", True))
            before = _query(db_path, "SELECT * FROM prices_daily ORDER BY ALL")
            old_inode = db_path.stat().st_ino
            code, third = _rebuild_main(data_dir, "--if-needed", "--memory-limit", "256MB", "--threads", "1")
            self.assertEqual((code, third["status"], third["mode"]), (0, "rebuilt", "rebuild"))
            self.assertEqual(_query(db_path, "SELECT * FROM prices_daily ORDER BY ALL"), before)
            self.assertEqual(Path(third["backup_path"]).stat().st_ino, old_inode)
            state = rebuild.load_maintenance(data_dir)
            self.assertEqual((state["baseline_bytes"], len(state["history"])), (db_path.stat().st_size, 1))

    def test_same_day_backups_do_not_collide_and_v3_needs_conversion_first(self) -> None:
        from src import rebuild

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self._v4_db(data_dir)
            first = rebuild.rebuild_database(data_dir, memory_limit="256MB", threads=1, now=NOW)
            second = rebuild.rebuild_database(data_dir, memory_limit="256MB", threads=1, now=NOW)
            self.assertEqual(Path(first["backup_path"]).name, "market.db.bak-2026-10-21")
            self.assertEqual(Path(second["backup_path"]).name, "market.db.bak-2026-10-21T220000Z")
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _make_v3_db(data_dir)
            with self.assertRaisesRegex(rebuild.RebuildError, "convert first"):
                rebuild.rebuild_database(data_dir, memory_limit="256MB", threads=1, now=NOW)

    def test_lock_held_exits_with_code_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with pipeline_lock(data_dir):
                code, printed = _rebuild_main(data_dir, "--if-needed")
        self.assertEqual((code, printed["status"]), (2, "lock_held"))


if __name__ == "__main__":
    unittest.main()
