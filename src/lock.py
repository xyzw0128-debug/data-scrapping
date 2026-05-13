"""Lock file helpers to prevent overlapping collector runs."""

from __future__ import annotations

import os
from pathlib import Path


class LockHeldError(RuntimeError):
    """Raised when another collector process already owns the lock."""


class FileLock:
    """Small exclusive lock based on atomic file creation.

    This intentionally avoids third-party dependencies. The lock file includes
    the current PID so a human can inspect it on a Raspberry Pi if a previous
    process was interrupted.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            self._fd = os.open(self.path, flags)
        except FileExistsError as exc:
            holder = self._read_holder()
            detail = f" Existing lock: {holder}" if holder else ""
            raise LockHeldError(f"Collector lock is already held at {self.path}.{detail}") from exc
        os.write(self._fd, f"pid={os.getpid()}\n".encode("utf-8"))
        os.write(self._fd, f"path={self.path}\n".encode("utf-8"))
        os.fsync(self._fd)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def _read_holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
