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
- 갱신 주기: 마지막 갱신 후 7일(`UNIVERSE_MAX_AGE_DAYS`)이 지나면 실행합니다. NASDAQ 목록(`config/symbols.txt`)이 없거나 비어 있으면 기간과 상관없이 바로 갱신합니다(`--skip-catalog` 제외). 갱신 이유는 결과 JSON의 `reason`에 남습니다.
- `--dry-run`은 네트워크 없이 갱신이 필요한지만 보고하고, `--check`는 실제로 받아 비교하되 파일은 쓰지 않습니다.
- 표기: Twelve Data와 Finnhub 모두 `BRK.B`, `BF.B` 점 표기를 씁니다. yfinance 표기(`BRK-B`)는 `symbol_status.yahoo_symbol`로 제공합니다.

## 2. 하루 실행 흐름 (`scripts/run_daily.sh`)

타이머: `Mon..Fri 16:10 America/New_York`, `Persistent=true`(서머타임 자동 반영, 겨울 06:10 KST / 여름 05:10 KST).

| 순서 | 단계 | 비고 |
|---|---|---|
| 1 | 종목 목록 갱신 | 7일마다 |
| 2 | 핵심 일봉 (`src.main --phase core`) | 바뀐 행만 저장 |
| 3 | FRED | 16개 + 시리즈 메타 |
| 4 | 스냅숏(core) | 완료 상태 기록 후 `market_snapshot.db` 교체 |
| 5 | 순환 일봉 (`src.main --phase rotation`) | 실행당 크레딧 상한에서 남는 만큼 |
| 6 | 기술지표 | 지표 행이 없는 날짜만 |
| 7 | 뉴스 | 핵심 종목 먼저 |
| 8 | 요약, 검증 | |
| 9 | DB 재구성 (`src.rebuild --if-needed`) | `market.db`가 마지막 재구성 직후 크기의 2배를 넘을 때만 |
| 10 | 스냅숏(full) | |

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
- 키는 key0 → key1 → key2 순서로 다 쓰고 넘어갑니다. 실행당 상한(`TWELVE_MAX_CREDITS`)은 1800입니다(2026-10-23, 처음 1400). 순환은 `TWELVE_MAX_SYMBOLS` 700종목에서 먼저 멈추므로 보통 약 1,760크레딧을 쓰고 key2의 약 300크레딧이 남습니다. 키를 순서대로 쓰고 분당 제한이 모든 호출에 걸리므로 키가 늘어도 수집 속도는 같습니다.
- 실행 시작 때 `/api_usage`의 서버 `daily_usage`를 읽어 `state.json` 값과 큰 쪽을 씁니다. 두 번 시도해도 읽지 못한 키는 그 실행에서 제외합니다(모르는 외부 사용량이 reserve를 깎지 않게). 로컬 예산이 없는 키는 조회하지 않습니다. 실행당 상한이 `/api_usage` 1크레딧을 낼 수 없으면(예: 핵심 단계가 상한을 다 쓴 뒤의 순환 단계) 어느 키도 조회하지 않고 제외하지도 않습니다(`usage_sync`에 `skipped: run_credit_cap`). 상한은 그 실행의 모든 유료 호출을 막으므로 reserve가 깎이지 않습니다.
- 결과가 불확실한 시도(네트워크 오류, 응답 유실·도중 끊김(`http.client.IncompleteRead`·`BadStatusLine` 등), HTTP 5xx)도 시도마다 크레딧을 차감합니다.
- 호출할 때마다 UTC 날짜를 확인합니다. 실행 중 자정을 넘으면 새 날 예산으로 셉니다.
- 순환 단계는 UTC 자정을 넘기곤 합니다(시작 여름 20:10 UTC, 겨울 21:10 UTC). 자정 뒤에 쓴 크레딧은 다음 날 키 예산에서 빠집니다. 겨울에는 자정 전에 약 1,190크레딧만 쓸 수 있어 상한 1800이면 약 570이 다음 날로 넘어가고, 다음 날 쓸 수 있는 양은 약 1,530입니다. 핵심 단계(약 1,055) 여유를 남기려고 상한을 2100(키 3개 전부)이 아닌 1800에 둡니다(2100이면 여유가 약 130).
- `state.json`의 `calls_used_today` 필드 이름은 유지하고 값은 크레딧으로 해석합니다(기존 값과 호환).
- 분당 7회 제한(`per_minute_limit`)은 모든 HTTP 호출에 적용합니다.

