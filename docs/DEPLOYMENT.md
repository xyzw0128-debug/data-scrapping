# DEPLOYMENT (NucBox M6 + Ubuntu 26.04)

## 사전 조건
- Python 3.11 이상 (`.venv`는 uv로 만든 Python 3.12)
- git, `flock`(util-linux), systemd 사용자 인스턴스
- 사용자 lingering: `loginctl show-user $USER -p Linger`가 `yes`여야 로그인하지 않은 상태에서도 타이머가 돕니다. `no`이면 `sudo loginctl enable-linger $USER`를 직접 실행합니다.
- 선택: rclone (백업 사용 시)

## 설치

```bash
scripts/install_local.sh
```

설치 스크립트가 하는 일:
1. `.venv`가 없으면 만듭니다(uv가 있으면 uv, 없으면 `python3 -m venv`).
2. `requirements.txt`를 `.venv`에 설치합니다.
3. `.env`가 없으면 `.env.example`을 복사하고 권한을 600으로 둡니다.
4. `systemd/`의 유닛 4개를 `~/.config/systemd/user/`에 설치합니다(`@ROOT@`를 저장소 경로로 바꿈).
5. `data-scrapping.timer`를 켭니다. 백업 타이머는 `ENABLE_BACKUP=1`일 때만 켭니다.

sudo는 쓰지 않습니다. 수집기는 lael 계정으로 돌기 때문에 데이터 파일 소유자도 lael입니다.

## API 키와 설정

`.env`(gitignore 대상, `chmod 600`)에 키와 설정을 넣습니다. 항목은 `.env.example`을 참고합니다.

- 필수: `TWELVE_DATA_API_KEY_1`(~`_3`), `FRED_API_KEY`, `FINNHUB_API_KEY`
- 선택: `ALPHA_VANTAGE_API_KEY`, `DISCORD_WEBHOOK_URL`
- 셸에서 준 값이 `.env`보다 우선합니다. 예: `DRY_RUN=1 scripts/run_daily.sh`

## 실행 시각

- `data-scrapping.timer`: `Mon..Fri 16:10 America/New_York`. 서머타임을 자동 반영합니다(겨울 06:10 KST, 여름 05:10 KST).
- `Persistent=true`: PC가 꺼져 있어 놓친 실행은 켜질 때 바로 돕니다. 그때가 장중이어도 끝나지 않은 당일 봉은 저장하지 않습니다.
- 소요 시간: 핵심 단계 약 2시간 30분(1,052크레딧 ÷ 분당 7회), 전체 약 3~5시간(뉴스 과거분을 채우는 첫 며칠은 더 김).

## 수동 점검

```bash
DRY_RUN=1 scripts/run_daily.sh
.venv/bin/python -m src.main --phase core --only SPY --only NVDA
.venv/bin/python -m src.snapshot --phase core --dry-run
.venv/bin/python scripts/progress.py --once
```

## systemd 사용자 유닛 명령

```bash
systemctl --user list-timers 'data-scrapping*' --all
systemctl --user status data-scrapping.service
journalctl --user -u data-scrapping -n 200 --no-pager
systemctl --user start data-scrapping.service      # 지금 1회 실행
systemctl --user disable --now data-scrapping.timer # 자동 실행 끄기
```

## rclone 백업 (현재 꺼짐)

1. rclone 설치 후 `rclone config`로 remote를 만듭니다(Google Drive 로그인은 직접 진행).
2. `.env`의 `RCLONE_DEST`(예: `Gdrive:data-scrapping`)를 확인합니다.
3. 점검: `RCLONE_DRY_RUN=1 scripts/rclone_backup.sh`
4. 켜기: `ENABLE_BACKUP=1 scripts/install_local.sh` 또는 `systemctl --user enable --now data-scrapping-backup.timer`

백업 스크립트는 단독으로 실행되면 파이프라인 잠금을 기다렸다가(최대 6시간) 복사하므로 쓰는 도중의 `market.db`를 올리지 않습니다. `run_daily.sh`의 `RUN_BACKUP=1`로 수집 직후에 돌릴 수도 있습니다. 재구성 임시 파일(`db/.*.tmp`)과 재구성 백업(`db/market.db.bak-*`)은 올리지 않습니다.

