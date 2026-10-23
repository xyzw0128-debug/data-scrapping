"""Twelve Data client error handling and the core collection flow with a fake HTTP layer."""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from src import main
from src.config import ProviderConfig
from src.rate_limit import KeyPool, PerMinuteRateLimiter, RunCreditCap
from src.twelve_data import BudgetExhaustedError, SymbolNotFoundError, TwelveDataClient, TwelveDataError, default_http_get


def _config() -> ProviderConfig:
    return ProviderConfig(
        name="twelve_data", enabled=True, api_key_env="TD", api_key_envs=("TD_1", "TD_2"),
        daily_limit=20, daily_reserve=2, per_minute_limit=0, base_url="https://api.twelvedata.com/time_series",
        interval="1day", outputsize=5000,
    )


def _client(http_get, cap: int | None = None, keys=("a", "b")) -> tuple[TwelveDataClient, dict, list]:
    state: dict = {}
    sleeps: list[float] = []
    env = {"TD_1": keys[0], "TD_2": keys[1] if len(keys) > 1 else "", "TD": ""}
    with patch.dict(os.environ, env, clear=False):
        pool = KeyPool.from_env(_config(), state)
    client = TwelveDataClient(_config(), pool, PerMinuteRateLimiter(0), RunCreditCap(cap), logging.getLogger("test"), http_get, sleeps.append)
    return client, state, sleeps


def _used(state: dict, index: int) -> int:
    return state["providers"][f"twelve_data:key{index}"]["calls_used_today"]


