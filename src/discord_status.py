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

from src.atomic_io import atomic_write_json
from src.env import load_project_env
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
    atomic_write_json(path, payload)


PHASE_LABELS = {"core": "핵심", "rotation": "순환"}


def _read_progress(data_dir: Path) -> tuple[str, int, int] | None:
    """(phase, handled, planned) of the Twelve Data phase from state.json, None when unknown.

    A running phase is ``current_run`` (saved every few symbols); after it ends, the latest
    ``runs`` entry finished today. Handled counts done and failed symbols. Core and rotation are
    separate invocations, so each has its own planned count (e.g. 526 core, up to 700 rotation).
    """
    path = data_dir / "state" / "state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    run = state.get("current_run")
    if isinstance(run, dict):
        phase, planned = run.get("phase"), run.get("planned")
    else:
        runs = state.get("runs")
        run = runs[-1] if isinstance(runs, list) and runs else None
        if not isinstance(run, dict) or not str(run.get("finished_at", "")).startswith(utc_today()):
            return None
        phase, planned = run.get("current_phase"), run.get("current_planned")
    if not isinstance(planned, int) or planned < 0:
        return None
    handled = int(run.get("processed") or 0) + int(run.get("failed") or 0)
    return str(phase or ""), min(handled, planned), planned


def _format_message(s: dict) -> str:
    phase = PHASE_LABELS.get(s.get("progress_phase", ""), s.get("progress_phase", ""))
    progress = f"{s['progress_done']}/{s['progress_total']}" if "progress_total" in s else "-"
    return (
        "📊 데이터 수집 상태\n\n"
        f"전원 상태: {s.get('power_status', '상태 미확인')}\n\n"
        f"상태: {s.get('status', '대기중')}\n"
        f"진행률: {(phase + ' ') if phase else ''}{progress}\n\n"
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
    p.add_argument("--refresh-progress", action="store_true", help="Read the Twelve Data phase progress from state.json")
    return p.parse_args()


def main() -> int:
    load_project_env(ROOT)
    args = parse_args()
    webhook = os.environ.get(args.discord_webhook_env, "")
    if not webhook:
        return 0

    state = _load(args.state_path)
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
        for key in ("progress_phase", "progress_done", "progress_total"):
            state.pop(key, None)
    if args.backup_start:
        state["backup_started_at"] = _now_hms()
    if args.backup_finish:
        state["backup_finished_at"] = _now_hms()
    if args.refresh_progress:
        progress = _read_progress(args.data_dir)
        if progress is not None:  # otherwise keep the last known progress
            state["progress_phase"], state["progress_done"], state["progress_total"] = progress

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
