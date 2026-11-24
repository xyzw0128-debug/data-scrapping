"""config/core_exclude.txt: core symbols taken out by hand (parser, universe, completion, snapshot, collectors, validation)."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import logging
import os
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from src import finnhub_news, main, snapshot, universe as universe_module, validate_duckdb
from src.data_rules import CoreExclusion, DataRules, DataRulesError, load_core_exclusions, load_data_rules
from src.lock import PIPELINE_LOCK_ENV
from src.snapshot import create_snapshot, evaluate_completion
from src.universe import Universe, load_universe

HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None
ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 24)


def _exclusion(symbol: str, day: str) -> CoreExclusion:
    return CoreExclusion(symbol=symbol, date=day, reason="test")


def _bar(day: str, close: float = 10.0) -> dict:
    return {"date": day, "open": close, "high": close, "low": close, "close": close, "volume": 100}


def _config(root: Path, exclude: str, extra: str | None = None, symbols: str = "") -> Path:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "core_etfs.txt").write_text("SPY\n", encoding="utf-8")
    (config / "symbols.txt").write_text(symbols, encoding="utf-8")
    (config / "fred_series.txt").write_text("", encoding="utf-8")
    (config / "core_exclude.txt").write_text(exclude, encoding="utf-8")
    if extra is not None:
        (config / "core_extra.txt").write_text(extra, encoding="utf-8")
    return config


def _saved_universe(data_dir: Path, sp500: list[str], former_core: tuple[str, ...] = ()) -> None:
    path = data_dir / "state" / "universe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "refreshed_at": "2026-10-28T20:10:55Z",
        "sp500": {"as_of": "2026-08-18", "symbols": sp500},
        "former_core": {symbol: {"removed_as_of": "2026-08-18"} for symbol in former_core},
        "delisted": {},
    }), encoding="utf-8")


class CoreExcludeFileTest(unittest.TestCase):
    def test_repository_file_loads(self) -> None:
        exclusions = load_core_exclusions(ROOT / "config")
        self.assertEqual({s: e.date for s, e in exclusions.items()}, {"WBD": "2026-10-06", "KVUE": "2026-11-02"})
        self.assertTrue(all(e.reason for e in exclusions.values()))
        self.assertEqual(load_data_rules(ROOT / "config").core_exclude, exclusions, "load_data_rules reads it before any paid call")

    def test_missing_file_comments_and_calendar_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            self.assertEqual(load_core_exclusions(config, TODAY), {})
            (config / "core_exclude.txt").write_text("# comment\n\n  # indented comment\nbrk.b 2026-10-03 a Saturday is fine\nAAA 2026-11-24 today\n", encoding="utf-8")
            exclusions = load_core_exclusions(config, TODAY)
        self.assertEqual(exclusions["BRK.B"], CoreExclusion("BRK.B", "2026-10-03", "a Saturday is fine"))
        self.assertEqual(exclusions["AAA"].date, "2026-11-24")

    def test_malformed_lines_are_rejected(self) -> None:
        cases = {
            "missing reason": "WBD 2026-10-06\n",
            "bad date": "WBD 2026-13-06 reason\n",
            "short date": "WBD 2026-10-6 reason\n",
            "bad symbol": "W*D 2026-10-06 reason\n",
            "listed twice": "WBD 2026-10-06 a\nWBD 2026-10-07 b\n",
            "future date": "WBD 2026-11-25 planned\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in cases.items():
                with self.subTest(name):
                    config = Path(tmp) / name.replace(" ", "_")
                    config.mkdir()
                    (config / "core_exclude.txt").write_text(text, encoding="utf-8")
                    with self.assertRaises(DataRulesError) as caught:
                        load_core_exclusions(config, TODAY)
                    self.assertIn("core_exclude.txt line", str(caught.exception))


class UniverseExclusionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.universe = Universe(
            etfs=["SPY", "GONEETF"], sp500=["AAA", "WBD", "KVUE"], extra=["XTRA"], nasdaq=["WBD", "NASQ", "OLD"],
            former_core={"OLD": {}}, excluded={s: _exclusion(s, "2026-10-06") for s in ("WBD", "KVUE", "GONEETF", "OLD")},
        )

    def test_excluded_symbols_leave_core_and_rotation(self) -> None:
        self.assertEqual(self.universe.core_symbols(), ["SPY", "AAA", "XTRA"])
        self.assertEqual(self.universe.rotation_symbols(), ["NASQ"], "WBD is on the NASDAQ list and OLD a former member")
        self.assertEqual(self.universe.inactive_symbols(), {"WBD", "KVUE", "GONEETF", "OLD"})
        self.assertEqual(self.universe.group_of("WBD"), "core_sp500")
        self.assertEqual(self.universe.group_of("GONEETF"), "core_etf")

    def test_all_symbols_keep_excluded_core_symbols(self) -> None:
        self.assertEqual(self.universe.all_symbols(), ["SPY", "GONEETF", "AAA", "WBD", "KVUE", "XTRA", "NASQ", "OLD"])

    def test_lines_can_go_once_no_core_list_has_the_symbol(self) -> None:
        with patch.object(universe_module, "MIN_SP500_MEMBERS", 3):
            self.assertEqual(self.universe.removable_exclusions(), ["OLD"])
        self.assertEqual(self.universe.removable_exclusions(), [], "an S&P 500 list below 490 members is not loaded: no judgment")

    def test_load_universe_reads_the_file_and_rejects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _saved_universe(root / "data", ["AAA", "WBD"])
            config = _config(root, "WBD 2026-10-06 acquired\n", extra="TWLO\n")
            loaded = load_universe(root / "data", config)
            self.assertEqual((loaded.core_symbols(), sorted(loaded.excluded)), (["SPY", "AAA", "TWLO"], ["WBD"]))

            (config / "core_extra.txt").write_text("TWLO\nWBD\n", encoding="utf-8")
            with self.assertRaises(DataRulesError) as caught:
                load_universe(root / "data", config)
            self.assertIn("core_extra.txt", str(caught.exception))


class CompletionWithExclusionsTest(unittest.TestCase):
    def test_excluded_symbols_are_not_expected(self) -> None:
        ok = {"core_session": "2026-11-23", "splits_checked_at": "2026-10-20T22:00:00Z"}
        stale = {"last_bar_date": "2026-10-06", "splits_checked_at": "2026-10-20T22:00:00Z"}
        state = {"symbols": {"SPY": {"twelve_data": ok}, "AAA": {"twelve_data": ok}, "WBD": {"twelve_data": stale}}}

        def evaluate(excluded: dict) -> dict:
            universe = Universe(etfs=["SPY"], sp500=["AAA", "WBD"], extra=[], nasdaq=[], excluded=excluded)
            return evaluate_completion(universe, state, {}, [], "2026-11-23", "x", min_sp500=2)

        before = evaluate({})
        self.assertEqual((before["status"], before["core_expected"], before["core_ok"], before["missing_symbols"]), ("partial", 3, 2, ["WBD"]))
        after = evaluate({"WBD": _exclusion("WBD", "2026-10-06")})
        self.assertEqual((after["status"], after["core_expected"], after["core_ok"], after["missing_symbols"]), ("complete", 2, 2, []))
        self.assertIsNone(after["universe_issue"], "the S&P 500 list size still counts the excluded member")

    @unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
    def test_snapshot_marks_excluded_symbols_inactive_and_keeps_their_rows(self) -> None:
        import duckdb  # type: ignore

        from src.storage import replace_symbol_prices

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            config = _config(root, "WBD 2026-10-06 acquired\n")
            _saved_universe(data, ["WBD"])
            replace_symbol_prices(data, "SPY", [_bar("2026-11-23")], {"2026-11-23": 9.9})
            replace_symbol_prices(data, "WBD", [_bar("2026-10-05", 31.0), _bar("2026-10-06", 30.95)], {"2026-10-05": 31.0, "2026-10-06": 30.95})
            state_path = data / "state" / "state.json"
            ok = {"core_session": "2026-11-23", "splits_checked_at": "2026-10-20T22:00:00Z", "last_success_at": "2026-11-24T01:20:00Z"}
            state_path.write_text(json.dumps({"symbols": {"SPY": {"twelve_data": ok}, "WBD": {"twelve_data": {"last_success_at": "2026-11-03T23:34:48Z"}}}}), encoding="utf-8")
            now = datetime(2026, 11, 24, 3, 40, tzinfo=UTC)
            result = create_snapshot(data, config, state_path, "core", "run1", "2026-11-24T01:10:00Z", now=now, min_sp500=0)
            con = duckdb.connect(str(data / "db" / "market_snapshot.db"), read_only=True)
            try:
                run = con.execute("SELECT status, core_expected, core_ok, missing_symbols FROM collection_runs").fetchone()
                status = con.execute("SELECT symbol, group_name, active, delisted_at, CAST(last_bar_date AS VARCHAR) FROM symbol_status ORDER BY symbol").fetchall()
                wbd_rows = con.execute("SELECT count(*) FROM prices_daily WHERE symbol = 'WBD'").fetchone()[0]
            finally:
                con.close()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(run, ("complete", 1, 1, "[]"))
        self.assertEqual(status, [("SPY", "core_etf", True, None, "2026-11-23"), ("WBD", "core_sp500", False, "2026-10-06", "2026-10-06")])
        self.assertEqual(wbd_rows, 2)


class CollectorsSkipExcludedTest(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp)
        config = _config(root, "WBD 2026-10-06 acquired\nKVUE 2026-11-02 acquired\nOLD 2026-09-01 left the index\n", symbols="WBD\nNASQ\n")
        (config / "providers.yaml").write_text(
            "twelve_data:\n  enabled: true\n  api_key_env: TD\n  daily_limit: 800\n  daily_reserve: 100\n  max_symbols_per_run: 5\n", encoding="utf-8"
        )
        _saved_universe(root / "data", ["AAA", "WBD", "KVUE"], former_core=("OLD",))
        return root

    def test_dry_run_plans_without_excluded_symbols_and_logs_removable_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            state_path = root / "data" / "state" / "state.json"
            args = argparse.Namespace(
                provider="twelve_data", phase="all", symbols=root / "config" / "symbols.txt", config_dir=root / "config",
                providers=root / "config" / "providers.yaml", data_dir=root / "data", state=state_path, dry_run=True,
                max_symbols=None, max_credits=None, run_id="test", force=False, no_usage_sync=True, only=None,
            )
            with self.assertLogs("test", level="INFO") as logs, patch.object(universe_module, "MIN_SP500_MEMBERS", 3):
                summary = main.run_collection(args, logging.getLogger("test"))
            self.assertFalse(state_path.exists(), "dry-run writes no state")
        self.assertEqual((summary["core"]["expected"], summary["core"]["sample"]), (2, ["SPY", "AAA"]))
        self.assertEqual(summary["rotation"]["sample"], ["NASQ"])
        self.assertEqual(summary["core_exclude"], {"symbols": ["KVUE", "OLD", "WBD"], "removable": ["OLD"]})
        removable = [line for line in logs.output if "core_exclude.txt can go" in line]
        self.assertEqual(len(removable), 1)
        self.assertIn("OLD", removable[0])
        self.assertIn("returns to rotation", removable[0])

    def test_only_an_excluded_symbol_collects_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            args = argparse.Namespace(
                provider="twelve_data", phase="all", symbols=root / "config" / "symbols.txt", config_dir=root / "config",
                providers=root / "config" / "providers.yaml", data_dir=root / "data", state=root / "data" / "state" / "state.json",
                dry_run=True, max_symbols=None, max_credits=None, run_id="test", force=False, no_usage_sync=True, only=["wbd"],
            )
            with self.assertLogs("test", level="INFO") as logs:
                summary = main.run_collection(args, logging.getLogger("test"))
        self.assertEqual((summary["core"]["pending"], summary["rotation"]["candidates"]), (0, 0))
        self.assertTrue(any("Not collecting WBD" in line for line in logs.output))

    def test_news_selection_skips_excluded_symbols(self) -> None:
        universe = Universe(
            etfs=["SPY"], sp500=["AAA", "WBD"], extra=[], nasdaq=["WBD", "NASQ"], former_core={"OLD": {}},
            excluded={"WBD": _exclusion("WBD", "2026-10-06"), "OLD": _exclusion("OLD", "2026-09-01")},
        )
        symbols, waiting = finnhub_news.universe_news_selection(universe, {}, {}, None)
        self.assertEqual((symbols, waiting), (["SPY", "AAA", "NASQ"], []))


@patch.dict(os.environ, {PIPELINE_LOCK_ENV: ""})
class MalformedFileStopsCollectorsTest(unittest.TestCase):
    def _run(self, module, argv: list[str]) -> tuple[int, dict]:
        out = io.StringIO()
        with patch("sys.argv", argv), \
                patch("src.twelve_data.urlopen", side_effect=AssertionError("network call")), \
                patch.object(finnhub_news, "urlopen", side_effect=AssertionError("network call")), \
                patch.object(universe_module, "fetch_text", side_effect=AssertionError("network call")), \
                patch.object(main, "TwelveDataClient", side_effect=AssertionError("client built")), \
                contextlib.redirect_stdout(out):
            code = module.main()
        return code, json.loads(out.getvalue())

    def test_every_cli_that_loads_the_universe_fails_before_any_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, "WBD 2026-10-06\n")
            data = root / "data"
            state = ["--state", str(data / "state" / "state.json")]
            common = ["--data-dir", str(data), "--config-dir", str(config)]
            cases = {
                "src.main": (main, ["src.main", *common, *state]),
                "src.main dry-run": (main, ["src.main", *common, *state, "--dry-run"]),
                "src.snapshot": (snapshot, ["src.snapshot", *common, *state, "--dry-run"]),
                "src.finnhub_news": (finnhub_news, ["src.finnhub_news", *common, "--state", str(data / "state" / "finnhub_news.json"), "--dry-run"]),
                "src.universe": (universe_module, ["src.universe", *common, "--force"]),
            }
            for name, (module, argv) in cases.items():
                with self.subTest(name):
                    code, result = self._run(module, argv)
                    self.assertEqual((code, result["status"]), (1, "data_rules_invalid"))
                    self.assertIn("core_exclude.txt line 1", result["error"])


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
class ExcludedButActiveWarningTest(unittest.TestCase):
    def test_warns_only_for_symbols_that_are_not_actually_stale(self) -> None:
        import duckdb  # type: ignore

        from src.storage import connect_db, ensure_db_schema, replace_symbol_prices

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            ensure_db_schema(data)
            replace_symbol_prices(data, "WBD", [_bar("2026-10-05"), _bar("2026-10-06")])  # last bar on the exclusion date
            replace_symbol_prices(data, "KVUE", [_bar("2026-10-29"), _bar("2026-10-30")])  # stopped before it
            replace_symbol_prices(data, "WDC", [_bar("2026-11-20"), _bar("2026-11-23")])  # a typo for WBD: still trading
            replace_symbol_prices(data, "NEWX", [_bar("2026-11-23")])  # excluded today, yet has the latest session
            con = connect_db(data)
            try:
                con.execute("INSERT INTO collection_runs (run_id, phase, session_date) VALUES ('r', 'core', DATE '2026-11-23')")
            finally:
                con.close()
            rules = DataRules(core_exclude={
                "WBD": _exclusion("WBD", "2026-10-06"), "KVUE": _exclusion("KVUE", "2026-11-02"),
                "WDC": _exclusion("WDC", "2026-10-06"), "NEWX": _exclusion("NEWX", "2026-11-23"), "NODATA": _exclusion("NODATA", "2026-10-01"),
            })
            con = duckdb.connect(str(data / "db" / "market.db"), read_only=True)
            try:
                warnings = {w["type"]: w for w in validate_duckdb.data_rule_warnings(con, rules)}
                quiet = validate_duckdb.data_rule_warnings(con, DataRules(core_exclude={"WBD": _exclusion("WBD", "2026-10-06")}))
            finally:
                con.close()
        self.assertEqual(warnings["core_exclude_active"]["items"], [
            {"symbol": "NEWX", "exclude_date": "2026-11-23", "last_bar_date": "2026-11-23"},
            {"symbol": "WDC", "exclude_date": "2026-10-06", "last_bar_date": "2026-11-23"},
        ])
        self.assertEqual(warnings["core_exclude_active"]["session_date"], "2026-11-23")
        self.assertNotIn("core_exclude_active", {w["type"] for w in quiet})


if __name__ == "__main__":
    unittest.main()
