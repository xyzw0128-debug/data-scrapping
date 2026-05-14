"""Storage helpers for raw responses and normalized DuckDB datasets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.state import utc_now_iso, utc_today


OHLCV_FIELDS = ["date", "open", "high", "low", "close", "volume"]
INDICATOR_FIELDS = ["sma_20", "sma_50", "sma_200", "rsi_14", "macd_12_26", "macd_signal_9", "macd_hist"]
MACRO_FIELDS = ["date", "value", "realtime_start", "realtime_end"]
NEWS_FIELDS = ["id", "symbol", "datetime", "date", "headline", "source", "summary", "url", "image", "category"]


def ensure_data_dirs(data_dir: Path) -> None:
    for relative in ["raw", "state", "logs", "db"]:
        (data_dir / relative).mkdir(parents=True, exist_ok=True)


def verify_writable(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write_probe.tmp"
    target = path / ".write_probe.ok"
    probe.write_text("ok\n", encoding="utf-8")
    os.replace(probe, target)
    target.unlink()


def get_db_path(data_dir: Path) -> Path:
    return data_dir / "db" / "market.db"


def _connect_db(data_dir: Path):
    import duckdb  # type: ignore

    db_path = get_db_path(data_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def ensure_db_schema(data_dir: Path) -> Path:
    db_path = get_db_path(data_dir)
    con = _connect_db(data_dir)
    try:
        con.begin()
        con.execute("""CREATE TABLE IF NOT EXISTS ohlcv (symbol VARCHAR NOT NULL, date VARCHAR NOT NULL, open VARCHAR, high VARCHAR, low VARCHAR, close VARCHAR, volume VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, date))""")
        con.execute("""CREATE TABLE IF NOT EXISTS indicators (symbol VARCHAR NOT NULL, date VARCHAR NOT NULL, open VARCHAR, high VARCHAR, low VARCHAR, close VARCHAR, volume VARCHAR, sma_20 VARCHAR, sma_50 VARCHAR, sma_200 VARCHAR, rsi_14 VARCHAR, macd_12_26 VARCHAR, macd_signal_9 VARCHAR, macd_hist VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, date))""")
        con.execute("""CREATE TABLE IF NOT EXISTS macro (series_id VARCHAR NOT NULL, date VARCHAR NOT NULL, value VARCHAR, realtime_start VARCHAR, realtime_end VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(series_id, date))""")
        con.execute("""CREATE TABLE IF NOT EXISTS news (symbol VARCHAR NOT NULL, id VARCHAR NOT NULL, datetime VARCHAR, date VARCHAR, headline VARCHAR, source VARCHAR, summary VARCHAR, url VARCHAR, image VARCHAR, category VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, id))""")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return db_path


def save_raw_json(data_dir: Path, provider: str, symbol: str, payload: dict[str, Any]) -> Path:
    safe_symbol = symbol.replace("/", "_")
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    raw_dir = data_dir / "raw" / provider
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{safe_symbol}_{stamp}.json"
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path


def normalize_twelve_data_ohlcv(payload: dict[str, Any]) -> list[dict[str, str]]:
    values = payload.get("values")
    if not isinstance(values, list):
        message = payload.get("message") or payload.get("status") or "missing values"
        raise ValueError(f"Twelve Data response has no values: {message}")
    rows: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        row = {"date": str(item.get("datetime", "")), "open": str(item.get("open", "")), "high": str(item.get("high", "")), "low": str(item.get("low", "")), "close": str(item.get("close", "")), "volume": str(item.get("volume", ""))}
        if row["date"]:
            rows.append(row)
    rows.sort(key=lambda row: row["date"])
    return rows


def upsert_ohlcv_db(data_dir: Path, symbol: str, rows: list[dict[str, str]]) -> str:
    db_path = ensure_db_schema(data_dir)
    con = _connect_db(data_dir)
    try:
        con.begin()
        now = utc_now_iso()
        for row in rows:
            if not row.get("date"):
                continue
            con.execute(
                """INSERT INTO ohlcv(symbol,date,open,high,low,close,volume,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume, updated_at=excluded.updated_at""",
                [symbol, row.get("date", ""), row.get("open", ""), row.get("high", ""), row.get("low", ""), row.get("close", ""), row.get("volume", ""), now],
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return str(db_path)


def save_indicator_rows_db(data_dir: Path, symbol: str, rows: list[dict[str, str]]) -> str:
    db_path = ensure_db_schema(data_dir)
    con = _connect_db(data_dir)
    try:
        con.begin()
        now = utc_now_iso()
        for row in rows:
            if not row.get("date"):
                continue
            con.execute(
                """INSERT INTO indicators(symbol,date,open,high,low,close,volume,sma_20,sma_50,sma_200,rsi_14,macd_12_26,macd_signal_9,macd_hist,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume, sma_20=excluded.sma_20, sma_50=excluded.sma_50, sma_200=excluded.sma_200, rsi_14=excluded.rsi_14, macd_12_26=excluded.macd_12_26, macd_signal_9=excluded.macd_signal_9, macd_hist=excluded.macd_hist, updated_at=excluded.updated_at""",
                [symbol, row.get("date", ""), row.get("open", ""), row.get("high", ""), row.get("low", ""), row.get("close", ""), row.get("volume", ""), row.get("sma_20", ""), row.get("sma_50", ""), row.get("sma_200", ""), row.get("rsi_14", ""), row.get("macd_12_26", ""), row.get("macd_signal_9", ""), row.get("macd_hist", ""), now],
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return str(db_path)


def upsert_macro_db(data_dir: Path, series_id: str, rows: list[dict[str, str]]) -> str:
    db_path = ensure_db_schema(data_dir)
    con = _connect_db(data_dir)
    try:
        con.begin()
        now = utc_now_iso()
        for row in rows:
            if not row.get("date"):
                continue
            con.execute(
                """INSERT INTO macro(series_id,date,value,realtime_start,realtime_end,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(series_id,date) DO UPDATE SET value=excluded.value, realtime_start=excluded.realtime_start, realtime_end=excluded.realtime_end, updated_at=excluded.updated_at""",
                [series_id, row.get("date", ""), row.get("value", ""), row.get("realtime_start", ""), row.get("realtime_end", ""), now],
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return str(db_path)


def upsert_news_db(data_dir: Path, symbol: str, rows: list[dict[str, str]]) -> str:
    db_path = ensure_db_schema(data_dir)
    con = _connect_db(data_dir)
    try:
        con.begin()
        now = utc_now_iso()
        for row in rows:
            row_id = row.get("id", "")
            if not row_id:
                continue
            con.execute(
                """INSERT INTO news(symbol,id,datetime,date,headline,source,summary,url,image,category,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,id) DO UPDATE SET datetime=excluded.datetime, date=excluded.date, headline=excluded.headline, source=excluded.source, summary=excluded.summary, url=excluded.url, image=excluded.image, category=excluded.category, updated_at=excluded.updated_at""",
                [symbol, row_id, row.get("datetime", ""), row.get("date", ""), row.get("headline", ""), row.get("source", ""), row.get("summary", ""), row.get("url", ""), row.get("image", ""), row.get("category", ""), now],
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return str(db_path)


def load_ohlcv_rows_db(data_dir: Path, symbol: str) -> list[dict[str, str]]:
    db_path = get_db_path(data_dir)
    if not db_path.exists():
        return []
    con = _connect_db(data_dir)
    try:
        result = con.execute("SELECT date, open, high, low, close, volume FROM ohlcv WHERE symbol = ? ORDER BY date", [symbol]).fetchall()
    finally:
        con.close()
    return [{"date": str(r[0] or ""), "open": str(r[1] or ""), "high": str(r[2] or ""), "low": str(r[3] or ""), "close": str(r[4] or ""), "volume": str(r[5] or "")} for r in result]


def save_run_summary(data_dir: Path, summary: dict[str, Any]) -> Path:
    run_dir = data_dir / "logs" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(summary.get("finished_at") or utc_now_iso()).replace(":", "").replace("-", "")
    path = run_dir / f"{utc_today()}_{stamp}.json"
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path
