"""Regression checks for batched DuckDB upserts, OHLCV write normalization, and typed views."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.storage import (
    connect_db,
    get_db_path,
    normalize_ohlcv_row,
    normalize_twelve_data_ohlcv,
    upsert_macro_db,
    upsert_news_db,
    upsert_ohlcv_db,
)


def _bar(**overrides: object) -> dict:
    return {"date": "2026-05-12", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10", **overrides}


class OhlcvNormalizationTest(unittest.TestCase):
    def test_values_become_numbers_and_empty_volume_becomes_none(self) -> None:
        self.assertEqual(
            normalize_ohlcv_row(_bar(volume="12345.0", close="1.5e3")),
            {"date": "2026-05-12", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1500.0, "volume": 12345},
        )
        self.assertEqual(normalize_ohlcv_row(_bar(volume="0"))["volume"], 0, "zero volume is kept, not NULL")
        self.assertIsNone(normalize_ohlcv_row(_bar(volume=""))["volume"])
        self.assertIsNone(normalize_ohlcv_row(_bar(volume=None))["volume"])
        self.assertEqual(normalize_ohlcv_row(_bar(open=12, volume=7.0)), normalize_ohlcv_row(_bar(open="12", volume="7")), "JSON numbers too")

    def test_malformed_values_raise(self) -> None:
        bad = [
            _bar(date=""), _bar(date="2026-02-30"), _bar(date="20260512"), _bar(date="2026-05-12T00:00:00"),
            _bar(close=""), _bar(close=None), _bar(close="abc"), _bar(close="nan"), _bar(close="inf"), _bar(close=True),
            _bar(close="1_000"), _bar(volume="1.5"), _bar(volume="-1"), _bar(volume="abc"), _bar(volume=float("nan")),
        ]
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                normalize_ohlcv_row(row)

    def test_twelve_data_response_is_validated_and_sorted(self) -> None:
        payload = {"values": [
            {"datetime": "2026-05-13", "open": "2", "high": "2", "low": "2", "close": "2", "volume": "5"},
            {"datetime": "2026-05-12", "open": "1", "high": "1", "low": "1", "close": "1"},
        ]}
        rows = normalize_twelve_data_ohlcv(payload)
        self.assertEqual([(r["date"], r["close"], r["volume"]) for r in rows], [("2026-05-12", 1.0, None), ("2026-05-13", 2.0, 5)])
        with self.assertRaisesRegex(ValueError, "invalid volume '1.5' on 2026-05-13"):
            normalize_twelve_data_ohlcv({"values": [{**payload["values"][0], "volume": "1.5"}]})
        with self.assertRaisesRegex(ValueError, "invalid date"):
            normalize_twelve_data_ohlcv({"values": [{"open": "1", "high": "1", "low": "1", "close": "1"}]})
        with self.assertRaisesRegex(ValueError, "not an object"):
            normalize_twelve_data_ohlcv({"values": ["2026-05-12"]})
        with self.assertRaisesRegex(ValueError, "no values"):
            normalize_twelve_data_ohlcv({"status": "error", "message": "boom"})


@unittest.skipIf(importlib.util.find_spec("duckdb") is None, "duckdb dependency is not installed")
class BulkUpsertTest(unittest.TestCase):
    def test_ohlcv_upsert_replaces_rows_with_typed_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            data_dir = Path(tmp_dir_name)
            upsert_ohlcv_db(data_dir, "AAPL", [_bar(volume="10"), _bar(date="2026-05-13", close="1.6", volume="")])
            upsert_ohlcv_db(data_dir, "AAPL", [
                _bar(date="2026-05-13", close="1.7", volume=""),
                _bar(date="2026-05-13", close="1.8", volume="0"),
            ])
            with self.assertRaises(ValueError):
                upsert_ohlcv_db(data_dir, "AAPL", [_bar(date="2026-05-14"), _bar(date="", close="9")])

            con = connect_db(data_dir, read_only=True)
            try:
                rows = con.execute("SELECT date, close, volume FROM ohlcv ORDER BY date").fetchall()
                typed = con.execute("SELECT date, close, volume FROM ohlcv_typed ORDER BY date").fetchall()
                types = {row[0]: row[1] for row in con.execute("DESCRIBE ohlcv").fetchall()}
            finally:
                con.close()

        self.assertEqual(rows, [(date(2026, 5, 12), 1.5, 10), (date(2026, 5, 13), 1.8, 0)], "the malformed batch wrote nothing")
        self.assertEqual(typed, [(date(2026, 5, 12), 1.5, 10.0), (date(2026, 5, 13), 1.8, 0.0)])
        self.assertEqual((types["date"], types["close"], types["volume"], types["updated_at"]), ("DATE", "DOUBLE", "BIGINT", "VARCHAR"))

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
