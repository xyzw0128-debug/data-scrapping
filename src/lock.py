"""Lock file helpers to prevent overlapping collector runs."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path


PIPELINE_LOCK_NAME = "pipeline.lock"
# Set by scripts/run_daily.sh after it takes the pipeline lock with flock(1); child CLIs
# then run under the parent's lock instead of trying to take it again.
PIPELINE_LOCK_ENV = "DATA_SCRAPPING_PIPELINE_LOCK"


class LockHeldError(RuntimeError):
    """Raised when another collector process already owns the lock."""


class FileLock:
    """Small exclusive lock based on atomic file creation.

    This intentionally avoids third-party dependencies. The lock file includes
    the current PID so a human can inspect it if a previous
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
            if not _holder_is_dead(holder):
                detail = f" Existing lock: {holder}" if holder else ""
                raise LockHeldError(f"Collector lock is already held at {self.path}.{detail}") from exc
            # The previous run was killed without releasing the lock (e.g. its terminal was closed).
            self.path.unlink(missing_ok=True)
            try:
                self._fd = os.open(self.path, flags)
            except FileExistsError as retry_exc:
                raise LockHeldError(f"Collector lock is already held at {self.path}.") from retry_exc
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


class PipelineLock:
    """Exclusive flock on data/state/pipeline.lock shared by every writer of market.db and state files.

    run_daily.sh holds it for the whole run and exports PIPELINE_LOCK_ENV=1 so the stages it
    starts skip acquiring it. A CLI started by hand takes it itself and fails fast while a
    run is in progress. The kernel drops a flock when its holder exits, so a killed run never
    leaves a stale lock behind. The file content (holder pid) is informational only.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if os.environ.get(PIPELINE_LOCK_ENV) == "1":
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = os.read(fd, 256).decode("utf-8", "replace").strip()
            os.close(fd)
            detail = f" Holder: {holder}" if holder else ""
            raise LockHeldError(f"Pipeline lock is held at {self.path}.{detail}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> PipelineLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def pipeline_lock(data_dir: Path) -> PipelineLock:
    """Return the pipeline lock for a data directory."""
    return PipelineLock(data_dir / "state" / PIPELINE_LOCK_NAME)


def pipeline_running(data_dir: Path) -> bool:
    """Return True when some process currently holds the pipeline lock."""
    path = data_dir / "state" / PIPELINE_LOCK_NAME
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def lock_held_exit(exc: LockHeldError) -> int:
    """Print the standard lock_held JSON result and return the CLI exit code."""
    print(json.dumps({"status": "lock_held", "error": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True))
    return 2


def _holder_is_dead(holder: str) -> bool:
    """Return True only when the lock names a PID that no longer exists."""
    for line in holder.splitlines():
        if line.startswith("pid="):
            try:
                pid = int(line[len("pid="):])
            except ValueError:
                return False
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            return False
    return False