| 항목 | 크레딧/실행 |
|---|---|
| `/api_usage` × 키 3개 | 3 |
| 핵심 526 × (`adjust=splits` + `adjust=all`) | 1,052 |
| 분할 이력 `adjust=none` | 첫 실행 526, 이후 분할 감지 종목만 |
| 순환 | `TWELVE_MAX_SYMBOLS` 700종목(약 700, 상한 1,800 안) |

### 핵심 종목 수집

1. `adjust=splits` 전체(5000행) → OHLCV
2. `adjust=all` 전체 → `adj_close`
3. 분할 이력이 없거나 분할이 감지되면 `adjust=none` 전체 → 분할 이벤트 재계산
4. 1·2가 모두 성공하면 한 트랜잭션으로 저장합니다(아래 "저장 규칙"). 하나라도 실패하면 아무것도 바꾸지 않습니다. 3은 핵심 종목을 모두 받은 뒤 따로 돕니다.

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

- 차분 저장(2026-11-24, `storage.sync_symbol_prices`): 종목마다 연결 하나로 저장된 행을 읽고 새 응답과 비교해, 한 트랜잭션에서 바뀐 것만 씁니다.
  - 응답에 새로 생긴 날짜는 넣고, 값(open·high·low·close·volume·adj_close)이 하나라도 다른 날짜는 바꿉니다. 같은 행은 쓰지 않으므로 `updated_at`도 그대로입니다.
  - 응답 첫 날짜 이후인데 응답에 없는 저장 날짜는 지웁니다(통째 교체 때와 같음. 범위 안의 날짜는 아래 "사라진 날짜"로 셉니다).
  - 응답 첫 날짜보다 오래된 행(5000행, 약 20년 밖)은 남깁니다. 겹치는 날짜 중 가장 오래된 20일의 `새 값 ÷ 저장값` 비율이 공통 배율이면(80% 이상이 중앙값과 1e-4 안) 그 배율로 다시 맞춥니다: 가격은 종가 비율을 곱하고 거래량은 나누며(반올림), `adj_close`는 `adj_close` 비율을 곱합니다. 1과의 차이가 1e-5 안이면 배율 1로 봅니다(`adj_close` 잡음 약 1e-6, 배당 최소 변화 실측 5.8e-4). 겹치는 날이 5일 미만이거나 비율이 갈리면(예: 그 20일 안의 분할) 기준을 확인할 수 없으므로 오래된 행을 모두 지웁니다(통째 교체와 같은 결과, 요약 `older_dropped`, WARNING 로그). 새 응답에 `adj_close`가 없으면(순환 종목) 오래된 행의 `adj_close`도 NULL이 됩니다. 데이터 규칙에 걸리는 오래된 행(휴장일, 시작일 이전)은 지웁니다.
  - 같은 트랜잭션에서, 종가를 넣거나 바꾸거나 지운 가장 이른 날짜부터 그 종목의 `indicators` 행을 지웁니다. 지표는 종가만 쓰므로 `adj_close`나 거래량만 바뀌면 지표는 그대로 둡니다. Alpha Vantage 저장(`upsert_ohlcv_db`)도 넣은 가장 이른 날짜부터 지표를 지웁니다.
  - 분할 이벤트(`adjust=none`)는 응답 첫 날짜 이후만 바꿉니다. 오래된 행의 분할 이벤트는 남으므로 `prices_daily.splits`도 유지됩니다.
  - 실행 요약: 단계별 `rows_written`(넣기·바꾸기·다시 맞춘 행)·`rows_deleted`·`older_dropped`, 종목별 `rebased`(행 수·배율)·`older_dropped`(있을 때만).
  - 근거(2026-11-24 측정, 읽기 전용): 10-27 백업과 11-03 스냅숏의 겹치는 행을 비교하면 핵심 526종목 2,408,388행 중 가격이 다른 행은 445개로 모두 10-27 봉(장 마감 직후 받은 잠정 봉)이었고, 거래량이 다른 행은 550개, `adj_close`가 바뀐 종목은 28개(배당)였습니다. 다시 받은 순환 3,483종목은 분할 기준이 바뀐 73종목을 빼면 6,171행만 달랐습니다. 다섯 세션이 지나도 나머지 과거 값은 비트 단위로 같습니다.
  - 통째 교체(`storage.replace_symbol_prices`)는 테스트와 일회성 도구용으로 남아 있습니다.
