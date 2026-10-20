# PROVIDERS

## Twelve Data
- **역할**: 기본 일일 OHLCV 수집 provider (`src.main`) 및 NASDAQ 티커 목록 조회(`src.fetch_tickers`).
- **알려진 한도(이 저장소 기본 설정)**: `daily_limit: 800`, `daily_reserve: 100`, `per_minute_limit: 8`, `max_symbols_per_run: 700`.
- **실제 사용 목표**: 기본 수집 경로로 사용하되, 일일 한도를 다 쓰지 않고 reserve를 남겨 장기 안정 운영.
- **주의사항**:
  - `state.json`의 `calls_used_today`를 기준으로 호출 가능 여부를 판단합니다.
  - `src.fetch_tickers`는 stocks endpoint를 호출해 `config/symbols.txt`를 원자적으로 갱신하며, 기본 exchange는 NASDAQ입니다.
  - `--force`는 당일 성공 심볼 재수집이므로 필요할 때만 사용합니다.

## Alpha Vantage
- **역할**: 보수적 백업 OHLCV 수집 provider (`src.alpha_vantage`).
- **알려진 한도(이 저장소 기본 설정)**: `daily_limit: 25`, `daily_reserve: 5`, `per_minute_limit: 5`, `max_symbols_per_run: 5`.
- **실제 사용 목표**: 주 수집기가 아니라 fallback/검증용 소량 수집.
- **주의사항**:
  - `TIME_SERIES_DAILY` + `outputsize=compact` 기반이라 대규모 백필 경로로 사용하지 않습니다.
  - 응답의 `Note`/`Information`/`Error Message`를 오류로 처리합니다.

## FRED
- **역할**: 거시 지표 시리즈 수집 (`src.fred`).
- **알려진 한도**: 코드/기본 설정 파일에 수치형 제한은 별도 정의되어 있지 않음.
- **실제 사용 목표**: `config/fred_series.txt`의 소규모 시리즈(현재 11개)를 주기적으로 적재.
- **주의사항**:
  - 기본은 목록 전체 수집이며, `--limit`로 런당 처리량을 제한할 수 있습니다.
  - 원본 응답은 `data/raw/fred/`에 저장됩니다.

## Finnhub
- **역할**: 심볼별 company news 수집 (`src.finnhub_news`).
- **알려진 한도**: 무료 플랜 분당 60회. 이 저장소는 `--per-minute-limit` 기본 30으로 절반만 사용.
- **실제 사용 목표**: 런마다 `--limit`(run_daily 기본 300)개 심볼을 로테이션하며 뉴스 누적 수집.
- **주의사항**:
  - DuckDB `news` 테이블에 `(symbol, id)` 기준으로 업서트됩니다.
  - 심볼 선택 순서: 최근 시도가 가장 오래된 심볼(미수집 우선) → 최근 60거래일 평균 거래대금 큰 순.
  - 심볼별 상태는 `data/state/finnhub_news.json`에 저장되며, 다음 수집은 마지막 성공 `to` 날짜 전날부터 이어집니다(최대 `--max-days-back`일).
  - HTTP 429 응답 시 60초 대기 후 1회 재시도합니다.

## providers.yaml 기본값과 이유
- `twelve_data`와 `alpha_vantage`만 `config/providers.yaml`에 정의되어 있습니다.
- 기본값은 모두 **보수적 운영**을 위한 값입니다.
  - 일일 제한(`daily_limit`)보다 낮은 실사용 예산을 만들기 위해 `daily_reserve`를 분리.
  - `max_symbols_per_run`을 작게 유지해 Raspberry Pi 환경에서 한 번의 실행 부담을 제한.
  - `outputsize`는 Twelve Data(5000)와 Alpha Vantage(compact)의 의도된 용도를 반영.
