"""Atomic writes used by state, summary, report, raw, and symbol-list files."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from src.atomic_io import atomic_write_bytes, atomic_write_json
from src.catalog import atomic_write_symbols


class AtomicWriteTest(unittest.TestCase):
    def test_json_is_sorted_indented_and_leaves_no_tmp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            atomic_write_json(path, {"b": 1, "a": "한글"})
            text = path.read_text(encoding="utf-8")
            leftovers = list(path.parent.glob("*.tmp"))
        self.assertEqual(text, '{\n  "a": "한글",\n  "b": 1\n}\n')
        self.assertEqual(leftovers, [])

    def test_dates_from_duckdb_serialize_as_iso_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            atomic_write_json(path, {"issues": [{"date": date(2026, 10, 20), "at": datetime(2026, 10, 20, 16, 10)}]})
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["issues"][0], {"date": "2026-10-20", "at": "2026-10-20T16:10:00"})

    def test_unserializable_payload_keeps_the_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, {"ok": True})
            with self.assertRaises(TypeError):
                atomic_write_json(path, {"bad": object()})
            saved = json.loads(path.read_text(encoding="utf-8"))
            leftovers = list(Path(tmp).glob("*.tmp"))
        self.assertEqual(saved, {"ok": True})
        self.assertEqual(leftovers, [])

    def test_bytes_and_symbol_list_overwrite_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw" / "SPY.json.gz"
            atomic_write_bytes(raw, b"old")
            atomic_write_bytes(raw, b"new")
            symbols = Path(tmp) / "symbols.txt"
            atomic_write_symbols(symbols, ["AAPL", "MSFT"])
            self.assertEqual(raw.read_bytes(), b"new")
            self.assertEqual(symbols.read_text(encoding="utf-8"), "AAPL\nMSFT\n")


if __name__ == "__main__":
    unittest.main()
