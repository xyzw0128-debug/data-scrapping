"""The daily summary CLI writes data/logs/daily/<date>.json."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import summary
from src.lock import PIPELINE_LOCK_ENV, pipeline_lock
from src.state import utc_today


def _run_main(data_dir: Path) -> tuple[int, str]:
    out = io.StringIO()
    with patch("sys.argv", ["src.summary", "--data-dir", str(data_dir)]), contextlib.redirect_stdout(out):
        code = summary.main()
    return code, out.getvalue()


@patch.dict(os.environ, {PIPELINE_LOCK_ENV: ""})
class SummaryMainTest(unittest.TestCase):
    def test_main_saves_daily_summary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            code, printed = _run_main(data_dir)
            path = data_dir / "logs" / "daily" / f"{utc_today()}.json"
            self.assertEqual(code, 0)
            self.assertTrue(path.exists())
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["date"], utc_today())
        self.assertEqual(json.loads(printed)["summary_path"], str(path))

    def test_summary_records_db_file_sizes(self) -> None:
        from src.storage import ensure_db_schema, get_db_path, get_snapshot_path

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            empty = summary.build_summary(data_dir)["files"]["db_files"]
            ensure_db_schema(data_dir)
            get_snapshot_path(data_dir).write_bytes(get_db_path(data_dir).read_bytes())
            built = summary.build_summary(data_dir)
            size = get_db_path(data_dir).stat().st_size
        self.assertEqual(empty, {"market_db_bytes": None, "snapshot_bytes": None})
        self.assertEqual(built["files"]["db_files"], {"market_db_bytes": size, "snapshot_bytes": size})
        self.assertIn(f"db_mb={round(size / 1e6)} snapshot_mb={round(size / 1e6)}", summary.format_discord_message(built))

    def test_lock_held_exits_without_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with pipeline_lock(data_dir):
                code, printed = _run_main(data_dir)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(printed)["status"], "lock_held")
            self.assertEqual(list(data_dir.glob("logs/daily/*.json")), [])


if __name__ == "__main__":
    unittest.main()
