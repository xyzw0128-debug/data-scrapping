"""Checks for the Discord progress line and the native-type OHLCV validation."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from src import discord_status, validate_duckdb
from src.state import utc_today

HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None


def _write_state(data_dir: Path, state: dict) -> None:
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / "state.json").write_text(json.dumps(state), encoding="utf-8")


class DiscordProgressTest(unittest.TestCase):
    def test_running_phase_uses_its_own_planned_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            # Rotation plans 700 symbols; 520 done today in core must not push it past 700.
            _write_state(data_dir, {"current_run": {"phase": "rotation", "planned": 700, "processed": 640, "failed": 3}})
            self.assertEqual(discord_status._read_progress(data_dir), ("rotation", 643, 700))

    def test_finished_phase_falls_back_to_todays_last_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            run = {"current_phase": "core", "current_planned": 526, "processed": 523, "failed": 3, "finished_at": f"{utc_today()}T23:40:00Z"}
            _write_state(data_dir, {"runs": [run]})
            self.assertEqual(discord_status._read_progress(data_dir), ("core", 526, 526))
            _write_state(data_dir, {"runs": [{**run, "finished_at": "2000-01-01T00:00:00Z"}]})
            self.assertIsNone(discord_status._read_progress(data_dir), "an old run is not today's progress")
            self.assertIsNone(discord_status._read_progress(data_dir / "missing"))

    def test_message_shows_phase_and_count(self) -> None:
        message = discord_status._format_message({"progress_phase": "rotation", "progress_done": 643, "progress_total": 700})
        self.assertIn("진행률: 순환 643/700", message)
        self.assertIn("진행률: -", discord_status._format_message({}))


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
class NativeValidationTest(unittest.TestCase):
    def test_invalid_rows_are_found_without_casts(self) -> None:
        from src.storage import connect_db, ensure_db_schema

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            ensure_db_schema(data_dir)
            con = connect_db(data_dir)
            try:
                rows = [
                    ("OK", "2024-06-03", 1.0, 2.0, 0.5, 1.5, 10),
                    ("HL", "2024-06-03", 1.0, 0.4, 0.5, 1.5, 10),  # high < low
                    ("VOL", "2024-06-03", 1.0, 2.0, 0.5, 1.5, -1),  # negative volume
                    ("NUL", "2024-06-03", 1.0, 2.0, 0.5, 1.5, None),  # missing volume
                    ("INF", "2024-06-03", 1.0, float("inf"), 0.5, 1.5, 10),  # not finite
                ]
                con.executemany("INSERT INTO ohlcv VALUES (?, CAST(? AS DATE), ?, ?, ?, ?, ?, 'x', NULL)", rows)
            finally:
                con.close()
            report = validate_duckdb.validate_with_duckdb(data_dir)
        flagged = sorted(issue["symbol"] for issue in report["issues"] if issue["type"] == "invalid_numeric_or_range")
        self.assertEqual(flagged, ["HL", "INF", "NUL", "VOL"])
        self.assertEqual(report["status"], "fail")


if __name__ == "__main__":
    unittest.main()
