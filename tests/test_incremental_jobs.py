"""Checks for incremental indicator selection and Finnhub news rotation."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src import finnhub_news
from src.indicators import add_indicators, calculate_batches, calculate_sma, find_stale_symbols
from src.storage import connect_db, upsert_ohlcv_db


@unittest.skipIf(importlib.util.find_spec("duckdb") is None, "duckdb dependency is not installed")
class IncrementalIndicatorsTest(unittest.TestCase):
    def test_only_symbols_with_missing_indicator_rows_are_stale(self) -> None:
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
                self.assertEqual(find_stale_symbols(con, ["AAA", "BBB"]), [])
                con.execute("DELETE FROM indicators WHERE symbol = 'BBB' AND date >= '2026-05-25'")
                self.assertEqual(find_stale_symbols(con, ["AAA", "BBB"]), ["BBB"])
                stored = con.execute("SELECT sma_20 FROM indicators WHERE symbol = 'AAA' AND date = '2026-05-20'").fetchone()[0]
            finally:
                con.close()

        self.assertEqual(stored, add_indicators(rows)[19]["sma_20"])

    def test_incremental_run_inserts_only_missing_rows_equal_to_a_full_run(self) -> None:
        def bars(closes: dict[int, float]) -> list[dict]:
            return [{"date": f"2026-05-{day:02d}", "open": "1", "high": "1", "low": "1", "close": str(close), "volume": "1"} for day, close in closes.items()]

        closes = {day: 10 + (day * 7 % 5) - day / 10 for day in range(1, 29)}
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            data_dir = Path(tmp_dir_name)
            upsert_ohlcv_db(data_dir, "AAA", bars({day: closes[day] for day in range(1, 26)}))
            con = connect_db(data_dir)
            try:
                calculate_batches(con, data_dir, ["AAA"], batch_size=10, incremental=True)
            finally:
                con.close()
            # New days and a changed close on day 24: the writer drops indicators from day 24 on.
            closes[24] += 1.5
            upsert_ohlcv_db(data_dir, "AAA", bars({day: closes[day] for day in range(24, 29)}))
            con = connect_db(data_dir)
            try:
                kept = con.execute("SELECT count(*), CAST(max(date) AS VARCHAR) FROM indicators WHERE symbol = 'AAA'").fetchone()
                results = calculate_batches(con, data_dir, find_stale_symbols(con, ["AAA"]), batch_size=10, incremental=True)
                incremental = con.execute("SELECT * EXCLUDE (updated_at) FROM indicators ORDER BY date").fetchall()
                calculate_batches(con, data_dir, ["AAA"], batch_size=10)
                full = con.execute("SELECT * EXCLUDE (updated_at) FROM indicators ORDER BY date").fetchall()
            finally:
                con.close()

        self.assertEqual(kept, (23, "2026-05-23"))
        self.assertEqual(results, [{"symbol": "AAA", "status": "done", "rows": 5}], "only days 24-28 are written")
        self.assertEqual(incremental, full)
        self.assertEqual(len(full), 28)

    def test_broken_prefix_is_rewritten(self) -> None:
        rows = [{"date": f"2026-05-{day:02d}", "open": "1", "high": "1", "low": "1", "close": str(day), "volume": "1"} for day in range(1, 29)]
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            data_dir = Path(tmp_dir_name)
            upsert_ohlcv_db(data_dir, "AAA", rows)
            con = connect_db(data_dir)
            try:
                calculate_batches(con, data_dir, ["AAA"], batch_size=10)
                con.execute("DELETE FROM indicators WHERE symbol = 'AAA' AND date = '2026-05-10'")  # a hole, not a missing tail
                results = calculate_batches(con, data_dir, find_stale_symbols(con, ["AAA"]), batch_size=10, incremental=True)
                count = con.execute("SELECT count(*) FROM indicators WHERE symbol = 'AAA'").fetchone()[0]
            finally:
                con.close()
        self.assertEqual((results[0]["rows"], count), (28, 28))

    def test_sma_is_bit_identical_to_a_fresh_sum_per_window(self) -> None:
        # Stored indicator rows were written with a fresh sum per window; new rows must match a
        # full recalculation of them exactly, or the 6-decimal rounding can flip.
        values: list[float | None] = [100 + ((i * 7919) % 1000) / 7.0 - i * 0.01 for i in range(5000)]
        values[2500] = None  # a gap restarts the window
        expected: list[float | None] = []
        window: list[float] = []
        for value in values:
            if value is None:
                expected.append(None)
                window.clear()
                continue
            window = (window + [value])[-200:]
            expected.append(sum(window) / 200 if len(window) == 200 else None)
        self.assertEqual(calculate_sma(values, 200), expected)
        self.assertEqual(calculate_sma([1.0, 2.0, 3.0, 4.0], 2), [None, 1.5, 2.5, 3.5])


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
