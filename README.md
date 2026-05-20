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
- Lock file protection so two collector runs do not overlap.
- Local `data/logs/collector.log` and per-run JSON summaries under `data/logs/runs/`.
- Local technical indicator calculation (SMA/RSI/MACD) stored in DuckDB.
- Minimal FRED macro series collection.
- Minimal Finnhub company news collection.
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
  db/          ← market.db (DuckDB, gitignored)
  logs/
  raw/
  state/
scripts/
  install_pi.sh
  rclone_backup.sh
  run_daily.sh
src/
  __init__.py
  alpha_vantage.py
  config.py
  discord_status.py
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
  test_rate_limiter.py
  test_twelve_data_retry.py
```

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

All collector entry points (`src/main.py`, `src/fred.py`, `src/finnhub_news.py`, `src/alpha_vantage.py`) load `.env` automatically on startup. For `scripts/run_daily.sh`, `.env` is also loaded automatically.

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

By default, a symbol that succeeded today is skipped until the next UTC day. Use `--force` only when you intentionally want to re-fetch symbols that already succeeded today. The collector also creates `data/state/collector.lock` during a run to prevent overlapping executions.

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
python3 -m src.finnhub_news --limit 2 --days-back 7
```

Dry-run:

```bash
python3 -m src.finnhub_news --dry-run --limit 2
```

## FRED macro data

```bash
python3 -m src.fred --limit 2
```

Dry-run:

```bash
python3 -m src.fred --dry-run --limit 2
```

## Local indicators

Calculate indicators from OHLCV data already in DuckDB:

```bash
python3 -m src.indicators --symbol AAPL
```

Indicator output (SMA 20/50/200, RSI 14, MACD 12/26/9) is written to the `indicators` table in `data/db/market.db`.

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
- `data/state/state.json` and `data/state/collector.lock`.
- `data/logs/` — collector logs, run summaries, daily summaries, validation reports.
- `config/symbols.txt` — runtime symbol list generated by ticker refresh.
- `.env` — local API keys.
