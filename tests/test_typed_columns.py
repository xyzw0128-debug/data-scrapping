"""Readers give the same results whether DuckDB returns VARCHAR (schema v3) or DATE/numeric values.

Schema v4 will store ohlcv dates as DATE and prices/volume as numbers. These readers must not
depend on string results: a DATE key never matches the provider's "YYYY-MM-DD" keys, and
``str(value or "")`` turns a numeric zero volume into "".
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.corporate_actions import split_basis_changed, split_events
from src.lock import PIPELINE_LOCK_ENV
from src.storage import _ohlcv_dict, connect_db, load_closes, load_ohlcv_rows_db, load_ohlcv_rows_for_symbols

TYPED_OHLCV_SQL = """CREATE TABLE ohlcv (symbol VARCHAR NOT NULL, date DATE NOT NULL, open DOUBLE, high DOUBLE, low DOUBLE,
    close DOUBLE, volume BIGINT, updated_at VARCHAR NOT NULL, adj_close DOUBLE, PRIMARY KEY(symbol, date))"""


def _typed_db(data_dir: Path, rows: list[tuple]) -> None:
    con = connect_db(data_dir)
    try:
        con.execute(TYPED_OHLCV_SQL)
        con.executemany("INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-21T00:00:00Z', NULL)", rows)
    finally:
        con.close()


@unittest.skipIf(importlib.util.find_spec("duckdb") is None, "duckdb dependency is not installed")
class TypedColumnReadersTest(unittest.TestCase):
    def test_load_closes_keys_are_iso_strings_and_split_logic_still_works(self) -> None:
        # Split-adjusted closes; the raw (adjust=none) price was 4x higher before a 4:1 split on 2024-06-10.
        days = ["2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07", "2024-06-10", "2024-06-11"]
        adjusted = [10.0, 10.2, 10.1, 10.3, 10.4, 10.5]
        raw = {day: close * (4 if day < "2024-06-10" else 1) for day, close in zip(days, adjusted)}
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _typed_db(data_dir, [("NVDA", date.fromisoformat(d), c, c, c, c, 1000) for d, c in zip(days, adjusted)])
            stored = load_closes(data_dir, "NVDA")
        self.assertEqual(sorted(stored), days)
        self.assertEqual(split_events(raw, stored), [("2024-06-10", 4.0)])
        self.assertTrue(split_basis_changed(stored, {day: close / 2 for day, close in stored.items()}))

    def test_zero_volume_stays_zero_and_null_becomes_empty(self) -> None:
        self.assertEqual(
            _ohlcv_dict((date(2026, 10, 20), 1.0, 2.0, 0.5, 1.5, 0)),
            {"date": "2026-10-20", "open": "1.0", "high": "2.0", "low": "0.5", "close": "1.5", "volume": "0"},
        )
        self.assertEqual(_ohlcv_dict(("2026-10-20", "1", "2", "0.5", "1.5", None))["volume"], "")
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _typed_db(data_dir, [("AAA", date(2026, 10, 19), 1.0, 1.0, 1.0, 1.0, 0), ("AAA", date(2026, 10, 20), 1.0, 1.0, 1.0, 1.0, None)])
            single = load_ohlcv_rows_db(data_dir, "AAA")
            con = connect_db(data_dir, read_only=True)
            try:
                batched = load_ohlcv_rows_for_symbols(con, ["AAA"])["AAA"]
            finally:
                con.close()
        self.assertEqual([r["volume"] for r in single], ["0", ""])
        self.assertEqual(batched, single)

    @patch.dict(os.environ, {PIPELINE_LOCK_ENV: ""})
    def test_validation_report_with_date_issues_is_saved(self) -> None:
        from src import validate_duckdb

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _typed_db(data_dir, [("BAD", date(2026, 10, 20), 1.0, 1.0, 2.0, 1.5, 10)])  # high < low
            con = connect_db(data_dir)
            try:
                for table in ["indicators", "macro", "news"]:
                    con.execute(f"CREATE TABLE {table} (symbol VARCHAR)")
            finally:
                con.close()
            with patch("sys.argv", ["src.validate_duckdb", "--data-dir", str(data_dir)]), contextlib.redirect_stdout(io.StringIO()):
                code = validate_duckdb.main()
            saved = json.loads((data_dir / "logs" / "validation" / "duckdb_latest.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(saved["issues"], [{"dataset": "ohlcv", "type": "invalid_numeric_or_range", "symbol": "BAD", "date": "2026-10-20"}])


if __name__ == "__main__":
    unittest.main()
