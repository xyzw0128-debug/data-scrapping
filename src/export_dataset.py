"""Export point-in-time ML datasets from market.db.

Outputs (under data/datasets/<name>/):
- train/val/test.parquet: one row per symbol-day with features, macro context, and forward-return labels.
- train/val/test.jsonl: chat-format examples ({"messages": [...]}) for LLM fine-tuning.
- metadata.json: filters, split boundaries, column descriptions, and row counts.

Leakage rules:
- Features at date t use only rows on or before t (lag/rolling windows).
- Labels are log returns from close(t) to close(t+h trading rows).
- Macro values join by an approximate publication date, not the observation date.
- Splits are chronological with an embargo gap so train labels never overlap val/test dates.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.state import utc_now_iso
from src.storage import get_db_path


ROOT = Path(__file__).resolve().parents[1]
SPLITS = ("train", "val", "test")
SERIES_ID_PATTERN = re.compile(r"^[A-Z0-9_]+$")

# Approximate release lag. Daily series are usable the next day; lower-frequency series one
# observation period plus 20 days after their observation date (e.g. April CPI, dated
# 2026-04-01, is treated as known from 2026-05-21). H.10 FX series are published weekly.
MACRO_EXTRA_LAG_DAYS = {"DEXKOUS": 7, "DTWEXBGS": 7}
MACRO_LABELS = {
    "FEDFUNDS": "연방기금금리(%)",
    "DGS10": "미 국채 10년물(%)",
    "UNRATE": "실업률(%)",
    "CPIAUCSL": "CPI 지수",
    "T10Y2Y": "10년-2년 금리차(%p)",
    "T10Y3M": "10년-3개월 금리차(%p)",
    "T10YIE": "10년 기대인플레이션(%)",
    "REAINTRATREARAT10Y": "10년 실질금리(%)",
    "BOGMBASE": "본원통화",
    "DTWEXBGS": "달러지수(광의)",
    "DEXKOUS": "원/달러 환율",
    "DGS2": "미 국채 2년물(%)",
    "BAA10Y": "Baa 회사채-10년물 스프레드(%p)",
    "VIXCLS": "VIX",
    "DTB3": "미 국채 3개월물(%)",
    "IR3TIB01KRM156N": "한국 3개월 금리(%)",
}

FEATURE_DESCRIPTIONS = {
    "ret_1d": "log(close_t / close_t-1)",
    "ret_5d": "log(close_t / close_t-5)",
    "ret_20d": "log(close_t / close_t-20)",
    "gap": "open_t / close_t-1 - 1",
    "hl_range": "(high_t - low_t) / close_t",
    "vol_20d": "20-row standard deviation of ret_1d (daily, not annualized)",
    "volume_z_20": "z-score of log(volume) within the trailing 20 rows",
    "dollar_volume_20d": "trailing 20-row mean of close * volume",
    "close_sma_20": "close / 20-row SMA - 1",
    "close_sma_50": "close / 50-row SMA - 1",
    "close_sma_200": "close / 200-row SMA - 1 (NULL until 200 rows exist)",
    "rsi_14": "Wilder RSI(14) from the indicators table",
    "macd_norm": "MACD(12,26) / close",
    "macd_hist_norm": "MACD histogram (signal 9) / close",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export leakage-safe ML datasets (Parquet + chat JSONL) from market.db.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data" / "datasets")
    parser.add_argument("--name", default=None, help="Dataset folder name; default dataset_<UTC date>")
    parser.add_argument("--format", choices=["parquet", "jsonl", "both"], default="both")
    parser.add_argument("--symbol", action="append", help="Export only this symbol; can be used multiple times")
    parser.add_argument("--max-symbols", type=int, default=None, help="Keep only the N most liquid symbols (quick samples; uses full history, so not point-in-time)")
    parser.add_argument("--include-all-symbols", action="store_true", help="Keep warrants/units/rights and non A-Z tickers")
    parser.add_argument("--start-date", default=None, help="First sample date YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="Last sample date YYYY-MM-DD")
    parser.add_argument("--min-price", type=float, default=5.0, help="Minimum close on the sample date")
    parser.add_argument("--min-dollar-volume", type=float, default=1_000_000.0, help="Minimum trailing 20-day average dollar volume")
    parser.add_argument("--min-history", type=int, default=200, help="Rows of price history required before a sample date")
    parser.add_argument("--max-abs-log-return", type=float, default=math.log(2), help="Drop samples whose feature or label window has a larger one-day move (default: doubling/halving)")
    parser.add_argument("--keep-extreme-moves", action="store_true", help="Disable the extreme one-day move filter")
    parser.add_argument("--allow-missing", action="store_true", help="Keep rows with NULL features")
    parser.add_argument("--horizons", default="1,5,20", help="Comma-separated label horizons in trading rows")
    parser.add_argument("--val-start", default=None, help="Validation start date; default: test start - 365 days")
    parser.add_argument("--test-start", default=None, help="Test start date; default: last price date - 365 days")
    parser.add_argument("--jsonl-window", type=int, default=20, help="Trading days shown in each JSONL prompt")
    parser.add_argument("--jsonl-horizon", type=int, default=5, help="Label horizon used for JSONL answers (must be in --horizons)")
    parser.add_argument("--jsonl-stride", type=int, default=5, help="Use every Nth eligible day per symbol for JSONL")
    parser.add_argument("--jsonl-max-examples", type=int, default=20000, help="Maximum JSONL examples per split")
    parser.add_argument("--jsonl-flat-band", type=float, default=0.01, help="Absolute return below this is labelled 보합")
    parser.add_argument("--jsonl-news", type=int, default=5, help="Maximum recent headlines per JSONL prompt")
    parser.add_argument("--include-personal-use-series", action="store_true", help="Also export FRED series flagged personal_use_only (copyright-restricted)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--memory-limit", default="4GB", help="DuckDB memory cap; larger intermediates spill to disk")
    return parser.parse_args()


def parse_horizons(value: str) -> list[int]:
    horizons = sorted({int(part) for part in value.split(",") if part.strip()})
    if not horizons or horizons[0] < 1:
        raise ValueError("--horizons must contain positive integers")
    return horizons


def embargo_days(max_horizon: int) -> int:
    """Calendar days that cover max_horizon trading rows plus a holiday buffer."""
    return math.ceil(max_horizon * 7 / 5) + 3


def resolve_splits(last_date: date, val_start: str | None, test_start: str | None) -> tuple[date, date]:
    test = date.fromisoformat(test_start) if test_start else last_date - timedelta(days=365)
    val = date.fromisoformat(val_start) if val_start else test - timedelta(days=365)
    if val >= test:
        raise ValueError("--val-start must be before --test-start")
    return val, test


def symbol_name_filter(include_all: bool) -> str:
    if include_all:
        return "TRUE"
    # NASDAQ fifth-letter suffixes: W = warrant, R = rights, U = unit.
    return "regexp_full_match(symbol, '[A-Z]{1,5}') AND NOT (length(symbol) = 5 AND right(symbol, 1) IN ('W', 'R', 'U'))"


def macro_series(con, include_personal_use: bool = False) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT series_id FROM src.macro WHERE TRY_CAST(value AS DOUBLE) IS NOT NULL ORDER BY series_id"
    ).fetchall()
    series = [row[0] for row in rows if SERIES_ID_PATTERN.match(row[0])]
    has_meta = con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'src' AND table_name = 'series_meta'"
    ).fetchone()[0]
    if include_personal_use or not has_meta:
        return series
    # Series whose FRED notes restrict reproduction (e.g. BAA10Y, VIXCLS) stay out of exported files.
    restricted = {row[0] for row in con.execute("SELECT series_id FROM src.series_meta WHERE personal_use_only").fetchall()}
    return [sid for sid in series if sid not in restricted]


def macro_column(series_id: str) -> str:
    return f"macro_{series_id.lower()}"


def build_tables(con, args: argparse.Namespace, horizons: list[int]) -> dict[str, object]:
    """Create universe, feature, and dataset temp tables; return split metadata."""
    max_h = max(horizons)
    symbol_filter = symbol_name_filter(args.include_all_symbols)
    if args.symbol:
        con.execute("CREATE TEMP TABLE selected AS SELECT unnest(?) AS symbol", [[s.strip().upper() for s in args.symbol]])
        symbol_filter += " AND symbol IN (SELECT symbol FROM selected)"
    con.execute(f"CREATE TEMP TABLE universe AS SELECT DISTINCT symbol FROM src.ohlcv WHERE {symbol_filter}")
    if args.max_symbols:
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE universe AS
            SELECT symbol FROM src.ohlcv
            WHERE symbol IN (SELECT symbol FROM universe)
            GROUP BY symbol
            ORDER BY median(TRY_CAST(close AS DOUBLE) * TRY_CAST(volume AS DOUBLE)) DESC NULLS LAST, symbol
            LIMIT ?
            """,
            [args.max_symbols],
        )

    con.execute(
        """
        CREATE TEMP TABLE prices AS
        SELECT symbol, d AS date, o AS open, h AS high, l AS low, c AS close, v AS volume
        FROM (
            SELECT symbol, TRY_CAST(date AS DATE) AS d, TRY_CAST(open AS DOUBLE) AS o, TRY_CAST(high AS DOUBLE) AS h,
                   TRY_CAST(low AS DOUBLE) AS l, TRY_CAST(close AS DOUBLE) AS c, TRY_CAST(volume AS DOUBLE) AS v
            FROM src.ohlcv WHERE symbol IN (SELECT symbol FROM universe)
        )
        WHERE d IS NOT NULL AND o > 0 AND h > 0 AND l > 0 AND c > 0 AND v > 0 AND h >= l
          AND dayofweek(d) BETWEEN 1 AND 5
        """
    )

    label_sql = ",\n".join(f"ln(lead(close, {h}) OVER w / close) AS fwd_ret_{h}d" for h in horizons)
    con.execute(
        f"""
        CREATE TEMP TABLE features AS
        WITH r AS (
            SELECT *, ln(close / lag(close) OVER w) AS ret_1d, lag(close) OVER w AS prev_close,
                   row_number() OVER w AS history_rows
            FROM prices
            WINDOW w AS (PARTITION BY symbol ORDER BY date)
        )
        SELECT symbol, date, open, high, low, close, volume, ret_1d,
               ln(close / lag(close, 5) OVER w) AS ret_5d,
               ln(close / lag(close, 20) OVER w) AS ret_20d,
               open / prev_close - 1 AS gap,
               (high - low) / close AS hl_range,
               CASE WHEN count(ret_1d) OVER w20 = 20 THEN stddev_samp(ret_1d) OVER w20 END AS vol_20d,
               CASE WHEN count(*) OVER w20 = 20
                    THEN coalesce((ln(volume) - avg(ln(volume)) OVER w20) / nullif(stddev_samp(ln(volume)) OVER w20, 0), 0) END AS volume_z_20,
               CASE WHEN count(*) OVER w20 = 20 THEN avg(close * volume) OVER w20 END AS dollar_volume_20d,
               CASE WHEN count(*) OVER w20 = 20 THEN close / avg(close) OVER w20 - 1 END AS close_sma_20,
               CASE WHEN count(*) OVER w50 = 50 THEN close / avg(close) OVER w50 - 1 END AS close_sma_50,
               CASE WHEN count(*) OVER w200 = 200 THEN close / avg(close) OVER w200 - 1 END AS close_sma_200,
               history_rows,
               max(abs(ret_1d)) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS max_move_past,
               max(abs(ret_1d)) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 1 FOLLOWING AND {max_h} FOLLOWING) AS max_move_future,
               {label_sql}
        FROM r
        WINDOW w AS (PARTITION BY symbol ORDER BY date),
               w20 AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
               w50 AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW),
               w200 AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW)
        """
    )

    extra_lag = " ".join(f"WHEN '{sid}' THEN {days}" for sid, days in MACRO_EXTRA_LAG_DAYS.items())
    con.execute(
        f"""
        CREATE TEMP TABLE macro_avail AS
        WITH m AS (
            SELECT series_id, TRY_CAST(date AS DATE) AS obs_date, TRY_CAST(value AS DOUBLE) AS value
            FROM src.macro
        ), valid AS (
            SELECT * FROM m WHERE obs_date IS NOT NULL AND value IS NOT NULL
        ), period AS (
            SELECT series_id, median(gap) AS period_days
            FROM (SELECT series_id, obs_date - lag(obs_date) OVER (PARTITION BY series_id ORDER BY obs_date) AS gap FROM valid)
            GROUP BY series_id
        )
        SELECT v.series_id, v.obs_date, v.value,
               v.obs_date + CAST(CASE WHEN p.period_days <= 7 THEN 1 ELSE round(p.period_days) + 20 END AS INTEGER)
                          + CAST(CASE v.series_id {extra_lag} ELSE 0 END AS INTEGER) AS available_date
        FROM valid v JOIN period p USING (series_id)
        """
    )
    series = macro_series(con, args.include_personal_use_series)
    macro_select = "".join(f", {macro_column(sid)}.value AS {macro_column(sid)}" for sid in series)
    macro_joins = "".join(
        f"\nASOF LEFT JOIN (SELECT available_date, value FROM macro_avail WHERE series_id = '{sid}') {macro_column(sid)}"
        f" ON f.date >= {macro_column(sid)}.available_date"
        for sid in series
    )

    last_date = con.execute("SELECT max(date) FROM prices").fetchone()[0]
    if last_date is None:
        raise ValueError("No usable OHLCV rows for the selected symbols")
    val_start, test_start = resolve_splits(last_date, args.val_start, args.test_start)
    embargo = embargo_days(max_h)
    val_cut = val_start - timedelta(days=embargo)
    test_cut = test_start - timedelta(days=embargo)

    conditions = [
        f"f.history_rows > {int(args.min_history)}",
        f"f.close >= {float(args.min_price)}",
        f"f.dollar_volume_20d >= {float(args.min_dollar_volume)}",
        f"f.fwd_ret_{max_h}d IS NOT NULL",
    ]
    if not args.keep_extreme_moves:
        limit = float(args.max_abs_log_return)
        conditions.append(f"coalesce(f.max_move_past, 0) <= {limit} AND coalesce(f.max_move_future, 0) <= {limit}")
    if args.start_date:
        conditions.append(f"f.date >= DATE '{date.fromisoformat(args.start_date).isoformat()}'")
    if args.end_date:
        conditions.append(f"f.date <= DATE '{date.fromisoformat(args.end_date).isoformat()}'")
    feature_columns = [*FEATURE_DESCRIPTIONS, *(macro_column(sid) for sid in series)]
    if not args.allow_missing:
        conditions.extend(f"{column} IS NOT NULL" for column in feature_columns)

    con.execute(
        f"""
        CREATE TEMP TABLE joined AS
        SELECT f.*, i.rsi_14, i.macd / f.close AS macd_norm, i.macd_hist / f.close AS macd_hist_norm{macro_select},
               CASE WHEN f.date >= DATE '{test_start}' THEN 'test'
                    WHEN f.date >= DATE '{val_start}' AND f.date < DATE '{test_cut}' THEN 'val'
                    WHEN f.date < DATE '{val_cut}' THEN 'train' END AS split
        FROM features f
        LEFT JOIN (
            SELECT symbol, TRY_CAST(date AS DATE) AS date, TRY_CAST(rsi_14 AS DOUBLE) AS rsi_14,
                   TRY_CAST(macd_12_26 AS DOUBLE) AS macd, TRY_CAST(macd_hist AS DOUBLE) AS macd_hist
            FROM src.indicators WHERE symbol IN (SELECT symbol FROM universe)
        ) i USING (symbol, date){macro_joins}
        """
    )
    con.execute("DROP TABLE features")
    con.execute(
        f"""
        CREATE TEMP TABLE dataset AS
        SELECT f.*, (f.split IS NOT NULL AND {' AND '.join(conditions)}) AS eligible
        FROM joined f
        ORDER BY f.symbol, f.date
        """
    )
    con.execute("DROP TABLE joined")

    return {
        "series": series,
        "feature_columns": feature_columns,
        "label_columns": [f"fwd_ret_{h}d" for h in horizons],
        "last_price_date": last_date.isoformat(),
        "val_start": val_start.isoformat(),
        "test_start": test_start.isoformat(),
        "embargo_days": embargo,
        "train_end_exclusive": val_cut.isoformat(),
        "val_end_exclusive": test_cut.isoformat(),
    }


