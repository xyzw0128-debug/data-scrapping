"""Simple daily budget checks for API providers."""

from __future__ import annotations

import os
from dataclasses import dataclass

from src.config import ProviderConfig


@dataclass(frozen=True)
class BudgetDecision:
    """Decision about whether another provider call can be made."""

    allowed: bool
    reason: str
    remaining_before_reserve: int


class DailyBudget:
    """Track provider calls/credits against a conservative daily budget."""

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
        """일일 크레딧 소진 시 내부 카운터를 강제로 한도까지 올려 다음 키로 로테이션되게 함."""
        self.provider_state["calls_used_today"] = self.config.daily_limit

class KeyPool:
    """Manage multiple API keys with sequential exhaustion strategy.

    Keys are tried in order. The current key is used until its daily budget
    is exhausted, then the next key is activated. This is the safest and most
    predictable rotation strategy for conservative long-term operation.

    State is tracked per-key under ``state["providers"]["{name}:key{index}"]``.

    Usage::

        pool = KeyPool.from_env(config, state)
        slot = pool.current_slot()
        if slot is None:
            # all keys exhausted today
            ...
        api_key, budget = slot
        budget.spend(1)

    """

    def __init__(self, slots: list[tuple[str, DailyBudget]]) -> None:
        # slots: [(api_key_value, DailyBudget), ...]
        self._slots = slots

    @classmethod
    def from_env(cls, config: ProviderConfig, state: dict) -> "KeyPool":
        """Build a KeyPool by resolving env vars and loading per-key state.

        Each key gets its own state bucket:
        ``state["providers"]["{provider_name}:key{index}"]``
        """
        from pathlib import Path
        from src.env import load_project_env
        from src.state import ensure_daily_provider_state

        load_project_env(Path(__file__).resolve().parents[1])
        key_envs = config.resolved_key_envs()
        slots: list[tuple[str, DailyBudget]] = []
        seen_api_keys: set[str] = set()
        for index, env_name in enumerate(key_envs):
            api_key = os.environ.get(env_name, "")
            if api_key:
                if api_key in seen_api_keys:
                    continue
                seen_api_keys.add(api_key)
            state_key = f"{config.name}:key{index}"
            provider_state = ensure_daily_provider_state(
                state, state_key, config.daily_limit, config.daily_reserve
            )
            budget = DailyBudget(config, provider_state)
            slots.append((api_key, budget))
        return cls(slots)

    def current_slot(self) -> tuple[str, DailyBudget] | None:
        """Return the first key that still has remaining budget, or None.

        Keys are tried in order (sequential exhaustion). A key with an empty
        API key string is skipped silently — this lets operators add fewer
        keys than the yaml lists without errors.
        """
        for api_key, budget in self._slots:
            if not api_key:
                continue
            if budget.remaining_before_reserve > 0:
                return api_key, budget
        return None

    def total_remaining(self) -> int:
        """Return the sum of remaining budget across all configured keys."""
        return sum(
            budget.remaining_before_reserve
            for api_key, budget in self._slots
            if api_key
        )

    def summary(self) -> list[dict]:
        """Return a snapshot of each key slot's usage for logging."""
        result = []
        for index, (api_key, budget) in enumerate(self._slots):
            result.append(
                {
                    "key_index": index,
                    "has_key": bool(api_key),
                    "calls_used_today": budget.calls_used,
                    "remaining_before_reserve": budget.remaining_before_reserve,
                }
            )
        return result
