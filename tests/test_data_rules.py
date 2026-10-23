"""Data rules: rule files, closed-day/history-start filters, patches, vanished dates, validation warnings, cleanup CLI."""

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
from pathlib import Path
from unittest.mock import patch

from src import main, ohlcv_cleanup, summary, validate_duckdb
from src.corporate_actions import split_basis_changed, split_events
from src.data_rules import BarPatch, DataRules, DataRulesError, load_data_rules
from src.lock import PIPELINE_LOCK_ENV, pipeline_lock
from src.rate_limit import RunCreditCap

HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None
ROOT = Path(__file__).resolve().parents[1]
PATCH_HEADER = "symbol,date,open,high,low,close,volume,ref_date,ref_close,source,note"
# 2024-06-08/09 are a weekend; 2024-06-19 is Juneteenth.
WEEK = ["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07", "2024-06-10", "2024-06-11", "2024-06-12"]


def _row(day: str, close: float, volume: int = 100) -> dict:
    return {"date": day, "open": close, "high": close, "low": close, "close": close, "volume": volume}


def _rows(prices: dict[str, float]) -> list[dict]:
    return [_row(day, close) for day, close in sorted(prices.items())]


def _bars(prices: dict[str, float]) -> list[dict]:
    """Twelve Data values, newest first."""
    return [{"datetime": d, "open": str(p), "high": str(p), "low": str(p), "close": str(p), "volume": "100"} for d, p in sorted(prices.items(), reverse=True)]


def _patch(day: str = "2024-06-10", close: float = 10.0, ref_date: str = "2024-06-11", ref_close: float = 11.0, volume: int = 500, symbol: str = "AAA") -> BarPatch:
    return BarPatch(symbol=symbol, date=day, open=close, high=close + 1, low=close - 1, close=close, volume=volume, ref_date=ref_date, ref_close=ref_close, source="test")


class FakeClient:
    """Answers time_series from per-adjust price maps and records every call."""

    def __init__(self, series: dict[str, dict[str, float]]):
        self.series = series
        self.calls: list[tuple[str, str]] = []

    def can_afford(self, credits: int) -> bool:
        return True

    def time_series(self, symbol: str, adjust: str) -> dict:
        self.calls.append((symbol, adjust))
        return {"status": "ok", "values": _bars(self.series[adjust])}


def _write_rules(config_dir: Path, history: str = "", patches: str = "", gaps: str = "") -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "history_start.txt").write_text(history, encoding="utf-8")
    (config_dir / "bar_patches.csv").write_text(patches, encoding="utf-8")
    (config_dir / "known_gaps.txt").write_text(gaps, encoding="utf-8")
    return config_dir


