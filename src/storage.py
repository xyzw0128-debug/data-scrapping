"""Storage helpers for raw responses and normalized DuckDB datasets."""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.atomic_io import atomic_write_bytes, atomic_write_json, json_default
from src.state import utc_now_iso, utc_today


OHLCV_FIELDS = ["date", "open", "high", "low", "close", "volume"]
PRICE_FIELDS = ["open", "high", "low", "close"]
INDICATOR_FIELDS = ["sma_20", "sma_50", "sma_200", "rsi_14", "macd_12_26", "macd_signal_9", "macd_hist"]
MACRO_FIELDS = ["date", "value", "realtime_start", "realtime_end"]
NEWS_FIELDS = ["id", "symbol", "datetime", "date", "headline", "source", "summary", "url", "image", "category"]

OHLCV_COLUMNS = ["symbol", *OHLCV_FIELDS, "updated_at"]
PRICE_COLUMNS = ["symbol", *OHLCV_FIELDS, "adj_close", "updated_at"]
INDICATOR_COLUMNS = ["symbol", "date", *INDICATOR_FIELDS, "updated_at"]
MACRO_COLUMNS = ["series_id", *MACRO_FIELDS, "first_seen_at", "updated_at"]
NEWS_COLUMNS = ["symbol", "id", "datetime", "date", "headline", "source", "summary", "url", "image", "category", "updated_at"]
SERIES_META_COLUMNS = ["series_id", "title", "frequency", "units", "release", "license_note", "personal_use_only", "updated_at"]

# Version 4 stores ohlcv/indicators dates as DATE and values as DOUBLE/BIGINT (about half the
# file size of the VARCHAR version 3). A version 3 file is converted once with
# ``python -m src.rebuild --to-v4``; ensure_db_schema refuses to write to it until then.
SCHEMA_VERSION = 4
REBUILD_HINT = "python -m src.rebuild --to-v4"

# ohlcv.updated_at stays VARCHAR: find_stale_symbols compares it with indicators.updated_at as
# ISO strings. Column order matches version 3 (adj_close was appended in version 2).
TABLE_SQL = {
    "ohlcv": """CREATE TABLE IF NOT EXISTS ohlcv (symbol VARCHAR NOT NULL, date DATE NOT NULL, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, updated_at VARCHAR NOT NULL, adj_close DOUBLE, PRIMARY KEY(symbol, date))""",
    "indicators": """CREATE TABLE IF NOT EXISTS indicators (symbol VARCHAR NOT NULL, date DATE NOT NULL, sma_20 DOUBLE, sma_50 DOUBLE, sma_200 DOUBLE, rsi_14 DOUBLE, macd_12_26 DOUBLE, macd_signal_9 DOUBLE, macd_hist DOUBLE, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, date))""",
    "macro": """CREATE TABLE IF NOT EXISTS macro (series_id VARCHAR NOT NULL, date VARCHAR NOT NULL, value VARCHAR, realtime_start VARCHAR, realtime_end VARCHAR, updated_at VARCHAR NOT NULL, first_seen_at VARCHAR, PRIMARY KEY(series_id, date))""",
    "news": """CREATE TABLE IF NOT EXISTS news (symbol VARCHAR NOT NULL, id VARCHAR NOT NULL, datetime VARCHAR, date VARCHAR, headline VARCHAR, source VARCHAR, summary VARCHAR, url VARCHAR, image VARCHAR, category VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, id))""",
    "schema_meta": """CREATE TABLE IF NOT EXISTS schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR)""",
    "corporate_actions": """CREATE TABLE IF NOT EXISTS corporate_actions (symbol VARCHAR NOT NULL, date DATE NOT NULL, type VARCHAR NOT NULL, value DOUBLE NOT NULL, source VARCHAR, updated_at VARCHAR NOT NULL, PRIMARY KEY(symbol, date, type))""",
    "symbol_status": """CREATE TABLE IF NOT EXISTS symbol_status (symbol VARCHAR PRIMARY KEY, yahoo_symbol VARCHAR, group_name VARCHAR, active BOOLEAN, delisted_at VARCHAR, last_bar_date DATE, last_success_at VARCHAR, core_session DATE, splits_complete BOOLEAN, updated_at VARCHAR)""",
    "collection_runs": """CREATE TABLE IF NOT EXISTS collection_runs (run_id VARCHAR NOT NULL, phase VARCHAR NOT NULL, started_at VARCHAR, finished_at VARCHAR, session_date DATE, status VARCHAR, core_expected INTEGER, core_ok INTEGER, missing_symbols VARCHAR, fred_expected INTEGER, fred_ok INTEGER, missing_series VARCHAR, note VARCHAR, splits_ok INTEGER, missing_splits VARCHAR, universe_issue VARCHAR, PRIMARY KEY(run_id, phase))""",
    "series_meta": """CREATE TABLE IF NOT EXISTS series_meta (series_id VARCHAR PRIMARY KEY, title VARCHAR, frequency VARCHAR, units VARCHAR, release VARCHAR, license_note VARCHAR, personal_use_only BOOLEAN, updated_at VARCHAR)""",
}

