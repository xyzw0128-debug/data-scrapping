"""Finnhub news backoff for symbols without new articles, per-symbol DB writes, and empty answers."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from src import finnhub_news, storage
from src.universe import Universe

HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None
ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("progress", ROOT / "scripts" / "progress.py")
progress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(progress)

D = date.fromisoformat


def _article(ident: int, day: str) -> dict:
    stamp = int(datetime.fromisoformat(day + "T15:00:00+00:00").timestamp())
    return {"id": ident, "datetime": stamp, "headline": f"h{ident}", "source": "s", "summary": "", "url": f"u{ident}"}


class FakeFinnhub:
    """Answers company-news URLs from a fixed article list, like Finnhub's inclusive from/to dates."""

    def __init__(self, articles: dict[str, list[dict]]) -> None:
        self.articles = articles
        self.requests: list[tuple[str, str, str]] = []

    def __call__(self, url: str, limiter=None) -> list[dict]:
        query = {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
        symbol, start, end = query["symbol"], query["from"], query["to"]
        self.requests.append((symbol, start, end))
        return [
            item for item in self.articles.get(symbol, [])
            if start <= datetime.fromtimestamp(item["datetime"], UTC).date().isoformat() <= end
        ]

    def symbols_on(self, end: str) -> list[str]:
        return [symbol for symbol, _, to in self.requests if to == end]


class BackoffRuleTest(unittest.TestCase):
    def test_wait_grows_with_the_zero_streak_and_stops_at_seven_days(self) -> None:
        self.assertEqual([finnhub_news.backoff_wait(n) for n in range(7)], [1, 1, 2, 4, 7, 7, 7])
        self.assertEqual(finnhub_news.backoff_wait(3, (1, 3)), 3)
        self.assertEqual(finnhub_news.parse_backoff_days("1, 2,4,7"), (1, 2, 4, 7))
        for bad in ("", "0,2", "a", "-1"):
            with self.assertRaises(ValueError):
                finnhub_news.parse_backoff_days(bad)

    def test_record_result_counts_new_articles_only(self) -> None:
        day = D("2026-11-03")
        entry: dict = {}
        done = {"complete": True, "new_rows": 0, "rows": 3}  # rows repeated from the overlap day
        finnhub_news.record_news_result(entry, done, day)
        self.assertEqual(entry, {"zero_streak": 1, "next_check_date": None})
        finnhub_news.record_news_result(entry, done, day)
        self.assertEqual(entry, {"zero_streak": 2, "next_check_date": "2026-11-05"})
        finnhub_news.record_news_result(entry, done, day)
        self.assertEqual(entry["next_check_date"], "2026-11-07")
        finnhub_news.record_news_result(entry, done, day)
        finnhub_news.record_news_result(entry, done, day)
        self.assertEqual(entry, {"zero_streak": 5, "next_check_date": "2026-11-10"})
        finnhub_news.record_news_result(entry, {"complete": True, "new_rows": 1, "rows": 1}, day)
        self.assertEqual(entry, {"zero_streak": 0, "next_check_date": None})

    def test_damaged_streak_restarts_from_zero(self) -> None:
        entry = {"zero_streak": "abc"}
        finnhub_news.record_news_result(entry, {"complete": True, "new_rows": 0, "rows": 0}, D("2026-11-03"))
        self.assertEqual(entry, {"zero_streak": 1, "next_check_date": None})

    def test_unfinished_window_stays_on_every_run(self) -> None:
        entry = {"zero_streak": 3, "next_check_date": "2026-11-01"}
        finnhub_news.record_news_result(entry, {"complete": False, "new_rows": 0, "rows": 250}, D("2026-11-03"))
        self.assertEqual(entry, {"zero_streak": 0, "next_check_date": None})

    def test_due_check_is_compatible_with_old_entries_and_guards_far_dates(self) -> None:
        day = D("2026-11-10")
        old_entry = {"status": "done", "rows": 0, "last_to": "2026-11-04", "last_attempt_at": "2026-11-04T03:00:00Z"}
        self.assertTrue(finnhub_news.is_news_due(old_entry, day))  # written before backoff: every run
        self.assertTrue(finnhub_news.is_news_due(None, day))
        self.assertTrue(finnhub_news.is_news_due({"next_check_date": None}, day))
        self.assertTrue(finnhub_news.is_news_due({"next_check_date": "bad"}, day))
        self.assertTrue(finnhub_news.is_news_due({"next_check_date": "2026-11-10"}, day))
        self.assertTrue(finnhub_news.is_news_due({"next_check_date": "2026-11-01"}, day))
        self.assertFalse(finnhub_news.is_news_due({"next_check_date": "2026-11-11"}, day))
        self.assertFalse(finnhub_news.is_news_due({"next_check_date": "2026-11-17"}, day))
        # Further than the longest wait: clock skew or a shortened schedule, so request it now.
        self.assertTrue(finnhub_news.is_news_due({"next_check_date": "2026-11-18"}, day))
        self.assertTrue(finnhub_news.is_news_due({"next_check_date": "2026-11-13"}, day, (1, 2)))

    def test_new_york_date_decides_the_run_day(self) -> None:
        # 00:30 UTC is still the previous evening in New York, where the news stage runs.
        self.assertEqual(finnhub_news.news_check_date(datetime(2026, 11, 25, 0, 30, tzinfo=UTC)), D("2026-11-24"))
        self.assertEqual(finnhub_news.news_check_date(datetime(2026, 11, 25, 6, 0, tzinfo=UTC)), D("2026-11-25"))


class SelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.universe = Universe(etfs=["SPY"], sp500=["AAPL"], extra=[], nasdaq=["AAPL", "AAA", "BBB", "CCC", "DEAD"],
                                 former_core={"OLD": {}}, delisted={"DEAD": {}})
        self.state = {
            "SPY": {"next_check_date": "2026-11-12"},  # core: ignored
            "AAPL": {"next_check_date": "2026-11-12"},  # core: ignored
            "AAA": {"next_check_date": "2026-11-12"},
            "BBB": {"next_check_date": "2026-11-10"},
            "OLD": {"next_check_date": "2026-11-11"},
        }

    def test_core_symbols_are_always_due_and_waiting_rotation_symbols_are_left_out(self) -> None:
        symbols, waiting = finnhub_news.universe_news_selection(self.universe, self.state, {}, None, D("2026-11-10"))
        self.assertEqual(symbols[:2], ["SPY", "AAPL"])
        self.assertEqual(sorted(symbols[2:]), ["BBB", "CCC"])
        self.assertEqual(sorted(waiting), ["AAA", "OLD"])

    def test_without_a_check_day_or_with_daily_waits_nothing_is_left_out(self) -> None:
        everything = ["AAA", "AAPL", "BBB", "CCC", "OLD", "SPY"]
        self.assertEqual(sorted(finnhub_news.universe_news_symbols(self.universe, self.state, {}, None)), everything)
        symbols, waiting = finnhub_news.universe_news_selection(self.universe, self.state, {}, None, D("2026-11-10"), (1,))
        self.assertEqual((sorted(symbols), waiting), (everything, []))

    def test_limit_applies_to_due_symbols(self) -> None:
        symbols, waiting = finnhub_news.universe_news_selection(self.universe, self.state, {"CCC": 9.0}, 3, D("2026-11-10"))
        self.assertEqual(symbols, ["SPY", "AAPL", "CCC"])
        self.assertEqual(len(waiting), 2)


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
class CollectWriteTest(unittest.TestCase):
    def test_pages_of_one_symbol_are_written_with_one_connection(self) -> None:
        busy = [_article(i, "2026-10-17") for i in range(1, 151)] + [_article(i, "2026-10-14") for i in range(151, 251)]
        older = [_article(i, "2026-10-14") for i in range(151, 201)] + [_article(i, "2026-09-20") for i in range(301, 311)]
        pages = {"2026-10-20": busy, "2026-10-14": older}

        def fake_fetch(url, limiter):
            return pages[parse_qs(urlparse(url).query)["to"][0]]

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage.ensure_db_schema(data_dir)
            entry: dict = {}
            with patch.object(storage, "_connect_db", wraps=storage.connect_db) as connect:
                result = finnhub_news.collect_symbol_news(data_dir, "MSFT", entry, ("2026-09-01", "2026-10-20"), "k", 10, fetch=fake_fetch)
            self.assertEqual(connect.call_count, 1)
            self.assertEqual((result["pages"], result["rows"], result["new_rows"], result["complete"]), (2, 310, 260, True))
            con = storage.connect_db(data_dir, read_only=True)
            try:
                self.assertEqual(con.execute("SELECT count(*) FROM news WHERE symbol = 'MSFT'").fetchone()[0], 260)
            finally:
                con.close()
            # The same window again: every id is already stored.
            again = finnhub_news.collect_symbol_news(data_dir, "MSFT", {}, ("2026-09-01", "2026-10-20"), "k", 10, fetch=fake_fetch)
            self.assertEqual((again["rows"], again["new_rows"]), (310, 0))

    def test_failed_page_writes_nothing_and_keeps_the_entry(self) -> None:
        calls = []

        def fake_fetch(url, limiter):
            calls.append(url)
            if len(calls) == 2:
                raise OSError("connection reset")
            return [_article(i, "2026-10-17") for i in range(1, 251)]

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage.ensure_db_schema(data_dir)
            entry = {"last_to": "2026-10-10"}
            with self.assertRaises(OSError):
                finnhub_news.collect_symbol_news(data_dir, "MSFT", entry, ("2026-10-09", "2026-10-20"), "k", 10, fetch=fake_fetch)
            self.assertEqual(entry, {"last_to": "2026-10-10"})
            con = storage.connect_db(data_dir, read_only=True)
            try:
                self.assertEqual(con.execute("SELECT count(*) FROM news").fetchone()[0], 0)
            finally:
                con.close()

    def test_empty_answer_keeps_the_previous_raw_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage.ensure_db_schema(data_dir)
            raw = data_dir / "raw" / "finnhub_news" / "AAA.json.gz"
            with patch.object(storage, "_connect_db", wraps=storage.connect_db) as connect:
                result = finnhub_news.collect_symbol_news(data_dir, "AAA", {}, ("2026-10-19", "2026-10-20"), "k", 10, fetch=lambda url, limiter: [])
            self.assertEqual((result["rows"], result["new_rows"], result["complete"]), (0, 0, True))
            self.assertEqual(connect.call_count, 0)  # nothing to write: no DB connection
            self.assertFalse(raw.exists())
            finnhub_news.collect_symbol_news(data_dir, "AAA", {}, ("2026-10-19", "2026-10-20"), "k", 10, fetch=lambda url, limiter: [_article(1, "2026-10-20")])
            saved = raw.read_bytes()
            finnhub_news.collect_symbol_news(data_dir, "AAA", {}, ("2026-10-20", "2026-10-21"), "k", 10, fetch=lambda url, limiter: [])
            self.assertEqual(raw.read_bytes(), saved)


def _write_config(root: Path) -> tuple[Path, Path]:
    config_dir, data_dir = root / "config", root / "data"
    config_dir.mkdir()
    (config_dir / "core_etfs.txt").write_text("SPY\n", encoding="utf-8")
    (config_dir / "symbols.txt").write_text("AAA\nBBB\nCCC\n", encoding="utf-8")
    return config_dir, data_dir


def _args(config_dir: Path, data_dir: Path, day: str, **overrides) -> SimpleNamespace:
    values = dict(
        symbols=None, symbol=None, config_dir=config_dir, data_dir=data_dir, state=data_dir / "state" / "finnhub_news.json",
        api_key_env="FINNHUB_TEST_KEY", dry_run=False, limit=None, from_date=None, to_date=day, days_back=7, max_days_back=365,
        max_pages=10, per_minute_limit=0, backoff_days=finnhub_news.DEFAULT_BACKOFF_DAYS, check_date=D(day),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
@patch.dict(os.environ, {"FINNHUB_TEST_KEY": "k"})
class RunBackoffTest(unittest.TestCase):
    def test_skipped_days_are_covered_by_the_next_request(self) -> None:
        # AAA has no news until an article on 11-06, a day it is not requested. BBB has news every
        # day. SPY (core) never has news but is requested every run. CCC has one article on 11-02
        # that comes back on 11-03 from the overlap day and must not count as new.
        days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]
        fake = FakeFinnhub({
            "AAA": [_article(1, "2026-11-06")],
            "BBB": [_article(100 + i, day) for i, day in enumerate(days)],
            "CCC": [_article(200, "2026-11-02")],
        })
        with tempfile.TemporaryDirectory() as tmp:
            config_dir, data_dir = _write_config(Path(tmp))
            state_path = data_dir / "state" / "finnhub_news.json"
            summaries = {}
            with patch.object(finnhub_news, "fetch_json", fake):
                for day in days:
                    summaries[day] = finnhub_news.run(_args(config_dir, data_dir, day))
            state = json.loads(state_path.read_text(encoding="utf-8"))["symbols"]
            con = storage.connect_db(data_dir, read_only=True)
            try:
                stored = con.execute("SELECT symbol, id FROM news ORDER BY symbol, id").fetchall()
            finally:
                con.close()

        requested = {day: sorted(fake.symbols_on(day)) for day in days}
        self.assertEqual(requested["2026-11-02"], ["AAA", "BBB", "CCC", "SPY"])
        self.assertEqual(requested["2026-11-03"], ["AAA", "BBB", "CCC", "SPY"])  # one empty run still waits 1 day
        self.assertEqual(requested["2026-11-04"], ["BBB", "CCC", "SPY"])  # AAA: 2 empty runs -> next 11-05
        self.assertEqual(requested["2026-11-05"], ["AAA", "BBB", "SPY"])  # CCC: 2 empty runs -> next 11-06
        self.assertEqual(requested["2026-11-06"], ["BBB", "CCC", "SPY"])  # AAA: 3 -> next 11-09; its article is today
        self.assertEqual(requested["2026-11-09"], ["AAA", "BBB", "SPY"])  # CCC: 3 -> next 11-10
        self.assertEqual(requested["2026-11-10"], ["AAA", "BBB", "CCC", "SPY"])  # AAA is back to every run

        # The request after the skipped days starts a day before last_to (11-05) and finds 11-06.
        self.assertIn(("AAA", "2026-11-04", "2026-11-09"), fake.requests)
        self.assertIn(("AAA", "1"), stored)
        aaa_09 = next(s for s in summaries["2026-11-09"]["symbols"] if s["symbol"] == "AAA")
        self.assertEqual((aaa_09["rows"], aaa_09["new_rows"]), (1, 1))
        self.assertEqual(state["AAA"]["zero_streak"], 1)  # 11-10 was empty again
        self.assertIsNone(state["AAA"]["next_check_date"])
        self.assertEqual(state["AAA"]["last_to"], "2026-11-10")

        # CCC's 11-02 article came back on 11-03 and 11-04 (overlap day) without counting as news.
        for day in ("2026-11-03", "2026-11-04"):
            ccc = next(s for s in summaries[day]["symbols"] if s["symbol"] == "CCC")
            self.assertEqual((ccc["rows"], ccc["new_rows"]), (1, 0))
        self.assertEqual(state["CCC"]["zero_streak"], 4)
        self.assertEqual(state["CCC"]["next_check_date"], "2026-11-17")

        self.assertEqual(state["SPY"]["zero_streak"], 7)  # core: counted, never skipped
        self.assertEqual(state["BBB"]["zero_streak"], 0)
        self.assertEqual((summaries["2026-11-04"]["planned"], summaries["2026-11-04"]["waiting_backoff"]), (3, 1))
        self.assertEqual(summaries["2026-11-02"]["new_rows"], 2)  # BBB + CCC

    def test_long_gap_is_capped_by_max_days_back(self) -> None:
        entry = {"last_to": "2025-01-01", "zero_streak": 9, "next_check_date": "2026-11-01"}
        self.assertEqual(finnhub_news.resolve_symbol_range(entry, D("2026-11-10"), 7, 365), ("2025-11-10", "2026-11-10"))
        entry = {"last_to": "2026-11-03", "zero_streak": 4, "next_check_date": "2026-11-10"}
        self.assertEqual(finnhub_news.resolve_symbol_range(entry, D("2026-11-10"), 7, 365), ("2026-11-02", "2026-11-10"))

    def test_failure_keeps_the_symbol_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir, data_dir = _write_config(Path(tmp))
            state_path = data_dir / "state" / "finnhub_news.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"schema_version": 2, "symbols": {
                "AAA": {"last_to": "2026-11-03", "zero_streak": 2, "next_check_date": "2026-11-05", "status": "done"},
            }}), encoding="utf-8")

            def failing(url, limiter=None):
                if "symbol=AAA" in url:
                    raise OSError("HTTP Error 502")
                return []

            with patch.object(finnhub_news, "fetch_json", failing):
                summary = finnhub_news.run(_args(config_dir, data_dir, "2026-11-05"))
            entry = json.loads(state_path.read_text(encoding="utf-8"))["symbols"]["AAA"]
        self.assertEqual(summary["failed"], 1)
        self.assertEqual((entry["status"], entry["zero_streak"], entry["next_check_date"], entry["last_to"]),
                         ("failed", 2, "2026-11-05", "2026-11-03"))
        self.assertTrue(finnhub_news.is_news_due(entry, D("2026-11-06")))

    def test_dry_run_writes_nothing_and_reports_waiting_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir, data_dir = _write_config(Path(tmp))
            state_path = data_dir / "state" / "finnhub_news.json"
            state_path.parent.mkdir(parents=True)
            original = json.dumps({"schema_version": 2, "symbols": {
                "AAA": {"last_to": "2026-11-03", "zero_streak": 3, "next_check_date": "2026-11-07"},
                "BBB": {"last_to": "2026-11-03", "rows": 0},  # written before backoff
            }})
            state_path.write_text(original, encoding="utf-8")
            calls = []
            with patch.object(finnhub_news, "fetch_json", lambda *a: calls.append(a)), \
                    patch.object(finnhub_news, "load_liquidity", side_effect=AssertionError("no --limit: no liquidity scan")):
                summary = finnhub_news.run(_args(config_dir, data_dir, "2026-11-05", dry_run=True))
            self.assertEqual(state_path.read_text(encoding="utf-8"), original)
            self.assertFalse(storage.get_db_path(data_dir).exists())
        self.assertEqual(calls, [])
        self.assertEqual(sorted(s["symbol"] for s in summary["symbols"]), ["BBB", "CCC", "SPY"])
        self.assertEqual((summary["planned"], summary["waiting_backoff"], summary["check_date"]), (3, 1, "2026-11-05"))

    def test_liquidity_is_read_only_with_a_limit_and_named_symbols_skip_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir, data_dir = _write_config(Path(tmp))
            state_path = data_dir / "state" / "finnhub_news.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"schema_version": 2, "symbols": {
                "AAA": {"zero_streak": 4, "next_check_date": "2026-11-09"},
            }}), encoding="utf-8")
            with patch.object(finnhub_news, "load_liquidity", return_value={"CCC": 5.0}) as liquidity:
                limited = finnhub_news.run(_args(config_dir, data_dir, "2026-11-05", dry_run=True, limit=2))
                named = finnhub_news.run(_args(config_dir, data_dir, "2026-11-05", dry_run=True, symbol=["AAA"]))
        self.assertEqual(liquidity.call_count, 1)
        self.assertEqual([s["symbol"] for s in limited["symbols"]], ["SPY", "CCC"])
        self.assertEqual([s["symbol"] for s in named["symbols"]], ["AAA"])


