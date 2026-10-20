"""Local technical indicator calculation from OHLCV rows stored in DuckDB."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.config import load_symbols
from src.state import utc_now_iso
from src.storage import (
    INDICATOR_COLUMNS,
    OHLCV_FIELDS,
    bulk_upsert,
    connect_db,
    ensure_data_dirs,
    ensure_db_schema,
    indicator_values,
    load_ohlcv_rows_db,
    load_ohlcv_rows_for_symbols,
    save_indicator_rows_db,
)


INDICATOR_FIELDS = [
    "sma_20",
    "sma_50",
    "sma_200",
    "rsi_14",
    "macd_12_26",
    "macd_signal_9",
    "macd_hist",
]


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calculate local indicators from OHLCV rows in DuckDB.")
    parser.add_argument("--symbols", type=Path, default=ROOT / "config" / "symbols.txt")
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


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6f}".rstrip("0").rstrip(".")



def calculate_sma(values: list[float | None], period: int) -> list[float | None]:
    """Calculate simple moving averages."""
    result: list[float | None] = []
    window: list[float] = []
    for value in values:
        if value is None:
            result.append(None)
            window.clear()
            continue
        window.append(value)
        if len(window) > period:
            window.pop(0)
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


def add_indicators(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return OHLCV rows with local technical indicator columns appended."""
    closes = [_parse_float(row.get("close", "")) for row in rows]
    sma_20 = calculate_sma(closes, 20)
    sma_50 = calculate_sma(closes, 50)
    sma_200 = calculate_sma(closes, 200)
    rsi_14 = calculate_rsi(closes, 14)
    macd, macd_signal, macd_hist = calculate_macd(closes)

    output: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        enriched = {field: row.get(field, "") for field in OHLCV_FIELDS}
        enriched.update(
            {
                "sma_20": _format_number(sma_20[index]),
                "sma_50": _format_number(sma_50[index]),
                "sma_200": _format_number(sma_200[index]),
                "rsi_14": _format_number(rsi_14[index]),
                "macd_12_26": _format_number(macd[index]),
                "macd_signal_9": _format_number(macd_signal[index]),
                "macd_hist": _format_number(macd_hist[index]),
            }
        )
        output.append(enriched)
    return output



def calculate_for_symbol(data_dir: Path, symbol: str) -> tuple[str, int, str | None]:
    """Calculate indicators for a symbol, returning status, row count, and output path."""
    rows = load_ohlcv_rows_db(data_dir, symbol)
    if not rows:
        return "missing_ohlcv", 0, None
    output_path = save_indicator_rows_db(data_dir, symbol, add_indicators(rows))
    return "done", len(rows), str(output_path)


def find_stale_symbols(con, symbols: list[str]) -> list[str]:
    """Return symbols whose OHLCV rows were written after their indicators, in input order."""
    stale = {
        row[0]
        for row in con.execute(
            """
            SELECT o.symbol
            FROM (SELECT symbol, max(updated_at) AS updated_at FROM ohlcv GROUP BY symbol) o
            LEFT JOIN (SELECT symbol, max(updated_at) AS updated_at FROM indicators GROUP BY symbol) i USING (symbol)
            WHERE i.updated_at IS NULL OR o.updated_at > i.updated_at
            """
        ).fetchall()
    }
    return [symbol for symbol in symbols if symbol in stale]


def calculate_batches(con, data_dir: Path, symbols: list[str], batch_size: int) -> list[dict[str, object]]:
    """Calculate and store indicators for symbols, one transaction per batch."""
    results: list[dict[str, object]] = []
    for start in range(0, len(symbols), max(1, batch_size)):
        batch = symbols[start : start + max(1, batch_size)]
        rows_by_symbol = load_ohlcv_rows_for_symbols(con, batch)
        updated_at = utc_now_iso()
        values: list[list[str]] = []
        for symbol in batch:
            rows = rows_by_symbol.get(symbol, [])
            values.extend(indicator_values(symbol, add_indicators(rows), updated_at))
            results.append({"symbol": symbol, "status": "done", "rows": len(rows)})
        con.begin()
        try:
            bulk_upsert(con, data_dir, "indicators", INDICATOR_COLUMNS, ["symbol", "date"], values)
            con.commit()
        except Exception:
            con.rollback()
            raise
        print(f"[indicators] {min(start + len(batch), len(symbols))}/{len(symbols)} symbols", file=sys.stderr, flush=True)
    return results


def main() -> int:
    args = parse_args()
    ensure_data_dirs(args.data_dir)
    ensure_db_schema(args.data_dir)
    symbols = args.symbol if args.symbol else load_symbols(args.symbols)
    mode = "selected" if args.symbol else "full" if args.full else "incremental"

    con = connect_db(args.data_dir)
    try:
        with_ohlcv = {row[0] for row in con.execute("SELECT DISTINCT symbol FROM ohlcv").fetchall()}
        candidates = [symbol for symbol in symbols if symbol in with_ohlcv]
        targets = candidates if mode != "incremental" else find_stale_symbols(con, candidates)
        results = calculate_batches(con, args.data_dir, targets, args.batch_size)
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
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
