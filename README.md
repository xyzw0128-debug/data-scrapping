# data-scrapping

Personal market data collector designed for long-term stable operation.

Collects daily OHLCV, macro indicators, and company news conservatively within API budgets, stores everything in a single DuckDB file (`data/db/market.db`), and resumes automatically from where it left off.

## Current scope

- Twelve Data daily OHLCV collection (primary).
- Alpha Vantage daily OHLCV backup collector.
- Twelve Data NASDAQ ticker list refresh into `config/symbols.txt`.
- `state.json` based resume tracking.
- Provider daily limit and reserve budget checks.
- Raw JSON archival under `data/raw/`.
- DuckDB storage (`data/db/market.db`) for OHLCV, indicators, macro, and news.
- Dry-run mode for testing without API calls.
- Lock file protection so two collector runs do not overlap; a lock left by a killed run (closed terminal) is detected and replaced.
- Local `data/logs/collector.log` and per-run JSON summaries under `data/logs/runs/`.
- Local technical indicator calculation (SMA/RSI/MACD) stored in DuckDB, recalculated only for symbols whose OHLCV changed.
- Batched DuckDB writes (staged newline-delimited JSON + one `INSERT OR REPLACE`) for OHLCV, indicators, macro, and news.
- Typed DuckDB views (`ohlcv_typed`, `indicators_typed`, `macro_typed`, `news_typed`) over the VARCHAR tables.
- FRED macro series collection.
- Finnhub company news collection that rotates through symbols and resumes per symbol.
- Leakage-safe ML dataset export (`src.export_dataset`): Parquet features/labels and chat-format JSONL for LLM fine-tuning.
- Daily summary and local hardware healthcheck JSON generation.
- Daily orchestration script and systemd service/timer templates.
- Lightweight DuckDB-based validation reports.
- rclone backup script and systemd backup timer templates.
- Discord webhook live status updates during collection runs.
- `.env` file auto-load in all collector entry points.

## Layout

```text
config/
  fred_series.txt
  providers.yaml
  symbols.txt.example
data/
  datasets/    ← exported ML datasets (gitignored)
  db/          ← market.db (DuckDB, gitignored)
  logs/
  raw/
  state/
scripts/
  install_pi.sh
  progress.py
  rclone_backup.sh
  run_daily.sh
src/
  __init__.py
  alpha_vantage.py
  config.py
  discord_status.py
  export_dataset.py
  fetch_tickers.py
  finnhub_news.py
  fred.py
  indicators.py
  lock.py
  logging_utils.py
  main.py
  rate_limit.py
  state.py
  storage.py
  summary.py
  validate.py
  validate_duckdb.py
systemd/
  data-scrapping-backup.service
  data-scrapping-backup.timer
  data-scrapping.env.example
  data-scrapping.service
  data-scrapping.timer
tests/
  test_alpha_vantage_storage.py
  test_export_dataset.py
  test_incremental_jobs.py
  test_lock.py
  test_rate_limiter.py
  test_storage_bulk.py
  test_twelve_data_retry.py
```

Run the tests with `python3 -m unittest discover -s tests`.

## Configuration

Copy `config/symbols.txt.example` to `config/symbols.txt`, then keep the first run small. One symbol goes on each line.

Provider budgets live in `config/providers.yaml`. The default Twelve Data budget is deliberately conservative and keeps a reserve below the configured daily limit.

## Environment variables

Create a `.env` file in the project root (gitignored):

```
TWELVE_DATA_API_KEY=your-key
FRED_API_KEY=your-key
FINNHUB_API_KEY=your-key
ALPHA_VANTAGE_API_KEY=your-key
DISCORD_WEBHOOK_URL=
```

All collector entry points (`src/main.py`, `src/fred.py`, `src/finnhub_news.py`, `src/alpha_vantage.py`) load `.env` automatically on startup, so `scripts/run_daily.sh` runs get API keys from `.env`. The shell script's own toggles (`RUN_*`, `*_LIMIT`, `SEND_DISCORD`, `RUN_BACKUP`, ...) are read from the shell environment, not from `.env`; export them or prefix the command (`RUN_BACKUP=1 scripts/run_daily.sh`) to change them.

