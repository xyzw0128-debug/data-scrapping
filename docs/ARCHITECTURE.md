# ARCHITECTURE

상세 설계(예산, 완료 판정, stockpred 데이터 계약)는 `docs/DESIGN.md`에 있습니다.

## 디렉토리 구조

```text
config/
  core_etfs.txt  fred_series.txt  providers.yaml  symbols.txt  symbols.txt.example
data/
  db/        market.db, market_snapshot.db, market.db.bak-<UTC 날짜>(재구성 전 원본, 손으로 지움)
  logs/      collector.log, runs/, daily/, validation/
  raw/       twelve_data/{splits,all,none}/<symbol>.json.gz, fred/, finnhub_news/
  state/     state.json, universe.json, sp500_hist.csv, fred.json, finnhub_news.json, db_maintenance.json, pipeline.lock
scripts/
  install_local.sh  lib_env.sh  progress.py  rclone_backup.sh  run_daily.sh
src/
systemd/
  data-scrapping.service  data-scrapping.timer  data-scrapping-backup.service  data-scrapping-backup.timer
```

## src 모듈 역할
- `src/main.py`: Twelve Data 일봉 수집 CLI. `--phase core`(핵심 종목: `adjust=splits`+`adjust=all`, 분할 이력 보충) / `rotation`(순환 종목) / `all`.
- `src/twelve_data.py`: Twelve Data 클라이언트. 엔드포인트별 크레딧 비용, 오류 분류(분당 한도/일일 소진/종목 없음), 키 전환, `/api_usage` 동기화.
- `src/rate_limit.py`: `PerMinuteRateLimiter`, 키별 일일 예산(`DailyBudget`), 순차 키 로테이션(`KeyPool`, UTC 날짜 재확인), 실행당 크레딧 상한(`RunCreditCap`).
- `src/scheduling.py`: 순환 수집 순서(미수집 → 오래된 순)와 실패 재시도 대기(1·2·4·8·16·30일, 404는 30일).
- `src/universe.py`: 핵심/순환 종목 목록, S&P 500 구성종목(fja05680 CSV), NASDAQ 목록, 상장폐지 표시. `python -m src.universe`로 갱신.
- `src/market_calendar.py`: NYSE 정기 휴장일과 기준 거래일(마지막으로 끝난 정규장) 계산.
- `src/corporate_actions.py`: `adjust=none`/`adjust=splits` 종가 비율로 분할 이벤트 계산, 분할 기준 변경 감지.
- `src/snapshot.py`: 완료 판정(`complete/partial/failed`), `symbol_status`·`collection_runs` 기록, `market_snapshot.db` 원자적 교체.
- `src/fred.py`: FRED 관측값(`first_seen_at` 보존 업서트), 시리즈 메타(`series_meta`, 권리 문구 감지), `data/state/fred.json`.
- `src/finnhub_news.py`: Finnhub 뉴스. 잘린 응답 페이지 나눠 받기, 종목별 이어받기 창, 핵심 종목 우선.
- `src/indicators.py`: OHLCV가 바뀐 종목만 SMA/RSI/MACD 재계산(기본은 OHLCV가 있는 모든 종목).
- `src/storage.py`: DuckDB 스키마 버전 4 생성·버전 확인(다른 버전은 `SchemaVersionError`), 뷰(`*_typed`, `prices_daily`), OHLCV 값 정규화(날짜 ISO·유한 실수·정수 거래량), 테이블 타입으로 읽는 배치 업서트, 종목 통째 교체, 분할 이벤트 저장, gzip 원본 저장.
- `src/rebuild.py`: `market.db`를 새 파일로 재구성(잠금 안 `CHECKPOINT` → 같은 디렉터리 임시 파일 → 원본과 비교 검증 → 하드링크 백업 → `os.replace`). `--to-v4`(스키마 버전 3 → 4 변환, 1회), `--if-needed`(마지막 재구성 직후 크기의 2배를 넘으면 정기 재구성).
- `src/lock.py`: 파이프라인 잠금(flock). `run_daily.sh`가 잡으면 하위 CLI는 상속하고, 단독 실행 CLI는 직접 잡습니다.
- `src/export_dataset.py`: 시점 누수 없는 ML 데이터셋 내보내기(개인용 FRED 시리즈 기본 제외).
- `src/summary.py`: 일일 요약(테이블 건수, 마지막 수집 상태, `market.db`·스냅숏 파일 크기, 디스크, CPU 온도 `k10temp`).
- `src/validate_duckdb.py`: OHLCV 중복/범위 검증. `src/validate.py`: 과거 CSV 검증 CLI(호환용으로만 유지, 다른 모듈은 의존하지 않음).
- `src/alpha_vantage.py`: Alpha Vantage 백업 수집기(기본 꺼짐; 같은 `ohlcv` 테이블에 덮어쓰고 분할 미반영 가격이라 켜면 기준이 섞임).
- `src/fetch_tickers.py`: 단독 티커 목록 갱신 도구(일상 운영은 `src.universe`가 대신함).
- `src/catalog.py`: Twelve Data 카탈로그(`/stocks`, `/etfs`) 응답에서 티커 추출, `symbols.txt` 저장(`universe`·`fetch_tickers` 공용).
- `src/discord_status.py`: Discord 실시간 상태 메시지.
- `src/config.py`, `src/state.py`, `src/env.py`, `src/logging_utils.py`: 설정·상태 파일·`.env`·로그 공용 모듈.
- `src/atomic_io.py`: 원자적 파일 저장(`<이름>.tmp` → fsync → `os.replace`). 상태·요약·검증 리포트·raw·종목 목록 파일이 모두 이 함수를 씁니다. JSON의 DATE/TIMESTAMP 값은 ISO 문자열로 저장합니다.

