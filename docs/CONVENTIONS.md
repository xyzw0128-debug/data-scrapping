# CONVENTIONS

## 파일명/함수명 규칙
- Python 모듈 파일명은 `snake_case.py`를 사용합니다.
- 함수명은 `snake_case`를 사용합니다.
- CLI 엔트리 모듈은 `parse_args()`와 `main()` 패턴을 유지합니다.
- 상수는 대문자 스네이크(`OHLCV_FIELDS`, `ROOT`)를 사용합니다.

## argparse + JSON output 패턴
- CLI는 `argparse`로 옵션을 정의합니다.
- 실행 결과는 사람이 읽기 쉬운 `json.dumps(..., indent=2, sort_keys=True)` 출력 패턴을 사용합니다.
- 실패 종료 코드는 각 CLI 목적에 맞게 명시합니다(예: `--fail-on-issues` 시 issue가 있으면 1).

## Atomic write 규칙
- 파일 저장 시 임시 파일(`*.tmp`)에 먼저 기록합니다.
- 가능하면 `flush + fsync` 후 `os.replace(...)`로 원자적 교체합니다.
- 상태/요약/CSV/원본 JSON 저장 모두 같은 패턴을 유지합니다.

## 테스트 방식
- 문법/임포트 기본 점검: `python -m compileall src`.
- 동작 점검은 우선 dry-run 사용:
  - `python -m src.main --dry-run`
  - `python -m src.alpha_vantage --dry-run --max-symbols 1`
  - `python -m src.fred --dry-run --limit 2`
  - `python -m src.finnhub_news --dry-run --limit 2`

## 새 모듈 추가 체크리스트
- [ ] 파일명/함수명이 기존 snake_case 규칙을 따르는가?
- [ ] CLI라면 `parse_args()`/`main()`/JSON 출력 패턴을 따르는가?
- [ ] 저장 파일이 있다면 atomic write(`*.tmp` + replace)를 적용했는가?
- [ ] `data/` 하위 경로 생성/쓰기 가능성(`ensure_data_dirs`, `verify_writable`)을 고려했는가?
- [ ] dry-run 또는 API-free 점검 경로를 제공했는가?
- [ ] README 또는 `docs/STAGES.md`에 반영이 필요한 변경을 업데이트했는가?
