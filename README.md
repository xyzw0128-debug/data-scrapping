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
  main.py
  rate_limit.py
  state.py
  storage.py
```

## Configuration

Edit `config/symbols.txt` and keep the first run small. One symbol goes on each line.

Provider budgets live in `config/providers.yaml`. The default Twelve Data budget is deliberately conservative and keeps a reserve below the configured daily limit.

## Dry run

Use dry-run mode first. It validates config loading, state creation, symbol selection, and writable storage paths without calling an external API.

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
```

## Operating principle

A successful run does not mean every symbol was collected. A successful run means the collector processed only what was safe for the current provider budget and saved enough state to continue next time.