- 기준 거래일보다 새로운 봉은 저장하지 않습니다.
- 기준 거래일 봉은 잠정값입니다: 16:10 ET 직후 받은 핵심 종목 봉은 다음 날 다시 받으면 종가가 최대 0.33%, 거래량이 최대 약 70배 달라집니다(10-27 봉, QQQ 560,255 → 30,942,800). 다음 수집이 그 행을 바꿉니다.
- 데이터 규칙(`src.data_rules`, 2026-10-23): 저장 직전에 적용합니다. 규칙 파일 세 개는 `/api_usage` 조회보다 먼저(dry-run 포함) 읽고, 잘못된 줄이 있으면 API를 하나도 부르지 않고 단계가 실패합니다(`data_rules_invalid`, 종료 코드 1). 날짜·숫자 형식, 거래일 여부, 중복, 저가 ≤ 시가·종가 ≤ 고가, 시작일과의 앞뒤, 보정과 알려진 빈칸의 겹침을 검사합니다.
  - 휴장일(주말, 정기 휴장일, 일회성 휴장일) 봉은 버립니다. Twelve Data가 휴장일 봉을 보내기도 합니다(2026-10-23 스냅숏 2,956개·349종목, 핵심 8개. TTWO 2025-12-25는 12-24 봉과 같고 Yahoo에는 없음).
  - `config/history_start.txt`(`SYMBOL YYYY-MM-DD 이유`): 그 날짜 이전 봉은 버립니다. GM 2010-11-18: 그 이전 391행(2009-01-21~2010-09-14)은 옛 General Motors이고 거래량이 모두 0이며, Yahoo GM도 2010-11-18에 시작합니다. 거래량 0만으로 자르는 규칙은 FERG·AMCR·DBC처럼 Yahoo와 시작일이 같은 종목도 자르므로 쓰지 않고, 확인한 종목만 올립니다.
  - `config/bar_patches.csv`: 두 번째 출처로 확인한 봉입니다. `adjust=splits` 응답에 그 날짜가 없고, 응답의 첫 날짜와 마지막 날짜 사이이며, `ref_date`가 응답에 있을 때만 넣습니다(마지막 봉 뒤는 넣지 않으므로 낡은 응답을 가리지 않음). 응답의 `ref_date` 종가가 `ref_close`와 2% 넘게 다르면(그 뒤 분할) 가격에 그 비율을 곱하고 거래량은 나눕니다. `adj_close`는 `adjust=all`에 그 날짜가 있으면 그 값, 없으면 보정 종가 × (`ref_date`의 adj_close ÷ 종가)이고, 둘 다 안 되면 보정을 건너뜁니다(핵심 종목에 중간 NULL `adj_close`를 만들지 않음). `adjust=none`에는 넣지 않으므로 분할 이벤트 계산 결과는 같습니다. 원본에 그 날짜가 생기면 원본을 쓰고 "보정 불필요" INFO 로그를 남깁니다. 첫 항목: MNST 2026-08-10(Twelve Data splits·all·none 모두 없음, Yahoo 값, 2:1 분할 전날).
  - `config/known_gaps.txt`: 어느 출처에도 없는 거래일입니다. 검증 경고에서 뺍니다. 첫 항목: FISV 2025-11-12(Twelve Data·Yahoo 모두 없음).
  - 사라진 날짜: 저장돼 있던 거래일이 새 응답의 범위 안에서 빠지면 실행 요약의 종목별 `vanished_dates`와 단계별 개수에 남기고, 핵심 종목은 WARNING 로그도 남깁니다. 저장된 행을 지키지는 않습니다(공급자가 거둬들인 봉이고, 분할로 배율이 바뀌면 옛 행이 틀린 값이 됨. 실측: WHLR 340일, EFSI 27일 등 순환 종목의 공급자 정정). 응답 첫 날짜보다 오래된 날(남김), 휴장일, 시작일 이전은 세지 않습니다. 순환 종목은 개수만 셉니다.
  - 실행 요약에 단계별 `dropped_bars`·`patched`·`vanished_dates`, 종목별 `dropped`·`patched`·`patch_skipped`·`vanished_dates`가 남습니다. 추가 API 호출은 없습니다.