def export_parquet(con, out_dir: Path, columns: list[str]) -> dict[str, dict[str, object]]:
    counts: dict[str, dict[str, object]] = {}
    column_list = ", ".join(columns)
    for split in SPLITS:
        path = out_dir / f"{split}.parquet"
        con.execute(
            f"COPY (SELECT {column_list} FROM dataset WHERE eligible AND split = '{split}' ORDER BY date, symbol) "
            f"TO '{_sql_path(path)}' (FORMAT parquet, COMPRESSION zstd)"
        )
        rows, symbols, first, last = con.execute(
            f"SELECT count(*), count(DISTINCT symbol), min(date), max(date) FROM dataset WHERE eligible AND split = '{split}'"
        ).fetchone()
        counts[split] = {"rows": rows, "symbols": symbols, "first_date": _iso(first), "last_date": _iso(last), "file": path.name}
    return counts


def _sql_path(path: Path) -> str:
    return str(path).replace("'", "''")


def _iso(value: object) -> str | None:
    return value.isoformat() if value is not None else None


def _signed_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value * 100:+.{digits}f}%"


def _number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:,.{digits}f}"


def direction_label(log_return: float, flat_band: float) -> str:
    simple = math.exp(log_return) - 1
    if abs(simple) < flat_band:
        return "보합"
    return "상승" if simple > 0 else "하락"


