"""Local technical indicator calculation from OHLCV rows stored in DuckDB.

Every OHLCV writer deletes a symbol's indicator rows from the earliest date whose close it
changed (storage._drop_indicators_from), and an indicator at a date depends only on closes up to
that date. So the default incremental run fills only the dates without an indicator row: it
calculates the symbol's whole series and inserts the missing tail. ``--full`` and ``--symbol``
rewrite every row of their symbols.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path

from src.config import load_symbols
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.state import utc_now_iso
from src.storage import (
    INDICATOR_COLUMNS,
    bulk_upsert,
    connect_db,
    ensure_data_dirs,
    ensure_db_schema,
    indicator_values,
    load_closes_for_symbols,
    load_ohlcv_rows_db,
    save_indicator_rows_db,
)


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calculate local indicators from OHLCV rows in DuckDB.")
    parser.add_argument("--symbols", type=Path, default=None, help="Restrict to this symbol file (default: every symbol with OHLCV)")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--symbol", action="append", help="Calculate only this symbol; can be used multiple times")
    parser.add_argument("--full", action="store_true", help="Recalculate every symbol instead of only symbols whose OHLCV changed")
    parser.add_argument("--batch-size", type=int, default=200, help="Symbols loaded and written per DuckDB transaction")
    return parser.parse_args()


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_")


def _parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_value(value: float | None) -> float | None:
    """Indicator value stored as DOUBLE, rounded to 6 decimals; None (warm-up rows) stays NULL."""
    if value is None:
        return None
    return round(value, 6)



def calculate_sma(values: list[float | None], period: int) -> list[float | None]:
    """Calculate simple moving averages, summing each window afresh.

    A running sum would be O(N) instead of O(N x period), but its rounding differs in the last
    bits and flips the 6-decimal rounding of stored values (2026-11-24 rehearsal: 14,712 of
    about 1 million rows for 200 symbols) while saving only about 8 seconds a day. Fresh sums
    keep new rows identical to a full recalculation of the stored ones.
    """
    result: list[float | None] = []
    window: deque[float] = deque()
    for value in values:
        if value is None:
            result.append(None)
            window.clear()
            continue
        window.append(value)
        if len(window) > period:
            window.popleft()
        result.append(sum(window) / period if len(window) == period else None)
    return result


def calculate_ema(values: list[float | None], period: int) -> list[float | None]:
    """Calculate an EMA seeded from the first complete SMA window."""
    result: list[float | None] = [None] * len(values)
    multiplier = 2 / (period + 1)
    valid_window: list[float] = []
    previous_ema: float | None = None

    for index, value in enumerate(values):
        if value is None:
            valid_window.clear()
            previous_ema = None
            continue
        if previous_ema is None:
            valid_window.append(value)
            if len(valid_window) == period:
                previous_ema = sum(valid_window) / period
                result[index] = previous_ema
            continue
        previous_ema = (value - previous_ema) * multiplier + previous_ema
        result[index] = previous_ema
    return result


def calculate_rsi(values: list[float | None], period: int = 14) -> list[float | None]:
    """Calculate Wilder RSI for a close-price series."""
    result: list[float | None] = [None] * len(values)
    gains: list[float] = []
    losses: list[float] = []
    avg_gain: float | None = None
    avg_loss: float | None = None
    previous: float | None = None

    for index, value in enumerate(values):
        if value is None:
            previous = None
            gains.clear()
            losses.clear()
            avg_gain = None
            avg_loss = None
            continue
        if previous is None:
            previous = value
            continue

        delta = value - previous
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        previous = value

        if avg_gain is None or avg_loss is None:
            gains.append(gain)
            losses.append(loss)
            if len(gains) == period:
                avg_gain = sum(gains) / period
                avg_loss = sum(losses) / period
            else:
                continue
        else:
            avg_gain = ((avg_gain * (period - 1)) + gain) / period
            avg_loss = ((avg_loss * (period - 1)) + loss) / period

        if avg_loss == 0:
            result[index] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[index] = 100 - (100 / (1 + rs))
    return result


def calculate_macd(
    values: list[float | None],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Calculate MACD, signal, and histogram series."""
    fast = calculate_ema(values, fast_period)
    slow = calculate_ema(values, slow_period)
    macd = [fast_value - slow_value if fast_value is not None and slow_value is not None else None for fast_value, slow_value in zip(fast, slow)]
    signal = calculate_ema(macd, signal_period)
    hist = [macd_value - signal_value if macd_value is not None and signal_value is not None else None for macd_value, signal_value in zip(macd, signal)]
    return macd, signal, hist