## Dry run

Use dry-run mode first. It validates config loading, state creation, symbol selection, the run lock, logging, summary generation, and writable storage paths without calling an external API.

```bash
python3 -m src.main --dry-run
```

## Refresh Twelve Data ticker list

Fetch the NASDAQ stock list from Twelve Data and write it to `config/symbols.txt`:

```bash
python3 -m src.fetch_tickers
```

Use `--dry-run` to print counts without writing, `--exchange` to request another exchange, and `--append` to merge with the current symbol file.

## Real Twelve Data run

```bash
python3 -m src.main --provider twelve_data
```

Optional overrides:

```bash
python3 -m src.main --provider twelve_data --max-symbols 2
python3 -m src.main --provider twelve_data --force
```

By default, a symbol that succeeded today is skipped until the next UTC day. Use `--force` only when you intentionally want to re-fetch symbols that already succeeded today. The collector also creates `data/state/collector.lock` during a run to prevent overlapping executions. If the PID recorded in an existing lock is no longer running (for example the terminal window was closed mid-run), the next run replaces the lock instead of refusing to start.

## Alpha Vantage OHLCV backup

Alpha Vantage is configured as a conservative backup source. It uses `TIME_SERIES_DAILY` with `outputsize=compact`, so treat it as a small fallback/verification collector rather than the main backfill path.

```bash
python3 -m src.alpha_vantage --max-symbols 1
```

API-free check:

```bash
python3 -m src.alpha_vantage --dry-run --max-symbols 1
```

## Finnhub company news

```bash
python3 -m src.finnhub_news --limit 300 --days-back 30
```

Each run picks the `--limit` symbols that were attempted least recently (never-fetched symbols first, most liquid first) and calls Finnhub at most `--per-minute-limit` times per minute (default 30; the free plan allows 60). Per-symbol progress lives in `data/state/finnhub_news.json`: a symbol fetched before resumes from the day before its last successful `to` date (capped at `--max-days-back`, default 365), and a new symbol starts `--days-back` days ago. `--from-date` forces one fixed window for every symbol. Tickers that are not plain symbols (for example `!OTC/QHUOY`) are skipped.

Dry-run:

```bash
python3 -m src.finnhub_news --dry-run --limit 2
```

## FRED macro data

```bash
python3 -m src.fred
```

Collects every series in `config/fred_series.txt` (pass `--limit N` to process only the first N). Dry-run:

```bash
python3 -m src.fred --dry-run --limit 2
```

## Local indicators

Calculate indicators from OHLCV data already in DuckDB:

```bash
python3 -m src.indicators            # only symbols whose OHLCV changed since their indicators were written
python3 -m src.indicators --full     # recalculate every symbol (~1.5 minutes for ~3,700 symbols)
python3 -m src.indicators --symbol AAPL
```

Indicator output (SMA 20/50/200, RSI 14, MACD 12/26/9) is written to the `indicators` table in `data/db/market.db`. Symbols are loaded and written in batches of `--batch-size` (default 200) per transaction.

## Typed views

The raw tables store provider values as `VARCHAR`. Typed views with `DATE`/`DOUBLE`/`TIMESTAMP` columns are created automatically the next time any collector writes to the DB:

```sql
SELECT symbol, date, close, volume FROM ohlcv_typed WHERE symbol = 'AAPL' ORDER BY date DESC LIMIT 5;
SELECT series_id, date, value FROM macro_typed WHERE series_id = 'DGS10';   -- FRED "." becomes NULL
```

## ML dataset export

Build a dataset for model training or LLM fine-tuning from `market.db` (read-only):

```bash
python3 -m src.export_dataset --name v1                      # Parquet + JSONL, all eligible symbols (~1 minute)
python3 -m src.export_dataset --name sample --max-symbols 30 # quick sample of the 30 most liquid symbols
python3 -m src.export_dataset --name pq --format parquet --horizons 1,5,20
```

