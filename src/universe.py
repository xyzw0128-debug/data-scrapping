"""Symbol universe: core symbols collected daily, rotation pool, and delisted marks.

Core = config/core_etfs.txt + current S&P 500 members + optional config/core_extra.txt.
S&P 500 membership comes from the same public CSV stockpred uses (fja05680/sp500), so
both programs agree on the universe. Members that leave the index move to ``former_core``
and keep being collected in rotation. Symbols absent from Twelve Data's US stock and ETF
catalogs are marked delisted: they are no longer requested, and their rows are kept.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from src.config import load_provider_config, load_symbols
from src.env import load_project_env
from src.fetch_tickers import atomic_write_symbols, extract_symbols
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.state import load_state, utc_now_iso


ROOT = Path(__file__).resolve().parents[1]
SP500_HIST_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)
VALID_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
# On the first refresh, members removed this many days before the latest change seed former_core.
FORMER_CORE_BOOTSTRAP_DAYS = 365
# Fewer S&P 500 members than this means the universe was never refreshed (or the CSV was bad);
# the snapshot then refuses to call a run complete.
MIN_SP500_MEMBERS = 490
# A catalog smaller than this is treated as a bad response and delisting is skipped.
MIN_STOCK_CATALOG = 5000
MIN_ETF_CATALOG = 1000


def universe_path(data_dir: Path) -> Path:
    return data_dir / "state" / "universe.json"


def sp500_cache_path(data_dir: Path) -> Path:
    return data_dir / "state" / "sp500_hist.csv"


def _read_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [symbol.strip().upper() for symbol in load_symbols(path) if symbol.strip()]


def yahoo_symbol(symbol: str) -> str:
    """Twelve Data/Finnhub use BRK.B; yfinance uses BRK-B."""
    return symbol.replace(".", "-")


@dataclass
class Universe:
    etfs: list[str]
    sp500: list[str]
    extra: list[str]
    nasdaq: list[str]
    sp500_as_of: str | None = None
    former_core: dict[str, dict] = field(default_factory=dict)
    delisted: dict[str, dict] = field(default_factory=dict)
    refreshed_at: str | None = None

    def core_symbols(self) -> list[str]:
        return list(dict.fromkeys([*self.etfs, *self.sp500, *self.extra]))

    def group_of(self, symbol: str) -> str:
        if symbol in self.etfs:
            return "core_etf"
        if symbol in self.sp500:
            return "core_sp500"
        if symbol in self.extra:
            return "core_extra"
        if symbol in self.former_core:
            return "former_core"
        return "rotation"

    def rotation_symbols(self) -> list[str]:
        """NASDAQ list plus former core members, minus core and delisted symbols."""
        core = set(self.core_symbols())
        pool = [*self.nasdaq, *sorted(self.former_core)]
        return [s for s in dict.fromkeys(pool) if s not in core and s not in self.delisted]

    def all_symbols(self) -> list[str]:
        return list(dict.fromkeys([*self.core_symbols(), *self.nasdaq, *sorted(self.former_core), *sorted(self.delisted)]))


def parse_sp500_history(text: str) -> list[tuple[str, list[str]]]:
    """Return [(change date, sorted tickers)] from the fja05680 CSV, oldest first."""
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        tickers = sorted({t.strip().upper() for t in str(row.get("tickers", "")).split(",") if t.strip()})
        rows.append((str(row["date"]), tickers))
    rows.sort(key=lambda item: item[0])
    return rows


def recent_removals(history: list[tuple[str, list[str]]], days: int) -> dict[str, str]:
    """Tickers removed within ``days`` of the latest change and not members now -> removal date."""
    if not history:
        return {}
    latest_date, latest = history[-1]
    cutoff = (date.fromisoformat(latest_date) - timedelta(days=days)).isoformat()
    current = set(latest)
    removed: dict[str, str] = {}
    for (_, before), (changed_on, after) in zip(history, history[1:]):
        if changed_on < cutoff:
            continue
        for ticker in set(before) - set(after):
            if ticker not in current:
                removed[ticker] = changed_on
    return removed


def load_universe(data_dir: Path, config_dir: Path = ROOT / "config", symbols_path: Path | None = None) -> Universe:
    """Build the universe from config files and the last refresh (data/state/universe.json)."""
    saved = {}
    path = universe_path(data_dir)
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
    sp500 = saved.get("sp500", {})
    members = list(sp500.get("symbols", []))
    as_of = sp500.get("as_of")
    if not members and sp500_cache_path(data_dir).exists():
        history = parse_sp500_history(sp500_cache_path(data_dir).read_text(encoding="utf-8"))
        if history:
            as_of, members = history[-1]
    return Universe(
        etfs=_read_list(config_dir / "core_etfs.txt"),
        sp500=members,
        extra=_read_list(config_dir / "core_extra.txt"),
        nasdaq=[s for s in _read_list(symbols_path or config_dir / "symbols.txt") if VALID_TICKER.match(s)],
        sp500_as_of=as_of,
        former_core=dict(saved.get("former_core", {})),
        delisted=dict(saved.get("delisted", {})),
        refreshed_at=saved.get("refreshed_at"),
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def save_universe(data_dir: Path, universe: Universe, extra: dict) -> None:
    payload = {
        "schema_version": 1,
        "refreshed_at": universe.refreshed_at,
        "sp500": {"as_of": universe.sp500_as_of, "symbols": universe.sp500, "source": SP500_HIST_URL},
        "former_core": universe.former_core,
        "delisted": universe.delisted,
        **extra,
    }
    _atomic_write_text(universe_path(data_dir), json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "data-scrapping/0.2"})
    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def refresh_universe(
    data_dir: Path,
    config_dir: Path,
    catalog: Callable[[str, dict], dict] | None,
    download: Callable[[str], str] = fetch_text,
    dry_run: bool = False,
    tracked_extra: set[str] | None = None,
) -> dict:
    """Refresh S&P 500 membership, the NASDAQ list, and delisted marks.

    ``catalog(endpoint, params)`` returns a Twelve Data reference payload (/stocks, /etfs);
    pass None to skip the NASDAQ list and delisting checks.
    """
    universe = load_universe(data_dir, config_dir)
    summary: dict = {"dry_run": dry_run}
    previous_members = set(universe.sp500)
    previous_symbols = set(universe.all_symbols())

    text = download(SP500_HIST_URL)
    history = parse_sp500_history(text)
    if not history:
        raise ValueError("S&P 500 history CSV has no rows")
    as_of, members = history[-1]
    if previous_members:
        removed = {ticker: as_of for ticker in previous_members - set(members)}
    else:
        removed = recent_removals(history, FORMER_CORE_BOOTSTRAP_DAYS)
    for ticker, removed_on in removed.items():
        universe.former_core.setdefault(ticker, {"removed_as_of": removed_on, "detected_at": utc_now_iso()})
    for ticker in members:
        universe.former_core.pop(ticker, None)
    summary["sp500"] = {
        "as_of": as_of,
        "count": len(members),
        "added": sorted(set(members) - previous_members) if previous_members else [],
        "removed": sorted(removed),
    }
    universe.sp500, universe.sp500_as_of = members, as_of

    if catalog is not None:
        nasdaq = [s for s in extract_symbols(catalog("stocks", {"exchange": "NASDAQ"})) if VALID_TICKER.match(s)]
        summary["nasdaq"] = {"count": len(nasdaq), "added": len(set(nasdaq) - set(universe.nasdaq)), "removed": len(set(universe.nasdaq) - set(nasdaq))}
        stocks = set(extract_symbols(catalog("stocks", {"country": "United States"})))
        etfs = set(extract_symbols(catalog("etfs", {"country": "United States"})))
        if len(stocks) < MIN_STOCK_CATALOG or len(etfs) < MIN_ETF_CATALOG:
            summary["delisted"] = {"skipped": f"catalog too small (stocks={len(stocks)}, etfs={len(etfs)})"}
        else:
            listed = stocks | etfs
            tracked = previous_symbols | set(nasdaq) | set(members) | set(universe.etfs) | (tracked_extra or set())
            newly = sorted(s for s in tracked if VALID_TICKER.match(s) and s not in listed and s not in universe.delisted)
            relisted = sorted(s for s in universe.delisted if s in listed)
            for symbol in newly:
                universe.delisted[symbol] = {"marked_at": utc_now_iso(), "reason": "absent_from_twelve_data_us_catalog"}
            for symbol in relisted:
                universe.delisted.pop(symbol, None)
            summary["delisted"] = {"newly_marked": len(newly), "relisted": relisted, "total": len(universe.delisted), "sample": newly[:20]}
        universe.nasdaq = nasdaq

    universe.refreshed_at = utc_now_iso()
    summary["core_count"] = len(universe.core_symbols())
    summary["rotation_count"] = len(universe.rotation_symbols())
    if not dry_run:
        _atomic_write_text(sp500_cache_path(data_dir), text)
        if catalog is not None:
            atomic_write_symbols(config_dir / "symbols.txt", universe.nasdaq)
        save_universe(data_dir, universe, {})
    return summary


def is_stale(universe: Universe, max_age_days: float, now: datetime | None = None) -> bool:
    if not universe.refreshed_at:
        return True
    refreshed = datetime.fromisoformat(universe.refreshed_at.replace("Z", "+00:00"))
    return (now or datetime.now(UTC)) - refreshed >= timedelta(days=max_age_days)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the core/rotation symbol universe.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--providers", type=Path, default=ROOT / "config" / "providers.yaml")
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "state" / "state.json")
    parser.add_argument("--max-age-days", type=float, default=7.0, help="Refresh only when the last refresh is older than this")
    parser.add_argument("--force", action="store_true", help="Refresh regardless of age")
    parser.add_argument("--skip-catalog", action="store_true", help="Refresh S&P 500 only (no Twelve Data calls)")
    parser.add_argument("--dry-run", action="store_true", help="Report whether a refresh is due, without network calls or writes")
    parser.add_argument("--check", action="store_true", help="Fetch and compare like a refresh, without writing files")
    return parser.parse_args()


def main() -> int:
    load_project_env(ROOT)
    args = parse_args()
    logger = logging.getLogger("collector")
    try:
        with pipeline_lock(args.data_dir):
            universe = load_universe(args.data_dir, args.config_dir)
            due = args.force or is_stale(universe, args.max_age_days)
            if args.dry_run:
                result = {
                    "status": "dry_run",
                    "refresh_due": due,
                    "refreshed_at": universe.refreshed_at,
                    "sp500_count": len(universe.sp500),
                    "sp500_as_of": universe.sp500_as_of,
                    "core_count": len(universe.core_symbols()),
                    "rotation_count": len(universe.rotation_symbols()),
                    "delisted_count": len(universe.delisted),
                }
            elif not due and not args.check:
                result = {"status": "fresh", "refreshed_at": universe.refreshed_at, "core_count": len(universe.core_symbols())}
            else:
                catalog = None
                tracked: set[str] = set()
                if not args.skip_catalog:
                    from src.rate_limit import KeyPool, PerMinuteRateLimiter, RunCreditCap
                    from src.twelve_data import TwelveDataClient

                    config = load_provider_config(args.providers, "twelve_data")
                    state = load_state(args.state)
                    tracked = {s for s, entry in state.get("symbols", {}).items() if isinstance(entry, dict) and "twelve_data" in entry}
                    client = TwelveDataClient(config, KeyPool.from_env(config, state), PerMinuteRateLimiter(config.per_minute_limit), RunCreditCap(None), logger)
                    catalog = client.request
                refreshed = refresh_universe(args.data_dir, args.config_dir, catalog, dry_run=args.check, tracked_extra=tracked)
                result = {"status": "checked" if args.check else "refreshed", **refreshed}
    except LockHeldError as exc:
        return lock_held_exit(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
