# DESIGN

이 문서는 2026-10-20에 확정한 수집기 개편 설계입니다. 구현 진행 상황은 `docs/STAGES.md`에 기록합니다.

## 0. 목표와 전제

- 최우선 목표는 미국 주식 데이터를 빠짐없이 쌓는 것입니다. 주 소비자는 stockpred(`~/Programs/stock_test`)이며 읽기 전용으로 씁니다.
- 실행 환경은 NucBox M6(Ubuntu 26.04, Ryzen 5 6600H, RAM 12GB, NVMe)입니다. lael 계정의 systemd 사용자 서비스로 돌고 sudo를 쓰지 않습니다.
- 아래 수치는 모두 초기값입니다. 운영 중 오류가 나면 설정값으로 조정합니다.
- 범위 밖: 모의투자 시간 조정(stockpred 쪽), rclone 백업(나중에), Alpha Vantage(꺼진 상태 유지), 한국 가격, 미국 재무제표, 과거 상장폐지 종목 이력.

## 1. 수집 대상

| 그룹 | 종목 | 받는 것 | 주기 |
|---|---|---|---|
| 핵심 | ETF 23개(`config/core_etfs.txt`) + S&P 500 현재 구성종목 + `config/core_extra.txt`(선택) | 분할 반영 OHLCV, `adj_close`, 분할 이벤트, 뉴스 | 매 거래일 |
| 순환 | NASDAQ 목록(`config/symbols.txt`) + S&P에서 빠진 옛 구성종목 | 분할 반영 OHLCV, 뉴스 | 남는 예산으로 돌아가며 |
| 거시 | FRED 16개(`config/fred_series.txt`) | 관측값, 시리즈 메타 | 매 실행 |

### 종목 목록 자동 갱신 (`src.universe`)

- S&P 500: stockpred와 같은 출처(fja05680/sp500 GitHub CSV)의 마지막 행. 빠진 종목은 `former_core`로 옮겨 순환 수집을 계속합니다.
- NASDAQ: Twelve Data `/stocks?exchange=NASDAQ`(0크레딧)로 `config/symbols.txt`를 갱신합니다.
- 상장폐지: Twelve Data 미국 전체 주식·ETF 카탈로그 어디에도 없는 종목을 `delisted`로 표시하고 더 이상 요청하지 않습니다. 이미 쌓인 행은 지우지 않습니다.
- 갱신 주기: 마지막 갱신 후 7일(`UNIVERSE_MAX_AGE_DAYS`)이 지나면 실행합니다.
- `--dry-run`은 네트워크 없이 갱신이 필요한지만 보고하고, `--check`는 실제로 받아 비교하되 파일은 쓰지 않습니다.
- 표기: Twelve Data와 Finnhub 모두 `BRK.B`, `BF.B` 점 표기를 씁니다. yfinance 표기(`BRK-B`)는 `symbol_status.yahoo_symbol`로 제공합니다.

## 2. 하루 실행 흐름 (`scripts/run_daily.sh`)

타이머: `Mon..Fri 16:10 America/New_York`, `Persistent=true`(서머타임 자동 반영, 겨울 06:10 KST / 여름 05:10 KST).

| 순서 | 단계 | 비고 |
|---|---|---|
| 1 | 종목 목록 갱신 | 7일마다 |
| 2 | 핵심 일봉 (`src.main --phase core`) | 종목별 통째 교체 |
| 3 | FRED | 16개 + 시리즈 메타 |
| 4 | 스냅숏(core) | 완료 상태 기록 후 `market_snapshot.db` 교체 |
| 5 | 순환 일봉 (`src.main --phase rotation`) | 실행당 크레딧 상한에서 남는 만큼 |
| 6 | 기술지표 | 바뀐 종목만 |
| 7 | 뉴스 | 핵심 종목 먼저 |
| 8 | 요약, 검증 | |
| 9 | 스냅숏(full) | |

- 단계가 실패해도 다음 단계를 계속하고 마지막에 실패 단계를 보고합니다.
- 실행 전체를 `data/state/pipeline.lock`(flock) 하나로 직렬화합니다.
- 놓친 실행은 바로 돌되, 기준 거래일보다 새로운 봉(장중 미완성 봉)은 저장하지 않습니다.

## 3. Twelve Data

### 크레딧 비용 (2026-10-20 `/api_usage` 실측)