- 이미 저장된 행은 `src.ohlcv_cleanup`(파이프라인 잠금, `--dry-run`은 개수만)이 같은 규칙으로 정리합니다. 휴장일 행과 시작일 이전 행을 한 트랜잭션에서 지우고, 영향받은 종목의 `indicators` 행을 모두 지워 다음 지표 단계가 다시 계산하게 합니다. 핵심 종목은 다음 실행, 순환 종목은 다음 차례에 저절로 정리되지만 상장폐지 종목은 다시 받지 않으므로 필요합니다. 보정 봉은 넣지 않습니다(다음 수집에서 들어감).
- 값 정규화(스키마 버전 4): 저장 전에 Python에서 봉을 검사합니다. 날짜는 `YYYY-MM-DD` 달력 날짜, open/high/low/close는 유한한 실수(빠지면 오류), volume은 0 이상의 정수(`"12345"`, `12345.0`은 허용, `1.5`는 오류, 빈 값은 NULL, 0은 0으로 보존), `adj_close`는 유한한 실수 또는 NULL입니다. 응답에 이상 값이 하나라도 있으면 그 종목만 실패(`ValueError`)로 처리해 기존 실패·재시도 흐름을 타고, 저장된 행은 바뀌지 않습니다. NDJSON에는 JSON 숫자로 쓰고 `read_json`이 테이블 타입으로 읽으므로, 정규화를 빠져나간 `""`·`"abc"`·잘못된 날짜도 DB에 들어가지 않고 오류가 납니다.
- `delisted` 종목은 다시 받지 않으므로 행이 보존됩니다.
- dry-run은 `state.json`, `finnhub_news.json`, DB에 아무것도 쓰지 않습니다.
- 원본 응답은 매일 5000행 전체를 다시 받으므로 응답마다 새 파일을 만들면 하루 1GB가 넘게 쌓입니다. 그래서 `data/raw/twelve_data/{splits,all,none}/<symbol>.json.gz`처럼 종목·조정 방식별 최신 1개만 gzip으로 덮어씁니다(약 0.6GB에서 멈춤). FRED, Finnhub도 같은 방식입니다. 2026-10-20 이전의 타임스탬프 파일(약 1.9GB)은 그대로 둡니다.
- `--only SYMBOL`로 두 단계를 특정 종목에 한정할 수 있습니다(점검, 문제 종목 재수집).

## 4. 기준 거래일과 완료 상태

- 기준 거래일(`session_date`): 실행 시점 뉴욕 시간으로 마지막으로 끝난 정규장. 16:10 ET 전이면 전 거래일입니다. NYSE 정기 휴장일(신정, MLK, 대통령의 날, 성금요일, 메모리얼, 준틴스, 독립기념일, 노동절, 추수감사절, 성탄절, 대체 휴일 규칙 포함)을 계산합니다. 일회성 휴장일(`SPECIAL_CLOSURES`: 2001-09-11~14, 2004-06-11, 2007-01-02, 2012-10-29·30, 2018-12-05, 2025-01-09)도 휴장으로 봅니다. 새 일회성 휴장은 직접 추가해야 하며, 추가하지 않으면 그날 봉을 기대해 핵심 종목이 `partial`이 됩니다.
- 검증(`src.validate_duckdb`)은 `issues`(중복 날짜, 잘못된 값)가 있을 때만 `fail`입니다. 데이터 규칙 점검은 `warnings`로 남기고 상태를 바꾸지 않습니다.
  - `core_gap`: 핵심 종목(활성 core_etf·core_sp500·core_extra)의 첫 봉(시작일이 더 늦으면 시작일)부터 마지막 봉(최근 `collection_runs.session_date`를 넘지 않음)까지의 거래일 중 봉이 없는 날. 알려진 빈칸과 보정 날짜는 뺍니다. 저장된 적 없는 빈칸도 잡힙니다.
  - `core_stale`: 마지막 봉이 그 기준 거래일보다 이른 핵심 종목, 봉이 없는 핵심 종목.
  - `closed_day_bars`(전체 종목 개수, 핵심 종목 목록), `before_history_start`, `patch_missing`(보정 파일에 있는데 DB에 없는 봉).
  - 보고서에 `generated_at`과 실행 ID(`DATA_SCRAPPING_RUN_ID`)를 기록합니다. `run_daily.sh`는 검증을 요약보다 먼저 돌리고, 요약은 이번 실행의 보고서일 때만 Discord에 `validation=<status> warnings=<유형:개수>`를, 아니면(dry-run에서 건너뜀, 검증 실패) `validation=N/A`를 씁니다.
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
| `collection_runs` | run_id, phase, started_at, finished_at, session_date, status, core_expected, core_ok, missing_symbols, fred_expected, fred_ok, missing_series, note, splits_ok, missing_splits, universe_issue (splits_ok 이후 3개는 스키마 버전 3에서 추가) |
| `series_meta` | series_id, title, frequency, units, release, license_note, personal_use_only, updated_at |
| `ohlcv` | symbol, date `DATE`, open/high/low/close `DOUBLE`, volume `BIGINT`, updated_at `VARCHAR`, adj_close `DOUBLE` (스키마 버전 4). PK (symbol, date) |
| `indicators` | symbol, date `DATE`, sma_20, sma_50, sma_200, rsi_14, macd_12_26, macd_signal_9, macd_hist `DOUBLE`(소수 6자리 반올림, 계산 전 구간은 NULL), updated_at `VARCHAR` (스키마 버전 4, OHLCV 중복 열 없음). PK (symbol, date) |
| `macro`, `news` | `VARCHAR` 그대로. 타입이 필요하면 `macro_typed`, `news_typed` 뷰 |
| `ohlcv_typed`, `indicators_typed` (뷰) | 스키마 버전 3과 같은 열·타입(volume `DOUBLE`) |

