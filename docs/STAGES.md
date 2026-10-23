# STAGES

## 완료된 단계
- [x] Stage 1 MVP 기본 OHLCV 수집기(Twelve Data) 구현
- [x] 상태 파일(`state.json`) 기반 재개 및 일일 provider 상태 관리
- [x] provider 일일 제한/리저브 기반 예산 체크
- [x] raw JSON 아카이브 및 심볼별 OHLCV CSV 업서트 저장
- [x] dry-run 모드 및 파일 락 기반 중복 실행 방지
- [x] 로컬 로그(`collector.log`) 및 실행 요약 JSON 저장
- [x] 로컬 기술지표(SMA/RSI/MACD) CSV 생성
- [x] FRED 매크로 시리즈 수집
- [x] Finnhub company news 수집
- [x] 일일 summary + 하드웨어(디스크/CPU 온도) 점검
- [x] 경량 CSV validation 리포트
- [x] Alpha Vantage 백업 수집기
- [x] rclone 백업 스크립트 및 systemd 템플릿
- [x] DuckDB 기반 검증 CLI 구현
- [x] Twelve Data NASDAQ 티커 목록 갱신 CLI(`src.fetch_tickers`) 추가
- [x] Twelve Data 런당 심볼 처리량을 700개로 확대
- [x] 수집 대기열에서 실패 심볼을 우선 재시도하도록 정렬
- [x] 수집기 per-minute API 호출 제한 적용
- [x] Twelve Data rate-limit 응답 시 60초 대기 후 1회 재시도
- [x] 런타임 심볼 목록을 gitignore 처리하고 예제 파일 추가
- [x] Alpha Vantage DuckDB 저장 경로 회귀 테스트 추가
- [x] run_daily.sh에 선택적 티커 목록 갱신 단계 추가
- [x] DuckDB 검증 결과를 기존 `data/logs/validation/` 리포트 체계와 연동
- [x] CSV 저장(ohlcv/indicators/macro/news)에서 DuckDB 단일 DB(`data/db/market.db`) 저장 구조로 전환
- [x] `src.finnhub_news`의 미사용 CSV upsert 코드(`upsert_news_csv`) 제거
- [x] `AGENTS.md`를 에이전트 실행용 체크리스트 형태로 정리
- [x] `src.summary` Discord webhook 요청에 커스텀 User-Agent 헤더 추가(403 대응)
- [x] Webhook 기반 Discord 상태 메시지 생성/수정(실시간 진행률/전원 상태 표시) 추가
- [x] Discord 진행률 카운트를 provider 중첩 상태(`symbols.<symbol>.twelve_data`) 기준으로 보정
- [x] 다중 API 키 순차 소진 로테이션(KeyPool) 구현 (Twelve Data, Alpha Vantage)
- [x] API/webhook CLI 진입점에서 프로젝트 루트 `.env` 자동 로드
- [x] 수집 대기열에서 이전 실패 종목을 신규/건너뛴 종목 뒤로 미룸
- [x] Twelve Data 실패 시도도 사용 중인 키 예산에 차감
- [x] `src.main`의 종목 처리·진행 상황 갱신을 헬퍼 함수로 분리
- [x] DuckDB 저장을 행 단위 upsert에서 배치 upsert(NDJSON 스테이징 + `INSERT OR REPLACE`)로 전환 (5,000행 기준 약 20초 → 0.1초 미만)
- [x] `src.indicators`를 OHLCV가 바뀐 심볼만 다시 계산하는 증분 방식으로 변경(`--full`로 전체 재계산), 전체 약 1.5분
- [x] 타입 변환 뷰(`ohlcv_typed`, `indicators_typed`, `macro_typed`, `news_typed`) 추가
- [x] `src.fred`가 `config/fred_series.txt`의 전체 시리즈를 수집하도록 기본값 변경, DB에 있던 7개 시리즈를 목록에 추가
- [x] `src.finnhub_news` 심볼 로테이션(최근 시도 오래된 순 → 유동성 순) + 심볼별 재개 상태(`data/state/finnhub_news.json`) + 분당 호출 제한
- [x] `run_daily.sh`에서 단계 실패 시에도 다음 단계 계속 실행, 마지막에 실패 단계 보고 후 종료 코드 1
- [x] 종료된 PID가 남긴 `collector.lock`을 다음 실행에서 자동 정리
- [x] ML 데이터셋 내보내기(`src.export_dataset`): 시점 누수 방지 피처/라벨 Parquet + LLM 파인튜닝용 chat JSONL + metadata.json

