"""State file handling for resumable long-running collection."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_today() -> str:
    """Return today's UTC date as YYYY-MM-DD."""
    return datetime.now(UTC).date().isoformat()


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_state() -> dict[str, Any]:
    """Create a fresh state document."""
    return {
        "schema_version": 1,
        "providers": {},
        "symbols": {},
        "runs": [],
    }


def load_state(path: Path) -> dict[str, Any]:
    """Load state.json, returning a default state if it does not exist."""
    if not path.exists():
        return default_state()
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically save state.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def ensure_daily_provider_state(
    state: dict[str, Any],
    provider_name: str,
    daily_limit: int,
    daily_reserve: int,
) -> dict[str, Any]:
    """Return provider state, resetting daily usage when UTC date changes."""
    today = utc_today()
    providers = state.setdefault("providers", {})
    provider = providers.setdefault(provider_name, {})
    if provider.get("date") != today:
        provider.clear()
        provider.update(
            {
                "date": today,
                "calls_used_today": 0,
                "daily_limit": daily_limit,
                "daily_reserve": daily_reserve,
                "last_reset_utc": utc_now_iso(),
            }
        )
    else:
        provider.setdefault("daily_limit", daily_limit)
        provider.setdefault("daily_reserve", daily_reserve)
        provider.setdefault("calls_used_today", 0)
    return provider