## 데이터 흐름

종목 목록 갱신 → 핵심 일봉 → FRED → 스냅숏(core) → 순환 일봉 → 지표 → 뉴스 → 요약·검증 → DB 재구성(필요할 때) → 스냅숏(full)

- 수집기는 원본 응답을 `data/raw/<provider>/`에 종목·시리즈별 최신 1개(gzip)로 남깁니다.
- 정규화된 데이터는 DuckDB 단일 파일(`data/db/market.db`)의 `ohlcv`, `indicators`, `macro`, `news`, `corporate_actions`, `series_meta` 테이블에 저장됩니다. 스키마 버전 4에서 `ohlcv`·`indicators`는 날짜를 `DATE`, 값을 `DOUBLE`/`BIGINT`로 저장합니다.
- 종목 통째 교체로 지운 행의 공간은 파일에 남으므로, 파일이 마지막 재구성 직후 크기의 2배를 넘으면 `src.rebuild`가 새 파일로 다시 만듭니다.
- 스냅숏 단계가 `symbol_status`, `collection_runs`를 기록하고 `market_snapshot.db`로 복사합니다. stockpred는 스냅숏만 읽습니다.

## 상태 파일 구조 요약

`data/state/state.json`
- `schema_version`
- `providers`: `twelve_data:key{n}`별 `date`, `calls_used_today`(크레딧), `daily_limit`, `daily_reserve`, `last_reset_utc`, `server_synced`.
- `symbols.<symbol>.twelve_data`: `status`(`done`/`failed`/`not_found`/`skipped_rate_limit`), `last_success_date`, `last_success_at`, `last_bar_date`, `core_session`, `fail_count`, `next_retry_date`, `splits_checked_at`, `splits_stale`, `split_events`, `rows`, `error`, `note`.
- `pipeline_run`: 실행 ID별 사용 크레딧(핵심·순환 단계가 상한을 나눠 씀).
- `current_run`: 진행 중인 단계(`phase`, `planned`, `processed` 등). 정상 종료 시 제거.
- `runs`: 최근 실행 요약 20개(종목 목록 제외).

그 밖의 상태 파일
- `universe.json`: S&P 500 목록과 기준일, `former_core`, `delisted`, 마지막 갱신 시각.
- `fred.json`: 시리즈별 `last_success_at`(완료 판정에 사용).
- `finnhub_news.json`: 종목별 `last_to`, 이어받기 창(`window_from`, `window_to`, `cursor_to`), `last_attempt_at`.
- `db_maintenance.json`: `baseline_bytes`(마지막 재구성 직후 `market.db` 크기, 정기 재구성 기준), `last_rebuild`, `history`(최근 20회: 방식, 전후 크기, 백업 경로, 소요 시간). 기준이 없을 때 처음 기록하면 `baseline_initialized_at`.