def format_example(
    symbol: str,
    window: list[dict],
    horizon: int,
    series: list[str],
    headlines: list[tuple[str, str, str]],
    flat_band: float,
) -> dict:
    """Build one chat example. Edit this function to change the prompt/answer format."""
    current = window[-1]
    lines = [
        f"종목: {symbol}",
        f"기준일: {current['date'].isoformat()} (장 마감 기준)",
        "",
        f"최근 {len(window)}거래일",
        "날짜 | 종가 | 등락률 | 거래량",
    ]
    for row in window:
        change = math.exp(row["ret_1d"]) - 1 if row["ret_1d"] is not None else None
        lines.append(f"{row['date'].isoformat()} | {_number(row['close'])} | {_signed_pct(change)} | {_number(row['volume'], 0)}")
    lines += [
        "",
        "기술지표",
        f"- 종가/20일선: {_signed_pct(current['close_sma_20'], 1)}",
        f"- 종가/50일선: {_signed_pct(current['close_sma_50'], 1)}",
        f"- 종가/200일선: {_signed_pct(current['close_sma_200'], 1)}",
        f"- RSI(14): {_number(current['rsi_14'], 1)}",
        f"- MACD 히스토그램/종가: {_signed_pct(current['macd_hist_norm'], 2)}",
        f"- 20일 일간 변동성: {_number(current['vol_20d'] * 100 if current['vol_20d'] is not None else None, 2)}%",
    ]
    macro_lines = [
        f"- {MACRO_LABELS.get(sid, sid)}: {_number(current[macro_column(sid)], 2)}"
        for sid in series
        if current.get(macro_column(sid)) is not None
    ]
    if macro_lines:
        lines += ["", "거시지표 (발표 시점 기준)", *macro_lines]
    if headlines:
        lines += ["", "최근 뉴스"]
        lines += [f"- {day} {source}: {headline}" for day, source, headline in headlines]
    lines += ["", f"질문: 향후 {horizon}거래일 동안 이 종목의 수익률은?"]

    label = current[f"fwd_ret_{horizon}d"]
    answer = f"{direction_label(label, flat_band)} ({_signed_pct(math.exp(label) - 1)})"
    return {
        "messages": [
            {"role": "system", "content": "당신은 미국 주식의 최근 시세, 기술지표, 거시지표, 뉴스를 바탕으로 향후 수익률을 예측하는 애널리스트입니다."},
            {"role": "user", "content": "\n".join(lines)},
            {"role": "assistant", "content": answer},
        ]
    }