## 2026-10-20 NucBox 이전 및 핵심 종목 수집 개편 (`docs/DESIGN.md`)
- [x] 설계 문서(`docs/DESIGN.md`) 작성: 핵심/순환 수집, 크레딧 예산, 완료 판정, stockpred 데이터 계약
- [x] 공통 파이프라인 잠금(`data/state/pipeline.lock`, flock): `run_daily.sh`와 데이터를 쓰는 모든 CLI가 공유, 죽은 프로세스의 잠금이 남지 않음
- [x] dry-run이 `state.json`·상태 파일·DB에 아무것도 쓰지 않도록 수정(`dry_run` 상태 오염 원인 제거)
- [x] Twelve Data 크레딧 기준 예산: 엔드포인트별 비용, `/api_usage` 서버 사용량 동기화, 실행 중 UTC 날짜 변경 반영, 실행당 크레딧 상한(`max_credits_per_run: 1400`, 2026-10-23 1800으로 상향)
- [x] 순환 수집 순서를 상태값 기준에서 "미수집 → 마지막 성공이 오래된 순"으로 변경(같은 A~B 700종목만 반복 수집하던 문제 해결)
- [x] 실패 재시도 대기(1·2·4·8·16·30일, 404는 30일), 4xx 응답 즉시 재요청 제거
- [x] 종목 목록 자동 갱신(`src.universe`): 핵심 526(ETF 23 + S&P 500), NASDAQ 목록, 상장폐지 표시(행 보존), S&P 제외 종목 순환 편입
- [x] 종목 통째 교체(옛 분할 기준 행·지표 행 삭제), 핵심 종목 `adj_close`(`adjust=all`), 분할 이벤트(`adjust=none` 비율) `corporate_actions`
- [x] 기준 거래일(NYSE 휴장일 포함) 계산, 장중 미완성 봉 저장 금지
- [x] 완료 판정(`complete/partial/failed`)과 `symbol_status`·`collection_runs` 기록, `market_snapshot.db` 원자적 교체, `prices_daily` 뷰
- [x] DuckDB 스키마 버전 2 마이그레이션(`ohlcv.adj_close`, `macro.first_seen_at`, 새 테이블·뷰)
- [x] Finnhub 잘린 응답 페이지 나눠 받기, 종목별 이어받기 창, 핵심 종목 우선, 전체 종목 대상(`FINNHUB_LIMIT` 비우면 전체)
- [x] FRED 5개 추가(DGS2, BAA10Y, VIXCLS, DTB3, IR3TIB01KRM156N), `first_seen_at`, `series_meta`(권리 제한 시리즈 개인용 표시, 데이터셋 내보내기 기본 제외)
- [x] 원본 응답을 종목·조정 방식별 최신 1개 gzip으로 저장(하루 1GB 증가 방지)
- [x] 실행 환경: `run_daily.sh`가 `.env`와 `.venv/bin/python`을 사용, systemd 사용자 유닛(`Mon..Fri 16:10 America/New_York`), `install_local.sh`, 루트 `.env.example`; `install_pi.sh`·`/etc/data-scrapping.env` 제거
- [x] `summary.py` CPU 온도를 hwmon `k10temp`에서 읽음, 마지막 수집 상태 표시
- [x] `progress.py`: 종목 수·계획 수를 실제 값으로, UTC 날짜, 단계·핵심 종목 완료율 표시, 파이프라인 잠금으로 실행 여부 판단
- [x] 실제 API 검증: 종목 목록 갱신, 핵심 3종목(NVDA·BRK.B·SPY)·순환 1종목, FRED 16개, 뉴스 2종목(페이지 나눠 받기), 스냅숏 동시 읽기, 지표 3,151종목 재계산, Yahoo 캐시 대조
- [x] 리뷰 반영(2026-10-20): 사용량 동기화 실패 키 제외·고정 키 예산 확인, 불확실한 시도 크레딧 차감, 완료 판정에 분할 이력·종목 목록 초기화 검사 추가(스키마 버전 3), 종목 목록 `--dry-run` 무네트워크(`--check` 분리)
- [x] systemd 사용자 타이머 켜기(`scripts/install_local.sh`, 첫 실행 2026-10-21 05:10 KST)
- [x] stockpred 스냅숏 어댑터(`SnapshotSource`, `data.price_source: snapshot`로 선택)와 Yahoo 대조 스크립트(stock_test `feat/snapshot-price-source` 브랜치): 핵심 3종목 허용 오차 통과

