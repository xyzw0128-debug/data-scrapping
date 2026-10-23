"""Per-symbol rules applied to provider bars before they are stored, and the checks behind them.

- Bars on days the NYSE was closed (weekends, holidays, ``SPECIAL_CLOSURES``) are dropped.
  Twelve Data sends some, e.g. TTWO 2025-12-25 repeating the 2025-12-24 bar.
- ``config/history_start.txt``: bars before a symbol's first valid date are dropped (GM before
  the 2010-11-18 IPO are the old General Motors, all with volume 0).
- ``config/bar_patches.csv``: bars verified against a second source, inserted only while the
  provider response lacks that date and has the reference day. Prices are kept with the close
  of a reference day on the same split basis, so a later split rescales them.
- ``config/known_gaps.txt``: trading days no source can fill; validation does not warn about them.

``load_data_rules`` runs before any paid call; a malformed line raises DataRulesError.
"""

from __future__ import annotations

import csv
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from src.market_calendar import is_trading_day


HISTORY_START_FILE = "history_start.txt"
BAR_PATCHES_FILE = "bar_patches.csv"
KNOWN_GAPS_FILE = "known_gaps.txt"
PATCH_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "ref_date", "ref_close", "source", "note"]
# A reference close this far from the recorded one means a new split; smaller differences are
# vendor rounding and leave the recorded values untouched.
PATCH_RESCALE_TOLERANCE = 0.02
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DataRulesError(RuntimeError):
    """A data rules file is malformed (raised before any provider call)."""


@dataclass(frozen=True)
class BarPatch:
    symbol: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    ref_date: str
    ref_close: float
    source: str
    note: str = ""

    def bar(self, ref_close_now: float) -> dict[str, Any]:
        """The bar on the split basis whose reference-day close is ``ref_close_now``."""
        factor = ref_close_now / self.ref_close
        if abs(factor - 1) <= PATCH_RESCALE_TOLERANCE:
            return {"date": self.date, "open": self.open, "high": self.high, "low": self.low, "close": self.close, "volume": self.volume}
        return {
            "date": self.date,
            **{name: round(getattr(self, name) * factor, 6) for name in ("open", "high", "low", "close")},
            "volume": round(self.volume / factor),
        }