class TwelveDataClientTest(unittest.TestCase):
    def test_per_minute_limit_sleeps_and_retries_without_charging(self) -> None:
        responses = iter([(429, {"status": "error", "message": "You have run out of API credits for the current minute."}),
                          (200, {"status": "ok", "values": []})])
        client, state, sleeps = _client(lambda url: next(responses))
        client.time_series("AAPL", "splits")
        self.assertEqual(sleeps, [60])
        self.assertEqual(_used(state, 0), 1)

    def test_daily_limit_rotates_to_next_key(self) -> None:
        seen = []

        def http_get(url):
            key = parse_qs(urlparse(url).query)["apikey"][0]
            seen.append(key)
            if key == "a":
                return 429, {"status": "error", "message": "You have run out of API credits for the day."}
            return 200, {"status": "ok", "values": []}

        client, state, _ = _client(http_get)
        client.time_series("AAPL", "splits")
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(_used(state, 0), 20)
        self.assertEqual(_used(state, 1), 1)

    def test_not_found_is_charged_and_not_retried(self) -> None:
        calls = []
        client, state, sleeps = _client(lambda url: calls.append(url) or (404, {"status": "error", "message": "**symbol** not found: ZZZZ"}))
        with self.assertRaises(SymbolNotFoundError):
            client.time_series("ZZZZ", "splits")
        self.assertEqual(len(calls), 1)
        self.assertEqual(_used(state, 0), 1)
        self.assertEqual(sleeps, [])

    def test_forbidden_is_not_charged(self) -> None:
        client, state, _ = _client(lambda url: (403, {"status": "error", "message": "/dividends is available exclusively with grow"}))
        with self.assertRaises(TwelveDataError):
            client.request("dividends", {"symbol": "MSFT"})
        self.assertEqual(_used(state, 0), 0)

    def test_uncertain_attempts_are_charged(self) -> None:
        responses = iter([OSError("connection reset"), (200, {"status": "ok", "values": []})])

        def flaky(url):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        client, state, sleeps = _client(flaky, cap=10)
        client.time_series("AAPL", "splits")
        self.assertEqual(_used(state, 0), 2, "lost response + success")
        self.assertEqual(client.cap.used, 2)

        client, state, _ = _client(lambda url: (502, {"status": "error", "message": "bad gateway"}), cap=10)
        with self.assertRaises(TwelveDataError):
            client.time_series("AAPL", "splits")
        self.assertEqual((_used(state, 0), client.cap.used), (2, 2))

    def test_http_exceptions_are_uncertain_attempts(self) -> None:
        # IncompleteRead/BadStatusLine are http.client.HTTPException, not OSError.
        responses = iter([http.client.IncompleteRead(b"{\"val"), (200, {"status": "ok", "values": []})])

        def flaky(url):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        client, state, sleeps = _client(flaky, cap=10)
        self.assertEqual(client.time_series("AAPL", "splits"), {"status": "ok", "values": []})
        self.assertEqual((_used(state, 0), client.cap.used), (2, 2), "lost response + success")
        self.assertEqual(sleeps, [2.0])

        def always_bad(url):
            raise http.client.BadStatusLine("")

        client, state, _ = _client(always_bad, cap=10)
        with self.assertRaises(TwelveDataError) as ctx:
            client.time_series("AAPL", "splits")
        self.assertEqual(ctx.exception.uncertain_attempts, 2)
        self.assertEqual((_used(state, 0), client.cap.used), (2, 2))

    def test_http_error_with_unreadable_body_keeps_status(self) -> None:
        class LostBody(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b"")

        def fake_urlopen(status, body):
            def urlopen(request, timeout):
                raise HTTPError(request.full_url, status, "Bad Gateway" if status == 502 else "Not Found", {}, body)
            return urlopen

        with patch("src.twelve_data.urlopen", fake_urlopen(502, LostBody())):
            self.assertEqual(default_http_get("https://example.invalid/x"), (502, {"status": "error", "message": "Bad Gateway"}))
        with patch("src.twelve_data.urlopen", fake_urlopen(404, io.BytesIO(b'{"status": "error", "message": "not found"}'))):
            self.assertEqual(default_http_get("https://example.invalid/x"), (404, {"status": "error", "message": "not found"}))

    def test_usage_sync_failure_excludes_key(self) -> None:
        def http_get(url):
            query = parse_qs(urlparse(url).query)
            if "api_usage" in url:
                if query["apikey"][0] == "a":
                    raise OSError("timeout")
                return 200, {"daily_usage": 5}
            return 200, {"status": "ok", "values": []}

        client, state, sleeps = _client(http_get)
        results = client.sync_usage()
        self.assertEqual([r.get("excluded", False) for r in results], [True, False])
        self.assertEqual(client.key_pool.current_slot().api_key, "b")
        self.assertIn("usage_sync_failed", client.key_pool.summary()[0]["disabled_reason"])
        self.assertEqual(_used(state, 1), 5, "server usage already includes the sync call")
        self.assertIn(5.0, sleeps, "one retry before excluding the key")

    def test_usage_sync_skips_key_without_budget(self) -> None:
        calls = []
        client, state, _ = _client(lambda url: calls.append(url) or (200, {"daily_usage": 0}), keys=("a",))
        client.key_pool.active_slots()[0].budget.spend(18)
        self.assertEqual(client.sync_usage(), [{"key_index": 0, "skipped": "no_local_budget"}])
        self.assertEqual(calls, [])
        with self.assertRaises(BudgetExhaustedError):
            client.request("api_usage", {}, slot=client.key_pool.slots[0])

    def test_usage_sync_skips_keys_when_run_cap_is_spent(self) -> None:
        calls = []
        client, state, sleeps = _client(lambda url: calls.append(url) or (200, {"daily_usage": 0}), cap=3)
        client.cap.spend(3)  # an earlier phase of the same run used the whole cap
        with self.assertNoLogs("test", level="WARNING"):
            results = client.sync_usage()
        self.assertEqual(results, [{"key_index": 0, "skipped": "run_credit_cap"}, {"key_index": 1, "skipped": "run_credit_cap"}])
        self.assertEqual(calls, [], "no /api_usage call")
        self.assertEqual([k["disabled_reason"] for k in client.key_pool.summary() if k["has_key"]], [None, None])
        self.assertEqual((_used(state, 0), _used(state, 1), sleeps), (0, 0, []))
        self.assertFalse(client.can_afford(1), "the cap still blocks every paid call")

    def test_usage_sync_failure_keeps_reason_when_cap_blocks_retry(self) -> None:
        def http_get(url):
            raise OSError("timeout")

        client, state, _ = _client(http_get, cap=2)
        results = client.sync_usage()
        self.assertTrue(results[0]["excluded"])
        self.assertIn("timeout", results[0]["error"], "the network failure, not run_credit_cap, is the reason")
        self.assertEqual(results[1], {"key_index": 1, "skipped": "run_credit_cap"})
        self.assertEqual([k["disabled_reason"] is None for k in client.key_pool.summary() if k["has_key"]], [False, True])
        self.assertEqual((_used(state, 0), client.cap.used), (2, 2), "both lost attempts are charged")

    def test_run_cap_blocks_calls(self) -> None:
        client, _, _ = _client(lambda url: (200, {"status": "ok", "values": []}), cap=1)
        client.time_series("AAPL", "splits")
        self.assertFalse(client.can_afford(1))
        with self.assertRaises(BudgetExhaustedError):
            client.time_series("MSFT", "splits")


