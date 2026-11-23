"""Checks for storage.sync_symbol_prices: write only differences, keep and rebase older rows."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src import main, storage
from src.data_rules import DataRules
from src.storage import plan_price_sync

HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None


def weekdays(start: str, count: int) -> list[str]:
    """``count`` consecutive weekdays from ``start`` (callers pick ranges without NYSE holidays)."""
    day, out = date.fromisoformat(start), []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


DAYS = weekdays("2024-02-20", 28)  # 2024-02-20 .. 2024-03-28: no holiday inside


def series(days: list[str], price: float = 100.0, volume: int = 1000, adj: float | None = 0.9) -> dict:
    """{date: (open, high, low, close, volume, adj_close)} with a slowly rising close."""
    out = {}
    for index, day in enumerate(days):
        close = price + index
        out[day] = (close, close + 1, close - 1, close, volume, None if adj is None else round(close * adj, 6))
    return out


def scaled(values: tuple, price: float = 1.0, adj: float = 1.0) -> tuple:
    open_, high, low, close, volume, adj_close = values
    return (open_ * price, high * price, low * price, close * price, round(volume / price), None if adj_close is None else adj_close * adj)


class PlanPriceSyncTest(unittest.TestCase):
    def test_identical_response_writes_nothing(self) -> None:
        stored = series(DAYS)
        plan = plan_price_sync(stored, dict(stored))
        self.assertFalse(plan.changes)
        self.assertEqual((plan.inserted, plan.updated, plan.deletes, plan.indicator_from), (0, 0, [], None))

    def test_new_day_and_revised_last_bar(self) -> None:
        stored = series(DAYS[:-1])
        fresh = series(DAYS)
        fresh[DAYS[-2]] = (*fresh[DAYS[-2]][:3], fresh[DAYS[-2]][3] + 0.25, 5000, fresh[DAYS[-2]][5])  # provisional bar revised
        plan = plan_price_sync(stored, fresh)
        self.assertEqual((plan.inserted, plan.updated, plan.deletes), (1, 1, []))
        self.assertEqual([day for day, _ in plan.upserts], DAYS[-2:])
        self.assertEqual(plan.indicator_from, DAYS[-2])

    def test_volume_only_revision_keeps_indicators(self) -> None:
        stored = series(DAYS)
        fresh = dict(stored)
        fresh[DAYS[-1]] = (*stored[DAYS[-1]][:4], 99999, stored[DAYS[-1]][5])
        plan = plan_price_sync(stored, fresh)
        self.assertEqual((plan.updated, plan.indicator_from), (1, None))

    def test_dividend_changes_only_adj_close(self) -> None:
        stored = series(DAYS)
        fresh = {day: scaled(values, adj=0.995) if day < DAYS[-3] else values for day, values in stored.items()}
        plan = plan_price_sync(stored, fresh)
        self.assertEqual(plan.updated, len(DAYS) - 3)
        self.assertIsNone(plan.indicator_from, "indicators read only close")

    def test_vanished_and_newer_stored_dates_are_deleted(self) -> None:
        stored = series(DAYS)
        fresh = {day: values for day, values in stored.items() if day not in (DAYS[10], DAYS[-1])}
        plan = plan_price_sync(stored, fresh)
        self.assertEqual(plan.deletes, [DAYS[10], DAYS[-1]])
        self.assertEqual(plan.indicator_from, DAYS[10])

    def test_older_rows_are_kept_when_the_basis_is_unchanged(self) -> None:
        stored = series(DAYS)
        fresh = {day: values for day, values in stored.items() if day >= DAYS[3]}  # the 5000-row window moved on
        plan = plan_price_sync(stored, fresh)
        self.assertFalse(plan.changes)
        self.assertEqual((plan.older_kept, plan.older_dropped, plan.price_factor, plan.adj_factor), (3, 0, 1.0, 1.0))

    def test_split_rebases_older_rows(self) -> None:
        stored = series(DAYS)
        # 2:1 split on DAYS[-1]: every earlier split-adjusted price halves and volume doubles.
        fresh = {day: scaled(values, price=0.5, adj=0.5) if day < DAYS[-1] else values for day, values in stored.items() if day >= DAYS[3]}
        plan = plan_price_sync(stored, fresh)
        self.assertEqual((plan.price_factor, plan.adj_factor, plan.rebased, plan.older_kept), (0.5, 0.5, 3, 3))
        rebased = dict(plan.upserts)
        self.assertEqual(rebased[DAYS[0]], scaled(stored[DAYS[0]], price=0.5, adj=0.5))
        self.assertEqual(rebased[DAYS[0]][4], 2000)
        self.assertEqual(plan.indicator_from, DAYS[0])

    def test_small_dividend_still_rebases_older_adj_close(self) -> None:
        stored = series(DAYS)
        factor = 1 - 5e-5  # e.g. a $0.01 quarterly dividend on a $200 stock
        fresh = {day: scaled(values, adj=factor) if day < DAYS[-1] else values for day, values in stored.items() if day >= DAYS[3]}
        plan = plan_price_sync(stored, fresh)
        self.assertEqual(plan.price_factor, 1.0)
        self.assertAlmostEqual(plan.adj_factor, factor, places=12)
        self.assertEqual(plan.rebased, 3)
        self.assertIsNone(plan.indicator_from)

    def test_adj_noise_is_not_a_rebase(self) -> None:
        stored = series(DAYS)
        fresh = {day: scaled(values, adj=1 + 1e-7) for day, values in stored.items() if day >= DAYS[3]}
        plan = plan_price_sync(stored, fresh)
        self.assertEqual((plan.adj_factor, plan.rebased), (1.0, 0))

    def test_event_inside_the_anchors_drops_older_rows(self) -> None:
        stored = series(DAYS)
        # A split dated among the oldest overlapping days: half the anchors moved, half did not.
        fresh = {day: scaled(values, price=0.5) if day < DAYS[13] else values for day, values in stored.items() if day >= DAYS[3]}
        plan = plan_price_sync(stored, fresh)
        self.assertIsNone(plan.price_factor)
        self.assertEqual((plan.older_dropped, plan.older_kept), (3, 0))
        self.assertEqual(sorted(set(DAYS[:3]) & set(plan.deletes)), DAYS[:3])
        self.assertEqual(plan.indicator_from, DAYS[0])

    def test_one_edited_anchor_does_not_block_the_factor(self) -> None:
        stored = series(DAYS)
        fresh = {day: values for day, values in stored.items() if day >= DAYS[3]}
        fresh[DAYS[5]] = scaled(fresh[DAYS[5]], price=1.01)
        plan = plan_price_sync(stored, fresh)
        self.assertEqual((plan.price_factor, plan.older_kept, plan.older_dropped), (1.0, 3, 0))

    def test_too_little_overlap_drops_older_rows(self) -> None:
        stored = series(DAYS[:10])
        fresh = series(DAYS[6:])  # 4 overlapping days
        plan = plan_price_sync(stored, fresh)
        self.assertEqual(plan.older_dropped, 6)

    def test_rejected_older_rows_are_deleted(self) -> None:
        stored = series(DAYS)
        fresh = {day: values for day, values in stored.items() if day >= DAYS[3]}
        plan = plan_price_sync(stored, fresh, keep_bar=lambda day: day != DAYS[1])
        self.assertEqual((plan.deletes, plan.older_kept), ([DAYS[1]], 2))
        self.assertEqual(plan.indicator_from, DAYS[1])

    def test_rotation_basis_clears_older_adj_close(self) -> None:
        stored = series(DAYS)  # was a core symbol: adj_close stored
        fresh = series(DAYS[3:], price=103.0, adj=None)
        plan = plan_price_sync(stored, fresh)
        self.assertEqual(plan.adj_factor, None)
        rebased = dict(plan.upserts)
        self.assertIsNone(rebased[DAYS[0]][5])
        self.assertEqual(rebased[DAYS[0]][:5], stored[DAYS[0]][:5])


def _bars(days: list[str], price: float = 100.0, factor: float = 1.0, until: str | None = None) -> list[dict]:
    """Twelve Data style newest-first values; prices before ``until`` are multiplied by factor."""
    out = []
    for index, day in enumerate(days):
        close = (price + index) * (factor if until and day < until else 1.0)
        out.append({"datetime": day, "open": str(close), "high": str(close), "low": str(close), "close": str(close), "volume": "1000"})
    return list(reversed(out))


class FakeClient:
    def __init__(self, series_by_adjust: dict[str, list[dict]]):
        self.series = series_by_adjust

    def can_afford(self, credits: int) -> bool:
        return True

    def time_series(self, symbol: str, adjust: str) -> dict:
        return {"status": "ok", "values": self.series[adjust]}


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
class SyncSymbolPricesTest(unittest.TestCase):
    def _rows(self, data_dir: Path, table: str = "ohlcv") -> list[tuple]:
        import duckdb  # type: ignore

        con = duckdb.connect(str(data_dir / "db" / "market.db"), read_only=True)
        try:
            return con.execute(f"SELECT CAST(date AS VARCHAR), * EXCLUDE (symbol, date) FROM {table} WHERE symbol = 'AAA' ORDER BY date").fetchall()
        finally:
            con.close()

    def test_unchanged_rows_keep_their_write_time_and_indicators(self) -> None:
        from src.indicators import calculate_batches, find_stale_symbols

        rows = [{"date": day, "open": str(100 + i), "high": str(101 + i), "low": str(99 + i), "close": str(100 + i), "volume": "1000"} for i, day in enumerate(DAYS[:-1])]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage.sync_symbol_prices(data_dir, "AAA", rows)
            con = storage.connect_db(data_dir)
            try:
                calculate_batches(con, data_dir, ["AAA"], batch_size=10)
            finally:
                con.close()
            before = self._rows(data_dir)
            same = storage.sync_symbol_prices(data_dir, "AAA", rows)
            self.assertEqual(self._rows(data_dir), before, "nothing rewritten")
            nxt = [*rows, {"date": DAYS[-1], "open": "200", "high": "200", "low": "200", "close": "200", "volume": "1"}]
            added = storage.sync_symbol_prices(data_dir, "AAA", nxt)
            after = self._rows(data_dir)
            con = storage.connect_db(data_dir)
            try:
                stale = find_stale_symbols(con, ["AAA"])
                written = calculate_batches(con, data_dir, stale, batch_size=10, incremental=True)
                indicator_count = con.execute("SELECT count(*) FROM indicators WHERE symbol = 'AAA'").fetchone()[0]
            finally:
                con.close()
        self.assertEqual((same["inserted"], same["updated"], same["deleted"], same["indicator_from"]), (0, 0, 0, None))
        self.assertEqual((added["inserted"], added["updated"], added["indicator_from"]), (1, 0, DAYS[-1]))
        self.assertEqual(after[:-1], before, "older rows keep updated_at")
        self.assertEqual((stale, written[0]["rows"], indicator_count), (["AAA"], 1, len(DAYS)))

    def test_malformed_response_changes_nothing(self) -> None:
        good = [{"date": day, "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"} for day in DAYS[:5]]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage.sync_symbol_prices(data_dir, "AAA", good)
            before = self._rows(data_dir)
            with self.assertRaises(ValueError):
                storage.sync_symbol_prices(data_dir, "AAA", [*good[1:], {**good[0], "volume": "1.5"}])
            self.assertEqual(self._rows(data_dir), before)

    def test_core_collection_keeps_and_rebases_older_rows_and_split_events(self) -> None:
        import duckdb  # type: ignore

        rules = DataRules()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            first = FakeClient({"splits": _bars(DAYS[:-1]), "all": _bars(DAYS[:-1], factor=0.99, until=DAYS[-1])})
            main.collect_core_symbol(first, data_dir, "AAA", DAYS[-2], rules)
            storage.replace_split_events(data_dir, "AAA", [(DAYS[1], 3.0)], "test")  # a split older than the next response
            # Next session: the response window starts 3 days later, and a 2:1 split happened on the last day.
            window = DAYS[3:]
            second = FakeClient({
                "splits": _bars(DAYS, factor=0.5, until=DAYS[-1])[:-3],
                "all": _bars(DAYS, factor=0.5 * 0.99, until=DAYS[-1])[:-3],
                "none": _bars(DAYS)[:-3],
            })
            result = main.collect_core_symbol(second, data_dir, "AAA", DAYS[-1], rules)
            events = main.refresh_split_events(second, data_dir, "AAA", DAYS[-1])
            con = duckdb.connect(str(data_dir / "db" / "market.db"), read_only=True)
            try:
                prices = con.execute("SELECT CAST(date AS VARCHAR), close, volume, adj_close, splits FROM prices_daily WHERE symbol = 'AAA' ORDER BY date").fetchall()
            finally:
                con.close()
        self.assertEqual(len(prices), len(DAYS), "rows older than the response are kept")
        self.assertEqual(result["storage"]["older_kept"], 3)
        self.assertEqual(result["storage"]["price_factor"], 0.5)
        self.assertTrue(result["split_basis_changed"])
        self.assertEqual(prices[0][:3], (DAYS[0], 50.0, 2000), "older row on the new split basis")
        self.assertAlmostEqual(prices[0][3], 100 * 0.99 * 0.5)
        self.assertEqual(events, 1)
        splits = {day: value for day, _, _, _, value in prices if value}
        self.assertEqual(splits, {DAYS[1]: 3.0, DAYS[-1]: 2.0}, "the older event survives the refresh")
        self.assertEqual(window[0], DAYS[3])


if __name__ == "__main__":
    unittest.main()