## 2026-10-21 첫 타이머 실행 후속 수정
- [x] `src.summary`: 요약 파일 저장이 `except LockHeldError` 블록의 `return` 뒤에 있어 실행되지 않던 문제 수정(`data/logs/daily/<date>.json` 생성, Discord 전송보다 먼저 저장)
- [x] `src.twelve_data`: `http.client.HTTPException`(`IncompleteRead`·`BadStatusLine` 등)을 결과 불확실 시도로 처리(크레딧 차감, 1회 재시도 후 그 종목만 실패). 핵심 수집 단계 전체가 중단되던 문제 해결. HTTP 오류 본문을 못 읽으면 상태 코드와 사유로 처리
- [x] `scripts/progress.py`: 잠금은 잡혀 있는데 `current_run`이 없으면 "실행 중"과 현재 단계(FRED·스냅숏·지표·뉴스 등) 표시, 뉴스 진행률(이번 실행 시도 수 / 대상 종목 수) 표시. 상태 파일 형식은 그대로
- [x] `scripts/run_daily.sh`: `SEND_DISCORD=1`일 때 Discord 상태 갱신 실패가 `set -e`로 실행 전체를 멈추지 않도록 경고만 남김
- [x] `src.universe`: NASDAQ 목록(`config/symbols.txt`)이 없거나 비어 있으면 마지막 갱신이 7일 안이어도 갱신(운영 폴더 브랜치 전환으로 파일이 지워져 순환 28종목·뉴스 약 555종목으로 줄던 문제). 갱신 이유를 결과 JSON `reason`(`forced`/`never_refreshed`/`nasdaq_list_missing`/`max_age`)에 기록, `--dry-run`은 `nasdaq_count`도 보고. `--skip-catalog`는 목록을 다시 쓰지 않으므로 이 조건을 보지 않음. 카탈로그 호출은 0크레딧
- [x] `src.twelve_data`: 핵심 단계가 실행당 상한을 다 쓴 뒤의 순환 단계에서 `sync_usage()`가 키를 `usage_sync_failed: run_credit_cap`으로 제외하고 경고를 남기던 문제 수정. 상한이 `/api_usage` 1크레딧을 못 내면 조회 없이 `usage_sync`에 `skipped: run_credit_cap`만 남기고 키를 제외하지 않음(상한이 그 실행의 유료 호출을 모두 막으므로 reserve 영향 없음). 키 예산 부족 건너뛰기와 두 번 실패한 키 제외는 그대로이고, 실제 실패 뒤 재시도가 예산에 막혀 제외될 때는 사유를 `run_credit_cap`이 아닌 실제 실패로 기록

## 2026-10-21 에이전트 작업 규칙 갱신
- [x] `AGENTS.md`를 개편 이후 상태로 갱신: 필수 읽기에 `docs/DESIGN.md`(설치·운영 작업은 `docs/DEPLOYMENT.md`) 추가, 크레딧 예산(실행당 상한 포함)·`data/state/` 상태 파일·파이프라인 잠금·dry-run·stockpred 스냅숏 계약 규칙, 운영 PC 주의사항(운영 폴더는 `main` 유지·코드 작업은 worktree, 실제 API 소량 호출, 실행 중 호출 금지, 키 비노출, 실행 감시 시 보고만), 테스트·dry-run 점검 단계 추가
- [x] `docs/STAGES.md` 맨 위에 제목 없이 있던 2026-05-21 항목 4개를 "완료된 단계"로 옮김
- [x] 변경 반영 방식을 PR 대신 로컬 fast-forward 병합으로 변경: worktree에서 테스트 → 수집이 돌지 않을 때 운영 폴더 `main`에 `git merge --ff-only` → `git push origin main`. 문서만 바꾸는 작업은 운영 폴더 `main`에서 바로 커밋 가능

