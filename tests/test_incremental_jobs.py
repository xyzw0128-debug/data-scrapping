"""Checks for incremental indicator selection and Finnhub news rotation."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src import finnhub_news
from src.indicators import add_indicators, calculate_batches, find_stale_symbols
from src.storage import connect_db, upsert_ohlcv_db


@unittest.skipIf(importlib.util.find_spec("duckdb") is None, "duckdb dependency is not installed")
class IncrementalIndicatorsTest(unittest.TestCase):
    def test_only_symbols_with_newer_ohlcv_are_stale(self) -> None:
        rows = [{"date": f"2026-05-{day:02d}", "open": "1", "high": "1", "low": "1", "close": str(day), "volume": "1"} for day in range(1, 29)]
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            data_dir = Path(tmp_dir_name)
            upsert_ohlcv_db(data_dir, "AAA", rows)
            upsert_ohlcv_db(data_dir, "BBB", rows)
            con = connect_db(data_dir)
            try:
                self.assertEqual(find_stale_symbols(con, ["AAA", "BBB", "ZZZ"]), ["AAA", "BBB"])
                results = calculate_batches(con, data_dir, ["AAA", "BBB"], batch_size=1)
                self.assertEqual([item["rows"] for item in results], [28, 28])
                con.execute("UPDATE indicators SET updated_at = '2000-01-01T00:00:00Z' WHERE symbol = 'BBB'")
                self.assertEqual(find_stale_symbols(con, ["AAA", "BBB"]), ["BBB"])
                stored = con.execute("SELECT sma_20 FROM indicators WHERE symbol = 'AAA' AND date = '2026-05-20'").fetchone()[0]
            finally:
                con.close()

        self.assertEqual(stored, add_indicators(rows)[19]["sma_20"])


class FinnhubRotationTest(unittest.TestCase):
    def test_never_fetched_liquid_symbols_come_first(self) -> None:
        state = {"AAA": {"last_attempt_at": "2026-10-01T00:00:00Z"}, "BBB": {"last_attempt_at": "2026-09-01T00:00:00Z"}}
        liquidity = {"CCC": 1.0, "DDD": 5.0}

        ordered = finnhub_news.choose_news_symbols(["AAA", "BBB", "CCC", "DDD", "!OTC/X", "CCC"], state, liquidity, limit=None)

        self.assertEqual(ordered, ["DDD", "CCC", "BBB", "AAA"])
        self.assertEqual(finnhub_news.choose_news_symbols(["AAA", "BBB", "CCC"], state, {}, limit=1), ["CCC"])

    def test_symbol_range_resumes_from_last_success(self) -> None:
        today = date(2026, 10, 20)
        self.assertEqual(finnhub_news.resolve_symbol_range({}, today, 30, 365), ("2026-09-20", "2026-10-20"))
        self.assertEqual(finnhub_news.resolve_symbol_range({"last_to": "2026-10-10"}, today, 30, 365), ("2026-10-09", "2026-10-20"))
        self.assertEqual(finnhub_news.resolve_symbol_range({"last_to": "2020-01-01"}, today, 30, 365), ("2025-10-20", "2026-10-20"))


if __name__ == "__main__":
    unittest.main()
