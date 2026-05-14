"""Regression checks for Twelve Data rate-limit retry handling."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import main
from src.config import ProviderConfig


class TwelveDataRetryTest(unittest.TestCase):
    def test_collect_symbol_sleeps_and_retries_once_for_rate_limit_message(self) -> None:
        config = ProviderConfig(
            name="twelve_data",
            enabled=True,
            api_key_env="TWELVE_DATA_API_KEY",
            daily_limit=800,
            daily_reserve=100,
            per_minute_limit=8,
            max_symbols_per_run=700,
            base_url="https://api.twelvedata.com/time_series",
            interval="1day",
            outputsize=5000,
        )
        rate_limit_payload = {"status": "error", "message": "rate limit reached"}
        success_payload = {
            "values": [
                {
                    "datetime": "2026-05-13",
                    "open": "10.00",
                    "high": "11.00",
                    "low": "9.50",
                    "close": "10.50",
                    "volume": "12345",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp_dir_name, patch.object(
            main, "fetch_json", side_effect=[rate_limit_payload, success_payload]
        ) as fetch_json, patch.object(main, "save_raw_json", side_effect=[Path("rate-limit.json"), Path("success.json")]), patch.object(
            main, "upsert_ohlcv_db", return_value="market.db"
        ) as upsert_ohlcv_db, patch("src.main.time.sleep") as sleep:
            status, row_count, note = main.collect_symbol(
                config,
                Path(tmp_dir_name),
                "AAPL",
                "demo",
                False,
                logging.getLogger("test"),
            )

        self.assertEqual(status, "done")
        self.assertEqual(row_count, 1)
        self.assertEqual(note, "raw=success.json db=market.db")
        self.assertEqual(fetch_json.call_count, 2)
        sleep.assert_called_once_with(60)
        upsert_ohlcv_db.assert_called_once()

    def test_collect_symbol_does_not_retry_non_rate_limit_value_error(self) -> None:
        config = ProviderConfig(
            name="twelve_data",
            enabled=True,
            api_key_env="TWELVE_DATA_API_KEY",
            daily_limit=800,
            daily_reserve=100,
            per_minute_limit=8,
            max_symbols_per_run=700,
            base_url="https://api.twelvedata.com/time_series",
            interval="1day",
            outputsize=5000,
        )

        with tempfile.TemporaryDirectory() as tmp_dir_name, patch.object(
            main, "fetch_json", return_value={"status": "error", "message": "bad symbol"}
        ) as fetch_json, patch.object(main, "save_raw_json", return_value=Path("bad-symbol.json")), patch(
            "src.main.time.sleep"
        ) as sleep:
            with self.assertRaises(ValueError):
                main.collect_symbol(
                    config,
                    Path(tmp_dir_name),
                    "AAPL",
                    "demo",
                    False,
                    logging.getLogger("test"),
                )

        self.assertEqual(fetch_json.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