# Typed views for analysis, dataset export, and stockpred. Their names, columns, and types are
# the same as in version 3 (where they cast VARCHAR columns); macro and news are still VARCHAR
# tables, so unparseable values (for example FRED's "." placeholder) become NULL there.
TYPED_VIEW_SQL = {
    "ohlcv_typed": """
        SELECT symbol, date, open, high, low, close, CAST(volume AS DOUBLE) AS volume, adj_close, updated_at
        FROM ohlcv""",
    "indicators_typed": """
        SELECT symbol, date, sma_20, sma_50, sma_200, rsi_14, macd_12_26, macd_signal_9, macd_hist, updated_at
        FROM indicators""",
    "macro_typed": """
        SELECT series_id, TRY_CAST(date AS DATE) AS date, TRY_CAST(value AS DOUBLE) AS value,
               realtime_start, realtime_end, first_seen_at, updated_at
        FROM macro""",
    "news_typed": """
        SELECT symbol, id, TRY_CAST(replace(datetime, 'Z', '') AS TIMESTAMP) AS datetime_utc,
               TRY_CAST(date AS DATE) AS date, headline, source, summary, url, image, category, updated_at
        FROM news""",
    # stockpred contract: same column names and meaning as stockpred's STD_COLS (yfinance
    # auto_adjust=False, actions=True). close is split-adjusted, adj_close also reflects
    # dividends, splits is the ratio on split dates. Dividends are unavailable on a Basic key.
    # volume stays DOUBLE as in version 3.
    "prices_daily": """
        SELECT o.symbol, o.date, o.open, o.high, o.low, o.close, o.adj_close,
               CAST(o.volume AS DOUBLE) AS volume, 0.0 AS dividends, coalesce(s.value, 0.0) AS splits
        FROM ohlcv o
        LEFT JOIN corporate_actions s
          ON s.symbol = o.symbol AND s.type = 'split' AND s.date = o.date""",
}


class SchemaVersionError(RuntimeError):
    """market.db has a schema version this code does not write (not a per-symbol failure)."""


_SCHEMA_READY: set[str] = set()


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


def get_snapshot_path(data_dir: Path) -> Path:
    return data_dir / "db" / "market_snapshot.db"


def connect_db(data_dir: Path, read_only: bool = False):
    import duckdb  # type: ignore

    db_path = get_db_path(data_dir)
    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=read_only)


_connect_db = connect_db


