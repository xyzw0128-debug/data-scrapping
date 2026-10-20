# STAGES

- [x] Project root `.env` auto-load added to API/webhook CLI entry points
- [x] Deferred previously failed symbols behind new/skipped symbols in collection queue
- [x] Count failed Twelve Data attempts against the active key budget
- [x] Split `src.main` symbol handling/progress updates into helper functions

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
- [x] Twelve Data 크레딧 기준 예산: 엔드포인트별 비용, `/api_usage` 서버 사용량 동기화, 실행 중 UTC 날짜 변경 반영, 실행당 크레딧 상한(`max_credits_per_run: 1400`)
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

## 이전 다음 작업
- [x] run_daily.sh에 DuckDB 검증 단계 포함
- [x] Pi 배포 스크립트(install_pi.sh) 및 배포 가이드(DEPLOYMENT.md) 추가 → 2026-10-20 `install_local.sh`로 교체

## 이후 예정 작업
- [ ] 운영 중 축적 데이터 기준으로 검증 규칙(무결성/중복/이상치) 확장
- [ ] 16:10 ET 직후 당일 일봉(거래량 포함) 확정 여부 확인, 필요하면 시작 시각 조정
- [ ] 운영 수치 조정: 실행당 1,400크레딧·분당 7회(Twelve Data), 분당 50회·잘림 기준 200건(Finnhub)
- [ ] S&P 공개 목록 반영 지연 모니터링(필요하면 `config/core_extra.txt` 사용)
- [ ] rclone 백업 설정 후 백업 타이머 켜기
- [ ] 상장폐지 종목 데이터 확보 검토(현재 심볼 목록 기준이라 생존 편향 존재; 2026-10-20부터는 상장폐지 종목 행을 보존)
- [ ] FRED 최초 발표값(ALFRED vintage) 기반 거시지표 시점 정렬 검토(`first_seen_at`은 2026-10-20 이후 관측값부터 기록)
