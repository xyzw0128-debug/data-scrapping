# ARCHITECTURE

## 디렉토리 구조

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
scripts/
  rclone_backup.sh
  run_daily.sh
src/
  __init__.py
  alpha_vantage.py
  config.py
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
- `src/finnhub_news.py`: Finnhub company news 수집 및 심볼별 CSV 업서트.
- `src/fred.py`: FRED 거시 지표 series 수집 및 CSV 저장.
- `src/indicators.py`: 로컬 OHLCV CSV 기반 기술지표(SMA/RSI/MACD) 계산.
- `src/summary.py`: 수집 결과/상태/디스크/CPU 온도 일일 요약 생성(옵션 Discord 전송).
- `src/validate.py`: OHLCV/indicators/macro/news CSV 경량 검증 리포트 생성.
- `src/config.py`: symbols/providers 설정 로딩 및 provider 설정 객체화.
- `src/rate_limit.py`: 일일 예산/리저브 기반 API 호출 가능 여부 판단.
- `src/state.py`: `state.json` 기본 스키마, 로드/저장, provider 일일 상태 리셋.
- `src/storage.py`: raw JSON/CSV 저장, OHLCV 정규화, 런 요약 저장.
- `src/lock.py`: 중복 실행 방지 파일 락.
- `src/logging_utils.py`: 콘솔 + 파일 로깅 설정.
- `src/__init__.py`: 패키지 메타 정보(설명 문자열).

## 데이터 흐름
수집 → raw JSON → CSV → indicators → summary → validation

- 수집기(`main.py`, `alpha_vantage.py`, `fred.py`, `finnhub_news.py`)가 API에서 데이터를 가져옵니다.
- 원본 응답은 `data/raw/<provider>/`에 JSON으로 저장됩니다.
- 정규화된 CSV가 도메인별(`data/ohlcv`, `data/macro`, `data/news`)로 저장됩니다.
- `indicators.py`가 `data/ohlcv/*.csv`를 읽어 `data/indicators/*.csv`를 생성합니다.
- `summary.py`가 상태/파일/하드웨어 정보를 `data/logs/daily/<date>.json`으로 요약합니다.
- `validate.py`가 CSV 품질 검증 결과를 `data/logs/validation/latest.json`에 기록합니다.

## 상태 파일 구조 요약
기본 상태 파일 경로는 `data/state/state.json`이며 핵심 키는 다음과 같습니다.
- `schema_version`: 상태 스키마 버전.
- `providers`: provider별 일일 사용 상태.
  - 예: `date`, `calls_used_today`, `daily_limit`, `daily_reserve`, `last_reset_utc`.
- `symbols`: 심볼별 provider 처리 상태.
  - 예: `status`, `updated_at`, `last_success_date`, `last_success_at`, `rows`, `error`, `note`.
- `runs`: 최근 실행 요약 배열(최대 20개 유지).