### `prices_daily` 컬럼 의미

| 컬럼 | 값 |
|---|---|
| open/high/low/close/volume | `adjust=splits` (yfinance `Close` 규약: 분할 반영, 배당 미반영) |
| adj_close | `adjust=all` 종가. 핵심 종목만, 순환 종목은 NULL |
| splits | 분할일에만 비율, 그 외 0 |
| dividends | 0 (무료 키로 받을 수 없음. stockpred 패널 계산은 이 컬럼을 쓰지 않음) |

- 타입: date `DATE`, open/high/low/close/adj_close/volume `DOUBLE`, dividends `DECIMAL(2,1)`, splits `DOUBLE`. 스키마 버전 4에서도 같습니다(volume은 `BIGINT`로 저장하고 뷰에서 `CAST(volume AS DOUBLE)`).
- 차분 저장(3절, 2026-11-24)으로도 컬럼 이름·의미는 그대로입니다. 바뀌는 것은 행 범위입니다: 5000행 응답보다 오래된 행이 지워지지 않고 남으므로, 상장 기간이 20년 넘는 종목은 이력이 거래일마다 1행씩 길어집니다. 분할·배당이 생기면 남은 옛 행도 같은 배율로 다시 맞추므로 한 종목 안에서 가격 기준이 섞이지 않습니다. 기준을 확인할 수 없으면 옛 행을 지웁니다(이전과 같은 결과).
- 데이터 규칙(3절, 2026-10-23)으로 컬럼 이름·의미는 그대로이고 행만 바뀝니다. 휴장일 봉과 시작일 이전 행이 빠지고(운영 반영 때 3,347행: 휴장일 2,956행, GM 2010-11-18 이전 391행), 보정 봉이 들어갑니다(MNST 2026-08-10). stockpred는 벤치마크 거래일로 맞추므로 휴장일 봉은 원래 쓰지 않았고, 달라지는 것은 GM·MNST이며 둘 다 Yahoo와 같아지는 방향입니다.

- Yahoo 캐시와 대조(2026-10-20, NVDA·BRK.B·SPY 5000행): close 일치(오차 5e-8 이하), volume 99% 구간 일치, 최근 2년 배당 조정계수 차이 0.007% 이하.
- 알려진 차이: Twelve Data `adjust=all`에는 2012~2013년 일부 배당(NVDA 첫 배당 3회, SPY 2012-09-21)이 빠져 있어 그 날짜를 지나는 수익률이 배당만큼(약 0.5%) 다릅니다. 그래서 어댑터 검증은 값 수준이 아니라 날짜별 조정계수 변화(`(adj/close)_t / (adj/close)_{t-1}`)를 비교하고, 허용 오차는 close 일치, 최근 2년 조정계수 0.01%, volume 1%로 둡니다.
- 대조 중 stockpred Yahoo 캐시의 2026-10-13 NVDA 종가가 장중 값(224.75, 확정 종가 228.52)으로 저장돼 있던 것을 발견했습니다. 스냅숏은 기준 거래일 이후 봉을 저장하지 않으므로 이런 값이 들어가지 않습니다.

### 스키마 버전 4와 재구성 (`src.rebuild`)