Output in `data/datasets/<name>/`:

- `train.parquet`, `val.parquet`, `test.parquet` — one row per symbol-day: OHLCV, features (`ret_1d/5d/20d`, `gap`, `hl_range`, `vol_20d`, `volume_z_20`, `dollar_volume_20d`, `close_sma_20/50/200`, `rsi_14`, `macd_norm`, `macd_hist_norm`), one `macro_<series>` column per FRED series, and labels `fwd_ret_<h>d` (log return from close t to close t+h trading rows).
- `train.jsonl`, `val.jsonl`, `test.jsonl` — chat-format examples (`{"messages": [system, user, assistant]}`): the prompt shows the last 20 trading days, indicators, macro values, and headlines published before that day's US close; the answer is `상승/하락/보합 (+x.xx%)` for the next 5 trading days. Edit `format_example()` in `src/export_dataset.py` to change the prompt.
- `metadata.json` — filters, split boundaries, column descriptions, row counts, and known caveats.

Leakage rules: features use only rows on or before the sample date; splits are chronological (default test = last 365 days, val = the 365 days before) with an embargo gap so train labels never reach into val/test; FRED values are joined by an approximate publication date (daily series +1 day, monthly series one period + 20 days, H.10 FX series +7 days). Default filters: plain A–Z tickers without warrant/rights/unit suffixes, close ≥ $5, 20-day average dollar volume ≥ $1M, 200 rows of history, and no one-day move beyond doubling/halving in the feature or label window. See `python3 -m src.export_dataset --help` for every option.

Load it with DuckDB or pandas:

```python
import duckdb
train = duckdb.sql("SELECT * FROM 'data/datasets/v1/train.parquet'").df()
```

## Daily summary and healthcheck

```bash
python3 -m src.summary
```

The summary is written to `data/logs/daily/<YYYY-MM-DD>.json`. To send to Discord:

```bash
python3 -m src.summary --send-discord
```

## Data validation

Lightweight DuckDB-based validation (duplicate dates, invalid price/volume ranges):

```bash
python3 -m src.validate_duckdb
```

Report output defaults to `data/logs/validation/duckdb_latest.json`.

## Daily orchestration

```bash
DRY_RUN=1 scripts/run_daily.sh
```

For a real run:

```bash
scripts/run_daily.sh
```

Stages run in order (OHLCV → indicators → FRED → Finnhub → summary → validation). A failing stage is reported and the remaining stages still run; the script exits non-zero at the end if any stage failed. Defaults: all FRED series, 300 Finnhub symbols per run with a 30-day first lookback (`FRED_LIMIT`, `FINNHUB_LIMIT`, `FINNHUB_DAYS_BACK` override them).

API keys are loaded from `.env` automatically. Systemd templates live under `systemd/`. Copy `systemd/data-scrapping.env.example` to `/etc/data-scrapping.env`, add real keys, then install the service and timer.

## rclone backup

```bash
export RCLONE_DEST="remote:data-scrapping"
RCLONE_DRY_RUN=1 scripts/rclone_backup.sh
```

Use `BACKUP_MODE=copy` by default. Only use `BACKUP_MODE=sync` when you intentionally want the remote to mirror local deletions.

## Discord live status

When `SEND_DISCORD=1` is set, `run_daily.sh` sends and updates a live status message in Discord showing collection progress, current symbol, backup status, and errors.

## Operating principle

A successful run does not mean every symbol was collected. A successful run means the collector processed only what was safe for the current provider budget and saved enough state to continue next time.

## Runtime files

Runtime outputs are intentionally ignored by git:

- `data/db/market.db` — DuckDB database.
- `data/raw/` — provider JSON responses.
- `data/state/state.json`, `data/state/finnhub_news.json`, and `data/state/collector.lock`.
- `data/datasets/` — exported ML datasets.
- `data/logs/` — collector logs, run summaries, daily summaries, validation reports.
- `config/symbols.txt` — runtime symbol list generated by ticker refresh.
- `.env` — local API keys.
