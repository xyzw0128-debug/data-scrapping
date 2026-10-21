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
- 파일 저장은 `src.atomic_io`의 `atomic_write_bytes`/`atomic_write_text`/`atomic_write_json`을 씁니다. 직접 `os.replace`를 구현하지 않습니다. 예외는 DuckDB 파일 교체 두 곳뿐입니다: 스냅숏(`src.snapshot`, 복사 → 읽기 전용 확인 → `os.replace`)과 재구성(`src.rebuild`, 임시 파일 → 원본과 비교 → 하드링크 백업 → `os.replace`). `market.db`를 다른 방법으로 옮기거나 지우지 않습니다.
- 이 함수들은 임시 파일(`<이름>.tmp`)에 쓰고 `flush + fsync` 후 `os.replace(...)`로 교체합니다. JSON은 직렬화가 끝난 뒤에 임시 파일을 만들므로, 직렬화에 실패하면 기존 파일이 그대로 남습니다.
- JSON 형식은 `indent=2, sort_keys=True` + 끝 줄바꿈이고, DuckDB DATE/TIMESTAMP 값은 ISO 문자열로 저장합니다.
- DB 결과를 Python에서 다룰 때 값이 문자열이라고 가정하지 않습니다(날짜는 `str(day)`로 맞추고, 숫자 0은 `value or ""`처럼 빈 값으로 바꾸지 않습니다).

## DB 쓰기 값 규칙 (스키마 버전 4)
- `ohlcv`·`indicators`는 날짜 `DATE`, 값 `DOUBLE`/`BIGINT` 열입니다. 값은 Python에서 정규화한 뒤 씁니다: OHLCV는 `storage.normalize_ohlcv_row`(날짜 `YYYY-MM-DD`, 유한한 실수, 0 이상 정수 거래량, 거래량·`adj_close`만 빈 값 → NULL), 지표는 `float` 또는 `None`.
- `bulk_upsert`는 NDJSON을 테이블 열 타입 그대로 `read_json`으로 읽습니다. 숫자는 JSON 숫자, 날짜는 ISO 문자열, NULL은 `None`으로 넘깁니다. `""`를 숫자 열에 넘기면 오류가 납니다.
- 이상 값은 `ValueError`로 그 종목만 실패시킵니다(기존 실패·재시도 흐름). 스키마 버전이 맞지 않는 것은 `SchemaVersionError`(`RuntimeError`)로 실행 전체를 멈춥니다. 종목별 `except ValueError`에 걸리지 않게 둘을 섞지 않습니다.
- 스키마를 바꾸면 `SCHEMA_VERSION`을 올리고, 제자리 `ALTER` 대신 `src.rebuild`의 변환 경로를 씁니다. 뷰(`prices_daily` 등)의 이름·열·타입은 stockpred 계약이라 그대로 둡니다.

## 잠금과 dry-run 규칙
- `market.db`나 `data/state/` 파일을 쓰는(또는 `market.db`를 여는) CLI는 `main()`에서 `pipeline_lock(args.data_dir)`을 잡고, `LockHeldError`면 `lock_held_exit()`(종료 코드 2)로 끝냅니다.
- dry-run은 수집 API를 부르지 않고 `state.json`·상태 파일·DB에 아무것도 쓰지 않습니다(필요하면 상태를 `copy.deepcopy`해서 씁니다). 로그와 실행 요약 파일은 남겨도 됩니다.

## 테스트 방식
- 단위 테스트: `.venv/bin/python -m unittest discover -s tests`.
- 문법/임포트 기본 점검: `.venv/bin/python -m compileall src`, 셸은 `bash -n scripts/*.sh`.
- 동작 점검은 우선 dry-run 사용:
  - `DRY_RUN=1 scripts/run_daily.sh`
  - `.venv/bin/python -m src.main --dry-run`
  - `.venv/bin/python -m src.fred --dry-run --limit 2`
  - `.venv/bin/python -m src.finnhub_news --dry-run --limit 2`
  - `.venv/bin/python -m src.snapshot --dry-run`
  - `.venv/bin/python -m src.rebuild --if-needed --dry-run` (재구성 판정만)
- 실제 API 점검은 소량으로: `.venv/bin/python -m src.main --phase core --only SPY`.

## 새 모듈 추가 체크리스트
- [ ] 파일명/함수명이 기존 snake_case 규칙을 따르는가?
- [ ] CLI라면 `parse_args()`/`main()`/JSON 출력 패턴을 따르는가?
- [ ] 저장 파일이 있다면 `src.atomic_io`로 저장하는가?
- [ ] `data/` 하위 경로 생성/쓰기 가능성(`ensure_data_dirs`, `verify_writable`)을 고려했는가?
- [ ] dry-run 또는 API-free 점검 경로를 제공했는가?
- [ ] DB나 상태 파일을 쓴다면 파이프라인 잠금을 잡는가?
- [ ] README 또는 `docs/STAGES.md`에 반영이 필요한 변경을 업데이트했는가?