| 호출 | 크레딧 |
|---|---|
| `time_series` (행 수 무관, 5000행 포함) | 1 |
| `/api_usage` | 1 |
| `/stocks`, `/etfs` | 0 |
| 403 오류 응답 | 0 |
| `/dividends`, `/splits` | 무료 키에서 사용 불가(403, Grow 이상 전용. AAPL만 예외적으로 열림) |

### 예산 규칙

- 키별 하루 예산 = `daily_limit - daily_reserve` = 700, UTC 날짜 기준. 서버 한도도 00:00 UTC에 초기화됩니다.
- 키는 key0 → key1 → key2 순서로 다 쓰고 넘어갑니다. 실행당 상한(`TWELVE_MAX_CREDITS`, 초기 1400)이 있어 보통 key2는 앞 키가 외부 사용 등으로 일찍 소진됐을 때만 쓰입니다.
- 실행 시작 때 `/api_usage`의 서버 `daily_usage`를 읽어 `state.json` 값과 큰 쪽을 씁니다. 두 번 시도해도 읽지 못한 키는 그 실행에서 제외합니다(모르는 외부 사용량이 reserve를 깎지 않게). 로컬 예산이 없는 키는 조회하지 않습니다.
- 결과가 불확실한 시도(네트워크 오류, 응답 유실, HTTP 5xx)도 시도마다 크레딧을 차감합니다.
- 호출할 때마다 UTC 날짜를 확인합니다. 실행 중 자정을 넘으면 새 날 예산으로 셉니다.
- `state.json`의 `calls_used_today` 필드 이름은 유지하고 값은 크레딧으로 해석합니다(기존 값과 호환).
- 분당 7회 제한(`per_minute_limit`)은 모든 HTTP 호출에 적용합니다.

| 항목 | 크레딧/실행 |
|---|---|
| `/api_usage` × 키 3개 | 3 |
| 핵심 526 × (`adjust=splits` + `adjust=all`) | 1,052 |
| 분할 이력 `adjust=none` | 첫 실행 526, 이후 분할 감지 종목만 |
| 순환 | 상한 1,400에서 남는 만큼(약 340) |

### 핵심 종목 수집

1. `adjust=splits` 전체(5000행) → OHLCV
2. `adjust=all` 전체 → `adj_close`
3. 분할 이력이 없거나 분할이 감지되면 `adjust=none` 전체 → 분할 이벤트 재계산
4. 세 응답이 모두 성공하면 한 트랜잭션으로 교체합니다. 하나라도 실패하면 아무것도 바꾸지 않습니다.

- 분할 이벤트: `ratio_t = none_close_t / splits_close_t`. 날짜순으로 `ratio`가 2% 넘게 바뀐 날 d에 `value = ratio_{d-1} / ratio_d`를 기록합니다(yfinance 규약: 4:1 → 4.0, 1:18 병합 → 0.0556). 분모 100 이내 분수에 0.5% 안으로 가까우면 그 분수로 맞춥니다.
- 분할 감지: 새로 받은 분할 반영 종가와 DB에 저장된 과거 종가를 겹치는 날짜(최근 20일 이전)로 비교해 중앙값 비율이 2% 넘게 다르면 분할로 봅니다.
- 핵심 종목은 같은 기준 거래일에 이미 성공했으면 다시 받지 않습니다(`--force` 제외).

### 순환 종목 수집

- 순서: 한 번도 성공하지 못한 종목 → `last_success_date`가 오래된 순 → `symbols.txt` 순서.
- 제외: 핵심 종목, `delisted`, 재시도 대기(`next_retry_date`)가 남은 종목.
- 실패: HTTP 404/종목 없음은 `not_found`로 30일 대기. 그 밖의 오류는 연속 실패 횟수에 따라 1·2·4·8·16·30일 대기. 성공하면 초기화합니다.
- 4xx 응답(429 제외)은 같은 요청을 바로 다시 보내지 않습니다.
- 예산이 떨어져 못 받은 종목은 실패로 치지 않습니다.

### 저장 규칙