def _bars(prices: dict[str, float]) -> list[dict]:
    return [{"datetime": d, "open": str(p), "high": str(p), "low": str(p), "close": str(p), "volume": "100"} for d, p in sorted(prices.items(), reverse=True)]


@unittest.skipIf(importlib.util.find_spec("duckdb") is None, "duckdb dependency is not installed")
class CoreCollectionTest(unittest.TestCase):
    def test_core_symbol_replaces_rows_keeps_adj_close_and_derives_split(self) -> None:
        import duckdb  # type: ignore

        split_adjusted = {"2024-06-06": 120.998, "2024-06-07": 120.888, "2024-06-10": 121.79, "2024-06-11": 120.91, "2024-06-12": 125.0}
        raw = {"2024-06-06": 1209.98, "2024-06-07": 1208.88, "2024-06-10": 121.79, "2024-06-11": 120.91, "2024-06-12": 125.0}
        adjusted_all = {d: round(p * 0.997, 5) for d, p in split_adjusted.items()}
        series = {"splits": split_adjusted, "all": adjusted_all, "none": raw}

        def http_get(url):
            adjust = parse_qs(urlparse(url).query)["adjust"][0]
            return 200, {"status": "ok", "values": _bars(series[adjust])}

        client, _, _ = _client(http_get)
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            main.replace_symbol_prices(data_dir, "NVDA", [{"date": "1999-01-04", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"}])
            # 2024-06-12 is newer than the finished session and must not be stored.
            result = main.collect_core_symbol(client, data_dir, "NVDA", "2024-06-11")
            events = main.refresh_split_events(client, data_dir, "NVDA", "2024-06-11")
            con = duckdb.connect(str(data_dir / "db" / "market.db"), read_only=True)
            try:
                dates = [r[0] for r in con.execute("SELECT date FROM ohlcv WHERE symbol = 'NVDA' ORDER BY date").fetchall()]
                adj = con.execute("SELECT adj_close FROM ohlcv WHERE symbol = 'NVDA' AND date = '2024-06-07'").fetchone()[0]
                prices = con.execute("SELECT date, close, adj_close, splits FROM prices_daily WHERE symbol = 'NVDA' ORDER BY date").fetchall()
            finally:
                con.close()

        self.assertEqual(result, {
            "rows": 4, "last_bar_date": "2024-06-11", "adj_ok": True, "split_basis_changed": False,
            "dropped": {}, "patches": [], "vanished_dates": [],  # 1999-01-04 left the response window: not vanished
        })
        self.assertEqual([str(d) for d in dates], ["2024-06-06", "2024-06-07", "2024-06-10", "2024-06-11"], "old 1999 row and unfinished bar are gone")
        self.assertEqual(adj, adjusted_all["2024-06-07"])
        self.assertEqual(events, 1)
        self.assertEqual([(str(d), s) for d, _, _, s in prices], [("2024-06-06", 0.0), ("2024-06-07", 0.0), ("2024-06-10", 10.0), ("2024-06-11", 0.0)])

    def test_failed_second_call_leaves_existing_rows(self) -> None:
        import duckdb  # type: ignore

        def http_get(url):
            adjust = parse_qs(urlparse(url).query)["adjust"][0]
            if adjust == "all":
                return 500, {"status": "error", "message": "server error"}
            return 200, {"status": "ok", "values": _bars({"2024-06-11": 10.0})}

        client, _, _ = _client(http_get)
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            main.replace_symbol_prices(data_dir, "AAA", [{"date": "2024-06-10", "open": "9", "high": "9", "low": "9", "close": "9", "volume": "1"}])
            with self.assertRaises(TwelveDataError):
                main.collect_core_symbol(client, data_dir, "AAA", "2024-06-11")
            con = duckdb.connect(str(data_dir / "db" / "market.db"), read_only=True)
            try:
                rows = con.execute("SELECT date, close FROM ohlcv WHERE symbol = 'AAA'").fetchall()
            finally:
                con.close()
        self.assertEqual([(str(d), close) for d, close in rows], [("2024-06-10", 9.0)])

    def test_malformed_bar_fails_only_that_symbol_and_keeps_its_rows(self) -> None:
        import duckdb  # type: ignore

        def http_get(url):
            query = parse_qs(urlparse(url).query)
            bars = _bars({"2024-06-10": 10.0, "2024-06-11": 10.5})
            if query["symbol"][0] == "AAA" and query["adjust"][0] == "all":
                bars[0]["volume"] = "1.5"  # newest bar: not a whole number
            return 200, {"status": "ok", "values": bars}

        client, state, _ = _client(http_get, cap=20)
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            main.replace_symbol_prices(data_dir, "AAA", [{"date": "2024-06-07", "open": "9", "high": "9", "low": "9", "close": "9", "volume": "0"}])
            args = argparse.Namespace(dry_run=False, force=False, data_dir=data_dir, state=data_dir / "state" / "state.json")
            summary = {"started_at": "2024-06-12T00:00:00Z", "run_id": "test", "processed": 0, "failed": 0, "skipped_rate_limit": 0, "symbols": []}
            run = main.Run(args, state, summary, logging.getLogger("test"), client.cap)
            main.run_core(run, client, ["AAA", "BBB"], "2024-06-11")
            con = duckdb.connect(str(data_dir / "db" / "market.db"), read_only=True)
            try:
                rows = con.execute("SELECT symbol, CAST(date AS VARCHAR), volume FROM ohlcv ORDER BY symbol, date").fetchall()
            finally:
                con.close()
        self.assertEqual({k: summary["core"][k] for k in ("done", "failed")}, {"done": 1, "failed": 1})
        entry = state["symbols"]["AAA"]["twelve_data"]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("invalid volume '1.5' on 2024-06-11", entry["error"])
        self.assertIsNotNone(entry["next_retry_date"])
        self.assertEqual(rows, [("AAA", "2024-06-07", 0), ("BBB", "2024-06-10", 100), ("BBB", "2024-06-11", 100)], "AAA keeps its old row, zero volume included")

    def test_lost_response_fails_one_symbol_and_core_continues(self) -> None:
        def http_get(url):
            query = parse_qs(urlparse(url).query)
            if query["symbol"][0] == "AAA":
                raise http.client.IncompleteRead(b"")
            return 200, {"status": "ok", "values": _bars({"2024-06-11": 10.0})}

        client, state, _ = _client(http_get, cap=20)
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            args = argparse.Namespace(dry_run=False, force=False, data_dir=data_dir, state=data_dir / "state" / "state.json")
            summary = {"started_at": "2024-06-12T00:00:00Z", "run_id": "test", "processed": 0, "failed": 0, "skipped_rate_limit": 0, "symbols": []}
            run = main.Run(args, state, summary, logging.getLogger("test"), client.cap)
            main.run_core(run, client, ["AAA", "BBB"], "2024-06-11")
        self.assertEqual({k: summary["core"][k] for k in ("done", "failed")}, {"done": 1, "failed": 1})
        self.assertEqual(state["symbols"]["AAA"]["twelve_data"]["status"], "failed")
        self.assertEqual(state["symbols"]["BBB"]["twelve_data"]["core_session"], "2024-06-11")
        # AAA: 2 uncertain attempts; BBB: adjust=splits + adjust=all + adjust=none (split history).
        self.assertEqual(client.cap.used, 5)


class DryRunTest(unittest.TestCase):
    def test_dry_run_writes_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "core_etfs.txt").write_text("SPY\n", encoding="utf-8")
            (root / "config" / "symbols.txt").write_text("AAA\nBBB\n", encoding="utf-8")
            (root / "config" / "providers.yaml").write_text(
                "twelve_data:\n  enabled: true\n  api_key_env: TD\n  daily_limit: 800\n  daily_reserve: 100\n  max_symbols_per_run: 5\n", encoding="utf-8"
            )
            state_path = root / "data" / "state" / "state.json"
            state_path.parent.mkdir(parents=True)
            original = {"schema_version": 1, "providers": {}, "symbols": {}, "runs": []}
            state_path.write_text(json.dumps(original), encoding="utf-8")
            args = argparse.Namespace(
                provider="twelve_data", phase="all", symbols=root / "config" / "symbols.txt", config_dir=root / "config",
                providers=root / "config" / "providers.yaml", data_dir=root / "data", state=state_path, dry_run=True,
                max_symbols=None, max_credits=None, run_id="test", force=False, no_usage_sync=True,
            )
            summary = main.run_collection(args, logging.getLogger("test"))
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original)
        self.assertEqual(summary["core"]["pending"], 1)
        self.assertEqual(summary["rotation"]["sample"], ["AAA", "BBB"])


if __name__ == "__main__":
    unittest.main()
