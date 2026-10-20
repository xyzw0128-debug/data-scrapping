"""Regression checks for the per-minute rate limiter, key rotation, and run credit cap."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src import rate_limit
from src.config import ProviderConfig
from src.rate_limit import KeyPool, RunCreditCap


def _config(**overrides) -> ProviderConfig:
    values = dict(name="twelve_data", enabled=True, api_key_env="TD", api_key_envs=("TD_1", "TD_2"), daily_limit=10, daily_reserve=2)
    values.update(overrides)
    return ProviderConfig(**values)


class PerMinuteRateLimiterTest(unittest.TestCase):
    def test_limiter_sleeps_before_exceeding_limit(self) -> None:
        limiter = rate_limit.PerMinuteRateLimiter(per_minute_limit=2)

        with patch("src.rate_limit.time.monotonic", side_effect=[0.0, 1.0, 2.0, 61.0]), patch(
            "src.rate_limit.time.sleep"
        ) as sleep:
            limiter.wait()
            limiter.wait()
            limiter.wait()

        sleep.assert_called_once_with(58.0)
        self.assertEqual(limiter.call_timestamps, [61.0])


class KeyPoolTest(unittest.TestCase):
    def test_keys_are_used_in_order_and_duplicates_skipped(self) -> None:
        state: dict = {}
        with patch.dict(os.environ, {"TD_1": "a", "TD_2": "b", "TD": "a"}, clear=False):
            pool = KeyPool.from_env(_config(), state)
        self.assertEqual([slot.api_key for slot in pool.active_slots()], ["a", "b"])
        first = pool.current_slot(cost=2)
        first.budget.spend(7)  # 8 usable credits per key
        self.assertEqual(pool.current_slot(cost=2).api_key, "b", "key a has only 1 credit left")
        self.assertEqual(pool.current_slot(cost=1).api_key, "a")
        self.assertEqual(pool.total_remaining(), 1 + 8)

    def test_new_utc_day_resets_budget_in_place(self) -> None:
        state: dict = {}
        with patch.dict(os.environ, {"TD_1": "a", "TD_2": "", "TD": ""}, clear=False):
            pool = KeyPool.from_env(_config(), state)
        pool.current_slot().budget.spend(8)
        self.assertIsNone(pool.current_slot())
        with patch("src.state.utc_today", return_value="2999-01-01"):
            slot = pool.current_slot()
        self.assertIsNotNone(slot)
        self.assertEqual(state["providers"]["twelve_data:key0"]["calls_used_today"], 0)

    def test_server_usage_only_raises_local_count(self) -> None:
        state: dict = {}
        with patch.dict(os.environ, {"TD_1": "a", "TD_2": "", "TD": ""}, clear=False):
            pool = KeyPool.from_env(_config(), state)
        budget = pool.current_slot().budget
        budget.spend(3)
        budget.sync_server_usage(1)
        self.assertEqual(budget.calls_used, 3)
        budget.sync_server_usage(6)
        self.assertEqual(budget.calls_used, 6)

    def test_run_credit_cap(self) -> None:
        cap = RunCreditCap(5, already_used=3)
        self.assertTrue(cap.allows(2))
        self.assertFalse(cap.allows(3))
        cap.spend(2)
        self.assertEqual(cap.remaining, 0)
        self.assertTrue(RunCreditCap(None).allows(10**6))


if __name__ == "__main__":
    unittest.main()
