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

Not included yet: Alpha Vantage fallback, Finnhub news, FRED macro data, Discord alerts, systemd timers, rclone backups, DuckDB validation, or local technical indicators.

## Layout

```text
config/
  providers.yaml
  symbols.txt
data/
  logs/
  ohlcv/
  raw/
  state/
src/
  config.py
  lock.py
  logging_utils.py
  main.py
  rate_limit.py
  state.py
  storage.py
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

## Operating principle

A successful run does not mean every symbol was collected. A successful run means the collector processed only what was safe for the current provider budget and saved enough state to continue next time.

## Runtime files

Runtime outputs are intentionally ignored by git:

- `data/raw/` provider JSON responses.
- `data/ohlcv/` symbol CSV files.
- `data/state/state.json` and `data/state/collector.lock`.
- `data/logs/collector.log` and `data/logs/runs/*.json`.

These files should exist on the Raspberry Pi/SSD during operation, but they should not be committed.
