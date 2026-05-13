# data-scrapping

Personal Raspberry Pi-friendly market data collector.

The Stage 1 MVP intentionally stays small: it reads a short symbol list, respects a conservative daily API budget, stores raw provider JSON, writes simple symbol-level OHLCV CSV files, and records resumable progress in `data/state/state.json`.

## Current scope

- Twelve Data daily OHLCV collection.
- `state.json` based resume tracking.
- Provider daily limit and reserve budget checks.
- Raw JSON archival under `data/raw/`.
- Simple OHLCV CSV output under `data/ohlcv/`.
- Dry-run mode for testing without API calls.
- Lock file protection so two collector runs do not overlap.
- Local `data/logs/collector.log` and per-run JSON summaries under `data/logs/runs/`.
- Local technical indicator CSV generation from collected OHLCV data.
- Minimal FRED macro series collection into `data/macro/`.
- Minimal Finnhub company news collection into `data/news/`.
- Daily summary and local hardware healthcheck JSON generation.

Not included yet: Alpha Vantage fallback, systemd timers, rclone backups, or DuckDB validation.

## Layout

```text
config/
  fred_series.txt
  providers.yaml
  symbols.txt
data/
  indicators/
  logs/
  macro/
  news/
  ohlcv/
  raw/
  state/
src/
  config.py
  lock.py
  finnhub_news.py
  fred.py
  indicators.py
  logging_utils.py
  main.py
  rate_limit.py
  state.py
  storage.py
  summary.py
```

## Configuration

Edit `config/symbols.txt` and keep the first run small. One symbol goes on each line.

Provider budgets live in `config/providers.yaml`. The default Twelve Data budget is deliberately conservative and keeps a reserve below the configured daily limit.

## Dry run

Use dry-run mode first. It validates config loading, state creation, symbol selection, the run lock, logging, summary generation, and writable storage paths without calling an external API.

```bash
python -m src.main --dry-run
```

## Real Twelve Data run

Set your Twelve Data API key in the environment and run the collector.

```bash
export TWELVE_DATA_API_KEY="your-api-key"
python -m src.main --provider twelve_data
```

Optional overrides:

```bash
python -m src.main --provider twelve_data --max-symbols 2
python -m src.main --provider twelve_data --force
```

By default, a symbol that succeeded today is skipped until the next UTC day. Use `--force` only when you intentionally want to re-fetch symbols that already succeeded today. The collector also creates `data/state/collector.lock` during a run to prevent overlapping executions.




## Finnhub company news

After setting a Finnhub API key, collect a small news batch:

```bash
export FINNHUB_API_KEY="your-finnhub-api-key"
python -m src.finnhub_news --limit 2 --days-back 7
```

Use dry-run mode to verify symbol/date selection without API calls:

```bash
python -m src.finnhub_news --dry-run --limit 2
```

News CSV files are written to `data/news/FINNHUB_<SYMBOL>.csv`.

## FRED macro data

After setting a FRED API key, collect a small macro batch:

```bash
export FRED_API_KEY="your-fred-api-key"
python -m src.fred --limit 2
```

Use dry-run mode to verify series selection without API calls:

```bash
python -m src.fred --dry-run --limit 2
```

Macro CSV files are written to `data/macro/FRED_<SERIES_ID>.csv`.

## Local indicators

After at least one OHLCV CSV exists under `data/ohlcv/`, calculate local indicators without spending API calls:

```bash
python -m src.indicators --symbol AAPL
```

Indicator output is written to `data/indicators/<SYMBOL>.csv` and includes SMA 20/50/200, RSI 14, and MACD 12/26/9 columns.


## Daily summary and healthcheck

Generate a local summary of collected files, state, disk usage, and Raspberry Pi CPU temperature when available:

```bash
python -m src.summary
```

The summary is written to `data/logs/daily/<YYYY-MM-DD>.json`. To send the same summary to Discord:

```bash
export DISCORD_WEBHOOK_URL="your-discord-webhook"
python -m src.summary --send-discord
```

## Operating principle

A successful run does not mean every symbol was collected. A successful run means the collector processed only what was safe for the current provider budget and saved enough state to continue next time.

## Runtime files

Runtime outputs are intentionally ignored by git:

- `data/raw/` provider JSON responses.
- `data/ohlcv/` symbol CSV files.
- `data/indicators/` symbol indicator CSV files.
- `data/macro/` FRED macro CSV files.
- `data/news/` Finnhub company news CSV files.
- `data/state/state.json` and `data/state/collector.lock`.
- `data/logs/collector.log`, `data/logs/runs/*.json`, and `data/logs/daily/*.json`.

These files should exist on the Raspberry Pi/SSD during operation, but they should not be committed.