def _load_news(con, symbol: str) -> tuple[list[datetime], list[tuple[str, str, str]]]:
    rows = con.execute(
        """
        SELECT TRY_CAST(replace(datetime, 'Z', '') AS TIMESTAMP) AS ts, date, source, headline
        FROM src.news WHERE symbol = ? AND headline <> ''
        ORDER BY ts
        """,
        [symbol],
    ).fetchall()
    rows = [row for row in rows if row[0] is not None]
    return [row[0] for row in rows], [(row[1], row[2], row[3]) for row in rows]


def news_before_close(
    stamps: list[datetime], items: list[tuple[str, str, str]], day: date, limit: int, lookback_days: int = 7
) -> list[tuple[str, str, str]]:
    """Headlines published in the lookback window and before 20:00 UTC (US close) on day."""
    cutoff = datetime(day.year, day.month, day.day, 20, 0)
    start = cutoff - timedelta(days=lookback_days)
    lo = bisect.bisect_left(stamps, start)
    hi = bisect.bisect_left(stamps, cutoff)
    return items[max(lo, hi - limit) : hi]


def export_jsonl(con, out_dir: Path, args: argparse.Namespace, series: list[str]) -> dict[str, int]:
    horizon = args.jsonl_horizon
    con.execute(
        f"""
        CREATE TEMP TABLE jsonl_targets AS
        SELECT symbol, date, split FROM (
            SELECT symbol, date, split,
                   row_number() OVER (PARTITION BY split ORDER BY hash(symbol, date, {int(args.seed)})) AS pick
            FROM dataset
            WHERE eligible AND history_rows % {max(1, int(args.jsonl_stride))} = 0
        )
        WHERE pick <= {int(args.jsonl_max_examples)}
        """
    )
    has_news = con.execute("SELECT count(*) FROM src.news").fetchone()[0] > 0 and args.jsonl_news > 0
    columns = ["symbol", "date", "close", "volume", "ret_1d", "close_sma_20", "close_sma_50", "close_sma_200",
               "rsi_14", "macd_hist_norm", "vol_20d", f"fwd_ret_{horizon}d", *(macro_column(sid) for sid in series)]
    cursor = con.execute(
        f"""
        SELECT {', '.join('d.' + c for c in columns)}, t.split AS target_split
        FROM dataset d LEFT JOIN jsonl_targets t USING (symbol, date)
        WHERE d.symbol IN (SELECT DISTINCT symbol FROM jsonl_targets)
        ORDER BY d.symbol, d.date
        """
    )
    names = [*columns, "target_split"]

    handles = {split: (out_dir / f"{split}.jsonl").open("w", encoding="utf-8") for split in SPLITS}
    counts = {split: 0 for split in SPLITS}

    def flush_symbol(rows: list[dict]) -> None:
        if not rows or not any(row["target_split"] for row in rows):
            return
        symbol = rows[0]["symbol"]
        stamps, items = _load_news(con_news, symbol) if has_news else ([], [])
        for index, row in enumerate(rows):
            split = row["target_split"]
            if not split or index + 1 < args.jsonl_window:
                continue
            window = rows[index + 1 - args.jsonl_window : index + 1]
            headlines = news_before_close(stamps, items, row["date"], args.jsonl_news) if has_news else []
            example = format_example(symbol, window, horizon, series, headlines, args.jsonl_flat_band)
            handles[split].write(json.dumps(example, ensure_ascii=False) + "\n")
            counts[split] += 1

    con_news = con.cursor() if has_news else None
    try:
        buffer: list[dict] = []
        while True:
            batch = cursor.fetchmany(50_000)
            if not batch:
                break
            for record in batch:
                row = dict(zip(names, record))
                if buffer and buffer[-1]["symbol"] != row["symbol"]:
                    flush_symbol(buffer)
                    buffer = []
                buffer.append(row)
        flush_symbol(buffer)
    finally:
        for handle in handles.values():
            handle.close()
    return counts


