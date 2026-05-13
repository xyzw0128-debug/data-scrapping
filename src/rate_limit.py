"""Simple daily budget checks for API providers."""

from __future__ import annotations

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