## 스키마 버전 4 변환 (1회)

스키마 버전 4 코드는 버전 3 `market.db`에 쓰지 않고 `SchemaVersionError`로 멈춥니다(수집 CLI는 API 호출 전에 확인). 그래서 코드 반영과 변환을 같은 시간대에 이어서 합니다.

1. 수집이 돌지 않는지 확인합니다: `systemctl --user is-active data-scrapping.service`가 `inactive`, `.venv/bin/python scripts/progress.py --once`, 다음 타이머(평일 16:10 New York) 전에 끝낼 시간이 있는지.
2. 운영 폴더에서 `git merge --ff-only <브랜치>`(안 되면 worktree에서 `main`을 병합하고 테스트를 다시 통과시킨 뒤).
3. 점검: `.venv/bin/python -m src.rebuild --to-v4 --dry-run`(버전 3, 크기, 여유 공간).
4. 변환: `.venv/bin/python -m src.rebuild --to-v4`. 운영 사본 리허설 기준 약 25초, 최대 RSS 약 2.2GB(`memory_limit` 3GB), 여유 공간은 원본 크기의 1.1배 이상 필요합니다. 출력 JSON의 `status`가 `converted`인지, `checks`의 행 수·뷰 체크섬을 확인합니다.
5. 확인: `ohlcv`·`indicators` 행 수, `prices_daily` 행 수, NVDA 분할 이벤트(2007-09-11 1.5, 2021-07-20 4.0, 2024-06-10 10.0).
6. 스냅숏 갱신: `.venv/bin/python -m src.snapshot --phase full`.
7. `git push origin main`.

변환 전 파일은 `data/db/market.db.bak-<UTC 날짜>`(하드링크)로 남습니다. 일주일쯤 운영을 확인한 뒤 지웁니다(그 전에 정기 재구성이 두 번 돌면 최신 2개 규칙으로 먼저 지워집니다). 변환이 실패하면 임시 파일만 지워지고 `market.db`는 버전 3 그대로입니다. 그때는 다음 수집 전에 운영 폴더 `main`을 반영 전 커밋으로 되돌립니다(아직 push 전이고 작업 트리가 깨끗하면 `git reset --hard ORIG_HEAD`).

## 데이터 경로

- 모든 CLI는 `--data-dir`로 데이터 경로를 바꿀 수 있습니다. `run_daily.sh`는 저장소의 `data/`를 씁니다.
- 디스크 사용량: `market.db` 약 1.1GB(스키마 버전 4. 버전 3은 약 2.3GB), 스냅숏 같은 크기, 원본 응답(gzip, 종목별 최신 1개) 약 0.6GB. 재구성 백업(`market.db.bak-*`)은 재구성이 성공할 때 최신 2개만 남기고 지웁니다(하나에 약 2GB).

## 트러블슈팅

- `{"status": "lock_held"}`(종료 코드 2): 다른 실행이 파이프라인 잠금(`data/state/pipeline.lock`)을 잡고 있습니다. 잠금은 프로세스가 끝나면 커널이 풀기 때문에 파일을 지울 필요가 없습니다. `.venv/bin/python scripts/progress.py --once`로 상태를 봅니다.
- 다른 프로그램이 `market.db`를 열어 두면 수집기 쓰기가 실패합니다. 읽기는 `market_snapshot.db`로 합니다.
- `SchemaVersionError: ... has schema version 3`: 스키마 버전 4 코드가 변환 전 DB를 만났습니다. 위의 "스키마 버전 4 변환"을 따릅니다.
- `db_rebuild` 단계 실패: `market.db`는 바뀌지 않았습니다. `journalctl --user -u data-scrapping`에서 오류를 봅니다. 남은 `.market.db.rebuild.tmp`는 다음 재구성이 지웁니다.
- API 키 오류: `.env`의 키 이름과 값을 확인한 뒤 `journalctl --user -u data-scrapping`과 `data/logs/collector.log`를 봅니다.
- 의존성 오류: `scripts/install_local.sh`를 다시 실행합니다.
