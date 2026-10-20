# ARCHITECTURE

## 디렉토리 구조

```text
config/
  fred_series.txt
  providers.yaml
  symbols.txt
  symbols.txt.example
data/
  indicators/
  logs/
  macro/
  news/
  ohlcv/
  raw/
  state/
scripts/
  rclone_backup.sh
  run_daily.sh
src/
  __init__.py
  alpha_vantage.py
  config.py
  export_dataset.py
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
  fetch_tickers.py
systemd/
  data-scrapping-backup.service
  data-scrapping-backup.timer
  data-scrapping.env.example
  data-scrapping.service
  data-scrapping.timer
```

## src 모듈 역할
- `src/main.py`: Twelve Data 기반 일일 OHLCV 수집의 기본 CLI 엔트리포인트.
- `src/alpha_vantage.py`: Alpha Vantage 일일 OHLCV 백업 수집기.
- `src/finnhub_news.py`: Finnhub company news 수집. 심볼 로테이션과 심볼별 재개 상태(`data/state/finnhub_news.json`) 관리, DuckDB `news` 테이블 업서트.
- `src/export_dataset.py`: `market.db`를 읽기 전용으로 붙여 시점 누수 없는 ML 데이터셋(Parquet 피처/라벨, chat JSONL, metadata.json)을 `data/datasets/<name>/`에 생성.
- `src/fetch_tickers.py`: Twelve Data stocks endpoint에서 exchange별 티커 목록을 받아 `config/symbols.txt`를 갱신.
- `src/fred.py`: FRED 거시 지표 series 수집 및 CSV 저장.
- `src/indicators.py`: DuckDB OHLCV 기반 기술지표(SMA/RSI/MACD) 계산. 기본은 OHLCV `updated_at`이 지표보다 최신인 심볼만 배치로 재계산.
- `src/summary.py`: 수집 결과/상태/디스크/CPU 온도 일일 요약 생성(옵션 Discord 전송).
- `src/validate.py`: OHLCV/indicators/macro/news CSV 경량 검증 리포트 생성.
- `src/validate_duckdb.py`: DuckDB SQL 기반 OHLCV 무결성(중복/가격 범위/거래량) 검증 리포트 생성.
- `src/config.py`: symbols/providers 설정 로딩 및 provider 설정 객체화.
- `src/rate_limit.py`: 일일 예산/리저브 기반 API 호출 가능 여부 판단.
- `src/state.py`: `state.json` 기본 스키마, 로드/저장, provider 일일 상태 리셋.
- `src/storage.py`: raw JSON 저장, OHLCV 정규화, DuckDB 스키마/타입 뷰 생성, 배치 upsert(`bulk_upsert`), 런 요약 저장.
- `src/lock.py`: 중복 실행 방지 파일 락. 기록된 PID가 종료된 lock은 자동 교체.
- `src/logging_utils.py`: 콘솔 + 파일 로깅 설정.
- `src/__init__.py`: 패키지 메타 정보(설명 문자열).

## 데이터 흐름
수집 → raw JSON → CSV → indicators → summary → validation

- `fetch_tickers.py`가 필요 시 Twelve Data stocks endpoint에서 티커 목록을 갱신합니다.
- 수집기(`main.py`, `alpha_vantage.py`, `fred.py`, `finnhub_news.py`)가 API에서 데이터를 가져옵니다.
- 원본 응답은 `data/raw/<provider>/`에 JSON으로 저장됩니다.
- 정규화된 CSV가 도메인별(`data/ohlcv`, `data/macro`, `data/news`)로 저장됩니다.
- `indicators.py`가 `data/ohlcv/*.csv`를 읽어 `data/indicators/*.csv`를 생성합니다.
- `summary.py`가 상태/파일/하드웨어 정보를 `data/logs/daily/<date>.json`으로 요약합니다.
- `validate.py`가 CSV 품질 검증 결과를 `data/logs/validation/latest.json`에 기록합니다.
- 현재 저장소는 CSV 대신 DuckDB 단일 DB(`data/db/market.db`)의 `ohlcv`/`indicators`/`macro`/`news` 테이블을 사용합니다.
- `export_dataset.py`가 DB를 읽어 `data/datasets/<name>/`에 학습용 데이터셋을 만듭니다(수집 파이프라인과 별도로 필요할 때 실행).

## 상태 파일 구조 요약
기본 상태 파일 경로는 `data/state/state.json`이며 핵심 키는 다음과 같습니다.
- `schema_version`: 상태 스키마 버전.
- `providers`: provider별 일일 사용 상태.
  - 예: `date`, `calls_used_today`, `daily_limit`, `daily_reserve`, `last_reset_utc`.
- `symbols`: 심볼별 provider 처리 상태.
  - 예: `status`, `updated_at`, `last_success_date`, `last_success_at`, `rows`, `error`, `note`.
- `runs`: 최근 실행 요약 배열(최대 20개 유지).
