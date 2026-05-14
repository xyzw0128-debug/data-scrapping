"""Local technical indicator calculation from stored OHLCV CSV files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.config import load_symbols
from src.storage import OHLCV_FIELDS, ensure_data_dirs, load_ohlcv_rows_db, save_indicator_rows_db


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
    parser = argparse.ArgumentParser(description="Calculate local indicators from OHLCV CSV files.")
    parser.add_argument("--symbols", type=Path, default=ROOT / "config" / "symbols.txt")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--symbol", action="append", help="Calculate only this symbol; can be used multiple times")
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


def main() -> int:
    args = parse_args()
    ensure_data_dirs(args.data_dir)
    symbols = args.symbol if args.symbol else load_symbols(args.symbols)
    summary = {"processed": 0, "missing_ohlcv": 0, "symbols": []}
    for symbol in symbols:
        status, row_count, output_path = calculate_for_symbol(args.data_dir, symbol)
        if status == "done":
            summary["processed"] += 1
        else:
            summary["missing_ohlcv"] += 1
        summary["symbols"].append({"symbol": symbol, "status": status, "rows": row_count, "output": output_path})

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