class RuleFilesTest(unittest.TestCase):
    def test_repository_rule_files_load(self) -> None:
        rules = load_data_rules(ROOT / "config")
        self.assertEqual(rules.history_start["GM"], "2010-11-18")
        mnst = rules.patches["MNST"][0]
        self.assertEqual((mnst.date, mnst.close, mnst.volume, mnst.ref_date, mnst.ref_close), ("2026-08-10", 45.715, 11943600, "2026-08-11", 45.53))
        self.assertIn(("FISV", "2025-11-12"), rules.known_gaps)

    def test_missing_files_mean_no_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = load_data_rules(Path(tmp))
        self.assertEqual((rules.history_start, rules.patches, rules.known_gaps), ({}, {}, frozenset()))

    def test_malformed_lines_are_rejected(self) -> None:
        good = "AAA,2024-06-10,10,11,9,10.5,500,2024-06-11,11,yahoo,checked"
        cases = {
            "history: missing reason": dict(history="GM 2010-11-18\n"),
            "history: bad date": dict(history="GM 2010-13-18 reason\n"),
            "history: closed day": dict(history="GM 2010-11-20 Saturday\n"),
            "history: bad symbol": dict(history="G M! 2010-11-18 reason\n"),
            "history: listed twice": dict(history="GM 2010-11-18 a\nGM 2010-11-19 b\n"),
            "gaps: listed twice": dict(gaps="AAA 2024-06-10 a\nAAA 2024-06-10 b\n"),
            "gaps: before start": dict(history="AAA 2024-06-11 a\n", gaps="AAA 2024-06-10 b\n"),
            "patches: header": dict(patches="symbol,date\n" + good + "\n"),
            "patches: columns": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-10,10\n"),
            "patches: twice": dict(patches=f"{PATCH_HEADER}\n{good}\n{good}\n"),
            "patches: ref is date": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-10,10,11,9,10.5,500,2024-06-10,11,yahoo,x\n"),
            "patches: holiday": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-19,10,11,9,10.5,500,2024-06-18,11,yahoo,x\n"),
            "patches: zero price": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-10,0,11,9,10.5,500,2024-06-11,11,yahoo,x\n"),
            "patches: not a number": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-10,abc,11,9,10.5,500,2024-06-11,11,yahoo,x\n"),
            "patches: high below close": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-10,10,10.2,9,10.5,500,2024-06-11,11,yahoo,x\n"),
            "patches: low above open": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-10,10,11,10.1,10.5,500,2024-06-11,11,yahoo,x\n"),
            "patches: fractional volume": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-10,10,11,9,10.5,1.5,2024-06-11,11,yahoo,x\n"),
            "patches: negative volume": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-10,10,11,9,10.5,-1,2024-06-11,11,yahoo,x\n"),
            "patches: ref_close": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-10,10,11,9,10.5,500,2024-06-11,0,yahoo,x\n"),
            "patches: no source": dict(patches=f"{PATCH_HEADER}\nAAA,2024-06-10,10,11,9,10.5,500,2024-06-11,11,,x\n"),
            "patches: before start": dict(history="AAA 2024-06-11 a\n", patches=f"{PATCH_HEADER}\n{good}\n"),
            "patches: also a known gap": dict(gaps="AAA 2024-06-10 a\n", patches=f"{PATCH_HEADER}\n{good}\n"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = _write_rules(Path(tmp) / "ok", patches=f"# comment\n{PATCH_HEADER}\n{good}\n", history="# c\nGM 2010-11-18 new GM\n")
            self.assertEqual(len(load_data_rules(config).patches["AAA"]), 1)
            for name, files in cases.items():
                with self.subTest(name):
                    with self.assertRaises(DataRulesError):
                        load_data_rules(_write_rules(Path(tmp) / name.replace(" ", "_").replace(":", ""), **files))


class FilterAndPatchTest(unittest.TestCase):
    def test_closed_days_and_history_start_are_dropped(self) -> None:
        rules = DataRules(history_start={"AAA": "2024-06-05"})
        rows = _rows({"2024-06-04": 1, "2024-06-05": 1, "2024-06-08": 1, "2024-06-19": 1, "2025-01-09": 1, "2025-01-10": 1})
        kept, dropped = rules.filter_rows("AAA", rows)
        self.assertEqual([r["date"] for r in kept], ["2024-06-05", "2025-01-10"])
        self.assertEqual(dropped, {"before_start": 1, "closed_day": 3})
        self.assertEqual([r["date"] for r in rules.filter_rows("BBB", rows)[0]], ["2024-06-04", "2024-06-05", "2025-01-10"])

    def test_patch_outcomes_against_splits_and_all(self) -> None:
        splits = _rows({"2024-06-07": 10.0, "2024-06-11": 11.0, "2024-06-12": 12.0})
        rules = DataRules(patches={"AAA": (_patch(),)})
        cases = [
            # (splits rows, adj_closes, expected status, expected adj for the patched day)
            (splits, {"2024-06-07": 9.9, "2024-06-10": 9.5, "2024-06-11": 10.89}, "applied", 9.5),  # all has the day: provider value
            (splits, {"2024-06-07": 9.9, "2024-06-11": 10.89}, "applied", 9.9),  # derived: 10 * 10.89 / 11
            (splits, {"2024-06-07": 9.9}, "adj_missing", None),  # neither: skip, never a NULL adj_close
            (splits, None, "applied", None),  # rotation: no adjust=all response
            (_rows({"2024-06-07": 10.0, "2024-06-10": 10.2, "2024-06-11": 11.0}), {}, "provider_has_date", None),
            (_rows({"2024-06-07": 10.0, "2024-06-12": 12.0}), {}, "ref_missing", None),
            (_rows({"2024-06-03": 9.0, "2024-06-07": 10.0}), {}, "outside_range", None),  # response ends before: stale data is not padded
            ([], {}, "outside_range", None),
        ]
        for rows, adj, status, patched_adj in cases:
            with self.subTest(status=status, adj=adj):
                out_rows, out_adj, results = rules.apply_patches("AAA", rows, adj)
                self.assertEqual(results, [{"date": "2024-06-10", "status": status}])
                has_bar = "2024-06-10" in {r["date"] for r in out_rows}
                self.assertEqual(has_bar, status in ("applied", "provider_has_date"))
                if status == "applied":
                    self.assertEqual([r["date"] for r in out_rows], ["2024-06-07", "2024-06-10", "2024-06-11", "2024-06-12"])
                    self.assertEqual(out_rows[1], {"date": "2024-06-10", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 500})
                    self.assertEqual(None if out_adj is None else out_adj.get("2024-06-10"), patched_adj)
                if status == "adj_missing":
                    self.assertNotIn("2024-06-10", out_adj)
        self.assertEqual(DataRules().apply_patches("AAA", splits, {}), (splits, {}, []))

    def test_patch_is_rescaled_after_a_later_split(self) -> None:
        rules = DataRules(patches={"AAA": (_patch(close=10.0, ref_close=11.0, volume=500),)})
        halved = _rows({"2024-06-07": 5.0, "2024-06-11": 5.5, "2024-06-12": 6.0})  # a 2:1 split after the patch was recorded
        rows, _, _ = rules.apply_patches("AAA", halved)
        self.assertEqual(rows[1], {"date": "2024-06-10", "open": 5.0, "high": 5.5, "low": 4.5, "close": 5.0, "volume": 1000})
        close_enough = _rows({"2024-06-07": 10.0, "2024-06-11": 11.05, "2024-06-12": 12.0})  # vendor rounding: values kept as recorded
        self.assertEqual(rules.apply_patches("AAA", close_enough)[0][1]["close"], 10.0)

    def test_vanished_dates_only_inside_the_new_range(self) -> None:
        rules = DataRules(history_start={"AAA": "2024-06-04"})
        stored = ["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-08", "2024-06-13"]
        rows = _rows({"2024-06-04": 1, "2024-06-06": 1, "2024-06-12": 1})
        # 06-03 is before the start, 06-08 a Saturday, 06-13 after the response: only 06-05 vanished.
        self.assertEqual(rules.vanished_dates("AAA", stored, rows), ["2024-06-05"])
        self.assertEqual(rules.vanished_dates("AAA", stored, []), [])


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
class CollectionWithRulesTest(unittest.TestCase):
    def _stored(self, data_dir: Path, symbol: str) -> list[tuple]:
        import duckdb  # type: ignore

        con = duckdb.connect(str(data_dir / "db" / "market.db"), read_only=True)
        try:
            return con.execute("SELECT CAST(date AS VARCHAR), close, adj_close, volume FROM ohlcv WHERE symbol = ? ORDER BY date", [symbol]).fetchall()
        finally:
            con.close()

    def test_core_symbol_drops_closed_days_cuts_history_and_patches(self) -> None:
        # adjust=splits lacks 2024-06-10 and sends a Saturday bar; a 2:1 split happened on 2024-06-11.
        splits = {"2024-06-04": 4.9, "2024-06-05": 5.0, "2024-06-06": 5.1, "2024-06-07": 5.2, "2024-06-08": 5.2, "2024-06-11": 5.5, "2024-06-12": 5.6}
        adjusted = {d: round(p * 0.99, 4) for d, p in splits.items()}
        raw = {d: p * (2 if d < "2024-06-11" else 1) for d, p in splits.items()}
        rules = DataRules(history_start={"AAA": "2024-06-05"}, patches={"AAA": (_patch(close=5.3, ref_close=5.5, volume=700),)})
        client = FakeClient({"splits": splits, "all": adjusted, "none": raw})
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            result = main.collect_core_symbol(client, data_dir, "AAA", "2024-06-12", rules)
            stored = self._stored(data_dir, "AAA")
            with_patch = main.load_closes(data_dir, "AAA")
            events = main.refresh_split_events(client, data_dir, "AAA", "2024-06-12")
        self.assertEqual([d for d, *_ in stored], ["2024-06-05", "2024-06-06", "2024-06-07", "2024-06-10", "2024-06-11", "2024-06-12"])
        self.assertEqual(stored[3], ("2024-06-10", 5.3, round(5.3 * adjusted["2024-06-11"] / 5.5, 6), 700))
        self.assertEqual(result["dropped"], {"before_start": 1, "closed_day": 1})
        self.assertEqual(result["patches"], [{"date": "2024-06-10", "status": "applied"}])
        self.assertEqual((result["last_bar_date"], result["adj_ok"]), ("2024-06-12", True))
        self.assertEqual(client.calls, [("AAA", "splits"), ("AAA", "all"), ("AAA", "none")], "no extra API call")
        # The patched day is not in adjust=none, so split events are the same with or without it.
        without_patch = {d: c for d, c in with_patch.items() if d != "2024-06-10"}
        raw_closes = {d: p for d, p in raw.items() if d >= "2024-06-05" and d != "2024-06-08"}
        self.assertEqual(split_events(raw_closes, with_patch), split_events(raw_closes, without_patch))
        self.assertEqual(split_events(raw_closes, with_patch), [("2024-06-11", 2.0)])
        self.assertEqual(events, 1)
        self.assertFalse(split_basis_changed(with_patch, with_patch))

    def test_run_core_reports_vanished_dates(self) -> None:
        week = {d: 10.0 for d in WEEK}
        client = FakeClient({"splits": {d: p for d, p in week.items() if d != "2024-06-05"}, "all": week, "none": week})
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            main.replace_symbol_prices(data_dir, "AAA", _rows({**week, "2024-06-08": 10.0}))  # a stored Saturday is not reported
            state: dict = {}
            args = argparse.Namespace(dry_run=False, force=False, data_dir=data_dir, state=data_dir / "state" / "state.json")
            run_summary = {"started_at": "x", "run_id": "t", "processed": 0, "failed": 0, "skipped_rate_limit": 0, "symbols": []}
            run = main.Run(args, state, run_summary, logging.getLogger("test"), RunCreditCap(None))
            with self.assertLogs("test", level="WARNING") as logs:
                main.run_core(run, client, ["AAA"], "2024-06-12")
            stored = [d for d, *_ in self._stored(data_dir, "AAA")]
        self.assertNotIn("2024-06-05", stored)
        self.assertEqual(run_summary["core"]["vanished_dates"], 1)
        self.assertEqual(run_summary["symbols"][0]["vanished_dates"], ["2024-06-05"])
        self.assertIn("AAA: 1 stored date(s) missing from the new response: 2024-06-05", "\n".join(logs.output))

    def test_rotation_symbol_uses_the_rules(self) -> None:
        client = FakeClient({"splits": {"2024-06-07": 1.0, "2024-06-08": 1.0, "2024-06-10": 1.1}})
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            result = main.collect_rotation_symbol(client, data_dir, "ROT", "2024-06-12", DataRules())
            stored = self._stored(data_dir, "ROT")
        self.assertEqual([(d, adj) for d, _, adj, _ in stored], [("2024-06-07", None), ("2024-06-10", None)])
        self.assertEqual(result["dropped"], {"closed_day": 1})

    @patch.dict(os.environ, {PIPELINE_LOCK_ENV: ""})
    def test_bad_rule_file_stops_before_any_call(self) -> None:
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                config = _write_rules(Path(tmp) / "config", history="GM 2010-11-20 Saturday\n")
                argv = ["src.main", "--data-dir", str(data_dir), "--state", str(data_dir / "state" / "state.json"), "--config-dir", str(config)]
                out = io.StringIO()
                with patch("sys.argv", argv + (["--dry-run"] if dry_run else [])), \
                        patch("src.twelve_data.urlopen", side_effect=AssertionError("network call")) as urlopen, \
                        patch.object(main, "TwelveDataClient", side_effect=AssertionError("client built")) as client, \
                        contextlib.redirect_stdout(out):
                    code = main.main()
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out.getvalue())["status"], "data_rules_invalid")
                urlopen.assert_not_called()
                client.assert_not_called()


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
class ValidationWarningsTest(unittest.TestCase):
    def _db(self, data_dir: Path) -> None:
        from src.storage import connect_db, ensure_db_schema, replace_symbol_prices

        ensure_db_schema(data_dir)
        aaa = {d: 10.0 for d in WEEK if d not in ("2024-06-05", "2024-06-07", "2024-06-10")}
        replace_symbol_prices(data_dir, "AAA", _rows({**aaa, "2024-06-08": 10.0}))  # never-stored gaps, one Saturday bar
        replace_symbol_prices(data_dir, "BBB", _rows({d: 5.0 for d in WEEK[:6]}))  # last bar 2024-06-10
        replace_symbol_prices(data_dir, "ROT", _rows({"2024-06-03": 1.0, "2024-06-12": 1.0}))  # rotation gaps are not checked
        con = connect_db(data_dir)
        try:
            con.executemany("INSERT INTO symbol_status (symbol, group_name, active) VALUES (?, ?, ?)",
                            [["AAA", "core_sp500", True], ["BBB", "core_etf", True], ["NEW", "core_sp500", True], ["ROT", "rotation", True], ["OLD", "core_sp500", False]])
            con.execute("INSERT INTO collection_runs (run_id, phase, session_date) VALUES ('r', 'core', DATE '2024-06-12')")
        finally:
            con.close()

    def test_warnings_by_type(self) -> None:
        import duckdb  # type: ignore

        rules = DataRules(
            history_start={"BBB": "2024-06-04"},
            patches={"AAA": (_patch(),)},
            known_gaps=frozenset({("AAA", "2024-06-07")}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self._db(data_dir)
            con = duckdb.connect(str(data_dir / "db" / "market.db"), read_only=True)
            try:
                warnings = {w["type"]: w for w in validate_duckdb.data_rule_warnings(con, rules)}
            finally:
                con.close()
            report = validate_duckdb.validate_with_duckdb(data_dir, rules)
        # 06-07 is a known gap and 06-10 a patch; only the never-stored 06-05 is a gap.
        self.assertEqual(warnings["core_gap"]["items"], [{"symbol": "AAA", "date": "2024-06-05"}])
        self.assertEqual(warnings["core_stale"]["items"], [{"symbol": "BBB", "last_bar_date": "2024-06-10"}, {"symbol": "NEW", "last_bar_date": None}])
        self.assertEqual(warnings["core_stale"]["session_date"], "2024-06-12")
        self.assertEqual({k: warnings["closed_day_bars"][k] for k in ("count", "symbols", "core_count")}, {"count": 1, "symbols": 1, "core_count": 1})
        self.assertEqual(warnings["before_history_start"]["items"], [{"symbol": "BBB", "rows": 1, "first_date": "2024-06-03"}])
        self.assertEqual(warnings["patch_missing"]["items"], [{"symbol": "AAA", "date": "2024-06-10"}])
        self.assertEqual((report["status"], report["warning_count"]), ("pass", 1 + 2 + 1 + 1 + 1), "warnings never fail the report")
        self.assertEqual(validate_duckdb.validate_with_duckdb(Path(tmp) / "missing", rules)["warnings"], [])

    @patch.dict(os.environ, {PIPELINE_LOCK_ENV: "", "DATA_SCRAPPING_RUN_ID": "run-1"})
    def test_report_carries_run_id_and_bad_rules_fail_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._db(data_dir)
            config = _write_rules(Path(tmp) / "config", history="BBB 2024-06-08 Saturday\n")
            with patch("sys.argv", ["src.validate_duckdb", "--data-dir", str(data_dir), "--config-dir", str(config)]), contextlib.redirect_stdout(io.StringIO()):
                validate_duckdb.main()
            saved = json.loads((data_dir / "logs" / "validation" / "duckdb_latest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["run_id"], "run-1")
        self.assertTrue(saved["generated_at"].endswith("Z"))
        self.assertEqual((saved["status"], saved["issues"][-1]["type"]), ("fail", "data_rules_invalid"))


class SummaryValidationTest(unittest.TestCase):
    def _report(self, data_dir: Path, run_id: str) -> None:
        path = data_dir / "logs" / "validation" / "duckdb_latest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"run_id": run_id, "status": "pass", "issue_count": 0, "warning_count": 3, "generated_at": "2024-06-12T23:00:00Z",
                                    "warnings": [{"type": "core_stale", "count": 2}, {"type": "core_gap", "count": 1}]}), encoding="utf-8")

    def test_only_this_runs_report_is_shown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self._report(data_dir, "run-1")
            this_run = summary.build_summary(data_dir, "run-1")
            other_run = summary.build_summary(data_dir, "run-2")
            no_run = summary.build_summary(data_dir, None)
        self.assertEqual(this_run["validation"]["warnings"], {"core_stale": 2, "core_gap": 1})
        self.assertIn("\nvalidation=pass warnings=core_gap:1,core_stale:2\n", summary.format_discord_message(this_run))
        for stale in (other_run, no_run):
            self.assertIsNone(stale["validation"])
            self.assertIn("\nvalidation=N/A\n", summary.format_discord_message(stale))


@unittest.skipIf(not HAS_DUCKDB, "duckdb dependency is not installed")
@patch.dict(os.environ, {PIPELINE_LOCK_ENV: ""})
class CleanupTest(unittest.TestCase):
    def _db(self, data_dir: Path) -> None:
        from src.storage import connect_db, ensure_db_schema, replace_symbol_prices

        ensure_db_schema(data_dir)
        # 2025-01-09 was a one-off closure; 2025-01-11 is a Saturday.
        replace_symbol_prices(data_dir, "AAA", _rows({"2025-01-07": 1, "2025-01-08": 1, "2025-01-09": 1, "2025-01-10": 1, "2025-01-11": 1}))
        replace_symbol_prices(data_dir, "BBB", _rows({"2025-01-06": 2, "2025-01-07": 2, "2025-01-08": 2, "2025-01-09": 2, "2025-01-10": 2}))
        replace_symbol_prices(data_dir, "CCC", _rows({"2025-01-08": 3, "2025-01-10": 3}))
        con = connect_db(data_dir)
        try:
            con.executemany("INSERT INTO indicators (symbol, date, updated_at) VALUES (?, CAST(? AS DATE), 'x')",
                            [["AAA", "2025-01-08"], ["BBB", "2025-01-08"], ["CCC", "2025-01-08"], ["CCC", "2025-01-10"]])
        finally:
            con.close()

    def _fingerprint(self, data_dir: Path) -> tuple:
        from src.storage import connect_db

        con = connect_db(data_dir, read_only=True)
        try:
            return (
                con.execute("SELECT count(*), sum(hash(symbol, date, close, volume)) FROM ohlcv").fetchone(),
                con.execute("SELECT count(*), sum(hash(symbol, date)) FROM indicators").fetchone(),
            )
        finally:
            con.close()

    def _args(self, data_dir: Path, config: Path, dry_run: bool) -> argparse.Namespace:
        return argparse.Namespace(data_dir=data_dir, config_dir=config, dry_run=dry_run, memory_limit="256MB")

    def test_dry_run_counts_and_changes_nothing_then_cleanup_deletes(self) -> None:
        from src.storage import connect_db

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = _write_rules(Path(tmp) / "config", history="BBB 2025-01-08 first valid day\n")
            self._db(data_dir)
            before = self._fingerprint(data_dir)
            dry = ohlcv_cleanup.run(self._args(data_dir, config, True))
            self.assertEqual(self._fingerprint(data_dir), before, "dry-run writes nothing")
            done = ohlcv_cleanup.run(self._args(data_dir, config, False))
            con = connect_db(data_dir, read_only=True)
            try:
                rows = con.execute("SELECT symbol, CAST(date AS VARCHAR) FROM ohlcv ORDER BY 1, 2").fetchall()
                indicators = con.execute("SELECT DISTINCT symbol FROM indicators ORDER BY 1").fetchall()
            finally:
                con.close()
            again = ohlcv_cleanup.run(self._args(data_dir, config, False))
        # Closed: AAA 01-09, 01-11 and BBB 01-09 (a closed day wins over before_start); BBB 01-06, 01-07 are before its start.
        expected_counts = {"closed_day_rows": 3, "before_start_rows": 2, "symbols": 2}
        self.assertEqual({k: dry[k] for k in expected_counts}, expected_counts)
        self.assertEqual((dry["status"], dry["deleted_rows"]), ("dry_run", 0))
        self.assertEqual((done["status"], done["deleted_rows"], done["indicator_rows_deleted"]), ("done", 5, 2))
        self.assertEqual(rows, [("AAA", "2025-01-07"), ("AAA", "2025-01-08"), ("AAA", "2025-01-10"), ("BBB", "2025-01-08"), ("BBB", "2025-01-10"), ("CCC", "2025-01-08"), ("CCC", "2025-01-10")])
        self.assertEqual(indicators, [("CCC",)], "affected symbols lose their indicator rows for recalculation")
        self.assertEqual(again["status"], "nothing_to_do")

    def test_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = _write_rules(Path(tmp) / "config")
            self._db(data_dir)
            before = self._fingerprint(data_dir)
            with patch.object(ohlcv_cleanup, "delete_indicators", side_effect=RuntimeError("disk full")):
                with self.assertRaises(RuntimeError):
                    ohlcv_cleanup.run(self._args(data_dir, config, False))
            self.assertEqual(self._fingerprint(data_dir), before)

    def test_lock_held_and_bad_rules_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._db(data_dir)
            good = _write_rules(Path(tmp) / "good")
            bad = _write_rules(Path(tmp) / "bad", gaps="AAA 2025-01-11 Saturday\n")
            for config, held, code in ((good, True, 2), (bad, False, 1)):
                out = io.StringIO()
                with patch("sys.argv", ["src.ohlcv_cleanup", "--data-dir", str(data_dir), "--config-dir", str(config)]), contextlib.redirect_stdout(out):
                    if held:
                        with pipeline_lock(data_dir):
                            result = ohlcv_cleanup.main()
                    else:
                        result = ohlcv_cleanup.main()
                self.assertEqual(result, code)
                self.assertEqual(json.loads(out.getvalue())["status"], "lock_held" if held else "data_rules_invalid")


if __name__ == "__main__":
    unittest.main()