def schema_version(con) -> int | None:
    """Schema version of an open database; None for an empty database (no tables yet)."""
    tables = {row[0] for row in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = current_database()").fetchall()}
    if not tables:
        return None
    if "schema_meta" not in tables:
        return 1
    row = con.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    return int(row[0]) if row else 1


def create_views(con) -> None:
    for view_name, sql in TYPED_VIEW_SQL.items():
        con.execute(f"CREATE OR REPLACE VIEW {view_name} AS {sql}")


def create_schema(con) -> None:
    """Create every version 4 table and view and record the version (empty database)."""
    for sql in TABLE_SQL.values():
        con.execute(sql)
    create_views(con)
    con.execute("INSERT OR REPLACE INTO schema_meta VALUES ('version', ?)", [str(SCHEMA_VERSION)])


def ensure_db_schema(data_dir: Path) -> Path:
    """Create a new database at version 4, or check an existing one, once per process.

    An older version is never migrated in place (that would rewrite the whole file and make it
    larger): it raises SchemaVersionError naming the one-off conversion command instead.
    """
    db_path = get_db_path(data_dir)
    if str(db_path) in _SCHEMA_READY and db_path.exists():
        return db_path
    con = _connect_db(data_dir)
    try:
        version = schema_version(con)
        if version is not None and version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"{db_path} has schema version {version}, this code writes version {SCHEMA_VERSION}. "
                + (f"Convert it once under the pipeline lock: {REBUILD_HINT}" if version < SCHEMA_VERSION else "Update the code.")
            )
        con.begin()
        try:
            if version is None:
                create_schema(con)
            else:
                existing_views = {row[0] for row in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal AND database_name = current_database()").fetchall()}
                for view_name, sql in TYPED_VIEW_SQL.items():
                    if view_name not in existing_views:
                        con.execute(f"CREATE VIEW {view_name} AS {sql}")
            con.commit()
        except Exception:
            con.rollback()
            raise
    finally:
        con.close()
    _SCHEMA_READY.add(str(db_path))
    return db_path


def bulk_upsert(
    con,
    data_dir: Path,
    table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    update_columns: Sequence[str] | None = None,
) -> int:
    """Upsert many rows with one statement.

    Rows are staged as newline-delimited JSON next to market.db and loaded with
    read_json. One INSERT ... ON CONFLICT per row costs several milliseconds on a
    multi-million-row table (about 20 seconds for a 5000-day symbol); staging the
    batch is roughly a thousand times faster. JSON keeps strings exact, so empty
    strings, quotes, and newlines in news text round-trip unchanged.

    read_json reads each column with the table's own type, so values must already be
    normalized (numbers as numbers, dates as ISO strings, None for NULL): a typed read
    rejects "", "abc", "1.5e3" for BIGINT, or an invalid date instead of guessing.

    By default a conflicting row is replaced. With ``update_columns`` only those
    columns are updated, so other columns of existing rows (e.g. first_seen_at) survive.
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
                handle.write(json.dumps(dict(zip(columns, row)), ensure_ascii=False, allow_nan=False, default=json_default))
                handle.write("\n")
        table_types = {row[0]: row[1] for row in con.execute(f"DESCRIBE {table}").fetchall()}
        column_list = ", ".join(columns)
        column_types = ", ".join(f"'{column}': '{table_types[column]}'" for column in columns)
        select = f"SELECT {column_list} FROM read_json(?, format = 'newline_delimited', columns = {{{column_types}}})"
        if update_columns is None:
            sql = f"INSERT OR REPLACE INTO {table} ({column_list}) {select}"
        else:
            assignments = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
            sql = f"INSERT INTO {table} ({column_list}) {select} ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET {assignments}"
        con.execute(sql, [staging_name])
    finally:
        os.unlink(staging_name)
    return len(deduped)


def _upsert(
    data_dir: Path,
    table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    rows: list[Sequence[Any]],
    update_columns: Sequence[str] | None = None,
) -> str:
    db_path = ensure_db_schema(data_dir)
    con = _connect_db(data_dir)
    try:
        con.begin()
        bulk_upsert(con, data_dir, table, columns, key_columns, rows, update_columns)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return str(db_path)


def save_raw_json(data_dir: Path, provider: str, symbol: str, payload: dict[str, Any]) -> Path:
    """Archive a response as a new timestamped file (small, infrequent responses)."""
    safe_symbol = symbol.replace("/", "_")
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    path = data_dir / "raw" / provider / f"{safe_symbol}_{stamp}.json"
    atomic_write_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return path


def save_raw_latest(data_dir: Path, provider: str, name: str, payload: Any) -> Path:
    """Keep only the latest response per provider/name, gzip-compressed.

    Daily full-history refetches would otherwise add about 1 GB of JSON per day; one
    compressed file per symbol and adjust mode keeps the archive bounded (~0.6 GB).
    """
    safe_name = name.replace("/", "_")
    path = data_dir / "raw" / provider / f"{safe_name}.json.gz"
    atomic_write_bytes(path, gzip.compress(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")))
    return path


# Write policy for ohlcv values, applied in Python before staging (read_json then reads them
# with the table types): the date must be an ISO "YYYY-MM-DD" calendar date, prices finite
# numbers, volume a whole number >= 0 (0 is kept and differs from NULL). "" or a missing value
# becomes NULL only for volume and adj_close; a missing date or price is an error. A ValueError
# fails that one symbol and leaves its stored rows untouched.
_DECIMAL_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BIGINT_MAX = 2**63 - 1


def iso_date(value: Any) -> str:
    if isinstance(value, str) and _ISO_DATE_RE.fullmatch(value):
        try:
            date.fromisoformat(value)
            return value
        except ValueError:
            pass
    raise ValueError(f"invalid date {value!r}")


def price_value(value: Any, column: str, required: bool = True) -> float | None:
    """A finite float; "" or None becomes None unless the column is required."""
    if value is None or value == "":
        if required:
            raise ValueError(f"missing {column}")
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    elif isinstance(value, str) and _DECIMAL_RE.fullmatch(value):
        number = float(value)
    else:
        raise ValueError(f"invalid {column} {value!r}")
    if not math.isfinite(number):
        raise ValueError(f"invalid {column} {value!r}")
    return number


def volume_value(value: Any) -> int | None:
    """A whole number >= 0 ("12345" and 12345.0 are accepted, 1.5 is not); "" or None becomes None."""
    if value is None or value == "":
        return None
    number: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and _DECIMAL_RE.fullmatch(value):
        parsed = Decimal(value)
        if parsed == parsed.to_integral_value():
            number = int(parsed)
    if number is None or not 0 <= number <= _BIGINT_MAX:
        raise ValueError(f"invalid volume {value!r}")
    return number


def normalize_ohlcv_row(item: dict[str, Any], date_key: str = "date") -> dict[str, Any]:
    """Validate one bar into {"date": "YYYY-MM-DD", open/high/low/close: float, volume: int | None}."""
    day = iso_date(item.get(date_key))
    try:
        row: dict[str, Any] = {"date": day}
        for field in PRICE_FIELDS:
            row[field] = price_value(item.get(field), field)
        row["volume"] = volume_value(item.get("volume"))
    except ValueError as exc:
        raise ValueError(f"{exc} on {day}") from None
    return row


def normalize_twelve_data_ohlcv(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Date-ordered, validated bars of a time_series response (ValueError on any malformed bar)."""
    values = payload.get("values")
    if not isinstance(values, list):
        message = payload.get("message") or payload.get("status") or "missing values"
        raise ValueError(f"Twelve Data response has no values: {message}")
    rows: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError(f"Twelve Data value is not an object: {str(item)[:80]!r}")
        rows.append(normalize_ohlcv_row(item, date_key="datetime"))
    rows.sort(key=lambda row: row["date"])
    return rows


def upsert_ohlcv_db(data_dir: Path, symbol: str, rows: list[dict[str, Any]]) -> str:
    now = utc_now_iso()
    values = [[symbol, *(bar[field] for field in OHLCV_FIELDS), now] for bar in map(normalize_ohlcv_row, rows)]
    return _upsert(data_dir, "ohlcv", OHLCV_COLUMNS, ["symbol", "date"], values)


def replace_symbol_prices(data_dir: Path, symbol: str, rows: list[dict[str, Any]], adj_closes: dict[str, Any] | None = None) -> str:
    """Replace every OHLCV row of a symbol in one transaction.

    Old rows outside the new response (an older split basis, or history beyond the
    5000-row window) are removed. The symbol's indicator rows are removed too, so the next
    indicator run recalculates them from the new prices. Rows are validated first, so a
    malformed value raises ValueError before anything is deleted.
    """
    now = utc_now_iso()
    adj_closes = adj_closes or {}
    values = [
        [symbol, *(bar[field] for field in OHLCV_FIELDS), price_value(adj_closes.get(bar["date"]), "adj_close", required=False), now]
        for bar in map(normalize_ohlcv_row, rows)
    ]
    db_path = ensure_db_schema(data_dir)
    con = _connect_db(data_dir)
    try:
        con.begin()
        con.execute("DELETE FROM ohlcv WHERE symbol = ?", [symbol])
        con.execute("DELETE FROM indicators WHERE symbol = ?", [symbol])
        bulk_upsert(con, data_dir, "ohlcv", PRICE_COLUMNS, ["symbol", "date"], values)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return str(db_path)


def replace_split_events(data_dir: Path, symbol: str, events: list[tuple[str, float]], source: str) -> None:
    """Replace a symbol's split events."""
    ensure_db_schema(data_dir)
    now = utc_now_iso()
    con = _connect_db(data_dir)
    try:
        con.begin()
        con.execute("DELETE FROM corporate_actions WHERE symbol = ? AND type = 'split'", [symbol])
        if events:
            con.executemany(
                "INSERT INTO corporate_actions VALUES (?, CAST(? AS DATE), 'split', ?, ?, ?)",
                [[symbol, day, value, source, now] for day, value in events],
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def load_closes(data_dir: Path, symbol: str) -> dict[str, float]:
    """Stored split-adjusted closes of a symbol by ISO date string (empty when absent).

    Keys are strings whatever the column type, so they match the provider's "YYYY-MM-DD" dates;
    a DATE column would otherwise return datetime.date keys that never match and silently
    disable split detection and split-event derivation.
    """
    if not get_db_path(data_dir).exists():
        return {}
    con = _connect_db(data_dir)
    try:
        rows = con.execute("SELECT date, TRY_CAST(close AS DOUBLE) FROM ohlcv WHERE symbol = ?", [symbol]).fetchall()
    finally:
        con.close()
    return {str(day): close for day, close in rows if close is not None}


def indicator_values(symbol: str, rows: list[dict[str, Any]], updated_at: str) -> list[list[Any]]:
    """Convert indicator rows into value lists ordered like INDICATOR_COLUMNS ("" or None is NULL)."""
    return [
        [symbol, row["date"], *(None if row.get(field) in (None, "") else row[field] for field in INDICATOR_FIELDS), updated_at]
        for row in rows
        if row.get("date")
    ]


def save_indicator_rows_db(data_dir: Path, symbol: str, rows: list[dict[str, Any]]) -> str:
    values = indicator_values(symbol, rows, utc_now_iso())
    return _upsert(data_dir, "indicators", INDICATOR_COLUMNS, ["symbol", "date"], values)


def upsert_macro_db(data_dir: Path, series_id: str, rows: list[dict[str, str]]) -> str:
    """Upsert observations; first_seen_at keeps the time an observation date was first stored."""
    now = utc_now_iso()
    values = [
        [series_id, *(row.get(field, "") for field in MACRO_FIELDS), now, now]
        for row in rows
        if row.get("date")
    ]
    return _upsert(data_dir, "macro", MACRO_COLUMNS, ["series_id", "date"], values, update_columns=["value", "realtime_start", "realtime_end", "updated_at"])


def upsert_series_meta(data_dir: Path, rows: list[dict[str, Any]]) -> str:
    now = utc_now_iso()
    values = [[*(row.get(column) for column in SERIES_META_COLUMNS[:-1]), now] for row in rows]
    return _upsert(data_dir, "series_meta", SERIES_META_COLUMNS, ["series_id"], values)


def upsert_news_db(data_dir: Path, symbol: str, rows: list[dict[str, str]]) -> str:
    now = utc_now_iso()
    values = [
        [symbol, row.get("id", ""), *(row.get(field, "") for field in NEWS_COLUMNS[2:-1]), now]
        for row in rows
        if row.get("id")
    ]
    return _upsert(data_dir, "news", NEWS_COLUMNS, ["symbol", "id"], values)


def _ohlcv_dict(record: Sequence[Any]) -> dict[str, str]:
    # Only NULL becomes ""; a numeric 0 (e.g. zero volume) must stay "0".
    return {field: "" if value is None else str(value) for field, value in zip(OHLCV_FIELDS, record)}


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
    stamp = str(summary.get("finished_at") or utc_now_iso()).replace(":", "").replace("-", "")
    return atomic_write_json(run_dir / f"{utc_today()}_{stamp}.json", summary)
