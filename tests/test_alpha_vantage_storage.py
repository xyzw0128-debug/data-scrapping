"""Regression checks for Alpha Vantage DuckDB storage integration."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import alpha_vantage
from src.config import ProviderConfig
from src.storage import get_db_path, upsert_ohlcv_db


@unittest.skipIf(importlib.util.find_spec("duckdb") is None, "duckdb dependency is not installed")
class AlphaVantageStorageTest(unittest.TestCase):
    def test_collect_symbol_writes_ohlcv_rows_to_duckdb(self) -> None:
        config = ProviderConfig(
            name="alpha_vantage",
            enabled=True,
            api_key_env="ALPHA_VANTAGE_API_KEY",
            daily_limit=25,
            daily_reserve=5,
            per_minute_limit=5,
            max_symbols_per_run=5,
            base_url="https://www.alphavantage.co/query",
            interval="1day",
            outputsize="compact",
        )
        payload = {
            alpha_vantage.TIME_SERIES_DAILY_KEY: {
                "2026-05-13": {
                    "1. open": "10.00",
                    "2. high": "11.00",
                    "3. low": "9.50",
                    "4. close": "10.50",
                    "5. volume": "12345",
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp_dir_name:
            data_dir = Path(tmp_dir_name)
            with patch.object(alpha_vantage, "fetch_json", return_value=payload):
                status, row_count, note = alpha_vantage.collect_symbol(config, data_dir, "AAPL", "demo", False)

            self.assertEqual(status, "done")
            self.assertEqual(row_count, 1)
            self.assertIn("db=", note or "")
            self.assertTrue(get_db_path(data_dir).exists())
            self.assertFalse((data_dir / "ohlcv" / "AAPL.csv").exists())

            import duckdb  # type: ignore

            con = duckdb.connect(str(get_db_path(data_dir)), read_only=True)
            try:
                rows = con.execute(
                    "SELECT symbol, date, open, high, low, close, volume FROM ohlcv WHERE symbol = ?",
                    ["AAPL"],
                ).fetchall()
            finally:
                con.close()

        self.assertEqual(rows, [("AAPL", "2026-05-13", "10.00", "11.00", "9.50", "10.50", "12345")])

    def test_alpha_vantage_imports_existing_duckdb_upsert(self) -> None:
        self.assertIs(alpha_vantage.upsert_ohlcv_db, upsert_ohlcv_db)


if __name__ == "__main__":
    unittest.main()