def add_indicators(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    """Return one {"date", indicator columns...} row per OHLCV row (None where undefined)."""
    closes = [_parse_float(row.get("close", "")) for row in rows]
    sma_20 = calculate_sma(closes, 20)
    sma_50 = calculate_sma(closes, 50)
    sma_200 = calculate_sma(closes, 200)
    rsi_14 = calculate_rsi(closes, 14)
    macd, macd_signal, macd_hist = calculate_macd(closes)

    output: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        output.append(
            {
                "date": row.get("date", ""),
                "sma_20": _round_value(sma_20[index]),
                "sma_50": _round_value(sma_50[index]),
                "sma_200": _round_value(sma_200[index]),
                "rsi_14": _round_value(rsi_14[index]),
                "macd_12_26": _round_value(macd[index]),
                "macd_signal_9": _round_value(macd_signal[index]),
                "macd_hist": _round_value(macd_hist[index]),
            }
        )
    return output



def calculate_for_symbol(data_dir: Path, symbol: str) -> tuple[str, int, str | None]:
    """Calculate indicators for a symbol, returning status, row count, and output path."""
    rows = load_ohlcv_rows_db(data_dir, symbol)
    if not rows:
        return "missing_ohlcv", 0, None
    output_path = save_indicator_rows_db(data_dir, symbol, add_indicators(rows))
    return "done", len(rows), str(output_path)


def find_stale_symbols(con, symbols: list[str]) -> list[str]:
    """Return symbols with OHLCV dates that have no indicator row, in input order.

    Indicator dates are always a subset of OHLCV dates (writers delete indicators with the
    prices they change), so a different row count or last date means missing indicator rows.
    """
    stale = {
        row[0]
        for row in con.execute(
            """
            SELECT o.symbol
            FROM (SELECT symbol, count(*) AS n, max(date) AS last_date FROM ohlcv GROUP BY symbol) o
            LEFT JOIN (SELECT symbol, count(*) AS n, max(date) AS last_date FROM indicators GROUP BY symbol) i USING (symbol)
            WHERE i.n IS NULL OR i.n <> o.n OR i.last_date <> o.last_date
            """
        ).fetchall()
    }
    return [symbol for symbol in symbols if symbol in stale]


def _missing_tail(rows: list[dict[str, object]], stored_count: int, stored_last: str | None) -> list[dict[str, object]] | None:
    """Indicator rows after the stored ones, or None when the stored rows are not an intact prefix.

    The stored rows are an intact prefix when they cover exactly the OHLCV dates up to their last
    date; those values are unchanged (same closes up to that date), so only later rows are new.
    """
    if stored_last is None:
        return rows if stored_count == 0 else None
    if sum(1 for row in rows if str(row["date"]) <= stored_last) != stored_count:
        return None
    return [row for row in rows if str(row["date"]) > stored_last]


def calculate_batches(con, data_dir: Path, symbols: list[str], batch_size: int, incremental: bool = False) -> list[dict[str, object]]:
    """Calculate and store indicators for symbols, one transaction per batch.

    Without ``incremental`` every row of each symbol is written. With it, only the dates after a
    symbol's intact stored prefix are inserted; a symbol whose stored rows are not an intact
    prefix (unexpected) has its indicator rows deleted and rewritten.
    """
    results: list[dict[str, object]] = []
    for start in range(0, len(symbols), max(1, batch_size)):
        batch = symbols[start : start + max(1, batch_size)]
        rows_by_symbol = load_closes_for_symbols(con, batch)
        stored: dict[str, tuple[int, str]] = {}
        if incremental:
            stored = {
                symbol: (count, str(last_date))
                for symbol, count, last_date in con.execute(
                    "SELECT symbol, count(*), max(date) FROM indicators WHERE symbol IN (SELECT unnest(?)) GROUP BY symbol", [batch]
                ).fetchall()
            }
        updated_at = utc_now_iso()
        values: list[list[object]] = []
        rewrite: list[str] = []
        for symbol in batch:
            computed = add_indicators(rows_by_symbol.get(symbol, []))
            if incremental:
                count, last_date = stored.get(symbol, (0, None))
                tail = _missing_tail(computed, count, last_date)
                if tail is None:
                    rewrite.append(symbol)
                else:
                    computed = tail
            values.extend(indicator_values(symbol, computed, updated_at))
            results.append({"symbol": symbol, "status": "done", "rows": len(computed)})
        con.begin()
        try:
            if rewrite:
                con.execute("DELETE FROM indicators WHERE symbol IN (SELECT unnest(?))", [rewrite])
            bulk_upsert(con, data_dir, "indicators", INDICATOR_COLUMNS, ["symbol", "date"], values)
            con.commit()
        except Exception:
            con.rollback()
            raise
        print(f"[indicators] {min(start + len(batch), len(symbols))}/{len(symbols)} symbols", file=sys.stderr, flush=True)
    return results


def run(args: argparse.Namespace) -> dict:
    ensure_data_dirs(args.data_dir)
    ensure_db_schema(args.data_dir)
    mode = "selected" if args.symbol else "full" if args.full else "incremental"

    con = connect_db(args.data_dir)
    try:
        with_ohlcv = {row[0] for row in con.execute("SELECT DISTINCT symbol FROM ohlcv").fetchall()}
        if args.symbol:
            symbols = args.symbol
        elif args.symbols is not None:
            symbols = load_symbols(args.symbols)
        else:
            symbols = sorted(with_ohlcv)
        candidates = [symbol for symbol in symbols if symbol in with_ohlcv]
        targets = candidates if mode != "incremental" else find_stale_symbols(con, candidates)
        results = calculate_batches(con, args.data_dir, targets, args.batch_size, incremental=mode == "incremental")
    finally:
        con.close()

    summary = {
        "mode": mode,
        "processed": len(results),
        "up_to_date": len(candidates) - len(targets),
        "missing_ohlcv": len(symbols) - len(candidates),
        "rows": sum(int(item["rows"]) for item in results),
        "symbols": results,
    }
    return summary


def main() -> int:
    args = parse_args()
    try:
        with pipeline_lock(args.data_dir):
            summary = run(args)
    except LockHeldError as exc:
        return lock_held_exit(exc)
    printable = {k: v for k, v in summary.items() if k != "symbols"}
    print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