- 스키마 버전 4(2026-10-21): `ohlcv`·`indicators`의 날짜·숫자를 `VARCHAR` 대신 `DATE`/`DOUBLE`/`BIGINT`로 저장합니다. 문자열 PK 인덱스가 줄어 파일이 절반이 됩니다(운영 사본 리허설 2,273MB → 1,118MB). 뷰의 이름·열·타입은 그대로입니다.
- `ensure_db_schema`는 빈 파일이면 버전 4로 만들고, 다른 버전 파일은 고치지 않고 `SchemaVersionError`(변환 명령 안내)로 멈춥니다. 제자리 마이그레이션은 파일을 오히려 키우기 때문입니다(복사본 시험 3.03GB). `src.main`·`src.fred`·`src.finnhub_news`는 시작할 때 확인하므로 API를 부르기 전에 멈춥니다.
- 변환(배포 때 1회): `python -m src.rebuild --to-v4`. 비어 있지 않은데 변환되지 않는 값(날짜가 아닌 값, 유한하지 않은 실수, 정수가 아니거나 음수인 거래량)이 하나라도 있으면 아무것도 바꾸지 않고 멈춥니다. `""`는 NULL이 됩니다.
- 재구성 절차(파이프라인 잠금 안): 원본 `CHECKPOINT` → 연결 닫기 → `market.db.wal`이 남아 있으면 중단 → 원본을 `READ_ONLY`로 `ATTACH` → 같은 디렉터리 임시 파일(`.market.db.rebuild.tmp`)에 새 DB 생성(변환은 `COPY FROM DATABASE ... (SCHEMA)` 뒤 `ohlcv`·`indicators`를 PK 포함 DDL로 다시 만들고 `NULLIF(col, '')` + `CAST`로 복사, 정기 재구성은 `COPY FROM DATABASE`) → 닫고 `READ_ONLY`로 다시 열어 원본과 비교(테이블 목록·행 수·제약 조건(PK)·버전 4 열 구성·뷰 열과 타입·모든 테이블과 뷰의 행 해시 합) → 임시 WAL 없음 확인 → 원본을 하드링크로 백업(`market.db.bak-<UTC 날짜>`, 같은 날 두 번째는 시각까지) → `os.replace`. 어느 단계에서 실패해도 임시 파일만 지우고 원본은 그대로 둡니다. 원본을 먼저 옮기지 않습니다.
- 모든 연결에 `memory_limit`(변환 3GB, 정기 재구성 2GB), `threads`(4), `temp_directory`를 지정합니다.
- 정기 재구성: 지우거나 바꾼 행의 공간은 파일에 남고 `VACUUM`으로는 회수되지 않습니다. 매일 종목을 통째로 교체하던 때(2026-11-24 이전)는 실행마다 약 260MB씩 늘어 10-28 재구성 직후 1.37GB가 5회 실행 뒤 2.69GB가 됐습니다(거의 매주 재구성). 차분 저장 리허설에서는 다섯 세션치 핵심 변화와 순환 하루치를 넣고 지표까지 채워도 50MB(1,359 → 1,409MB) 늘었습니다. `run_daily.sh`는 전체 스냅숏 직전에 `src.rebuild --if-needed`를 돌려 `market.db`가 마지막 재구성 직후 크기(`data/state/db_maintenance.json`의 `baseline_bytes`)의 2배(`DB_REBUILD_GROWTH_FACTOR`)를 넘을 때만 재구성합니다. 기준이 없으면 현재 크기를 기준으로 기록만 합니다. 실패해도 다음 단계를 계속하고, dry-run은 판정만 합니다. 스냅숏은 그대로 파일 복사입니다.
- 백업 보관: 재구성이 성공해 `os.replace`까지 끝난 뒤에만, `market.db.bak-*` 중 최신 2개(이번 백업 포함, `--keep-backups`)를 남기고 나머지를 지웁니다(2026-10-21 결정). 실패한 재구성은 아무것도 지우지 않습니다. 지운 목록은 출력 JSON과 `db_maintenance.json` `history`의 `pruned_backups`에 남습니다. 백업은 `market.db`와 같은 디스크에 있어 디스크 고장에는 대비하지 못합니다(rclone 백업 대상에서도 제외). SSD를 추가하면 백업 위치를 옮길 예정입니다(`docs/STAGES.md` 예정 작업).
- 리허설(2026-10-21, 운영 스냅숏 사본, NVMe, `systemd-run MemoryMax=5G`): 변환 23.1초(검증 5.0초)·최대 RSS 2.2GB, `prices_daily` 등 뷰 5개 체크섬·NVDA/AAPL 분할 이벤트 동일, 재계산한 지표 241만 행 옛 값과 차이 0. 핵심 526종목 교체 2회 1,118 → 1,174 → 1,172MB, 지표 재계산 뒤 1,315MB. 정기 재구성 16.3초·최대 RSS 2.3GB로 1,315 → 1,113MB. 재구성 뒤 하루 주기(핵심 526종목 교체 + 지표 재계산)를 3번 재현하면 1,113 → 1,234 → 1,300 → 1,383MB(주기당 약 70~120MB, 순환·뉴스 제외)라 2배 기준(2,226MB)에는 대략 2~3주 뒤 닿습니다. 재현 사본의 `src.validate_duckdb` 이슈 0건.

