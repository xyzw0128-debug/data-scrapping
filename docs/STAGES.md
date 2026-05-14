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
- [x] DuckDB 검증 결과를 기존 `data/logs/validation/` 리포트 체계와 연동
- [x] CSV 저장(ohlcv/indicators/macro/news)에서 DuckDB 단일 DB(`data/db/market.db`) 저장 구조로 전환

## 다음 작업
- [x] run_daily.sh에 DuckDB 검증 단계 포함
- [x] Pi 배포 스크립트(install_pi.sh) 및 배포 가이드(DEPLOYMENT.md) 추가

## 이후 예정 작업
- [ ] 운영 중 축적 데이터 기준으로 검증 규칙(무결성/중복/이상치) 확장
- [ ] SSD 마운트 경로를 config로 관리하는 방안 검토