## 2026-10-21 효율 개선 1단계: 구조 정리·관측·스키마 v4 대비
- [x] `src.atomic_io`: 원자적 저장 공용 함수. `state.json`·실행 요약·일일 요약·검증 리포트·Discord 상태·`universe.json`·S&P CSV·`symbols.txt`·raw gzip 저장 8곳을 교체. 일일 요약·Discord 상태·검증 리포트에 빠져 있던 fsync 추가. 직렬화에 실패하면 기존 파일을 그대로 두고, JSON의 DATE/TIMESTAMP는 ISO 문자열로 저장. 파일 형식은 그대로
- [x] `src.catalog`: `extract_symbols`·`atomic_write_symbols`를 `src.fetch_tickers`에서 분리(`src.universe`가 단독 CLI 모듈에 의존하지 않음)
- [x] `src.validate_duckdb`가 옛 CSV 모듈(`src.validate`)에 의존하지 않음. `src.validate` CLI는 호환용으로 유지
- [x] 일일 요약에 `market.db`·스냅숏 파일 크기 기록(`files.db_files`, Discord `db_mb`·`snapshot_mb`). 매일 종목을 통째로 교체하면 지운 행의 공간이 회수되지 않아 파일이 커지는지 관측하기 위함. 운영 DB 복사본 시험(2026-10-21): 실제 저장 경로로 핵심 526종목 교체를 이틀 재현하면 2,273MB → 2,504MB → 2,581MB, `VACUUM`은 효과 없음, 새 파일로 재구성하면 원래 크기로 돌아옴
- [x] 스키마 v4(DATE·숫자 열) 대비, v3(VARCHAR)에서는 결과 동일: `load_closes` 키를 ISO 문자열로 고정(DATE 키면 분할 감지·분할 이벤트 계산이 조용히 빈 결과를 내고 기존 분할 기록을 지움), `_ohlcv_dict`는 NULL만 빈 문자열로(거래량 0 보존), DuckDB 검증 리포트의 날짜 값 직렬화(저장·출력)