## 6. 뉴스 (Finnhub)

- 분당 50회(`FINNHUB_PER_MINUTE`), 한도는 응답 헤더 기준 분당 60입니다. 하루 한도는 없습니다.
- 대상: 핵심 종목은 매 실행, 나머지 활성 종목은 아래 0건 간격으로 조회일이 된 것만입니다. 핵심 종목 먼저 처리합니다. `FINNHUB_LIMIT`이 비어 있으면 대상 전체입니다.
- 한 번에 약 250건에서 잘립니다(MSFT 1년 요청 → 최근 1주 250건). 200건 이상이고 가장 오래된 기사가 시작일보다 늦으면 `to`를 가장 오래된 기사 날짜로 당겨 다시 요청합니다. 하루에 250건이 넘으면 하루를 당깁니다.
- 종목당 실행마다 최대 10페이지. 다 못 받으면 `window_from`, `window_to`, `cursor_to`를 저장해 다음 실행에서 이어 받습니다. `last_to`는 구간을 끝까지 받은 뒤에만 갱신합니다.
- 첫 수집은 365일 전부터(`FINNHUB_DAYS_BACK`, 무료 범위 약 1년)입니다.
- 0건 간격(2026-11-24): 11-04 실행에서 뉴스 단계가 1시간 39분(전체 5시간 51분의 28%)이었고, 2,899/4,924종목이 0건이었습니다.
  - 핵심이 아닌 종목은 새 기사 없이 끝난 실행이 연속 1·2·3·4회 이상이면 다음 조회까지 1·2·4·7일을 기다립니다(`FINNHUB_BACKOFF_DAYS`=`1,2,4,7`, `--backoff-days`; `1`이면 모두 매 실행). 새 기사가 1건이라도 나오면 바로 매 실행으로 돌아갑니다. 핵심 종목도 연속 횟수는 세지만 건너뛰지 않습니다.
  - 새 기사는 그 종목에 저장돼 있지 않던 기사 id입니다. 조회 구간이 `last_to` 하루 전부터라 이미 저장한 기사가 다시 오기 때문입니다. 응답 건수는 `rows`, 새 기사 수는 `new_rows`입니다.
  - 상태: `finnhub_news.json` 종목 항목의 `zero_streak`(연속 횟수), `next_check_date`(이 날짜부터 조회, 비어 있으면 매 실행), `new_rows`. 필드가 없는 기존 항목은 매 실행 조회합니다. `next_check_date`가 가장 긴 간격보다 먼 날이면(시계 오류, 간격을 줄였을 때) 바로 조회합니다.
  - 조회일은 뉴욕 날짜로 판단합니다(`--check-date`로 바꿀 수 있음). 뉴스 단계는 UTC 자정 근처에 시작하므로(11-04 실측 01:22 UTC, 여름에는 한 시간 이른 약 00:20 UTC) UTC 날짜로 세면 연속한 두 실행이 같은 날짜가 되어 "내일" 조회할 종목을 건너뛸 수 있습니다. 수집 구간의 `to`는 전처럼 UTC 오늘입니다.
  - 건너뛴 날의 기사는 빠지지 않고 늦게 들어옵니다. 다음 조회가 `last_to` 하루 전부터(최대 `--max-days-back` 365일) 받기 때문입니다.
  - 실패는 전과 같습니다: 연속 횟수와 조회일을 바꾸지 않으므로 다음 실행에서 다시 조회합니다. 다 못 받은 구간이 남은 종목도 매 실행 조회하고, `--symbol`로 지정한 종목은 간격과 상관없이 조회합니다. `--symbols` 파일로 고른 종목은 핵심 구분이 없으므로 모두 간격을 따릅니다.
  - 효과 추정(2026-11-24, 11-04 스냅숏의 기사 날짜로 08-03~11-04 평일을 재현, 읽기 전용): 10-05 이후 순환 4,375종목 중 하루 평균 2,225개를 조회합니다(1,424~3,386, 월 3,296·화 2,331·수 1,726·목 1,504·금 2,096; 주말에 떨어지는 조회일이 월요일로 몰림). 핵심 526을 더하면 실행당 약 2,750개(지금 4,901, 약 44% 감소)이고, 분당 50회로 뉴스 단계가 약 1시간 39분에서 약 56분(월요일 약 77분)으로 줄 것으로 봅니다. 대가로 순환 종목 기사를 그날 받는 비율이 92.5%에서 67.4%로, 3일 안에 받는 비율이 100%에서 91.9%로 떨어지고 가장 늦은 기사는 6일 늦습니다. 핵심 종목은 그대로입니다.
  - 배포 뒤 처음 두 실행은 필드가 없거나 연속 1회라 전처럼 모두 조회하고, 그 뒤 1~2주에 걸쳐 줄어듭니다.
