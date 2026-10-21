"""Per-symbol collection order and failure backoff shared by the OHLCV collectors."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from src.state import utc_now_iso


# Consecutive generic failures wait 1, 2, 4, 8, 16, then 30 days before the next attempt.
BACKOFF_DAYS = (1, 2, 4, 8, 16, 30)
NOT_FOUND_RETRY_DAYS = 30


def provider_entry(state: dict, symbol: str, provider: str) -> dict:
    """Return (creating if needed) ``state["symbols"][symbol][provider]``."""
    return state.setdefault("symbols", {}).setdefault(symbol, {}).setdefault(provider, {})


def peek_entry(state: dict, symbol: str, provider: str) -> dict:
    """Return a symbol's provider entry without adding it to state."""
    entry = state.get("symbols", {}).get(symbol, {})
    return entry.get(provider, {}) if isinstance(entry, dict) else {}


def retry_blocked(entry: dict, today: str) -> bool:
    """True while a failed symbol is still inside its backoff window."""
    next_retry = entry.get("next_retry_date")
    return bool(next_retry) and str(next_retry) > today


def record_success(entry: dict, today: str, **extra: object) -> None:
    now = utc_now_iso()
    entry.update(
        {
            "status": "done",
            "updated_at": now,
            "last_success_date": today,
            "last_success_at": now,
            "fail_count": 0,
            "next_retry_date": None,
            "error": None,
            **extra,
        }
    )


def record_failure(entry: dict, today: str, error: str, not_found: bool = False) -> None:
    fail_count = int(entry.get("fail_count") or 0) + 1
    wait_days = NOT_FOUND_RETRY_DAYS if not_found else BACKOFF_DAYS[min(fail_count, len(BACKOFF_DAYS)) - 1]
    entry.update(
        {
            "status": "not_found" if not_found else "failed",
            "updated_at": utc_now_iso(),
            "fail_count": fail_count,
            "next_retry_date": (date.fromisoformat(today) + timedelta(days=wait_days)).isoformat(),
            "error": error,
        }
    )


def record_skip(entry: dict, reason: str) -> None:
    """Budget ran out before this symbol; not a failure, so no backoff."""
    entry.update({"status": "skipped_rate_limit", "updated_at": utc_now_iso(), "reason": reason})


def choose_rotation_symbols(
    symbols: Iterable[str],
    state: dict,
    provider: str,
    limit: int | None,
    today: str,
    force: bool = False,
    exclude: set[str] | None = None,
) -> list[str]:
    """Order symbols by data staleness: never collected first, then oldest last success.

    Skips symbols already collected today, symbols in ``exclude`` (core or delisted), and
    failed symbols still waiting out their backoff. ``force`` ignores the last two checks
    that depend on history. Ties keep the input order. State is not modified.
    """
    exclude = exclude or set()
    ranked: list[tuple[int, str, int, str]] = []
    for index, symbol in enumerate(dict.fromkeys(symbols)):
        if symbol in exclude:
            continue
        entry = peek_entry(state, symbol, provider)
        last_success = str(entry.get("last_success_date") or "")
        if not force and (last_success == today or retry_blocked(entry, today)):
            continue
        ranked.append((1 if last_success else 0, last_success, index, symbol))
    ranked.sort()
    chosen = [symbol for *_, symbol in ranked]
    return chosen if limit is None else chosen[: max(0, limit)]