## 2026-10-21 효율 개선 2단계: 스키마 v4·재구성 (`docs/DESIGN.md` 5절)
- [x] DuckDB 스키마 버전 4: `ohlcv`는 date `DATE`, open/high/low/close/adj_close `DOUBLE`, volume `BIGINT`(updated_at은 `find_stale_symbols`의 문자열 비교 때문에 `VARCHAR`, 열 순서는 버전 3과 같음). `indicators`는 중복 OHLCV 5개 열을 없애고 date `DATE`, 지표 7개 `DOUBLE`(소수 6자리 반올림, 계산 전 구간 NULL). `macro`·`news`는 그대로. 뷰 `prices_daily`·`ohlcv_typed`·`indicators_typed`·`macro_typed`·`news_typed`의 이름·열·타입 유지(volume은 뷰에서 `CAST(volume AS DOUBLE)`)
- [x] 쓰기 정규화: Twelve Data 봉을 Python에서 검사(날짜 `YYYY-MM-DD`, 가격 유한 실수, 거래량 0 이상 정수, 거래량·`adj_close`만 빈 값 → NULL, 거래량 0 보존). 이상 값은 그 종목만 실패(`ValueError`, 기존 실패·재시도 흐름)하고 저장된 행은 그대로. NDJSON에 JSON 숫자로 쓰고 `read_json`을 테이블 타입으로 읽음. Alpha Vantage 저장도 같은 정규화를 거침. 운영 원본 응답 1,401개(`splits` 527·`all` 526·`none` 348)가 모두 통과
- [x] `src.rebuild`: 공용 재구성 CLI(파이프라인 잠금, JSON 출력). 원본 `CHECKPOINT` → 연결 닫기 → WAL 남으면 중단 → 원본 `READ_ONLY` `ATTACH` → 같은 디렉터리 임시 파일에 생성 → `READ_ONLY`로 다시 열어 검증(테이블·행 수·제약 조건·버전 4 열 구성·뷰 열과 타입·행 해시 합) → 임시 WAL 없음 확인 → 하드링크 백업(`market.db.bak-<UTC 날짜>`) → `os.replace`. 실패하면 임시 파일만 지우고 원본은 그대로. 모든 연결에 `memory_limit`(변환 3GB, 정기 2GB)·`threads`·`temp_directory` 지정. 재구성 직후 크기를 `data/state/db_maintenance.json`에 기록
- [x] `--to-v4`: 버전 3 → 4 변환(`COPY FROM DATABASE ... (SCHEMA)` 뒤 `ohlcv`·`indicators`를 PK 포함 DDL로 다시 만들고, 비어 있지 않은데 변환되지 않는 값이 0건인지 검사한 뒤 `NULLIF(col, '')` + `CAST`로 복사). DuckDB `CAST('1.5' AS BIGINT)`는 오류 없이 2가 되므로 거래량은 정수 여부까지 검사
- [x] 정기 재구성: `run_daily.sh`가 전체 스냅숏 직전에 `src.rebuild --if-needed` 실행(`RUN_DB_REBUILD`, `DB_REBUILD_GROWTH_FACTOR`=2). 마지막 재구성 직후 크기의 2배를 넘을 때만 재구성, 기준이 없으면 현재 크기를 기준으로 기록만. 실패해도 다음 단계 계속, dry-run은 판정만. 스냅숏은 파일 복사 그대로. `progress.py` 단계 이름 "DB 재구성", rclone 백업에서 재구성 임시 파일·백업 제외
- [x] 배포 보호: 버전 4 코드가 다른 버전 DB를 만나면 `ensure_db_schema`가 자동 변환 없이 `SchemaVersionError`(변환 명령 안내)를 냄. `src.main`·`src.fred`·`src.finnhub_news`는 시작할 때 확인해 API 호출 전에 멈춤
- [x] 테스트: v3 → v4 변환(뷰 결과 동일, 거래량 0 보존, `""` → NULL, 분할 이벤트), 변환 불가 값·검증 실패·`os.replace` 실패 시 원본 보존과 임시 파일 정리, 하드링크 백업(같은 inode), 정기 재구성 판정, 쓰기 정규화, 이상 값 종목만 실패, v3 DB에서 수집 시작 전 중단
- [x] 리허설(운영 스냅숏 사본, NVMe, `systemd-run MemoryMax=5G`): 변환 2,273MB → 1,118MB, 23.1초(검증 5.0초), 최대 RSS 2.2GB. 독립 스크립트로 비교한 뷰 5개 행 수·열 타입·체크섬, NVDA(2007-09-11 1.5, 2021-07-20 4.0, 2024-06-10 10.0)·AAPL(2014-06-09 7.0, 2020-08-31 4.0) 분할 이벤트(저장값, `load_closes` + `split_events` 재계산, `prices_daily.splits`)가 변환 전과 같음. 운영 원본으로 핵심 526종목 교체 2회 실패 0건(1,174 → 1,172MB), 지표 재계산 526종목 36초·옛 값과 차이 0행, 재현 후에도 `prices_daily` 행 단위 동일. 정기 재구성 1,315MB → 1,113MB, 16.3초, 최대 RSS 2.3GB. 재구성 뒤 하루 주기(핵심 526종목 교체 + 지표 재계산)를 3번 재현하면 1,113 → 1,234 → 1,300 → 1,383MB(주기당 약 70~120MB, 순환·뉴스 제외)라 2배 기준(2,226MB)에는 대략 2~3주 뒤 닿습니다. 재현 사본의 `src.validate_duckdb` 이슈 0건
- [x] 백업 보관 개수 2개(2026-10-21 결정): 재구성이 성공한 뒤에만 `market.db.bak-*` 중 최신 2개(이번 백업 포함)를 남기고 나머지 삭제(`--keep-backups`, 실패한 재구성은 삭제하지 않음, 지운 목록은 `pruned_backups`)
- [x] 운영 반영(2026-10-21 15:05~15:08 KST, 수집 대기 중): 운영 폴더 `main` fast-forward(ae4dc9d → 73636d7) → `src.rebuild --to-v4`: 2,273MB → 1,120MB, 22.9초(검증 4.7초), 최대 RSS 2.16GB(`systemd-run MemoryMax=5G`). 독립 스크립트 비교에서 테이블 행 수, 뷰 5개 열·타입·체크섬(`prices_daily` 10,158,942행), NVDA·AAPL 분할 이벤트가 변환 전과 같음 → `src.snapshot --phase full`(스냅숏도 버전 4, 상태는 변환 전과 같은 `partial`: 핵심 524/526, 분할 이력 348, 누락 PSKY·WBD)
- [ ] 변환 백업(`data/db/market.db.bak-2026-10-21`, 2.27GB) 2026-10-28쯤 운영 확인 후 삭제

