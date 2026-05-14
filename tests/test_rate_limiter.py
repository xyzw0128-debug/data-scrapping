"""Regression checks for collector per-minute rate limiters."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src import alpha_vantage, main


class PerMinuteRateLimiterTest(unittest.TestCase):
    def test_twelve_data_limiter_sleeps_before_exceeding_limit(self) -> None:
        limiter = main.PerMinuteRateLimiter(per_minute_limit=2)

        with patch("src.main.time.monotonic", side_effect=[0.0, 1.0, 2.0, 61.0]), patch(
            "src.main.time.sleep"
        ) as sleep:
            limiter.wait()
            limiter.wait()
            limiter.wait()

        sleep.assert_called_once_with(58.0)
        self.assertEqual(limiter.call_timestamps, [61.0])

    def test_alpha_vantage_limiter_sleeps_before_exceeding_limit(self) -> None:
        limiter = alpha_vantage.PerMinuteRateLimiter(per_minute_limit=2)

        with patch("src.alpha_vantage.time.monotonic", side_effect=[0.0, 1.0, 2.0, 61.0]), patch(
            "src.alpha_vantage.time.sleep"
        ) as sleep:
            limiter.wait()
            limiter.wait()
            limiter.wait()

        sleep.assert_called_once_with(58.0)
        self.assertEqual(limiter.call_timestamps, [61.0])


if __name__ == "__main__":
    unittest.main()
