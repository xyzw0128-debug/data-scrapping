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
- **실제 사용 목표**: `config/fred_series.txt`의 소규모 시리즈를 주기적으로 적재.
- **주의사항**:
  - `--limit`로 런당 처리량을 제한할 수 있습니다.
  - 원본 응답은 `data/raw/fred/`에 저장됩니다.

## Finnhub
- **역할**: 심볼별 company news 수집 (`src.finnhub_news`).
- **알려진 한도**: 코드/기본 설정 파일에 수치형 제한은 별도 정의되어 있지 않음.
- **실제 사용 목표**: 지정 기간(`--days-back` 또는 from/to)의 뉴스 배치 수집.
- **주의사항**:
  - 심볼별 CSV(`FINNHUB_<SYMBOL>.csv`)에 ID/URL 기준 업서트됩니다.
  - `--limit`로 런당 심볼 수를 제한할 수 있습니다.

## providers.yaml 기본값과 이유
- `twelve_data`와 `alpha_vantage`만 `config/providers.yaml`에 정의되어 있습니다.
- 기본값은 모두 **보수적 운영**을 위한 값입니다.
  - 일일 제한(`daily_limit`)보다 낮은 실사용 예산을 만들기 위해 `daily_reserve`를 분리.
  - `max_symbols_per_run`을 작게 유지해 Raspberry Pi 환경에서 한 번의 실행 부담을 제한.
  - `outputsize`는 Twelve Data(5000)와 Alpha Vantage(compact)의 의도된 용도를 반영.
