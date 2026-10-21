# data-scrapping

Personal market data collector designed for long-term stable operation on one always-on Linux PC (NucBox M6, Ubuntu 26.04).

Collects US daily OHLCV, macro indicators, and company news conservatively within free API budgets, stores everything in a single DuckDB file (`data/db/market.db`), resumes automatically from where it left off, and publishes a read-only snapshot (`data/db/market_snapshot.db`) for stockpred.

The design, budgets, and the stockpred data contract are in [`docs/DESIGN.md`](docs/DESIGN.md).

## Current scope

- Core universe collected every trading day: 23 ETFs (`config/core_etfs.txt`) + current S&P 500 members + optional `config/core_extra.txt`.
- Rotation universe collected with the remaining budget: the NASDAQ list (`config/symbols.txt`) plus symbols that left the S&P 500. Never-collected symbols first, then the stalest.
- Weekly universe refresh (`src.universe`, sooner when the NASDAQ list is missing or empty): S&P 500 membership from the same public CSV stockpred uses, the NASDAQ list from Twelve Data, and delisted marks for symbols absent from Twelve Data's US catalogs (their rows are kept; they are no longer requested).
- Twelve Data daily OHLCV with per-symbol full replacement (old split basis rows never linger), `adj_close` (dividend-adjusted close) for core symbols, and split events derived from raw vs split-adjusted prices.
- Credit-based budgets: per-key daily budgets with reserve, a per-run credit cap, server-side usage sync (`/api_usage`), UTC day rollover during a run, and sequential multi-key rotation.
- Session awareness: bars of an unfinished session are never stored; NYSE holidays are handled.
- Completion record (`collection_runs`, `symbol_status`) and an atomically replaced snapshot for readers.
- DuckDB schema version 4: `ohlcv`/`indicators` store DATE/DOUBLE/BIGINT values (about half the size of the VARCHAR version 3). Bars are validated in Python before writing; a malformed value fails only that symbol. Typed view names, columns, and types are unchanged.
- `market.db` rebuild (`src.rebuild`): the one-off v3 → v4 conversion, and a periodic rebuild into a new file when `market.db` grows past twice its size right after the last rebuild (deleted rows of daily full replacement are never reclaimed in place). Verified against the original before an atomic swap; the original is kept as a hard-linked backup.
- FRED macro series with `first_seen_at` (when an observation first appeared) and `series_meta` (copyright-restricted series are flagged personal-use only).
- Finnhub company news with paging for truncated responses and per-symbol resumable windows, core symbols first.
- Local technical indicators (SMA/RSI/MACD), recalculated only for symbols whose OHLCV changed.
- Leakage-safe ML dataset export (`src.export_dataset`).
- One pipeline lock (`data/state/pipeline.lock`, flock) shared by `scripts/run_daily.sh` and every CLI that writes data.
- Dry-run mode that calls no collection API and writes no state or DB rows.
- systemd user timer (16:10 New York time, Mon–Fri), progress monitor, daily summary with host healthcheck, DuckDB validation, Discord status, optional rclone backup.

## Layout

```text
config/
  core_etfs.txt        ← core ETFs (tracked)
  core_extra.txt       ← optional extra core symbols (create when needed)
  fred_series.txt
  providers.yaml
  symbols.txt          ← NASDAQ list written by the universe refresh (gitignored)
  symbols.txt.example
data/
  datasets/            ← exported ML datasets (gitignored)
  db/                  ← market.db and market_snapshot.db (gitignored)
  logs/
  raw/                 ← latest raw response per symbol/series (gzip)
  state/               ← state.json, universe.json, fred.json, finnhub_news.json, db_maintenance.json, pipeline.lock
docs/
scripts/
  install_local.sh  lib_env.sh  progress.py  rclone_backup.sh  run_daily.sh
src/
  alpha_vantage.py  atomic_io.py  catalog.py  config.py  corporate_actions.py  discord_status.py  env.py
  export_dataset.py  fetch_tickers.py  finnhub_news.py  fred.py  indicators.py
  lock.py  logging_utils.py  main.py  market_calendar.py  rate_limit.py  rebuild.py
  scheduling.py  snapshot.py  state.py  storage.py  summary.py  twelve_data.py
  universe.py  validate.py  validate_duckdb.py
systemd/
  data-scrapping.service  data-scrapping.timer
  data-scrapping-backup.service  data-scrapping-backup.timer
tests/
```

Run the tests with `.venv/bin/python -m unittest discover -s tests`.

## Install on this PC

```bash
scripts/install_local.sh
```

It creates/uses `.venv`, installs `requirements.txt`, copies `.env.example` to `.env` if missing, installs the systemd **user** units to `~/.config/systemd/user`, and enables `data-scrapping.timer`. The backup timer is installed but not enabled (`ENABLE_BACKUP=1` enables it). See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Configuration

`.env` in the project root (gitignored, see `.env.example`) holds API keys and run settings. Every Python CLI, `scripts/run_daily.sh`, and `scripts/rclone_backup.sh` read it; a variable already set in the environment wins.

Provider budgets live in `config/providers.yaml`. Twelve Data: 800 credits/day per key with a 100 reserve, 7 calls/minute, 1400 credits per run (`max_credits_per_run`, overridden by `TWELVE_MAX_CREDITS`).

