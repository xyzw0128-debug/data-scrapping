"""Split events derived from Twelve Data price series.

Basic keys cannot call /splits, but ``time_series`` serves both raw (adjust=none) and
split-adjusted (adjust=splits) prices. Their ratio on each date is the product of all later
splits, so the ratio changes exactly on split dates. Values follow yfinance's
``Stock Splits`` convention: 4.0 for a 4:1 split, 0.0556 for a 1:18 reverse split.
"""

from __future__ import annotations

import statistics
from fractions import Fraction


SPLIT_CHANGE_THRESHOLD = 0.02
SNAP_TOLERANCE = 0.005
MAX_SPLIT_DENOMINATOR = 100
# Split detection compares a fresh split-adjusted series with the stored one.
DETECTION_MIN_OVERLAP = 5
DETECTION_THRESHOLD = 0.02


def snap_ratio(value: float) -> float:
    """Round a measured ratio to the nearby simple fraction (e.g. 9.9998 -> 10.0, 0.05556 -> 1/18)."""
    fraction = Fraction(value).limit_denominator(MAX_SPLIT_DENOMINATOR)
    if fraction and abs(float(fraction) / value - 1) < SNAP_TOLERANCE:
        return round(float(fraction), 6)
    return round(value, 6)


def split_events(raw_closes: dict[str, float], adjusted_closes: dict[str, float]) -> list[tuple[str, float]]:
    """Return [(date, ratio)] where the raw/split-adjusted close ratio changes."""
    events: list[tuple[str, float]] = []
    previous: float | None = None
    for day in sorted(raw_closes):
        raw, adjusted = raw_closes.get(day), adjusted_closes.get(day)
        if not raw or not adjusted or raw <= 0 or adjusted <= 0:
            continue
        ratio = raw / adjusted
        if previous is not None:
            change = previous / ratio
            if abs(change - 1) > SPLIT_CHANGE_THRESHOLD:
                events.append((day, snap_ratio(change)))
        previous = ratio
    return events


def split_basis_changed(stored_closes: dict[str, float], fresh_closes: dict[str, float]) -> bool:
    """True when stored split-adjusted closes differ from fresh ones by a common factor.

    A new split rescales every earlier split-adjusted price, so the median stored/fresh
    ratio over shared dates moves away from 1. Isolated vendor corrections do not.
    """
    ratios = [
        stored_closes[day] / fresh_closes[day]
        for day in stored_closes.keys() & fresh_closes.keys()
        if stored_closes[day] > 0 and fresh_closes[day] > 0
    ]
    if len(ratios) < DETECTION_MIN_OVERLAP:
        return False
    return abs(statistics.median(ratios) - 1) > DETECTION_THRESHOLD
