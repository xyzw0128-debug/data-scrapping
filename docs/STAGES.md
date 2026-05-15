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
- [x] Alpha Vantage DuckDB 저장 경로 회귀 테스트 추가
- [x] run_daily.sh에 선택적 티커 목록 갱신 단계 추가
- [x] DuckDB 검증 결과를 기존 `data/logs/validation/` 리포트 체계와 연동
- [x] CSV 저장(ohlcv/indicators/macro/news)에서 DuckDB 단일 DB(`data/db/market.db`) 저장 구조로 전환
- [x] `src.finnhub_news`의 미사용 CSV upsert 코드(`upsert_news_csv`) 제거
- [x] `AGENTS.md`를 에이전트 실행용 체크리스트 형태로 정리
- [x] `src.summary` Discord webhook 요청에 커스텀 User-Agent 헤더 추가(403 대응)
- [x] Webhook 기반 Discord 상태 메시지 생성/수정(실시간 진행률/전원 상태 표시) 추가

## 다음 작업
- [x] run_daily.sh에 DuckDB 검증 단계 포함
- [x] Pi 배포 스크립트(install_pi.sh) 및 배포 가이드(DEPLOYMENT.md) 추가

## 이후 예정 작업
- [ ] 운영 중 축적 데이터 기준으로 검증 규칙(무결성/중복/이상치) 확장
- [ ] SSD 마운트 경로를 config로 관리하는 방안 검토