- 종목 통째 교체: 한 트랜잭션에서 `ohlcv` 종목 행 삭제 → 새 행 삽입 → `indicators` 종목 행 삭제 → (핵심) 분할 이벤트 교체. 5000행(약 20년)보다 오래된 행은 사라집니다.
- 기준 거래일보다 새로운 봉은 저장하지 않습니다.
- `delisted` 종목은 교체하지 않으므로 행이 보존됩니다.
- dry-run은 `state.json`, `finnhub_news.json`, DB에 아무것도 쓰지 않습니다.
- 원본 응답은 매일 5000행 전체를 다시 받으므로 응답마다 새 파일을 만들면 하루 1GB가 넘게 쌓입니다. 그래서 `data/raw/twelve_data/{splits,all,none}/<symbol>.json.gz`처럼 종목·조정 방식별 최신 1개만 gzip으로 덮어씁니다(약 0.6GB에서 멈춤). FRED, Finnhub도 같은 방식입니다. 2026-10-20 이전의 타임스탬프 파일(약 1.9GB)은 그대로 둡니다.
- `--only SYMBOL`로 두 단계를 특정 종목에 한정할 수 있습니다(점검, 문제 종목 재수집).

## 4. 기준 거래일과 완료 상태

- 기준 거래일(`session_date`): 실행 시점 뉴욕 시간으로 마지막으로 끝난 정규장. 16:10 ET 전이면 전 거래일입니다. NYSE 정기 휴장일(신정, MLK, 대통령의 날, 성금요일, 메모리얼, 준틴스, 독립기념일, 노동절, 추수감사절, 성탄절, 대체 휴일 규칙 포함)을 계산합니다.
- `collection_runs.status`

| 상태 | 조건 |
|---|---|
| `complete` | S&P 500 목록이 적재돼 있고(490개 이상), 핵심 종목 전부가 기준 거래일 봉·`adj_close`·분할 이력을 갖고, FRED 목록 전부가 이번 실행에서 성공 |
| `partial` | 일부 누락. `missing_symbols`, `missing_splits`, `missing_series`에 목록 기록 |
| `failed` | 종목 목록이 초기화되지 않았거나(`universe_issue`), 핵심 종목이 하나도 성공하지 못함 |

- 첫 1~2일은 분할 이력 보충(526크레딧)이 실행당 상한에 걸려 `partial`이 정상입니다.

- `partial`이어도 스냅숏은 만들고 상태를 함께 기록합니다.

## 5. 스냅숏과 stockpred 데이터 계약

- DuckDB는 쓰는 프로세스가 있으면 다른 프로세스가 읽기 전용으로도 열 수 없습니다. stockpred는 `data/db/market_snapshot.db`만 엽니다.
- 만드는 순서(잠금 안): `symbol_status`·`collection_runs` 기록 → `CHECKPOINT` → 연결 닫기 → 임시 파일로 복사 → 읽기 전용으로 열어 확인 → `os.replace`.

### 테이블/뷰

| 이름 | 내용 |
|---|---|
| `prices_daily` (뷰) | symbol, date, open, high, low, close, adj_close, volume, dividends, splits — stockpred `STD_COLS`와 같은 이름·의미 |
| `corporate_actions` | symbol, date, type(`split`), value, source, updated_at |
| `symbol_status` | symbol, yahoo_symbol, group_name, active, delisted_at, last_bar_date, last_success_at, core_session, splits_complete |
| `collection_runs` | run_id, phase, started_at, finished_at, session_date, status, core_expected, core_ok, missing_symbols, fred_expected, fred_ok, missing_series, note, splits_ok, missing_splits, universe_issue (스키마 버전 3) |
| `series_meta` | series_id, title, frequency, units, release, license_note, personal_use_only, updated_at |
| `ohlcv`, `macro`, `news`, `indicators` | 기존 테이블. `ohlcv.adj_close`, `macro.first_seen_at` 추가 |

### `prices_daily` 컬럼 의미

| 컬럼 | 값 |
|---|---|
| open/high/low/close/volume | `adjust=splits` (yfinance `Close` 규약: 분할 반영, 배당 미반영) |
| adj_close | `adjust=all` 종가. 핵심 종목만, 순환 종목은 NULL |
| splits | 분할일에만 비율, 그 외 0 |
| dividends | 0 (무료 키로 받을 수 없음. stockpred 패널 계산은 이 컬럼을 쓰지 않음) |

