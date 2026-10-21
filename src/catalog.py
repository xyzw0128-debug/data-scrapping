"""Twelve Data reference catalog helpers shared by src.universe and src.fetch_tickers."""

from __future__ import annotations

from pathlib import Path

from src.atomic_io import atomic_write_text


def extract_symbols(payload: dict) -> list[str]:
    """Sorted, de-duplicated upper-case symbols from a /stocks or /etfs payload."""
    if payload.get("status") == "error":
        message = payload.get("message") or payload.get("code") or "unknown Twelve Data error"
        raise ValueError(f"Twelve Data stocks endpoint error: {message}")

    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("Twelve Data stocks response missing data list")

    symbols: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_symbol = item.get("symbol")
        if not raw_symbol:
            continue
        symbol = str(raw_symbol).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    symbols.sort()
    return symbols


def atomic_write_symbols(path: Path, symbols: list[str]) -> None:
    """Write one symbol per line (config/symbols.txt format)."""
    atomic_write_text(path, "".join(f"{symbol}\n" for symbol in symbols))