- 쓰기: 한 종목의 페이지를 모두 받은 뒤 DB 연결 하나·트랜잭션 하나로 씁니다(`storage.upsert_symbol_news`, 전에는 페이지마다 연결). 기사가 없으면 DB를 열지 않습니다. 중간 페이지가 실패하면 아무것도 쓰지 않고 상태도 그대로라 다음 실행이 같은 구간을 다시 받습니다. 실행 전체에 연결 하나를 쥐지는 않습니다.
- 빈 응답(`items: []`)이면 원본 파일(`data/raw/finnhub_news/<symbol>.json.gz`)을 덮어쓰지 않습니다. 기사가 있던 마지막 응답이 남습니다.
- 유동성(`ohlcv` 종목별 최근 60행 평균 거래대금, 전체 윈도 스캔)은 `--limit`이 있을 때만 읽습니다. 순서의 동점만 가르므로 대상 전체를 처리할 때는 필요 없습니다.
- 실행 요약: `check_date`, `backoff_days`, `waiting_backoff`(간격 때문에 뺀 종목 수), `new_rows`. `progress.py`의 뉴스 대상 수도 쉬는 종목을 뺍니다(`FINNHUB_BACKOFF_DAYS` 반영, 이번 실행에 이미 시도한 종목은 넣음).

## 7. FRED

- 기존 11개 + DGS2, BAA10Y, VIXCLS, DTB3, IR3TIB01KRM156N.
- `macro.first_seen_at`: 수집기가 그 관측값을 처음 받은 시각. 기존 행은 NULL(기록 시작 전)입니다. 값이 수정돼도 유지됩니다.
- `series_meta.personal_use_only`: 시리즈 설명에 저작권·복제 제한 문구가 있으면 참입니다(BAA10Y: Moody's, VIXCLS: CBOE). 개인 연구용으로만 쓰고, `src.export_dataset`은 기본으로 제외합니다.
- IR3TIB01KRM156N은 한국 월간 지표이며 관측일 대비 약 44일 뒤 공개됩니다.
- FRED 점검 페이지(HTTP 200 HTML)처럼 JSON이 아닌 응답은 `FredUnavailable`로 기록하고, 그 실행에서 남은 시리즈는 요청하지 않습니다. 단계는 실패(종료 코드 1)이고 완료 판정은 `missing_series`로 `partial`이 됩니다. 관측값은 매번 전체 이력을 받으므로 다음 성공 실행에서 채워집니다.

## 8. 실행 환경

- Python: `run_daily.sh` 기본값은 `.venv/bin/python`(없으면 `python3`).
- 설정: `run_daily.sh`도 `.env`를 읽습니다. 셸에서 준 값이 우선입니다. `/etc/data-scrapping.env`는 쓰지 않습니다.
- 설치: `scripts/install_local.sh`(venv 확인·생성, 패키지 설치, 사용자 유닛 설치·활성화). 백업 타이머는 설치만 하고 켜지 않습니다.
- 상태 표시: `summary.py`는 CPU 온도를 hwmon `k10temp`에서 읽습니다. `progress.py`는 종목 수를 파일에서 읽고 UTC 날짜를 씁니다. 잠금은 잡혀 있는데 `current_run`이 없으면(Twelve Data 외 단계) "실행 중"과 현재 단계(잠금 보유 프로세스와 그 자식의 명령줄), 뉴스 진행률(`finnhub_news.json`에서 잠금을 잡은 뒤 시도한 종목 수)을 보여 줍니다. "오늘(UTC)" 실패·건너뜀 수는 `updated_at`이 오늘(UTC)인 종목만 세고(`failed`·`not_found`), 최근 실패 목록은 최근 24시간 안의 실패를 날짜와 함께 보여 줍니다.

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
- Twelve Data 실행당 1,800크레딧·분당 7회, Finnhub 분당 50회에서 오류가 나는지.
- 겨울(표준시)에 전날 실행이 UTC 자정 뒤에 쓴 크레딧 때문에 핵심 단계가 키 예산에 막히지 않는지.
- 검증 경고: 새 `core_gap`(보정할지 알려진 빈칸으로 둘지 결정), 핵심 종목 `vanished_dates` WARNING, `patch_missing`. 순환 종목 `vanished_dates`는 1주 운영 뒤 소음 수준을 보고 계속 셀지 정합니다.
