"""Checks for collector lock handling, including locks left by killed runs."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.lock import FileLock, LockHeldError


class FileLockTest(unittest.TestCase):
    def test_live_holder_blocks_second_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            path = Path(tmp_dir_name) / "collector.lock"
            with FileLock(path):
                with self.assertRaises(LockHeldError):
                    FileLock(path).acquire()
            self.assertFalse(path.exists())

    def test_lock_from_dead_process_is_replaced(self) -> None:
        dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            path = Path(tmp_dir_name) / "collector.lock"
            path.write_text(f"pid={dead.stdout.strip()}\npath={path}\n", encoding="utf-8")
            with FileLock(path):
                self.assertIn(f"pid={os.getpid()}", path.read_text(encoding="utf-8"))
            self.assertFalse(path.exists())

    def test_unreadable_holder_is_treated_as_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            path = Path(tmp_dir_name) / "collector.lock"
            path.write_text("garbage\n", encoding="utf-8")
            with self.assertRaises(LockHeldError):
                FileLock(path).acquire()


if __name__ == "__main__":
    unittest.main()
