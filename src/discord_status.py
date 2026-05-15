"""Webhook-based live Discord status updater for run_daily."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.state import utc_today

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "state" / "discord_status.json"


def _now_hms() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _count_progress(data_dir: Path, total: int) -> int:
    path = data_dir / "state" / "state.json"
    if not path.exists():
        return 0
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    symbols = state.get("symbols", {}) if isinstance(state, dict) else {}
    today = utc_today()
    done = 0
    for item in symbols.values() if isinstance(symbols, dict) else []:
        if isinstance(item, dict) and item.get("last_success_date") == today:
            done += 1
    return min(done, total)


def _format_message(s: dict) -> str:
    return (
        "📊 데이터 수집 상태\n\n"
        f"전원 상태: {s.get('power_status', '상태 미확인')}\n\n"
        f"상태: {s.get('status', '대기중')}\n"
        f"진행률: {s.get('progress_done', 0)}/{s.get('progress_total', 700)}\n\n"
        f"수집 시작: {s.get('collect_started_at', '대기중')}\n"
        f"최근 업데이트: {s.get('last_updated_at', _now_hms())}\n\n"
        "현재 처리:\n"
        f"- {s.get('current_symbol', '없음')}\n\n"
        "백업:\n"
        f"- 시작: {s.get('backup_started_at', '대기중')}\n"
        f"- 완료: {s.get('backup_finished_at', '대기중')}\n\n"
        "에러:\n"
        f"- {s.get('last_error', '없음')}"
    )


def _post_or_patch(webhook_url: str, state: dict, create: bool) -> dict:
    body = json.dumps({"content": _format_message(state)}).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "data-scrapping-summary/0.1"}
    if create:
        url = webhook_url + ("&" if "?" in webhook_url else "?") + urlencode({"wait": "true"})
        req = Request(url, data=body, headers=headers, method="POST")
    else:
        message_id = state.get("message_id", "")
        url = webhook_url.rstrip("/") + f"/messages/{message_id}"
        req = Request(url, data=body, headers=headers, method="PATCH")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8")) if create else {}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live Discord status helper")
    p.add_argument("--data-dir", type=Path, default=ROOT / "data")
    p.add_argument("--state-path", type=Path, default=STATE_PATH)
    p.add_argument("--discord-webhook-env", default="DISCORD_WEBHOOK_URL")
    p.add_argument("--status", default=None)
    p.add_argument("--current-symbol", default=None)
    p.add_argument("--backup-start", action="store_true")
    p.add_argument("--backup-finish", action="store_true")
    p.add_argument("--error", default=None)
    p.add_argument("--set-start", action="store_true")
    p.add_argument("--progress-total", type=int, default=700)
    p.add_argument("--refresh-progress", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    webhook = os.environ.get(args.discord_webhook_env, "")
    if not webhook:
        return 0

    state = _load(args.state_path)
    state.setdefault("progress_total", args.progress_total)
    state.setdefault("progress_done", 0)
    state["power_status"] = "실행 중"
    state["last_updated_at"] = _now_hms()

    if args.status is not None:
        state["status"] = args.status
    if args.current_symbol is not None:
        state["current_symbol"] = args.current_symbol
    if args.error is not None:
        state["last_error"] = args.error
    if args.set_start:
        state["collect_started_at"] = _now_hms()
        state["progress_done"] = 0
    if args.backup_start:
        state["backup_started_at"] = _now_hms()
    if args.backup_finish:
        state["backup_finished_at"] = _now_hms()
    if args.refresh_progress:
        state["progress_done"] = _count_progress(args.data_dir, state.get("progress_total", args.progress_total))

    create = not state.get("message_id")
    try:
        res = _post_or_patch(webhook, state, create=create)
    except HTTPError as exc:
        state["last_error"] = f"discord_http_{exc.code}"
        _save(args.state_path, state)
        return 1

    if create:
        state["message_id"] = str(res.get("id", ""))
    _save(args.state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