## 2026-10-22 두 번째 타이머 실행 후속 수정
- [x] `src.fred`: FRED 점검 중 응답(HTTP 200 + HTML "Site Maintenance | St. Louis Fed")에 16개 시리즈가 모두 `Expecting value: line 1 column 1 (char 0)`으로 실패하던 것을, JSON이 아닌 응답이면 `FredUnavailable`(HTTP 상태·Content-Type·HTML 제목)로 기록하도록 수정. 한 번 만나면 남은 시리즈는 요청하지 않고 `not requested: ...`로 실패 기록, 결과 JSON에 `unavailable`. 단계 종료 코드 1과 완료 판정(`missing_series`)은 그대로
- [x] `scripts/progress.py`: "오늘(UTC)" 실패·건너뜀 수를 `updated_at`이 오늘(UTC)인 종목만 세도록 수정(2026-06-25 실패 15개가 오늘 실패로 보이던 문제), `not_found`도 실패로 셈. 최근 실패 목록은 최근 24시간 안의 실패(`not_found` 포함)를 날짜와 함께 최신순으로 표시(전에는 날짜 없이 시각 문자열로 정렬). 상태 파일 형식은 그대로

## 2026-10-23 Twelve Data 실행당 크레딧 상한 1800
- [x] 실행당 상한 1400 → 1800(`config/providers.yaml`, `.env.example`, 운영 `.env`의 `TWELVE_MAX_CREDITS`). `TWELVE_MAX_SYMBOLS`는 700 그대로라 순환이 하루 약 340 → 700종목(약 4,380종목 한 바퀴 약 13 → 6거래일), 사용량 약 1,760. 2026-10-23 실행 실측(핵심 1,055크레딧 150분, 순환 345크레딧 49.5분, 둘 다 분당 7.0)으로 보면 OHLCV 단계가 약 50분 늘어남. 키를 순서대로 쓰고 분당 제한이 모든 호출에 걸리므로 키가 늘어도 속도는 같음. 핵심 스냅숏 게시 시각은 그대로
- [x] 2100(키 3개 전부)은 보류: 겨울(21:10 UTC 시작)에는 자정 전에 약 1,190크레딧만 쓸 수 있어 나머지가 다음 날 키 예산에서 빠지고, 다음 날 핵심 단계(약 1,055) 여유가 약 130으로 줄어듦(1800이면 약 470)
- [ ] 표준시 전환(2026-11-02 첫 실행) 뒤 핵심 단계가 키 예산에 막히지 않는지 확인

## 이전 다음 작업
- [x] run_daily.sh에 DuckDB 검증 단계 포함
- [x] Pi 배포 스크립트(install_pi.sh) 및 배포 가이드(DEPLOYMENT.md) 추가 → 2026-10-20 `install_local.sh`로 교체

## 이후 예정 작업
- [ ] 운영 중 축적 데이터 기준으로 검증 규칙(무결성/중복/이상치) 확장
- [ ] 16:10 ET 직후 당일 일봉(거래량 포함) 확정 여부 확인, 필요하면 시작 시각 조정
- [ ] 운영 수치 조정: 실행당 1,800크레딧·분당 7회(Twelve Data), 분당 50회·잘림 기준 200건(Finnhub)
- [ ] S&P 공개 목록 반영 지연 모니터링(필요하면 `config/core_extra.txt` 사용)
- [ ] rclone 백업 설정 후 백업 타이머 켜기
- [ ] 상장폐지 종목 데이터 확보 검토(현재 심볼 목록 기준이라 생존 편향 존재; 2026-10-20부터는 상장폐지 종목 행을 보존)
- [ ] FRED 최초 발표값(ALFRED vintage) 기반 거시지표 시점 정렬 검토(`first_seen_at`은 2026-10-20 이후 관측값부터 기록)
- [ ] 효율 개선 3단계: 2026-10-20 이전 raw 타임스탬프 파일(5,312개, 1.946GB)을 `docs/DESIGN.md` 보관 규칙 수정 후 tar.gz로 압축 보관(약 0.21GB). 원본 삭제는 대조 후 별도 확인
- [ ] 바뀐 행만 upsert하는 쓰기 최적화 검토(2단계 이후 쌓인 파일 크기 기록을 보고 판단, 분할 기준 변경 시 전체 갱신·지표 재계산 필요)
- [ ] 재구성 백업을 추가 SSD로 옮기기(사용자가 SSD를 단 뒤 요청할 때). 다른 파일 시스템에는 하드링크를 만들 수 없으므로, `os.replace` 전에 원본을 SSD로 복사(약 2GB)하고 확인하는 방식으로 바꿔야 함. 보관 개수 2개는 그대로
- 보류: Twelve Data 키별 병렬 수집 레인(총 크레딧은 그대로이고 단축되는 건 핵심 단계뿐, 약관 2.3(h)·(i) 위험). 시간 단축이 필요하고 Twelve Data가 키별 독립 한도 사용을 허용한다고 확인되면 재검토
