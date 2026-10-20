"""End-to-end checks for the leakage-safe dataset export."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src import export_dataset
from src.indicators import add_indicators
from src.storage import save_indicator_rows_db, upsert_macro_db, upsert_ohlcv_db


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _seed_db(data_dir: Path) -> dict[str, list[tuple[date, float]]]:
    closes: dict[str, list[tuple[date, float]]] = {}
    for offset, symbol in enumerate(["AAA", "CCC", "DDDDW"]):
        rows = []
        series = []
        for index, day in enumerate(_weekdays(date(2023, 1, 2), 400)):
            close = 20 + offset + 2 * math.sin(index / 5) + index * 0.01
            series.append((day, close))
            rows.append({"date": day.isoformat(), "open": f"{close:.4f}", "high": f"{close + 0.5:.4f}", "low": f"{close - 0.5:.4f}", "close": f"{close:.4f}", "volume": "1000000"})
        upsert_ohlcv_db(data_dir, symbol, rows)
        save_indicator_rows_db(data_dir, symbol, add_indicators(rows))
        closes[symbol] = series
    upsert_macro_db(data_dir, "CPIAUCSL", [
        {"date": f"{year}-{month:02d}-01", "value": str(300 + year - 2022 + month / 100), "realtime_start": "", "realtime_end": ""}
        for year in (2022, 2023, 2024)
        for month in range(1, 13)
    ])
    upsert_macro_db(data_dir, "DGS10", [
        {"date": day.isoformat(), "value": "." if index % 50 == 0 else f"{3 + index / 1000:.3f}", "realtime_start": "", "realtime_end": ""}
        for index, day in enumerate(_weekdays(date(2022, 12, 1), 420))
    ])
    return closes


@unittest.skipIf(importlib.util.find_spec("duckdb") is None, "duckdb dependency is not installed")
class ExportDatasetTest(unittest.TestCase):
    def test_export_is_chronological_and_point_in_time(self) -> None:
        import duckdb  # type: ignore

        with tempfile.TemporaryDirectory() as tmp_dir_name:
            data_dir = Path(tmp_dir_name) / "data"
            closes = _seed_db(data_dir)
            argv = [
                "export_dataset", "--data-dir", str(data_dir), "--out-dir", str(Path(tmp_dir_name) / "out"), "--name", "t",
                "--val-start", "2024-02-01", "--test-start", "2024-05-01", "--jsonl-stride", "1",
            ]
            with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(export_dataset.main(), 0)

            out = Path(tmp_dir_name) / "out" / "t"
            metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
            con = duckdb.connect()
            try:
                train = con.execute(f"SELECT symbol, date, close, fwd_ret_5d, macro_cpiaucsl, macro_dgs10 FROM read_parquet('{out / 'train.parquet'}') ORDER BY symbol, date").fetchall()
                val_min = con.execute(f"SELECT min(date) FROM read_parquet('{out / 'val.parquet'}')").fetchone()[0]
                test_min = con.execute(f"SELECT min(date) FROM read_parquet('{out / 'test.parquet'}')").fetchone()[0]
                symbols = {row[0] for row in con.execute(f"SELECT DISTINCT symbol FROM read_parquet('{out}/*.parquet')").fetchall()}
            finally:
                con.close()
            jsonl_lines = (out / "train.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(symbols, {"AAA", "CCC"}, "five-letter W suffix warrants are excluded")
        self.assertTrue(train)
        train_end = max(row[1] for row in train)
        self.assertLess(train_end, date(2024, 2, 1) - timedelta(days=metadata["splits"]["embargo_days"]))
        self.assertGreaterEqual(val_min, date(2024, 2, 1))
        self.assertGreaterEqual(test_min, date(2024, 5, 1))

        series = closes["AAA"]
        index = {day: position for position, (day, _) in enumerate(series)}
        symbol, day, close, fwd_ret_5d, cpi, dgs10 = next(row for row in train if row[0] == "AAA")
        position = index[day]
        self.assertAlmostEqual(fwd_ret_5d, math.log(round(series[position + 5][1], 4) / round(series[position][1], 4)), places=9)
        # Monthly CPI (median gap 31 days) becomes visible 31 + 20 days after its observation date.
        months = [date(year, month, 1) for year in (2022, 2023, 2024) for month in range(1, 13)]
        expected_month = max(month for month in months if month + timedelta(days=51) <= day)
        self.assertAlmostEqual(cpi, 300 + expected_month.year - 2022 + expected_month.month / 100, places=6)
        self.assertIsNotNone(dgs10)

        example = json.loads(jsonl_lines[0])
        self.assertEqual([message["role"] for message in example["messages"]], ["system", "user", "assistant"])
        self.assertRegex(example["messages"][2]["content"], r"^(상승|하락|보합) \([+-]\d+\.\d{2}%\)$")

    def test_news_before_close_excludes_after_close_headlines(self) -> None:
        stamps = [datetime(2024, 3, 14, 15), datetime(2024, 3, 15, 19, 59), datetime(2024, 3, 15, 20, 0), datetime(2024, 3, 1)]
        items = [("2024-03-14", "a", "one"), ("2024-03-15", "b", "two"), ("2024-03-15", "c", "after close"), ("2024-03-01", "d", "old")]
        order = sorted(range(len(stamps)), key=lambda i: stamps[i])
        stamps = [stamps[i] for i in order]
        items = [items[i] for i in order]

        headlines = export_dataset.news_before_close(stamps, items, date(2024, 3, 15), limit=5)

        self.assertEqual([headline for _, _, headline in headlines], ["one", "two"])

    def test_direction_label_uses_flat_band(self) -> None:
        self.assertEqual(export_dataset.direction_label(math.log(1.005), 0.01), "보합")
        self.assertEqual(export_dataset.direction_label(math.log(1.02), 0.01), "상승")
        self.assertEqual(export_dataset.direction_label(math.log(0.97), 0.01), "하락")


if __name__ == "__main__":
    unittest.main()