- Yahoo 캐시와 대조(2026-10-20, NVDA·BRK.B·SPY 5000행): close 일치(오차 5e-8 이하), volume 99% 구간 일치, 최근 2년 배당 조정계수 차이 0.007% 이하.
- 알려진 차이: Twelve Data `adjust=all`에는 2012~2013년 일부 배당(NVDA 첫 배당 3회, SPY 2012-09-21)이 빠져 있어 그 날짜를 지나는 수익률이 배당만큼(약 0.5%) 다릅니다. 그래서 어댑터 검증은 값 수준이 아니라 날짜별 조정계수 변화(`(adj/close)_t / (adj/close)_{t-1}`)를 비교하고, 허용 오차는 close 일치, 최근 2년 조정계수 0.01%, volume 1%로 둡니다.
- 대조 중 stockpred Yahoo 캐시의 2026-10-13 NVDA 종가가 장중 값(224.75, 확정 종가 228.52)으로 저장돼 있던 것을 발견했습니다. 스냅숏은 기준 거래일 이후 봉을 저장하지 않으므로 이런 값이 들어가지 않습니다.

## 6. 뉴스 (Finnhub)

- 분당 50회(`FINNHUB_PER_MINUTE`), 한도는 응답 헤더 기준 분당 60입니다. 하루 한도는 없습니다.
- 대상: 핵심 종목 먼저, 그다음 나머지 활성 종목. `FINNHUB_LIMIT`이 비어 있으면 전체입니다.
- 한 번에 약 250건에서 잘립니다(MSFT 1년 요청 → 최근 1주 250건). 200건 이상이고 가장 오래된 기사가 시작일보다 늦으면 `to`를 가장 오래된 기사 날짜로 당겨 다시 요청합니다. 하루에 250건이 넘으면 하루를 당깁니다.
- 종목당 실행마다 최대 10페이지. 다 못 받으면 `window_from`, `window_to`, `cursor_to`를 저장해 다음 실행에서 이어 받습니다. `last_to`는 구간을 끝까지 받은 뒤에만 갱신합니다.
- 첫 수집은 365일 전부터(`FINNHUB_DAYS_BACK`, 무료 범위 약 1년)입니다.

## 7. FRED

- 기존 11개 + DGS2, BAA10Y, VIXCLS, DTB3, IR3TIB01KRM156N.
- `macro.first_seen_at`: 수집기가 그 관측값을 처음 받은 시각. 기존 행은 NULL(기록 시작 전)입니다. 값이 수정돼도 유지됩니다.
- `series_meta.personal_use_only`: 시리즈 설명에 저작권·복제 제한 문구가 있으면 참입니다(BAA10Y: Moody's, VIXCLS: CBOE). 개인 연구용으로만 쓰고, `src.export_dataset`은 기본으로 제외합니다.
- IR3TIB01KRM156N은 한국 월간 지표이며 관측일 대비 약 44일 뒤 공개됩니다.

## 8. 실행 환경

- Python: `run_daily.sh` 기본값은 `.venv/bin/python`(없으면 `python3`).
- 설정: `run_daily.sh`도 `.env`를 읽습니다. 셸에서 준 값이 우선입니다. `/etc/data-scrapping.env`는 쓰지 않습니다.
- 설치: `scripts/install_local.sh`(venv 확인·생성, 패키지 설치, 사용자 유닛 설치·활성화). 백업 타이머는 설치만 하고 켜지 않습니다.
- 상태 표시: `summary.py`는 CPU 온도를 hwmon `k10temp`에서 읽습니다. `progress.py`는 종목 수를 파일에서 읽고 UTC 날짜를 씁니다.

## 9. 작업 순서

| 순서 | 작업 | 저장소 |
|---|---|---|
| 1 | 공통 잠금, dry-run 무기록 | data-scrapping |
| 2 | 크레딧 예산 | data-scrapping |
| 3 | 순환 순서, 실패 처리 | data-scrapping |
| 4 | 종목 목록 자동화 | data-scrapping |
| 5 | 통째 교체, `adj_close`, 분할 이벤트 | data-scrapping |
| 6 | 기준 거래일, 완료 상태, 스냅숏 | data-scrapping |
| 7 | 뉴스 | data-scrapping |
| 8 | FRED | data-scrapping |
| 9 | 실행 환경 | data-scrapping |
| 10 | `progress.py`, `summary.py` | data-scrapping |
| 11 | 문서, `STAGES.md` | data-scrapping |
| 12 | 수집기 검증 | data-scrapping |
| 13 | 스냅숏 어댑터, Yahoo 대조 | stock_test |

## 10. 운영하며 확인할 것

- 16:10 ET에 그날 일봉(거래량 포함)이 확정돼 있는지.
- S&P 공개 목록의 반영 지연(2026-10-20 기준 마지막 변경 2026-08-18).
- Finnhub 잘림 기준 200건.
- Twelve Data 실행당 1,400크레딧·분당 7회, Finnhub 분당 50회에서 오류가 나는지.