def main() -> int:
    args = parse_args()
    try:
        with pipeline_lock(args.data_dir):
            return export(args)
    except LockHeldError as exc:
        return lock_held_exit(exc)


def export(args: argparse.Namespace) -> int:
    import duckdb  # type: ignore

    horizons = parse_horizons(args.horizons)
    if args.format in ("jsonl", "both") and args.jsonl_horizon not in horizons:
        raise ValueError("--jsonl-horizon must be one of --horizons")
    db_path = get_db_path(args.data_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"market.db not found: {db_path}")

    name = args.name or f"dataset_{datetime.now(UTC).date().isoformat()}"
    out_dir = args.out_dir / name
    tmp_dir = args.out_dir / f".{name}.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory = '{_sql_path(tmp_dir / 'duckdb_spill')}'")
        con.execute(f"SET memory_limit = '{args.memory_limit.replace(chr(39), '')}'")
        con.execute("SET preserve_insertion_order = false")
        con.execute(f"ATTACH '{_sql_path(db_path)}' AS src (READ_ONLY)")
        split_info = build_tables(con, args, horizons)
        series = split_info["series"]
        base_columns = ["symbol", "date", "split", "open", "high", "low", "close", "volume"]
        columns = [*base_columns, *split_info["feature_columns"], *split_info["label_columns"]]

        outputs: dict[str, object] = {}
        if args.format in ("parquet", "both"):
            outputs["parquet"] = export_parquet(con, tmp_dir, columns)
        if args.format in ("jsonl", "both"):
            outputs["jsonl"] = export_jsonl(con, tmp_dir, args, series)
        eligible_symbols = con.execute("SELECT count(DISTINCT symbol) FROM dataset WHERE eligible").fetchone()[0]
    finally:
        con.close()
    shutil.rmtree(tmp_dir / "duckdb_spill", ignore_errors=True)

    metadata = {
        "generated_at": utc_now_iso(),
        "source_db": str(db_path),
        "name": name,
        "format": args.format,
        "filters": {
            "symbols": args.symbol,
            "max_symbols": args.max_symbols,
            "include_all_symbols": args.include_all_symbols,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "min_price": args.min_price,
            "min_dollar_volume_20d": args.min_dollar_volume,
            "min_history_rows": args.min_history,
            "max_abs_log_return": None if args.keep_extreme_moves else args.max_abs_log_return,
            "allow_missing": args.allow_missing,
        },
        "splits": {key: value for key, value in split_info.items() if key not in ("series", "feature_columns", "label_columns")},
        "eligible_symbols": eligible_symbols,
        "columns": {
            "features": {column: FEATURE_DESCRIPTIONS.get(column, f"FRED {column[6:].upper()} as of its approximate release date") for column in split_info["feature_columns"]},
            "labels": {column: f"log(close_t+{column[8:-1]} / close_t), t+h counted in trading rows" for column in split_info["label_columns"]},
        },
        "jsonl": {
            "window": args.jsonl_window,
            "horizon": args.jsonl_horizon,
            "stride": args.jsonl_stride,
            "max_examples_per_split": args.jsonl_max_examples,
            "flat_band": args.jsonl_flat_band,
            "seed": args.seed,
        } if args.format in ("jsonl", "both") else None,
        "outputs": outputs,
        "notes": [
            "Prices are split-adjusted as returned by Twelve Data at fetch time; returns exclude dividends, and --min-price compares against the adjusted close.",
            "The symbol list is today's NASDAQ listing, so delisted companies are missing (survivorship bias).",
            "FRED values are the latest revised vintage, joined by an approximate release date rather than the true first-release date.",
            "The extreme-move filter also checks the label window, so it removes some genuine large moves along with bad ticks; use --keep-extreme-moves to disable.",
        ],
    }
    (tmp_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    tmp_dir.rename(out_dir)
    metadata["out_dir"] = str(out_dir)
    print(json.dumps({key: metadata[key] for key in ("out_dir", "eligible_symbols", "splits", "outputs")}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
