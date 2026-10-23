"""Collection order, session calendar, split derivation, locks, universe, snapshot, news paging, FRED rights and outages."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src import finnhub_news, fred
from src.corporate_actions import snap_ratio, split_basis_changed, split_events
from src.lock import PIPELINE_LOCK_ENV, LockHeldError, pipeline_lock, pipeline_running
from src.market_calendar import SPECIAL_CLOSURES, expected_session, is_trading_day, nyse_holidays, previous_trading_day, trading_days
from src.scheduling import choose_rotation_symbols, record_failure, record_success
from src.snapshot import create_snapshot, evaluate_completion
from src.universe import Universe, load_universe, parse_sp500_history, recent_removals, refresh_reason, refresh_universe, universe_path

HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None
NY = ZoneInfo("America/New_York")


class SchedulingTest(unittest.TestCase):
    def test_never_collected_first_then_oldest_success(self) -> None:
        state = {"symbols": {
            "AAA": {"twelve_data": {"last_success_date": "2026-06-25"}},
            "BBB": {"twelve_data": {"last_success_date": "2026-05-20"}},
            "CCC": {"twelve_data": {"status": "dry_run"}},
            "DDD": {"twelve_data": {"last_success_date": "2026-10-20"}},
            "EEE": {"twelve_data": {"status": "not_found", "next_retry_date": "2026-11-19"}},
        }}
        order = choose_rotation_symbols(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "SPY"], state, "twelve_data", None, "2026-10-20", exclude={"SPY"})
        self.assertEqual(order, ["CCC", "FFF", "BBB", "AAA"])
        self.assertEqual(choose_rotation_symbols(["DDD", "EEE"], state, "twelve_data", None, "2026-10-20", force=True), ["EEE", "DDD"])
        self.assertNotIn("FFF", state["symbols"], "choosing must not add state entries")

    def test_backoff_doubles_and_not_found_waits_30_days(self) -> None:
        entry: dict = {}
        waits = []
        for _ in range(7):
            record_failure(entry, "2026-10-20", "boom")
            waits.append((date.fromisoformat(entry["next_retry_date"]) - date(2026, 10, 20)).days)
        self.assertEqual(waits, [1, 2, 4, 8, 16, 30, 30])
        record_success(entry, "2026-10-21")
        self.assertEqual((entry["fail_count"], entry["next_retry_date"]), (0, None))
        record_failure(entry, "2026-10-20", "**symbol** not found", not_found=True)
        self.assertEqual((entry["status"], entry["next_retry_date"]), ("not_found", "2026-11-19"))


class MarketCalendarTest(unittest.TestCase):
    def test_holidays_and_observed_rules(self) -> None:
        h2026 = nyse_holidays(2026)
        for day in ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"]:
            self.assertIn(date.fromisoformat(day), h2026, day)
        self.assertEqual(len(h2026), 10)
        self.assertTrue(is_trading_day(date(2021, 12, 31)), "Saturday New Year's Day is not observed on Friday")

    def test_expected_session(self) -> None:
        def at(y, m, d, hh, mm):
            return datetime(y, m, d, hh, mm, tzinfo=NY)
        self.assertEqual(expected_session(at(2026, 10, 20, 15, 59)), date(2026, 10, 19), "before close: previous session")
        self.assertEqual(expected_session(at(2026, 10, 20, 16, 10)), date(2026, 10, 20))
        self.assertEqual(expected_session(at(2026, 10, 24, 12, 0)), date(2026, 10, 23), "Saturday: Friday")
        self.assertEqual(expected_session(at(2026, 11, 26, 18, 0)), date(2026, 11, 25), "Thanksgiving: Wednesday")
        self.assertEqual(expected_session(at(2026, 10, 20, 21, 30).astimezone(ZoneInfo("Asia/Seoul"))), date(2026, 10, 20))

    def test_special_closures_are_not_sessions(self) -> None:
        expected = ["2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14", "2004-06-11", "2007-01-02", "2012-10-29", "2012-10-30", "2018-12-05", "2025-01-09"]
        self.assertEqual(sorted(d.isoformat() for d in SPECIAL_CLOSURES), expected)
        for day in SPECIAL_CLOSURES:
            self.assertLess(day.weekday(), 5, f"{day} is a weekday")
            self.assertFalse(is_trading_day(day), day)
            evening = datetime(day.year, day.month, day.day, 18, 0, tzinfo=NY)
            before = previous_trading_day(day)
            self.assertNotIn(before, SPECIAL_CLOSURES)
            self.assertEqual(expected_session(evening), before, f"{day}: the session before the closure")
            following = day + timedelta(days=1)
            while not is_trading_day(following):
                following += timedelta(days=1)
            self.assertEqual(previous_trading_day(following), before, f"{day} is skipped going back")
        # September 2001: the market was shut Tuesday-Friday and reopened Monday 2001-09-17.
        self.assertEqual(expected_session(datetime(2001, 9, 14, 18, 0, tzinfo=NY)), date(2001, 9, 10))
        self.assertEqual(previous_trading_day(date(2001, 9, 17)), date(2001, 9, 10))
        self.assertEqual(len(trading_days(date(2025, 1, 6), date(2025, 1, 10))), 4, "2025-01-09 is not counted")


class SplitDerivationTest(unittest.TestCase):
    def test_split_and_reverse_split(self) -> None:
        adjusted = {"d1": 120.998, "d2": 120.888, "d3": 121.79, "d4": 2.0, "d5": 2.1}
        raw = {"d1": 1209.98, "d2": 1208.88, "d3": 121.79, "d4": 36.0, "d5": 37.8}
        self.assertEqual(split_events(raw, adjusted), [("d3", 10.0), ("d4", 0.055556)])
        self.assertEqual(snap_ratio(1.4999), 1.5)

    def test_split_basis_change_detection(self) -> None:
        stored = {f"2024-01-{d:02d}": 100.0 + d for d in range(1, 11)}
        self.assertFalse(split_basis_changed(stored, {k: v * 1.001 for k, v in stored.items()}))
        self.assertTrue(split_basis_changed(stored, {k: v / 4 for k, v in stored.items()}))
        self.assertFalse(split_basis_changed({"x": 1.0}, {"x": 4.0}), "too little overlap")


class PipelineLockTest(unittest.TestCase):
    def test_second_holder_fails_and_parent_lock_is_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.dict(os.environ, {PIPELINE_LOCK_ENV: ""}):
                self.assertFalse(pipeline_running(data_dir))
                with pipeline_lock(data_dir):
                    self.assertTrue(pipeline_running(data_dir))
                    with self.assertRaises(LockHeldError):
                        pipeline_lock(data_dir).acquire()
                    with patch.dict(os.environ, {PIPELINE_LOCK_ENV: "1"}):
                        with pipeline_lock(data_dir):
                            pass
                self.assertFalse(pipeline_running(data_dir))


SP500_CSV = "date,tickers\n2024-01-02,\"AAA,BBB,CCC\"\n2025-03-03,\"AAA,BBB,DDD\"\n2026-08-18,\"AAA,BBB,EEE\"\n"


def _catalog(endpoint: str, params: dict) -> dict:
    if endpoint == "etfs":
        return {"data": [{"symbol": "SPY"}] + [{"symbol": f"E{i}"} for i in range(1000)]}
    if params.get("exchange") == "NASDAQ":
        return {"data": [{"symbol": s} for s in ["AAA", "NEWW", "!OTC/X"]]}
    return {"data": [{"symbol": s} for s in ["AAA", "BBB", "EEE", "DDD", "NEWW"]] + [{"symbol": f"S{i}"} for i in range(5000)]}


class UniverseTest(unittest.TestCase):
    def test_history_parsing_and_bootstrap_removals(self) -> None:
        history = parse_sp500_history(SP500_CSV)
        self.assertEqual(history[-1], ("2026-08-18", ["AAA", "BBB", "EEE"]))
        self.assertEqual(recent_removals(history, 365), {"DDD": "2026-08-18"}, "CCC left more than a year before")

    def test_refresh_marks_former_core_and_delisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, data = root / "config", root / "data"
            config.mkdir()
            (config / "core_etfs.txt").write_text("SPY\n", encoding="utf-8")
            (config / "symbols.txt").write_text("AAA\nGONE\n!OTC/X\n", encoding="utf-8")
            summary = refresh_universe(data, config, _catalog, download=lambda url: SP500_CSV)
            universe = load_universe(data, config)
            self.assertEqual(universe.core_symbols(), ["SPY", "AAA", "BBB", "EEE"])
            self.assertEqual(sorted(universe.former_core), ["DDD"])
            self.assertIn("GONE", universe.delisted)
            self.assertEqual(universe.rotation_symbols(), ["NEWW", "DDD"])
            self.assertEqual((config / "symbols.txt").read_text(encoding="utf-8"), "AAA\nNEWW\n")
            self.assertEqual(summary["delisted"]["newly_marked"], 1)
            self.assertEqual(universe.group_of("BBB"), "core_sp500")


class UniverseDryRunTest(unittest.TestCase):
    def test_dry_run_makes_no_network_calls(self) -> None:
        import contextlib
        import io

        from src import universe as universe_module

        def no_network(*args, **kwargs):
            raise AssertionError("dry-run must not touch the network")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "core_etfs.txt").write_text("SPY\n", encoding="utf-8")
            argv = ["universe", "--data-dir", str(root / "data"), "--config-dir", str(root / "config"), "--dry-run", "--force"]
            out = io.StringIO()
            with patch("sys.argv", argv), patch.object(universe_module, "fetch_text", no_network), \
                    patch("src.twelve_data.default_http_get", no_network), contextlib.redirect_stdout(out):
                self.assertEqual(universe_module.main(), 0)
            self.assertFalse((root / "data" / "state" / "universe.json").exists())
        result = json.loads(out.getvalue())
        self.assertEqual((result["status"], result["refresh_due"], result["sp500_count"]), ("dry_run", True, 0))


def _run_universe_main(root: Path, *flags: str) -> tuple[int, dict, list[str]]:
    """Run ``python -m src.universe`` offline; returns (exit code, JSON output, network calls made)."""
    from src import universe as universe_module

    calls: list[str] = []

    def fake_catalog(endpoint: str, params: dict) -> dict:
        calls.append(endpoint)
        return _catalog(endpoint, params)

    def fake_download(url: str) -> str:
        calls.append("sp500_csv")
        return SP500_CSV

    real_refresh = universe_module.refresh_universe
    argv = ["universe", "--data-dir", str(root / "data"), "--config-dir", str(root / "config"), "--state", str(root / "state.json"), *flags]
    out = io.StringIO()
    with patch("sys.argv", argv), \
            patch.object(universe_module, "refresh_universe", lambda *a, **kw: real_refresh(*a, download=fake_download, **kw)), \
            patch("src.twelve_data.TwelveDataClient", lambda *a: SimpleNamespace(request=fake_catalog)), \
            contextlib.redirect_stdout(out):
        code = universe_module.main()
    return code, json.loads(out.getvalue()), calls


class UniverseRefreshReasonTest(unittest.TestCase):
    def test_reason_values(self) -> None:
        now = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)

        def reason(nasdaq, refreshed_at="2026-10-20T12:17:32Z", **kwargs):
            universe = Universe(etfs=["SPY"], sp500=["AAA"], extra=[], nasdaq=nasdaq, refreshed_at=refreshed_at)
            return refresh_reason(universe, 7, now=now, **kwargs)

        self.assertIsNone(reason(["NEWW"]))
        self.assertEqual(reason([]), "nasdaq_list_missing")
        self.assertIsNone(reason([], need_nasdaq=False), "--skip-catalog does not rewrite the list")
        self.assertEqual(reason([], refreshed_at=None), "never_refreshed")
        self.assertEqual(reason(["NEWW"], refreshed_at="2026-10-14T03:00:00Z"), "max_age")

    def test_missing_nasdaq_list_is_refreshed_despite_recent_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, data = root / "config", root / "data"
            config.mkdir()
            (config / "core_etfs.txt").write_text("SPY\n", encoding="utf-8")
            refresh_universe(data, config, _catalog, download=lambda url: SP500_CSV)
            (config / "symbols.txt").unlink()  # a branch switch deleted the gitignored list
            saved = universe_path(data).read_text(encoding="utf-8")

            code, result, calls = _run_universe_main(root, "--dry-run")
            self.assertEqual((code, result["refresh_due"], result["reason"], result["nasdaq_count"]), (0, True, "nasdaq_list_missing", 0))
            self.assertEqual(calls, [], "dry-run must not touch the network")
            self.assertFalse((config / "symbols.txt").exists())
            self.assertEqual(universe_path(data).read_text(encoding="utf-8"), saved)

            (config / "symbols.txt").write_text("", encoding="utf-8")
            _, result, _ = _run_universe_main(root, "--dry-run")
            self.assertEqual((result["refresh_due"], result["reason"]), (True, "nasdaq_list_missing"), "an empty list counts as missing")
            _, result, calls = _run_universe_main(root, "--skip-catalog")
            self.assertEqual((result["status"], calls), ("fresh", []), "an S&P-only refresh cannot restore the list")

            code, result, calls = _run_universe_main(root)
            self.assertEqual((code, result["status"], result["reason"]), (0, "refreshed", "nasdaq_list_missing"))
            self.assertEqual(calls, ["sp500_csv", "stocks", "stocks", "etfs"])
            self.assertEqual((config / "symbols.txt").read_text(encoding="utf-8"), "AAA\nNEWW\n")

            _, result, calls = _run_universe_main(root)
            self.assertEqual((result["status"], calls), ("fresh", []))


class CompletionTest(unittest.TestCase):
    def test_status_values(self) -> None:
        universe = Universe(etfs=["SPY"], sp500=["AAA"], extra=[], nasdaq=[])
        ok = {"core_session": "2026-10-20", "splits_checked_at": "2026-10-20T22:00:00Z"}
        state = {"symbols": {"SPY": {"twelve_data": ok}, "AAA": {"twelve_data": ok}}}
        fred_state = {"series": {"DGS10": {"last_success_at": "2026-10-20T21:00:00Z"}}}

        def evaluate(st, series=("DGS10",), min_sp500=1):
            return evaluate_completion(universe, st, fred_state, list(series), "2026-10-20", "2026-10-20T20:00:00Z", min_sp500)

        self.assertEqual(evaluate(state)["status"], "complete")
        partial = evaluate({"symbols": {"SPY": {"twelve_data": ok}}}, ("DGS10", "VIXCLS"))
        self.assertEqual((partial["status"], partial["missing_symbols"], partial["missing_series"]), ("partial", ["AAA"], ["VIXCLS"]))
        self.assertEqual(evaluate({}, ())["status"], "failed")

    def test_missing_split_history_is_not_complete(self) -> None:
        universe = Universe(etfs=["SPY"], sp500=["AAA"], extra=[], nasdaq=[])
        prices_only = {"core_session": "2026-10-20"}
        stale = {"core_session": "2026-10-20", "splits_checked_at": "2026-10-01T00:00:00Z", "splits_stale": True}
        state = {"symbols": {"SPY": {"twelve_data": prices_only}, "AAA": {"twelve_data": stale}}}
        result = evaluate_completion(universe, state, {}, [], "2026-10-20", "x", min_sp500=1)
        self.assertEqual((result["status"], result["splits_ok"], result["missing_splits"]), ("partial", 0, ["SPY", "AAA"]))

    def test_uninitialized_universe_fails(self) -> None:
        universe = Universe(etfs=["SPY"], sp500=[], extra=[], nasdaq=[])
        ok = {"core_session": "2026-10-20", "splits_checked_at": "2026-10-20T22:00:00Z"}
        result = evaluate_completion(universe, {"symbols": {"SPY": {"twelve_data": ok}}}, {}, [], "2026-10-20", "x")
        self.assertEqual(result["status"], "failed")
        self.assertIn("universe_not_initialized", result["universe_issue"])

    @unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
    def test_snapshot_is_published_with_status(self) -> None:
        import duckdb  # type: ignore

        from src.storage import replace_symbol_prices

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, data = root / "config", root / "data"
            config.mkdir()
            (config / "core_etfs.txt").write_text("SPY\n", encoding="utf-8")
            (config / "fred_series.txt").write_text("", encoding="utf-8")
            replace_symbol_prices(data, "SPY", [{"date": "2026-10-20", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"}], {"2026-10-20": "0.99"})
            state_path = data / "state" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {"core_session": "2026-10-20", "splits_checked_at": "2026-10-20T22:00:00Z"}
            state_path.write_text(json.dumps({"symbols": {"SPY": {"twelve_data": entry}}}), encoding="utf-8")
            now = datetime(2026, 10, 20, 17, 0, tzinfo=NY)
            result = create_snapshot(data, config, state_path, "core", "run1", "2026-10-20T20:00:00Z", now=now, min_sp500=0)
            con = duckdb.connect(str(data / "db" / "market_snapshot.db"), read_only=True)
            try:
                run = con.execute("SELECT status, core_ok, CAST(session_date AS VARCHAR), splits_ok, missing_splits FROM collection_runs").fetchone()
                status = con.execute("SELECT group_name, yahoo_symbol, CAST(last_bar_date AS VARCHAR) FROM symbol_status").fetchall()
                price = con.execute("SELECT close, adj_close, splits, dividends FROM prices_daily").fetchone()
            finally:
                con.close()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(run, ("complete", 1, "2026-10-20", 1, "[]"))
        self.assertEqual(status, [("core_etf", "SPY", "2026-10-20")])
        self.assertEqual(price, (1.0, 0.99, 0.0, 0.0))


def _articles(day_counts: dict[str, int]) -> list[dict]:
    items, ident = [], 0
    for day, count in day_counts.items():
        stamp = int(datetime.fromisoformat(day + "T12:00:00+00:00").timestamp())
        for _ in range(count):
            ident += 1
            items.append({"id": ident, "datetime": stamp, "headline": f"h{ident}", "source": "s", "summary": "", "url": f"u{ident}"})
    return items


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
class NewsPagingTest(unittest.TestCase):
    def test_truncated_window_is_walked_back_and_resumed(self) -> None:
        pages = {
            "2026-10-20": _articles({"2026-10-14": 100, "2026-10-17": 150}),  # 250 -> truncated
            "2026-10-14": _articles({"2026-09-20": 10, "2026-10-14": 100}),  # 110 -> window done
        }
        requested = []

        def fake_fetch(url, limiter):
            to = url.split("to=")[1].split("&")[0]
            requested.append(to)
            return pages[to]

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            entry: dict = {}
            first = finnhub_news.collect_symbol_news(data_dir, "MSFT", entry, ("2025-10-20", "2026-10-20"), "k", 1, fetch=fake_fetch)
            self.assertFalse(first["complete"])
            self.assertEqual((entry["window_from"], entry["cursor_to"]), ("2025-10-20", "2026-10-14"))
            self.assertNotIn("last_to", entry)
            second = finnhub_news.collect_symbol_news(data_dir, "MSFT", entry, ("ignored", "ignored"), "k", 10, fetch=fake_fetch)
        self.assertTrue(second["complete"])
        self.assertEqual(requested, ["2026-10-20", "2026-10-14"])
        self.assertEqual(entry["last_to"], "2026-10-20")
        self.assertIsNone(entry["window_from"])

    def test_single_busy_day_steps_back(self) -> None:
        rows = finnhub_news.normalize_news(_articles({"2026-10-20": 250}), "AAPL")
        self.assertEqual(finnhub_news.next_cursor(rows, "2026-10-01", "2026-10-20"), "2026-10-19")
        self.assertIsNone(finnhub_news.next_cursor(rows[:150], "2026-10-01", "2026-10-20"))

    def test_core_symbols_come_first(self) -> None:
        order = finnhub_news.choose_news_symbols(["ZZZ", "AAA"], {"SPY": {"last_attempt_at": "2026-10-20"}}, {}, None, priority=["SPY", "BRK.B"])
        self.assertEqual(order, ["BRK.B", "SPY", "ZZZ", "AAA"])


class FredTest(unittest.TestCase):
    def test_rights_note_flags_restricted_series(self) -> None:
        self.assertIsNone(fred.rights_note("Averages of business days. Source: H.15 release."))
        note = fred.rights_note("Spread data. Copyright, 2016, Chicago Board Options Exchange, Inc. Reprinted with permission.")
        self.assertIn("Copyright", note)

    def test_non_json_answer_names_what_came_back(self) -> None:
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}

        page = b"<!doctype html><html><head>\n<title>Site Maintenance | St. Louis Fed<</title></head><body>We'll be back soon!</body></html>"
        with patch.object(fred, "urlopen", return_value=Response(page)):
            with self.assertRaises(fred.FredUnavailable) as caught:
                fred.fetch_json("https://api.stlouisfed.org/fred/series/observations?series_id=DGS10")
        self.assertEqual(str(caught.exception), "FRED returned non-JSON (HTTP 200, text/html; charset=utf-8): Site Maintenance | St. Louis Fed<")
        self.assertEqual(fred.describe_body(b"  \n"), "empty body")
        self.assertEqual(fred.describe_body(b"Service  Unavailable"), "Service Unavailable")

    def test_unavailable_fred_is_not_asked_for_the_remaining_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(data_dir=Path(tmp), series=["FEDFUNDS", "DGS10", "VIXCLS"], series_file=None, limit=None, api_key_env="FRED_API_KEY", dry_run=False)
            down = fred.FredUnavailable("FRED returned non-JSON (HTTP 200, text/html): Site Maintenance")
            with patch.dict(os.environ, {"FRED_API_KEY": "test"}), patch.object(fred, "ensure_db_schema"), patch.object(fred, "fetch_json", side_effect=down) as fetch:
                summary = fred.run(args)
            saved = json.loads((Path(tmp) / "state" / "fred.json").read_text(encoding="utf-8"))["series"]
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual((summary["processed"], summary["failed"], summary["unavailable"]), (0, 3, str(down)))
        self.assertEqual([saved[s]["status"] for s in args.series], ["failed"] * 3)
        self.assertEqual(saved["VIXCLS"]["error"], f"not requested: {down}")

    @unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
    def test_first_seen_at_survives_revisions(self) -> None:
        from src.storage import connect_db, upsert_macro_db

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch("src.storage.utc_now_iso", return_value="2026-10-20T21:00:00Z"):
                upsert_macro_db(data_dir, "UNRATE", [{"date": "2026-09-01", "value": "4.3"}])
            with patch("src.storage.utc_now_iso", return_value="2026-11-06T21:00:00Z"):
                upsert_macro_db(data_dir, "UNRATE", [{"date": "2026-09-01", "value": "4.4"}, {"date": "2026-10-01", "value": "4.5"}])
            con = connect_db(data_dir, read_only=True)
            try:
                rows = con.execute("SELECT date, value, first_seen_at, updated_at FROM macro ORDER BY date").fetchall()
            finally:
                con.close()
        self.assertEqual(rows, [
            ("2026-09-01", "4.4", "2026-10-20T21:00:00Z", "2026-11-06T21:00:00Z"),
            ("2026-10-01", "4.5", "2026-11-06T21:00:00Z", "2026-11-06T21:00:00Z"),
        ])


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
class SchemaVersionTest(unittest.TestCase):
    def test_new_database_is_created_at_version_4_with_the_same_view_columns(self) -> None:
        from src import storage

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage.ensure_db_schema(data_dir)
            con = storage.connect_db(data_dir, read_only=True)
            try:
                version = storage.schema_version(con)
                prices = [(row[0], row[1]) for row in con.execute("DESCRIBE prices_daily").fetchall()]
                indicator_columns = [row[0] for row in con.execute("DESCRIBE indicators").fetchall()]
            finally:
                con.close()
        self.assertEqual(version, 4)
        self.assertEqual(prices, [
            ("symbol", "VARCHAR"), ("date", "DATE"), ("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"),
            ("adj_close", "DOUBLE"), ("volume", "DOUBLE"), ("dividends", "DECIMAL(2,1)"), ("splits", "DOUBLE"),
        ], "stockpred contract: same names and types as schema version 3")
        self.assertEqual(indicator_columns, ["symbol", "date", *storage.INDICATOR_FIELDS, "updated_at"])

    def test_older_database_is_refused_with_the_conversion_command(self) -> None:
        import duckdb  # type: ignore

        from src import storage

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "db").mkdir()
            con = duckdb.connect(str(data_dir / "db" / "market.db"))
            con.execute("CREATE TABLE ohlcv (symbol VARCHAR NOT NULL, date VARCHAR NOT NULL, close VARCHAR, PRIMARY KEY(symbol, date))")
            con.execute("CREATE TABLE schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
            con.execute("INSERT INTO schema_meta VALUES ('version', '3')")
            con.close()
            with self.assertRaisesRegex(storage.SchemaVersionError, r"schema version 3.*src\.rebuild --to-v4"):
                storage.ensure_db_schema(data_dir)
            with self.assertRaises(storage.SchemaVersionError):
                storage.replace_symbol_prices(data_dir, "AAA", [{"date": "2026-01-02", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"}])
            con = duckdb.connect(str(data_dir / "db" / "market.db"), read_only=True)
            try:
                tables = {row[0] for row in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
                version = con.execute("SELECT value FROM schema_meta").fetchone()[0]
            finally:
                con.close()
        self.assertEqual((tables, version), ({"ohlcv", "schema_meta"}, "3"), "nothing is migrated in place")


if __name__ == "__main__":
    unittest.main()
