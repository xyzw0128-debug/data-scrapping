"""Twelve Data daily OHLCV collector: core symbols every session, the rest in rotation.

Phases (``--phase``):
- core: ETFs + S&P 500 (+ config/core_extra.txt). Each symbol takes adjust=splits and
  adjust=all full histories and replaces its rows in one transaction; then symbols without
  split history (or whose split basis changed) get adjust=none to derive split events.
- rotation: the NASDAQ list and former core members, never-collected first, then the
  stalest. One adjust=splits call per symbol.

Every call goes through the key budgets, the per-run credit cap, and the per-minute limit.
Bars newer than the latest finished session (an unfinished intraday bar) are never stored.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from src.config import load_provider_config
from src.corporate_actions import split_basis_changed, split_events
from src.env import load_project_env
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.logging_utils import setup_logging
from src.market_calendar import expected_session
from src.rate_limit import KeyPool, PerMinuteRateLimiter, RunCreditCap
from src.scheduling import choose_rotation_symbols, provider_entry, record_failure, record_skip, record_success
from src.state import load_state, save_state, utc_now_iso, utc_today
from src.storage import (
    ensure_data_dirs,
    ensure_db_schema,
    load_closes,
    normalize_twelve_data_ohlcv,
    replace_split_events,
    replace_symbol_prices,
    save_raw_latest,
    save_run_summary,
    verify_writable,
)
from src.twelve_data import BudgetExhaustedError, SymbolNotFoundError, TwelveDataClient, TwelveDataError
from src.universe import load_universe


ROOT = Path(__file__).resolve().parents[1]
PROVIDER = "twelve_data"
SPLIT_SOURCE = "twelve_data_adjust_ratio"
RUN_ID_ENV = "DATA_SCRAPPING_RUN_ID"
CORE_SYMBOL_CREDITS = 2
SAVE_EVERY = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Twelve Data daily OHLCV for core and rotation symbols.")
    parser.add_argument("--provider", default=PROVIDER, help="Provider name from config/providers.yaml")
    parser.add_argument("--phase", choices=["core", "rotation", "all"], default="all")
    parser.add_argument("--symbols", type=Path, default=ROOT / "config" / "symbols.txt", help="NASDAQ rotation list")
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--providers", type=Path, default=ROOT / "config" / "providers.yaml")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "state" / "state.json")
    parser.add_argument("--dry-run", action="store_true", help="Plan the run without API calls or any state/DB writes")
    parser.add_argument("--max-symbols", type=int, default=None, help="Rotation symbols per run (default: max_symbols_per_run)")
    parser.add_argument("--max-credits", type=int, default=None, help="Credits per pipeline run (default: max_credits_per_run; 0 = no cap)")
    parser.add_argument("--run-id", default=None, help=f"Pipeline run id shared by phases (default: ${RUN_ID_ENV} or a new id)")
    parser.add_argument("--only", action="append", help="Restrict both phases to this symbol; can be used multiple times")
    parser.add_argument("--force", action="store_true", help="Collect symbols even if they already succeeded for this session/day")
    parser.add_argument("--no-usage-sync", action="store_true", help="Skip reading server-side usage (/api_usage, 1 credit per key)")
    parser.add_argument("--skip-lock", action="store_true", help="Disable the pipeline lock, useful only for local debugging")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def bars_until(payload: dict, session: str) -> list[dict]:
    """Validated daily bars dated on or before the latest finished session.

    A malformed bar anywhere in the response raises ValueError, which fails only this symbol
    (normal failure/retry bookkeeping) and leaves its stored rows untouched.
    """
    return [row for row in normalize_twelve_data_ohlcv(payload) if row["date"] <= session]


def _close_map(rows: list[dict]) -> dict[str, float]:
    closes: dict[str, float] = {}
    for row in rows:
        try:
            closes[row["date"]] = float(row["close"])
        except (TypeError, ValueError):
            continue
    return closes


def collect_core_symbol(client: TwelveDataClient, data_dir: Path, symbol: str, session: str) -> dict:
    """Fetch split-adjusted bars and adj_close for one core symbol and replace its rows.

    Nothing is written unless both calls succeed, so a failure leaves the old data intact.
    """
    if not client.can_afford(CORE_SYMBOL_CREDITS):
        raise BudgetExhaustedError("insufficient_credits_for_core_symbol")
    split_payload = client.time_series(symbol, "splits")
    save_raw_latest(data_dir, f"{PROVIDER}/splits", symbol, split_payload)
    rows = bars_until(split_payload, session)
    if not rows:
        raise ValueError(f"No OHLCV rows on or before {session}")
    all_payload = client.time_series(symbol, "all")
    save_raw_latest(data_dir, f"{PROVIDER}/all", symbol, all_payload)
    adj_closes = {row["date"]: row["close"] for row in bars_until(all_payload, session)}

    stored = load_closes(data_dir, symbol)
    changed = bool(stored) and split_basis_changed(stored, _close_map(rows))
    replace_symbol_prices(data_dir, symbol, rows, adj_closes)
    last_bar = rows[-1]["date"]
    return {"rows": len(rows), "last_bar_date": last_bar, "adj_ok": last_bar in adj_closes, "split_basis_changed": changed}


def refresh_split_events(client: TwelveDataClient, data_dir: Path, symbol: str, session: str) -> int:
    """Derive split events from adjust=none vs the stored split-adjusted closes."""
    payload = client.time_series(symbol, "none")
    save_raw_latest(data_dir, f"{PROVIDER}/none", symbol, payload)
    raw = _close_map(bars_until(payload, session))
    events = split_events(raw, load_closes(data_dir, symbol))
    replace_split_events(data_dir, symbol, events, SPLIT_SOURCE)
    return len(events)


def collect_rotation_symbol(client: TwelveDataClient, data_dir: Path, symbol: str, session: str) -> dict:
    payload = client.time_series(symbol, "splits")
    save_raw_latest(data_dir, f"{PROVIDER}/splits", symbol, payload)
    rows = bars_until(payload, session)
    if not rows:
        raise ValueError(f"No OHLCV rows on or before {session}")
    replace_symbol_prices(data_dir, symbol, rows)
    return {"rows": len(rows), "last_bar_date": rows[-1]["date"]}


class Run:
    """Mutable bookkeeping for one collector invocation."""

    def __init__(self, args: argparse.Namespace, state: dict, summary: dict, logger: logging.Logger, cap: RunCreditCap):
        self.args = args
        self.state = state
        self.summary = summary
        self.logger = logger
        self.cap = cap
        self.handled = 0

    def persist(self, last_symbol: str | None = None) -> None:
        self.state["current_run"] = {
            "started_at": self.summary["started_at"],
            "run_id": self.summary["run_id"],
            "phase": self.summary["current_phase"],
            "planned": self.summary.get("current_planned"),
            "processed": self.summary["processed"],
            "failed": self.summary["failed"],
            "skipped_rate_limit": self.summary["skipped_rate_limit"],
            "last_symbol": last_symbol,
            "updated_at": utc_now_iso(),
        }
        self.state["pipeline_run"] = {"run_id": self.summary["run_id"], "credits_used": self.cap.used, "updated_at": utc_now_iso()}
        if not self.args.dry_run:
            save_state(self.args.state, self.state)

    def tick(self, symbol: str) -> None:
        self.handled += 1
        if self.handled % SAVE_EVERY == 0:
            self.persist(symbol)
        else:
            self.state.setdefault("current_run", {})["last_symbol"] = symbol

    def ok(self, bucket: dict, symbol: str, status: str = "done", **detail: object) -> None:
        bucket["done"] += 1
        self.summary["processed"] += 1
        self.summary["symbols"].append({"symbol": symbol, "status": status, **detail})

    def fail(self, bucket: dict, entry: dict, symbol: str, exc: Exception) -> None:
        not_found = isinstance(exc, SymbolNotFoundError)
        record_failure(entry, utc_today(), str(exc), not_found=not_found)
        bucket["failed"] += 1
        self.summary["failed"] += 1
        self.summary["symbols"].append({"symbol": symbol, "status": "not_found" if not_found else "failed", "error": str(exc)})
        self.logger.warning("Failed to collect %s: %s", symbol, exc)

    def skip(self, bucket: dict, entry: dict, symbol: str, reason: str) -> None:
        record_skip(entry, reason)
        bucket["skipped"] += 1
        self.summary["skipped_rate_limit"] += 1
        self.logger.warning("Stopping %s at %s: %s", self.summary["current_phase"], symbol, reason)


def run_core(run: Run, client: TwelveDataClient, core: list[str], session: str) -> None:
    args, state = run.args, run.state
    today = utc_today()
    pending = [s for s in core if args.force or provider_entry(state, s, PROVIDER).get("core_session") != session]
    bucket = run.summary["core"] = {"expected": len(core), "pending": len(pending), "done": 0, "failed": 0, "skipped": 0}
    run.summary["current_phase"] = "core"
    run.summary["current_planned"] = len(pending)
    if args.dry_run:
        run.summary["core"]["sample"] = pending[:10]
        return

    run.persist()
    for symbol in pending:
        entry = provider_entry(state, symbol, PROVIDER)
        try:
            result = collect_core_symbol(client, args.data_dir, symbol, session)
        except BudgetExhaustedError as exc:
            run.skip(bucket, entry, symbol, str(exc))
            break
        except (TwelveDataError, ValueError) as exc:
            run.fail(bucket, entry, symbol, exc)
        else:
            complete = result["last_bar_date"] == session and result["adj_ok"]
            record_success(
                entry,
                today,
                rows=result["rows"],
                last_bar_date=result["last_bar_date"],
                core_session=session if complete else None,
                note=None if complete else f"last bar {result['last_bar_date']} / adj_ok={result['adj_ok']}",
            )
            if result["split_basis_changed"]:
                entry["splits_stale"] = True
                run.logger.info("Split basis changed for %s; split events will be recalculated", symbol)
            run.ok(bucket, symbol, rows=result["rows"], last_bar_date=result["last_bar_date"])
        run.tick(symbol)

    # Split history: changed basis first, then symbols never checked; only symbols refreshed today.
    split_bucket = run.summary["splits"] = {"pending": 0, "done": 0, "failed": 0, "skipped": 0, "events": 0}
    candidates = []
    for symbol in core:
        entry = provider_entry(state, symbol, PROVIDER)
        if entry.get("last_success_date") != today:
            continue
        if entry.get("splits_stale"):
            candidates.append((0, symbol))
        elif not entry.get("splits_checked_at"):
            candidates.append((1, symbol))
    candidates.sort()
    split_bucket["pending"] = len(candidates)
    for _, symbol in candidates:
        entry = provider_entry(state, symbol, PROVIDER)
        try:
            count = refresh_split_events(client, args.data_dir, symbol, session)
        except BudgetExhaustedError as exc:
            split_bucket["skipped"] = len(candidates) - split_bucket["done"] - split_bucket["failed"]
            run.logger.warning("Stopping split history at %s: %s", symbol, exc)
            break
        except (TwelveDataError, ValueError) as exc:
            split_bucket["failed"] += 1
            run.logger.warning("Split history failed for %s: %s", symbol, exc)
        else:
            entry.update({"splits_checked_at": utc_now_iso(), "splits_stale": False, "split_events": count})
            split_bucket["done"] += 1
            split_bucket["events"] += count
        run.tick(symbol)


def run_rotation(run: Run, client: TwelveDataClient, pool: list[str], exclude: set[str], session: str, max_symbols: int | None) -> None:
    args, state = run.args, run.state
    today = utc_today()
    candidates = choose_rotation_symbols(pool, state, PROVIDER, max_symbols, today, args.force, exclude)
    bucket = run.summary["rotation"] = {"pool": len(pool), "candidates": len(candidates), "done": 0, "failed": 0, "skipped": 0}
    run.summary["current_phase"] = "rotation"
    run.summary["current_planned"] = len(candidates)
    if args.dry_run:
        run.summary["rotation"]["sample"] = candidates[:10]
        return

    run.persist()
    for symbol in candidates:
        entry = provider_entry(state, symbol, PROVIDER)
        try:
            result = collect_rotation_symbol(client, args.data_dir, symbol, session)
        except BudgetExhaustedError as exc:
            run.skip(bucket, entry, symbol, str(exc))
            break
        except (TwelveDataError, ValueError) as exc:
            run.fail(bucket, entry, symbol, exc)
        else:
            record_success(entry, today, rows=result["rows"], last_bar_date=result["last_bar_date"], note=None)
            run.ok(bucket, symbol, rows=result["rows"], last_bar_date=result["last_bar_date"])
        run.tick(symbol)


def run_collection(args: argparse.Namespace, logger: logging.Logger) -> dict:
    ensure_data_dirs(args.data_dir)
    verify_writable(args.data_dir)
    verify_writable(args.data_dir / "state")
    if not args.dry_run:
        ensure_db_schema(args.data_dir)  # an unconverted schema stops the run before any paid call

    config = load_provider_config(args.providers, args.provider)
    if config.name != PROVIDER:
        raise ValueError("src.main collects only the twelve_data provider")
    universe = load_universe(args.data_dir, args.config_dir, args.symbols)
    core = universe.core_symbols()
    rotation_pool = universe.rotation_symbols()
    only = {symbol.strip().upper() for symbol in (getattr(args, "only", None) or [])}
    if only:
        core = [symbol for symbol in core if symbol in only]
        rotation_pool = [symbol for symbol in rotation_pool if symbol in only]
    session = expected_session(datetime.now(UTC)).isoformat()

    state = load_state(args.state)
    if args.dry_run:
        state = copy.deepcopy(state)  # dry-run never writes state

    run_id = args.run_id or os.environ.get(RUN_ID_ENV) or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    previous = state.get("pipeline_run") or {}
    already_used = int(previous.get("credits_used", 0)) if previous.get("run_id") == run_id else 0
    limit = args.max_credits if args.max_credits is not None else config.max_credits_per_run
    cap = RunCreditCap(limit if limit and limit > 0 else None, already_used)
    key_pool = KeyPool.from_env(config, state)
    client = TwelveDataClient(config, key_pool, PerMinuteRateLimiter(config.per_minute_limit), cap, logger)

    summary: dict = {
        "started_at": utc_now_iso(),
        "provider": config.name,
        "run_id": run_id,
        "phase": args.phase,
        "current_phase": args.phase,
        "session_date": session,
        "dry_run": args.dry_run,
        "force": args.force,
        "processed": 0,
        "failed": 0,
        "skipped_rate_limit": 0,
        "credits": {"cap": cap.limit, "used_before": already_used},
        "symbols": [],
    }
    run = Run(args, state, summary, logger, cap)
    if not args.dry_run and not args.no_usage_sync:
        summary["usage_sync"] = client.sync_usage()
        if not key_pool.active_slots():
            logger.error("No Twelve Data key passed the usage check; nothing will be collected this run")
    summary["key_pool_start"] = key_pool.summary()
    logger.info(
        "Starting %s phase=%s session=%s core=%s credits_cap=%s used_before=%s remaining_keys=%s dry_run=%s",
        run_id, args.phase, session, len(core), cap.limit, already_used, key_pool.total_remaining(), args.dry_run,
    )

    try:
        if args.phase in ("core", "all"):
            run_core(run, client, core, session)
        if args.phase in ("rotation", "all"):
            max_symbols = args.max_symbols if args.max_symbols is not None else (config.max_symbols_per_run or None)
            exclude = set(universe.core_symbols()) | set(universe.delisted)
            run_rotation(run, client, rotation_pool, exclude, session, max_symbols)
    finally:
        summary["finished_at"] = utc_now_iso()
        summary["credits"]["used_this_invocation"] = cap.used - already_used
        summary["credits"]["used_run_total"] = cap.used
        summary["key_pool_end"] = key_pool.summary()
        run.persist()
        state.setdefault("runs", []).append({k: v for k, v in summary.items() if k != "symbols"})
        state["runs"] = state["runs"][-20:]
        state.pop("current_run", None)
        if not args.dry_run:
            save_state(args.state, state)

    summary["summary_path"] = str(save_run_summary(args.data_dir, summary))
    logger.info(
        "Finished %s processed=%s failed=%s skipped=%s credits=%s",
        args.phase, summary["processed"], summary["failed"], summary["skipped_rate_limit"], summary["credits"],
    )
    return summary


def main() -> int:
    load_project_env(ROOT)
    args = parse_args()
    logger = setup_logging(args.data_dir / "logs", args.verbose)
    try:
        if args.skip_lock:
            summary = run_collection(args, logger)
        else:
            with pipeline_lock(args.data_dir):
                summary = run_collection(args, logger)
    except LockHeldError as exc:
        logger.error("%s", exc)
        return lock_held_exit(exc)

    printable = {k: v for k, v in summary.items() if k != "symbols"}
    printable["failures"] = [item for item in summary["symbols"] if item["status"] != "done"][:50]
    print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if summary.get("core", {}).get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
