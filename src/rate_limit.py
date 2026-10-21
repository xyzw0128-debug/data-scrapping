"""Daily credit budgets and API key rotation for providers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from src.config import ProviderConfig
from src.state import ensure_daily_provider_state


class PerMinuteRateLimiter:
    """Track API call timestamps and sleep before exceeding a per-minute limit."""

    def __init__(self, per_minute_limit: int) -> None:
        self.per_minute_limit = per_minute_limit
        self.call_timestamps: list[float] = []

    def wait(self) -> None:
        if self.per_minute_limit <= 0:
            return

        while True:
            now = time.monotonic()
            self.call_timestamps = [
                timestamp for timestamp in self.call_timestamps if now - timestamp < 60.0
            ]
            if len(self.call_timestamps) < self.per_minute_limit:
                self.call_timestamps.append(now)
                return

            sleep_seconds = max(0.0, 60.0 - (now - self.call_timestamps[0]))
            time.sleep(sleep_seconds)


@dataclass(frozen=True)
class BudgetDecision:
    """Decision about whether another provider call can be made."""

    allowed: bool
    reason: str
    remaining_before_reserve: int


class DailyBudget:
    """Track provider credits against a conservative daily budget.

    The state field keeps its historical name ``calls_used_today``; its value is credits.
    Every call so far cost one credit, so existing state files need no migration.
    """

    def __init__(self, config: ProviderConfig, provider_state: dict):
        self.config = config
        self.provider_state = provider_state

    @property
    def calls_used(self) -> int:
        return int(self.provider_state.get("calls_used_today", 0))

    @property
    def remaining_before_reserve(self) -> int:
        return max(0, self.config.usable_daily_budget - self.calls_used)

    def can_spend(self, cost: int = 1) -> BudgetDecision:
        if not self.config.enabled:
            return BudgetDecision(False, "provider_disabled", self.remaining_before_reserve)
        if cost <= self.remaining_before_reserve:
            return BudgetDecision(True, "ok", self.remaining_before_reserve)
        return BudgetDecision(False, "daily_budget_reserved", self.remaining_before_reserve)

    def spend(self, cost: int = 1) -> None:
        self.provider_state["calls_used_today"] = self.calls_used + cost

    def force_exhaust(self) -> None:
        """Mark the key as used up for today after the provider reports its daily limit."""
        self.provider_state["calls_used_today"] = max(self.calls_used, self.config.daily_limit)

    def sync_server_usage(self, server_used: int) -> None:
        """Adopt the provider-reported usage when it is higher (calls made outside this state)."""
        if server_used > self.calls_used:
            self.provider_state["calls_used_today"] = server_used
            self.provider_state["server_synced"] = True


@dataclass
class KeySlot:
    """One API key with its own per-day state bucket ``{provider}:key{index}``."""

    index: int
    api_key: str
    state_key: str
    budget: DailyBudget
    # Set when the key must not be used for the rest of this run (e.g. its usage could not be verified).
    disabled_reason: str | None = None


class KeyPool:
    """Manage multiple API keys with a sequential exhaustion strategy.

    Keys are used in order; the next key is activated only after the current one has no
    budget left. Each key keeps its own state bucket ``state["providers"]["{name}:key{index}"]``.
    Every lookup re-checks the UTC date, so a run that crosses 00:00 UTC (when providers
    reset their daily quota) starts counting against the new day.
    """

    def __init__(self, config: ProviderConfig, state: dict, slots: list[KeySlot]) -> None:
        self.config = config
        self.state = state
        self.slots = slots

    @classmethod
    def from_env(cls, config: ProviderConfig, state: dict) -> "KeyPool":
        """Resolve key env vars in order and attach each key's daily state bucket."""
        from src.env import load_project_env

        load_project_env(Path(__file__).resolve().parents[1])
        slots: list[KeySlot] = []
        seen_api_keys: set[str] = set()
        for index, env_name in enumerate(config.resolved_key_envs()):
            api_key = os.environ.get(env_name, "")
            if api_key:
                if api_key in seen_api_keys:
                    continue
                seen_api_keys.add(api_key)
            state_key = f"{config.name}:key{index}"
            provider_state = ensure_daily_provider_state(state, state_key, config.daily_limit, config.daily_reserve)
            slots.append(KeySlot(index, api_key, state_key, DailyBudget(config, provider_state)))
        return cls(config, state, slots)

    def _refresh_day(self) -> None:
        # ensure_daily_provider_state resets the same dict in place, so budgets stay valid.
        for slot in self.slots:
            ensure_daily_provider_state(self.state, slot.state_key, self.config.daily_limit, self.config.daily_reserve)

    def active_slots(self) -> list[KeySlot]:
        """Slots that have an API key configured and are not excluded for this run."""
        return [slot for slot in self.slots if slot.api_key and not slot.disabled_reason]

    def current_slot(self, cost: int = 1) -> KeySlot | None:
        """Return the first key that can still pay ``cost`` credits today, or None."""
        self._refresh_day()
        for slot in self.active_slots():
            if slot.budget.can_spend(cost).allowed:
                return slot
        return None

    def total_remaining(self) -> int:
        """Sum of remaining budget across all configured keys."""
        self._refresh_day()
        return sum(slot.budget.remaining_before_reserve for slot in self.active_slots())

    def summary(self) -> list[dict]:
        """Snapshot of each key slot's usage for logging."""
        self._refresh_day()
        return [
            {
                "key_index": slot.index,
                "has_key": bool(slot.api_key),
                "calls_used_today": slot.budget.calls_used,
                "remaining_before_reserve": slot.budget.remaining_before_reserve,
                "disabled_reason": slot.disabled_reason,
            }
            for slot in self.slots
        ]


class RunCreditCap:
    """Credits one pipeline run may spend across its collector phases (None = unlimited)."""

    def __init__(self, limit: int | None, already_used: int = 0) -> None:
        self.limit = limit
        self.used = already_used

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)

    def allows(self, cost: int) -> bool:
        return self.limit is None or cost <= self.limit - self.used

    def spend(self, cost: int) -> None:
        self.used += cost
