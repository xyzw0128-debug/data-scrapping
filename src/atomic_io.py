"""Atomic file writes shared by every state, summary, report, raw, and list file.

Data goes to a sibling ``<name>.tmp`` file, is flushed and fsynced, then moved over the
target with ``os.replace``. Readers see either the old file or the new one, never a partial
write, and a failure before the replace leaves the old file untouched.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path


def atomic_write_text(path: Path, text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))


def json_default(value: Any) -> str:
    """Serialize DuckDB DATE/TIMESTAMP values (datetime.date/datetime) as ISO 8601 strings."""
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def atomic_write_json(path: Path, payload: Any) -> Path:
    """Write indented, key-sorted JSON with a trailing newline.

    The payload is serialized before the temporary file is created, so an unserializable
    value raises without touching the existing file.
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=json_default) + "\n"
    return atomic_write_text(path, text)