@patch.dict(os.environ, {"FINNHUB_LIMIT": "", "FINNHUB_BACKOFF_DAYS": ""})
class ProgressPlannedTest(unittest.TestCase):
    def test_planned_leaves_out_waiting_symbols_but_keeps_this_runs_attempts(self) -> None:
        universe = Universe(etfs=["SPY"], sp500=[], extra=[], nasdaq=["AAA", "BBB", "CCC"])
        since = datetime(2026, 11, 25, 1, 0, tzinfo=UTC)
        today = finnhub_news.news_check_date()
        later = (today + timedelta(days=3)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "state").mkdir()
            (data_dir / "state" / "finnhub_news.json").write_text(json.dumps({"schema_version": 2, "symbols": {
                "AAA": {"next_check_date": later, "last_attempt_at": "2026-11-20T01:00:00Z"},  # waiting
                "BBB": {"next_check_date": later, "last_attempt_at": "2026-11-25T01:05:00Z"},  # tried this run
            }}), encoding="utf-8")
            with patch.object(progress, "DATA_DIR", data_dir), patch.object(progress, "load_universe", lambda _: universe):
                self.assertEqual(progress.news_planned(since), 3)  # SPY, BBB, CCC
                with patch.dict(os.environ, {"FINNHUB_BACKOFF_DAYS": "1"}):
                    self.assertEqual(progress.news_planned(since), 4)
                with patch.dict(os.environ, {"FINNHUB_LIMIT": "2"}):
                    self.assertEqual(progress.news_planned(since), 2)


if __name__ == "__main__":
    unittest.main()
