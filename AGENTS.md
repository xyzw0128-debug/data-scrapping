# AGENTS

에이전트가 바로 실행하기 쉬운 형태의 작업 규칙 요약입니다.

## 0) 시작 전 필수 읽기 (순서 고정)
1. `README.md`
2. `docs/PROJECT.md`
3. `docs/ARCHITECTURE.md`
4. `docs/DESIGN.md`
5. `docs/PROVIDERS.md`
6. `docs/CONVENTIONS.md`
7. `docs/STAGES.md`

설치·systemd 유닛·`.env`·백업을 건드리는 작업이면 `docs/DEPLOYMENT.md`도 읽습니다.

## 1) 작업 중 판단 기준 (Always)
- README + 실제 코드(`src/`, `config/`, `scripts/`, `systemd/`)를 기준으로 판단합니다.
- 코드에 없는 내용은 문서에 단정적으로 쓰지 않습니다.
- API 제한 보수 운영을 절대 깨지 않습니다: 키별 일일 예산(`daily_limit - daily_reserve`), 실행당 크레딧 상한(`max_credits_per_run`), 분당 제한(`per_minute_limit`), `/api_usage` 서버 사용량 동기화, 결과가 불확실한 시도의 크레딧 차감.
- 실행 중단 후 재개 가능하도록 `data/state/` 상태 파일 흐름을 유지합니다: `state.json`(provider 사용량·종목 상태), `universe.json`, `fred.json`(완료 판정에 사용), `finnhub_news.json`(뉴스 이어받기 창).
- `market.db`나 `data/state/`를 쓰는 CLI는 `pipeline_lock`을 잡고, 잡지 못하면 `lock_held_exit()`(종료 코드 2)로 끝냅니다.
- dry-run은 수집 API를 부르지 않고 상태 파일·DB에 아무것도 쓰지 않습니다.
- stockpred 데이터 계약(`docs/DESIGN.md` 5절)을 깨지 않습니다: 다른 프로그램은 `market_snapshot.db`만 읽고, `prices_daily` 등 스냅숏 테이블·뷰의 컬럼 이름과 의미를 유지하며, 끝나지 않은 장의 봉은 저장하지 않습니다.

## 2) 운영 PC에서 작업할 때 (Live)
- 이 저장소는 매일 수집이 도는 실제 운영 PC입니다(systemd 사용자 타이머, 평일 16:10 New York 시간).
- 운영 폴더(이 저장소 루트)는 `main`에 두고 브랜치를 바꾸지 않습니다. 타이머가 이 폴더의 코드를 그대로 실행하기 때문입니다. 코드 작업은 git worktree에서 합니다. `config/symbols.txt`를 추적하던 옛 커밋(`f8dde34` 이전)을 checkout했다가 돌아오면 이 파일이 지워집니다(다음 실행의 `src.universe`가 다시 받습니다).
- 실제 API 호출은 매일 수집과 같은 크레딧을 씁니다. 점검은 dry-run을 먼저 쓰고, 실제 호출은 `--only SPY`처럼 소량만 합니다.
- 수집이 도는 중(파이프라인 잠금이 잡혀 있을 때)에는 실제 API를 호출하지 않습니다. 진행 상태는 `.venv/bin/python scripts/progress.py --once`로 봅니다.
- 데이터 조회는 `market_snapshot.db`로 합니다. `market.db`를 열어 두면 수집기 쓰기가 실패합니다.
- `.env`의 API 키와 webhook URL은 출력하거나 커밋하지 않습니다. `SEND_DISCORD=1`이면 dry-run도 Discord로 메시지를 보냅니다.
- 실제 수집 실행을 지켜볼 때는 코드를 고치지 않고 오류만 정리해서 보고합니다. 크레딧 상한 초과, 종목 대량 실패, 디스크·DB·메모리 오류, 멈춘 실행처럼 심각한 경우에만 `systemctl --user stop data-scrapping.service`로 멈춥니다. 수정은 실행이 끝난 뒤 제안하고 승인을 받은 다음 합니다.

## 3) 완료 직전 체크리스트 (Done gate)
- [ ] 변경이 README/문서와 충돌하지 않는지 확인
- [ ] 테스트 통과: `.venv/bin/python -m unittest discover -s tests`, `.venv/bin/python -m compileall src`, 셸을 바꿨으면 `bash -n scripts/*.sh`
- [ ] 수집 흐름을 바꿨으면 `DRY_RUN=1 scripts/run_daily.sh`로 점검
- [ ] 상태 재개 흐름(`data/state/` 상태 파일) 훼손 여부 확인
- [ ] API budget/reserve/실행당 크레딧 상한 원칙 위반 여부 확인
- [ ] 데이터를 쓰는 CLI가 파이프라인 잠금을 잡는지, dry-run이 아무것도 쓰지 않는지 확인
- [ ] 스냅숏 계약(`prices_daily` 등)을 바꿨다면 stockpred 쪽 영향을 함께 기록
- [ ] `docs/STAGES.md`를 현재 상태로 업데이트(4절의 예외만 생략 가능)

## 4) 문서 반영 원칙
- 기능/운영 흐름에 영향이 있는 변경은 `docs/STAGES.md`에 기록합니다.
- 설정값·예산·스냅숏 계약이 바뀌면 `docs/DESIGN.md`, `docs/PROVIDERS.md`, README의 해당 내용도 같이 고칩니다.
- 단순 오탈자/형식 수정은 `docs/STAGES.md` 기록을 생략할 수 있지만, 생략 시 PR 본문에 사유를 남깁니다.