@dataclass(frozen=True)
class DataRules:
    history_start: dict[str, str] = field(default_factory=dict)
    patches: dict[str, tuple[BarPatch, ...]] = field(default_factory=dict)
    known_gaps: frozenset[tuple[str, str]] = frozenset()

    def keep_bar(self, symbol: str, day: str) -> bool:
        return is_trading_day(date.fromisoformat(day)) and day >= self.history_start.get(symbol, "")

    def filter_rows(self, symbol: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Rows worth storing, and how many were dropped per reason (only non-zero reasons)."""
        start = self.history_start.get(symbol, "")
        kept: list[dict[str, Any]] = []
        dropped: dict[str, int] = {}
        for row in rows:
            if not is_trading_day(date.fromisoformat(row["date"])):
                dropped["closed_day"] = dropped.get("closed_day", 0) + 1
            elif row["date"] < start:
                dropped["before_start"] = dropped.get("before_start", 0) + 1
            else:
                kept.append(row)
        return kept, dropped

    def apply_patches(
        self, symbol: str, rows: list[dict[str, Any]], adj_closes: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[dict[str, str]]]:
        """Insert this symbol's patches that the adjust=splits rows lack.

        ``rows`` are the date-ordered adjust=splits bars. ``adj_closes`` (core symbols: the
        adjust=all closes by date) gets the patched day's value: the provider's own when it has
        that date, else the patched close times the reference day's adj_close/close ratio. A
        patch whose adj_close cannot be derived is skipped, so no core row gets a NULL adj_close.
        Returns the rows, the adj_closes and one {date, status} per patch: ``applied``,
        ``provider_has_date``, ``outside_range`` (not strictly inside the response, so a stale
        response is never padded), ``ref_missing`` or ``adj_missing``.
        """
        patches = self.patches.get(symbol, ())
        if not patches or not rows:
            return rows, adj_closes, [{"date": patch.date, "status": "outside_range"} for patch in patches]
        closes = {row["date"]: float(row["close"]) for row in rows}
        first, last = rows[0]["date"], rows[-1]["date"]
        adj = dict(adj_closes) if adj_closes is not None else None
        added: list[dict[str, Any]] = []
        results: list[dict[str, str]] = []
        for patch in patches:
            if patch.date in closes:
                status = "provider_has_date"
            elif not first < patch.date < last:
                status = "outside_range"
            elif patch.ref_date not in closes or closes[patch.ref_date] <= 0:
                status = "ref_missing"
            else:
                bar = patch.bar(closes[patch.ref_date])
                status = "applied"
                if adj is not None and adj.get(patch.date) in (None, ""):
                    ref_adj = adj.get(patch.ref_date)
                    if ref_adj in (None, ""):
                        status = "adj_missing"
                    else:
                        adj[patch.date] = round(bar["close"] * float(ref_adj) / closes[patch.ref_date], 6)
                if status == "applied":
                    added.append(bar)
            results.append({"date": patch.date, "status": status})
        if added:
            rows = sorted([*rows, *added], key=lambda row: row["date"])
        return rows, adj, results

    def vanished_dates(self, symbol: str, stored_dates: Iterable[str], rows: list[dict[str, Any]]) -> list[str]:
        """Stored trading days inside the new rows' range that the new rows no longer have.

        Days that leave the 5000-row window, closed days and days before the symbol's start are
        not counted: dropping those is expected.
        """
        if not rows:
            return []
        first, last = rows[0]["date"], rows[-1]["date"]
        fresh = {row["date"] for row in rows}
        return sorted(day for day in stored_dates if first <= day <= last and day not in fresh and self.keep_bar(symbol, day))


def _fail(path: Path, line_no: int, message: str) -> DataRulesError:
    return DataRulesError(f"{path.name} line {line_no}: {message}")


def _symbol(value: str, path: Path, line_no: int) -> str:
    symbol = value.strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise _fail(path, line_no, f"invalid symbol {value!r}")
    return symbol


def _trading_date(value: str, path: Path, line_no: int, what: str = "date") -> str:
    text = value.strip()
    try:
        if not _DATE_RE.fullmatch(text):
            raise ValueError
        day = date.fromisoformat(text)
    except ValueError:
        raise _fail(path, line_no, f"invalid {what} {value!r} (YYYY-MM-DD)") from None
    if not is_trading_day(day):
        raise _fail(path, line_no, f"{what} {text} is not a trading day")
    return text


def _positive(value: str, column: str, path: Path, line_no: int) -> float:
    try:
        number = float(value)
    except ValueError:
        raise _fail(path, line_no, f"invalid {column} {value!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise _fail(path, line_no, f"{column} must be a positive number, got {value!r}")
    return number


def _content_lines(path: Path) -> list[tuple[int, str]]:
    if not path.exists():
        return []
    return [(no, line) for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if line.strip() and not line.lstrip().startswith("#")]


def _dated_entries(path: Path) -> list[tuple[int, str, str]]:
    """``SYMBOL YYYY-MM-DD reason`` lines as (line number, symbol, date); the reason is required."""
    entries = []
    for line_no, line in _content_lines(path):
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            raise _fail(path, line_no, "expected 'SYMBOL YYYY-MM-DD reason'")
        entries.append((line_no, _symbol(parts[0], path, line_no), _trading_date(parts[1], path, line_no)))
    return entries


def _load_history_start(path: Path) -> dict[str, str]:
    starts: dict[str, str] = {}
    for line_no, symbol, day in _dated_entries(path):
        if symbol in starts:
            raise _fail(path, line_no, f"{symbol} is listed twice")
        starts[symbol] = day
    return starts


def _load_known_gaps(path: Path) -> set[tuple[str, str]]:
    gaps: set[tuple[str, str]] = set()
    for line_no, symbol, day in _dated_entries(path):
        if (symbol, day) in gaps:
            raise _fail(path, line_no, f"{symbol} {day} is listed twice")
        gaps.add((symbol, day))
    return gaps


def _load_patches(path: Path) -> list[BarPatch]:
    lines = _content_lines(path)
    if not lines:
        return []
    header_no, header = lines[0]
    if [name.strip() for name in header.split(",")] != PATCH_COLUMNS:
        raise _fail(path, header_no, f"header must be {','.join(PATCH_COLUMNS)}")
    patches: list[BarPatch] = []
    seen: set[tuple[str, str]] = set()
    for (line_no, _), values in zip(lines[1:], csv.reader(line for _, line in lines[1:])):
        if len(values) != len(PATCH_COLUMNS):
            raise _fail(path, line_no, f"expected {len(PATCH_COLUMNS)} columns, got {len(values)}")
        item = dict(zip(PATCH_COLUMNS, (value.strip() for value in values)))
        symbol = _symbol(item["symbol"], path, line_no)
        day = _trading_date(item["date"], path, line_no)
        ref_date = _trading_date(item["ref_date"], path, line_no, "ref_date")
        if ref_date == day:
            raise _fail(path, line_no, "ref_date must differ from date")
        if (symbol, day) in seen:
            raise _fail(path, line_no, f"{symbol} {day} is listed twice")
        seen.add((symbol, day))
        prices = {name: _positive(item[name], name, path, line_no) for name in ("open", "high", "low", "close")}
        if not prices["low"] <= min(prices["open"], prices["close"]) or not max(prices["open"], prices["close"]) <= prices["high"]:
            raise _fail(path, line_no, "prices must satisfy low <= open, close <= high")
        try:
            volume = int(item["volume"])
        except ValueError:
            raise _fail(path, line_no, f"invalid volume {item['volume']!r} (a whole number)") from None
        if volume < 0:
            raise _fail(path, line_no, "volume must not be negative")
        if not item["source"]:
            raise _fail(path, line_no, "source is required")
        patches.append(BarPatch(
            symbol=symbol, date=day, volume=volume, ref_date=ref_date,
            ref_close=_positive(item["ref_close"], "ref_close", path, line_no),
            source=item["source"], note=item["note"], **prices,
        ))
    return patches


def load_data_rules(config_dir: Path) -> DataRules:
    """Read and check the three rule files (a missing file means no rules of that kind)."""
    history_start = _load_history_start(config_dir / HISTORY_START_FILE)
    known_gaps = _load_known_gaps(config_dir / KNOWN_GAPS_FILE)
    patches = _load_patches(config_dir / BAR_PATCHES_FILE)
    patch_path = config_dir / BAR_PATCHES_FILE
    by_symbol: dict[str, list[BarPatch]] = {}
    for patch in patches:
        if patch.date < history_start.get(patch.symbol, ""):
            raise DataRulesError(f"{patch_path.name}: {patch.symbol} {patch.date} is before its history start {history_start[patch.symbol]}")
        if (patch.symbol, patch.date) in known_gaps:
            raise DataRulesError(f"{patch_path.name}: {patch.symbol} {patch.date} is also in {KNOWN_GAPS_FILE}")
        by_symbol.setdefault(patch.symbol, []).append(patch)
    for symbol, day in sorted(known_gaps):
        if day < history_start.get(symbol, ""):
            raise DataRulesError(f"{KNOWN_GAPS_FILE}: {symbol} {day} is before its history start {history_start[symbol]}")
    return DataRules(
        history_start=history_start,
        patches={symbol: tuple(sorted(items, key=lambda p: p.date)) for symbol, items in by_symbol.items()},
        known_gaps=frozenset(known_gaps),
    )
