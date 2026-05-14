# DEPLOYMENT (Raspberry Pi 5 + Ubuntu)

## 사전 조건
- Python 3.11 이상
- git
- pip
- 선택: rclone (백업 타이머 사용 시)

## 설치 순서
1. 저장소 클론
   ```bash
   git clone <repo-url>
   cd data-scrapping
   ```
2. 설치 스크립트 실행
   ```bash
   scripts/install_pi.sh
   ```
3. `/etc/data-scrapping.env` API 키 설정
4. dry-run 수동 테스트
5. 실제 1종목 테스트
6. systemd timer 상태 확인

## API 키 설정
`/etc/data-scrapping.env`를 열어 실제 키를 입력합니다.

```bash
sudoedit /etc/data-scrapping.env
sudo chmod 600 /etc/data-scrapping.env
```

필수 키(운영 시):
- `TWELVE_DATA_API_KEY`
- `FRED_API_KEY`
- `FINNHUB_API_KEY`
- 선택: `ALPHA_VANTAGE_API_KEY`, `DISCORD_WEBHOOK_URL`

## 수동 테스트 명령
```bash
DRY_RUN=1 scripts/run_daily.sh
python -m src.main --provider twelve_data --max-symbols 1
python -m src.validate_duckdb
```

## systemd 운영 명령
상태 확인:
```bash
sudo systemctl status data-scrapping.timer
sudo systemctl status data-scrapping.service
sudo systemctl status data-scrapping-backup.timer
sudo systemctl status data-scrapping-backup.service
```

로그 확인:
```bash
sudo journalctl -u data-scrapping.service -n 200 --no-pager
sudo journalctl -u data-scrapping-backup.service -n 200 --no-pager
```

enable:
```bash
sudo systemctl enable data-scrapping.timer
sudo systemctl enable data-scrapping-backup.timer
```

disable:
```bash
sudo systemctl disable data-scrapping.timer
sudo systemctl disable data-scrapping-backup.timer
```

수동 1회 실행:
```bash
sudo systemctl start data-scrapping.service
sudo systemctl start data-scrapping-backup.service
```

## rclone 설정
rclone은 별도 설치/설정이 필요합니다.

```bash
rclone config
```

`/etc/data-scrapping.env`에서 `RCLONE_DEST`를 설정합니다.

```bash
RCLONE_DEST=remote:data-scrapping
```

백업 스크립트 dry-run 예시:
```bash
RCLONE_DRY_RUN=1 scripts/rclone_backup.sh
```

## SSD 마운트 사용 시
코드/기존 스크립트가 지원하는 범위 내에서만 설정합니다.

- 백업 스크립트 대상 경로: `/etc/data-scrapping.env`의 `BACKUP_DATA_DIR`
- 수집기 데이터 경로 변경: 실행 명령에 `--data-dir` 사용
  - 예: `python -m src.main --provider twelve_data --data-dir /mnt/ssd/data --max-symbols 1`

## 트러블슈팅
- `data/state/collector.lock`이 남아 실행이 막히는 경우:
  - 실행 중인 수집 프로세스가 없는지 확인 후 lock 파일을 정리합니다.
- API 키 오류:
  - `/etc/data-scrapping.env`의 키 이름/값을 재확인하고 서비스 재실행 후 로그를 확인합니다.
- DuckDB 의존성 오류:
  - `python -m pip install -r requirements.txt --break-system-packages` 재실행
- 로그 확인:
  - `data/logs/collector.log`
  - `sudo journalctl -u data-scrapping.service -n 200 --no-pager`
