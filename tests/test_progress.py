"""scripts/progress.py shows non-Twelve-Data stages while the pipeline lock is held."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src.lock import PIPELINE_LOCK_ENV, pipeline_lock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("progress", ROOT / "scripts" / "progress.py")
progress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(progress)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fake_process(proc: Path, pid: int, ppid: int, argv: list[str]) -> None:
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    (proc / str(pid) / "stat").write_text(f"{pid} ({Path(argv[0]).name} x) S {ppid} {pid} 0\n", encoding="utf-8")


class RunningStageTest(unittest.TestCase):
    def test_stage_from_run_daily_child_or_direct_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            _fake_process(proc, 100, 1, ["/bin/bash", "scripts/run_daily.sh"])
            _fake_process(proc, 150, 100, ["/bin/bash", "scripts/run_daily.sh"])  # Discord status monitor subshell
            _fake_process(proc, 151, 150, [".venv/bin/python", "-m", "src.discord_status", "--refresh-progress"])
            _fake_process(proc, 200, 100, [".venv/bin/python", "-m", "src.snapshot", "--phase", "core"])
            _fake_process(proc, 300, 1, [".venv/bin/python", "-m", "src.finnhub_news", "--limit", "2"])
            self.assertEqual(progress.running_stage(100, proc), "스냅숏 (core)")
            self.assertEqual(progress.running_stage(300, proc), "뉴스(Finnhub)")
            self.assertIsNone(progress.running_stage(150, proc), "Discord status is not a stage")
            self.assertIsNone(progress.running_stage(999, proc))
        self.assertEqual(progress.stage_of(["bash", "/home/x/scripts/rclone_backup.sh"]), "백업")


@patch.dict(os.environ, {PIPELINE_LOCK_ENV: "", "FINNHUB_LIMIT": ""})
class RenderTest(unittest.TestCase):
    def _render(self, data_dir: Path, state: dict) -> str:
        out = io.StringIO()
        with patch.object(progress, "DATA_DIR", data_dir), patch.object(progress, "news_planned", lambda: 10), contextlib.redirect_stdout(out):
            progress.render(state, {})
        return out.getvalue()

    def test_lock_without_current_run_shows_running_and_news_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with pipeline_lock(data_dir):
                started = progress.lock_info(data_dir / "state" / "pipeline.lock")["since"]
                (data_dir / "state" / "finnhub_news.json").write_text(json.dumps({"schema_version": 2, "symbols": {
                    "AAA": {"status": "done", "last_attempt_at": _iso(started)},
                    "BBB": {"status": "failed", "last_attempt_at": _iso(started + timedelta(seconds=5))},
                    "OLD": {"status": "done", "last_attempt_at": _iso(started - timedelta(days=1))},
                }}), encoding="utf-8")
                text = self._render(data_dir, {"providers": {}, "symbols": {}, "runs": []})
        self.assertIn("● 실행 중", text)
        self.assertNotIn("대기 중", text)
        self.assertIn(f"pid {os.getpid()}", text)
        self.assertIn("2 / 10 종목 시도  ✗1", text)

    def test_no_lock_is_waiting_and_stale_current_run_is_abnormal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.assertIn("대기 중", self._render(data_dir, {"symbols": {}, "runs": []}))
            stale = {"symbols": {}, "current_run": {"phase": "core", "updated_at": _iso(datetime.now(UTC))}}
            self.assertIn("비정상 종료", self._render(data_dir, stale))


if __name__ == "__main__":
    unittest.main()
