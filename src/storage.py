"""Storage helpers for raw responses and normalized DuckDB datasets."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.state import utc_now_iso, utc_today


OHLCV_FIELDS = ["date", "open", "high", "low", "close", "volume"]
INDICATOR_FIELDS = ["sma_20", "sma_50", "sma_200", "rsi_14", "macd_12_26", "macd_signal_9", "macd_hist"]
MACRO_FIELDS = ["date", "value", "realtime_start", "realtime_end"]
NEWS_FIELDS = ["id", "symbol", "datetime", "date", "headline", "source", "summary", "url", "image", "category"]

OHLCV_COLUMNS = ["symbol", *OHLCV_FIELDS, "updated_at"]
INDICATOR_COLUMNS = ["symbol", *OHLCV_FIELDS, *INDICATOR_FIELDS, "updated_at"]
MACRO_COLUMNS = ["series_id", *MACRO_FIELDS, "updated_at"]
NEWS_COLUMNS = ["symbol", "id", "datetime", "date", "headline", "source", "summary", "url", "image", "category", "updated_at"]

# Raw tables keep provider values as VARCHAR. These views expose the same rows with
# DATE/DOUBLE/TIMESTAMP types for analysis and dataset export; unparseable values
# (for example FRED's "." placeholder) become NULL.
TYPED_VIEW_SQL = {
    "ohlcv_typed": """
        SELECT symbol, TRY_CAST(date AS DATE) AS date,
               TRY_CAST(open AS DOUBLE) AS open, TRY_CAST(high AS DOUBLE) AS high,
               TRY_CAST(low AS DOUBLE) AS low, TRY_CAST(close AS DOUBLE) AS close,
               TRY_CAST(volume AS DOUBLE) AS volume, updated_at
        FROM ohlcv""",
    "indicators_typed": """
        SELECT symbol, TRY_CAST(date AS DATE) AS date,
               TRY_CAST(sma_20 AS DOUBLE) AS sma_20, TRY_CAST(sma_50 AS DOUBLE) AS sma_50,
               TRY_CAST(sma_200 AS DOUBLE) AS sma_200, TRY_CAST(rsi_14 AS DOUBLE) AS rsi_14,
               TRY_CAST(macd_12_26 AS DOUBLE) AS macd_12_26, TRY_CAST(macd_signal_9 AS DOUBLE) AS macd_signal_9,
               TRY_CAST(macd_hist AS DOUBLE) AS macd_hist, updated_at
        FROM indicators""",
    "macro_typed": """
        SELECT series_id, TRY_CAST(date AS DATE) AS date, TRY_CAST(value AS DOUBLE) AS value,
               realtime_start, realtime_end, updated_at
        FROM macro""",
    "news_typed": """
        SELECT symbol, id, TRY_CAST(replace(datetime, 'Z', '') AS TIMESTAMP) AS datetime_utc,
               TRY_CAST(date AS DATE) AS date, headline, source, summary, url, image, category, updated_at
        FROM news""",
}


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


def connect_db(data_dir: Path, read_only: bool = False):
    import duckdb  # type: ignore

    db_path = get_db_path(data_dir)
    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=read_only)


_connect_db = connect_db


def ensure_db_schema(data_dir: Path) -> Path:
    db_path = get_db_path(data_dir)
    con = _connect_db(data_dir)
    try:
        con.begin()
        con.execute("""CREATE TABLE IF NOT EXISTS ohlcv (symbol VARCHAR NOT NULL, date VARCHAR NOT NULL, open VARCHAR, high VARCHAR, low VARCHAR, close VARCHAR, volume VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, date))""")
        con.execute("""CREATE TABLE IF NOT EXISTS indicators (symbol VARCHAR NOT NULL, date VARCHAR NOT NULL, open VARCHAR, high VARCHAR, low VARCHAR, close VARCHAR, volume VARCHAR, sma_20 VARCHAR, sma_50 VARCHAR, sma_200 VARCHAR, rsi_14 VARCHAR, macd_12_26 VARCHAR, macd_signal_9 VARCHAR, macd_hist VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, date))""")
        con.execute("""CREATE TABLE IF NOT EXISTS macro (series_id VARCHAR NOT NULL, date VARCHAR NOT NULL, value VARCHAR, realtime_start VARCHAR, realtime_end VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(series_id, date))""")
        con.execute("""CREATE TABLE IF NOT EXISTS news (symbol VARCHAR NOT NULL, id VARCHAR NOT NULL, datetime VARCHAR, date VARCHAR, headline VARCHAR, source VARCHAR, summary VARCHAR, url VARCHAR, image VARCHAR, category VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, id))""")
        existing_views = {row[0] for row in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
        for view_name, sql in TYPED_VIEW_SQL.items():
            if view_name not in existing_views:
                con.execute(f"CREATE OR REPLACE VIEW {view_name} AS {sql}")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return db_path


def bulk_upsert(
    con,
    data_dir: Path,
    table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> int:
    """Upsert many rows with one INSERT OR REPLACE statement.

    Rows are staged as newline-delimited JSON next to market.db and loaded with
    read_json. One INSERT ... ON CONFLICT per row costs several milliseconds on a
    multi-million-row table (about 20 seconds for a 5000-day symbol); staging the
    batch is roughly a thousand times faster. JSON keeps strings exact, so empty
    strings, quotes, and newlines in news text round-trip unchanged.
    """
    key_indexes = [columns.index(column) for column in key_columns]
    deduped: dict[tuple[Any, ...], Sequence[Any]] = {}
    for row in rows:
        deduped[tuple(row[index] for index in key_indexes)] = row
    if not deduped:
        return 0

    staging_dir = get_db_path(data_dir).parent
    staging_dir.mkdir(parents=True, exist_ok=True)
    fd, staging_name = tempfile.mkstemp(prefix=f".{table}_", suffix=".ndjson.tmp", dir=staging_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in deduped.values():
                handle.write(json.dumps(dict(zip(columns, row)), ensure_ascii=False))
                handle.write("\n")
        column_list = ", ".join(columns)
        column_types = ", ".join(f"'{column}': 'VARCHAR'" for column in columns)
        con.execute(
            f"INSERT OR REPLACE INTO {table} ({column_list}) "
            f"SELECT {column_list} FROM read_json(?, format = 'newline_delimited', columns = {{{column_types}}})",
            [staging_name],
        )
    finally:
        os.unlink(staging_name)
    return len(deduped)


def _upsert(data_dir: Path, table: str, columns: Sequence[str], key_columns: Sequence[str], rows: list[Sequence[Any]]) -> str:
    db_path = ensure_db_schema(data_dir)
    con = _connect_db(data_dir)
    try:
        con.begin()
        bulk_upsert(con, data_dir, table, columns, key_columns, rows)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return str(db_path)


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
    now = utc_now_iso()
    values = [
        [symbol, *(row.get(field, "") for field in OHLCV_FIELDS), now]
        for row in rows
        if row.get("date")
    ]
    return _upsert(data_dir, "ohlcv", OHLCV_COLUMNS, ["symbol", "date"], values)


def indicator_values(symbol: str, rows: list[dict[str, str]], updated_at: str) -> list[list[str]]:
    """Convert indicator rows into value lists ordered like INDICATOR_COLUMNS."""
    return [
        [symbol, *(row.get(field, "") for field in OHLCV_FIELDS), *(row.get(field, "") for field in INDICATOR_FIELDS), updated_at]
        for row in rows
        if row.get("date")
    ]


def save_indicator_rows_db(data_dir: Path, symbol: str, rows: list[dict[str, str]]) -> str:
    values = indicator_values(symbol, rows, utc_now_iso())
    return _upsert(data_dir, "indicators", INDICATOR_COLUMNS, ["symbol", "date"], values)


def upsert_macro_db(data_dir: Path, series_id: str, rows: list[dict[str, str]]) -> str:
    now = utc_now_iso()
    values = [
        [series_id, *(row.get(field, "") for field in MACRO_FIELDS), now]
        for row in rows
        if row.get("date")
    ]
    return _upsert(data_dir, "macro", MACRO_COLUMNS, ["series_id", "date"], values)


def upsert_news_db(data_dir: Path, symbol: str, rows: list[dict[str, str]]) -> str:
    now = utc_now_iso()
    values = [
        [symbol, row.get("id", ""), *(row.get(field, "") for field in NEWS_COLUMNS[2:-1]), now]
        for row in rows
        if row.get("id")
    ]
    return _upsert(data_dir, "news", NEWS_COLUMNS, ["symbol", "id"], values)


def _ohlcv_dict(record: Sequence[Any]) -> dict[str, str]:
    return {field: str(value or "") for field, value in zip(OHLCV_FIELDS, record)}


def load_ohlcv_rows_db(data_dir: Path, symbol: str) -> list[dict[str, str]]:
    db_path = get_db_path(data_dir)
    if not db_path.exists():
        return []
    con = _connect_db(data_dir)
    try:
        result = con.execute("SELECT date, open, high, low, close, volume FROM ohlcv WHERE symbol = ? ORDER BY date", [symbol]).fetchall()
    finally:
        con.close()
    return [_ohlcv_dict(r) for r in result]


def load_ohlcv_rows_for_symbols(con, symbols: Sequence[str]) -> dict[str, list[dict[str, str]]]:
    """Load date-ordered OHLCV rows for several symbols with one query on an open connection."""
    rows_by_symbol: dict[str, list[dict[str, str]]] = {symbol: [] for symbol in symbols}
    if not symbols:
        return rows_by_symbol
    result = con.execute(
        "SELECT symbol, date, open, high, low, close, volume FROM ohlcv WHERE symbol IN (SELECT unnest(?)) ORDER BY symbol, date",
        [list(symbols)],
    ).fetchall()
    for record in result:
        rows_by_symbol[record[0]].append(_ohlcv_dict(record[1:]))
    return rows_by_symbol


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