## Daily run

```bash
DRY_RUN=1 scripts/run_daily.sh   # plan only: no collection API calls, no state/DB writes
scripts/run_daily.sh             # real run (the timer does this at 16:10 New York time)
```

Stages: universe refresh (weekly) → Twelve Data core → FRED → core snapshot → Twelve Data rotation → indicators → Finnhub news → summary → DuckDB validation → `market.db` rebuild (only past twice the size after the last rebuild) → full snapshot → optional backup. A failing stage is reported and the remaining stages still run; the script exits non-zero at the end if any stage failed. A failed Discord status update (`SEND_DISCORD=1`) is logged and never stops the run.

## Individual collectors

```bash
.venv/bin/python -m src.universe --force                   # refresh S&P 500 / NASDAQ / delisted marks
.venv/bin/python -m src.universe --check                   # fetch and compare without writing (--dry-run: no network)
.venv/bin/python -m src.main --phase core                  # core symbols (2 credits each + split history)
.venv/bin/python -m src.main --phase rotation --max-symbols 100
.venv/bin/python -m src.main --phase core --only NVDA --only BRK.B   # selected symbols only
.venv/bin/python -m src.fred
.venv/bin/python -m src.finnhub_news --symbol MSFT --days-back 365
.venv/bin/python -m src.snapshot --phase full               # completion record + market_snapshot.db
```

Each CLI takes the pipeline lock and exits with code 2 and `{"status": "lock_held"}` while another run is in progress. A run started at any time stores bars only up to the latest finished session (16:10 New York time on NYSE trading days).

## Snapshot for stockpred

Read `data/db/market_snapshot.db` read-only; never open `market.db` from another process while collectors run (DuckDB refuses a second process). Useful objects: `prices_daily` (stockpred `STD_COLS` names), `corporate_actions`, `symbol_status`, `collection_runs`, `macro_typed`, `news_typed`, `series_meta`.

```sql
SELECT status, session_date, core_ok, core_expected FROM collection_runs ORDER BY finished_at DESC LIMIT 1;
SELECT date, open, high, low, close, adj_close, volume, dividends, splits FROM prices_daily WHERE symbol = 'NVDA' ORDER BY date;
```

## Local indicators

```bash
.venv/bin/python -m src.indicators            # symbols whose OHLCV changed since their indicators were written
.venv/bin/python -m src.indicators --full     # every symbol
```

## ML dataset export

```bash
.venv/bin/python -m src.export_dataset --name v1
```

Output in `data/datasets/<name>/`: Parquet features/labels, chat-format JSONL, and `metadata.json`. FRED series flagged personal-use only (copyright-restricted, e.g. BAA10Y, VIXCLS) are excluded unless `--include-personal-use-series` is given. See `--help` for every option and the leakage rules.

## Monitoring

```bash
.venv/bin/python scripts/progress.py          # live progress (running stage, core completion, news progress, key budgets)
.venv/bin/python -m src.summary               # data/logs/daily/<date>.json (table counts, DB and snapshot file sizes, disk, CPU temperature)
.venv/bin/python -m src.validate_duckdb       # data/logs/validation/duckdb_latest.json
.venv/bin/python -m src.rebuild --if-needed --dry-run   # market.db size vs. the rebuild threshold (writes nothing)
journalctl --user -u data-scrapping -n 200 --no-pager
```

## market.db schema and rebuild

```bash
.venv/bin/python -m src.rebuild --to-v4 --dry-run   # current schema version, size, free space
.venv/bin/python -m src.rebuild --to-v4             # one-off: convert a schema version 3 market.db to version 4
.venv/bin/python -m src.rebuild --if-needed         # rebuild only past 2x the size after the last rebuild (run_daily.sh does this)
.venv/bin/python -m src.rebuild                     # rebuild now
```

Code at schema version 4 refuses to write to an older `market.db` (`SchemaVersionError` naming the conversion command); collectors check this before any API call. Each rebuild runs under the pipeline lock, checkpoints the source, builds `.market.db.rebuild.tmp` next to it with an explicit DuckDB `memory_limit` (3 GB for the conversion, 2 GB otherwise), verifies row counts, primary keys, view columns/types and row-hash checksums against the source, hard-links the original as `data/db/market.db.bak-<UTC date>`, and swaps the new file in with `os.replace`. A failure removes only the temporary file. After a successful swap only the newest two `market.db.bak-*` backups are kept (`--keep-backups`); a failed rebuild deletes nothing. The size after each rebuild is recorded in `data/state/db_maintenance.json`; `RUN_DB_REBUILD=0` turns the daily check off.

## Operating principle

A successful run does not mean every symbol was collected. A successful run means the collector processed only what was safe for the current provider budget and saved enough state to continue next time. `collection_runs.status` tells readers whether the core universe is complete for the latest session (prices, `adj_close`, split history, FRED); `missing_symbols`, `missing_splits`, `missing_series`, and `universe_issue` say what is missing.

## Runtime files

Runtime outputs are ignored by git: `data/db/`, `data/raw/`, `data/state/`, `data/logs/`, `data/datasets/`, `config/symbols.txt`, and `.env`.
