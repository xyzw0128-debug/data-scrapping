"""Regression checks for batched DuckDB upserts and typed views."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from src.storage import connect_db, get_db_path, upsert_macro_db, upsert_news_db, upsert_ohlcv_db


@unittest.skipIf(importlib.util.find_spec("duckdb") is None, "duckdb dependency is not installed")
class BulkUpsertTest(unittest.TestCase):
    def test_ohlcv_upsert_replaces_existing_rows_and_keeps_empty_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            data_dir = Path(tmp_dir_name)
            upsert_ohlcv_db(data_dir, "AAPL", [
                {"date": "2026-05-12", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"},
                {"date": "2026-05-13", "open": "1", "high": "2", "low": "0.5", "close": "1.6", "volume": ""},
            ])
            upsert_ohlcv_db(data_dir, "AAPL", [
                {"date": "2026-05-13", "open": "1", "high": "2", "low": "0.5", "close": "1.7", "volume": ""},
                {"date": "2026-05-13", "open": "1", "high": "2", "low": "0.5", "close": "1.8", "volume": ""},
                {"date": "", "open": "9", "high": "9", "low": "9", "close": "9", "volume": "9"},
            ])

            con = connect_db(data_dir, read_only=True)
            try:
                rows = con.execute("SELECT date, close, volume FROM ohlcv ORDER BY date").fetchall()
                typed = con.execute("SELECT date, close, volume FROM ohlcv_typed ORDER BY date").fetchall()
            finally:
                con.close()

        self.assertEqual(rows, [("2026-05-12", "1.5", "10"), ("2026-05-13", "1.8", "")])
        self.assertEqual(str(typed[0][0]), "2026-05-12")
        self.assertEqual(typed[0][1], 1.5)
        self.assertIsNone(typed[1][2])

    def test_news_text_round_trips_quotes_commas_and_newlines(self) -> None:
        summary = 'He said "buy", then\nleft, quickly.'
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            data_dir = Path(tmp_dir_name)
            upsert_news_db(data_dir, "AAPL", [
                {"id": "1", "datetime": "2026-05-13T14:30:00Z", "date": "2026-05-13", "headline": "Q, \"A\"", "source": "x", "summary": summary, "url": "u", "image": "", "category": "company"},
            ])
            con = connect_db(data_dir, read_only=True)
            try:
                row = con.execute("SELECT headline, summary, image FROM news").fetchone()
                stamp = con.execute("SELECT datetime_utc FROM news_typed").fetchone()[0]
            finally:
                con.close()

        self.assertEqual(row, ('Q, "A"', summary, ""))
        self.assertEqual(stamp.isoformat(), "2026-05-13T14:30:00")

    def test_macro_placeholder_values_are_null_in_typed_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            data_dir = Path(tmp_dir_name)
            upsert_macro_db(data_dir, "DGS10", [
                {"date": "2026-05-11", "value": "4.40", "realtime_start": "r", "realtime_end": "r"},
                {"date": "2026-05-12", "value": ".", "realtime_start": "r", "realtime_end": "r"},
            ])
            con = connect_db(data_dir, read_only=True)
            try:
                values = con.execute("SELECT value FROM macro_typed ORDER BY date").fetchall()
            finally:
                con.close()
            self.assertFalse(any(path.name.endswith(".ndjson.tmp") for path in get_db_path(data_dir).parent.iterdir()))

        self.assertEqual(values, [(4.4,), (None,)])


if __name__ == "__main__":
    unittest.main()
