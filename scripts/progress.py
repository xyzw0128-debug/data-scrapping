"""실시간 수집 진행도 표시 스크립트 (개선판).

사용법:
    python scripts/progress.py          # 3초 갱신 (기본)
    python scripts/progress.py -i 5    # 5초 갱신
    python scripts/progress.py --once  # 한 번만 출력
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "state" / "state.json"
PROVIDERS_PATH = ROOT / "config" / "providers.yaml"

# ── 상수 ──────────────────────────────────────────────────────────────────────
TOTAL_SYMBOLS = 4445          # 전체 심볼 수 (symbols.txt 기준 대략값)
RUN_MAX_SYMBOLS = 700         # 하루 최대 처리 심볼 수
BAR_WIDTH = 38
REFRESH_SEC = 60


# ── ANSI 컬러 ─────────────────────────────────────────────────────────────────
class C:
    RST   = "\033[0m"
    BOLD  = "\033[1m"
    DIM   = "\033[2m"
    RED   = "\033[31m"
    GRN   = "\033[32m"
    YLW   = "\033[33m"
    BLU   = "\033[34m"
    MAG   = "\033[35m"
    CYN   = "\033[36m"
    WHT   = "\033[37m"
    BRED  = "\033[91m"
    BGRN  = "\033[92m"
    BYLW  = "\033[93m"
    BBLU  = "\033[94m"
    BCYN  = "\033[96m"

def _no_color() -> bool:
    return not sys.stdout.isatty() or os.environ.get("NO_COLOR", "") != ""


def c(code: str, text: str) -> str:
    if _no_color():
        return text
    return f"{code}{text}{C.RST}"


# ── 유틸 ──────────────────────────────────────────────────────────────────────
def clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_provider_limits() -> dict[str, int]:
    """providers.yaml에서 daily_limit 값을 읽어온다."""
    limits: dict[str, int] = {}
    if not PROVIDERS_PATH.exists():
        return limits
    current: str | None = None
    for line in PROVIDERS_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" ") and stripped.endswith(":"):
            current = stripped[:-1]
            limits.setdefault(current, 800)
            continue
        if current and ":" in stripped:
            key, val = stripped.split(":", 1)
            if key.strip() == "daily_limit":
                try:
                    limits[current] = int(val.strip())
                except ValueError:
                    pass
    return limits


def progress_bar(done: int, total: int, width: int = BAR_WIDTH, color: str = C.BGRN) -> str:
    if total <= 0:
        filled = 0
    else:
        filled = min(int(width * done / total), width)
    bar = "█" * filled + c(C.DIM, "░" * (width - filled))
    pct = done / total * 100 if total else 0.0
    pct_str = c(C.BOLD, f"{pct:5.1f}%")
    return f"{c(color, bar)} {pct_str}"


def fmt_elapsed(started_at: str) -> str:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        elapsed = datetime.now(UTC) - start
        total_s = int(elapsed.total_seconds())
        m, s = divmod(total_s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    except Exception:
        return "--:--:--"


def fmt_eta(done: int, total: int, started_at: str) -> str:
    """남은 시간 추정 (started_at 기준 선형 보간)."""
    if done <= 0 or total <= 0:
        return "?"
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        elapsed_s = (datetime.now(UTC) - start).total_seconds()
        rate = done / elapsed_s  # symbols/sec
        remaining = (total - done) / rate
        m, s = divmod(int(remaining), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}h {m}m"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"
    except Exception:
        return "?"


def fmt_time(iso: str) -> str:
    """ISO 타임스탬프를 로컬 HH:MM:SS로."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M:%S")
    except Exception:
        return iso[:19] if iso else "-"


def divider(char: str = "─", width: int = 55) -> str:
    return c(C.DIM, char * width)


# ── 키풀 상태 읽기 ─────────────────────────────────────────────────────────────
def read_key_pool(state: dict, provider: str = "twelve_data") -> list[dict]:
    """state["providers"]에서 키 슬롯 정보를 읽어온다."""
    providers = state.get("providers", {})
    slots = []
    idx = 0
    while True:
        key = f"{provider}:key{idx}"
        if key not in providers:
            # 단일 키 방식 fallback
            if idx == 0 and provider in providers:
                slots.append({"index": 0, "data": providers[provider]})
            break
        slots.append({"index": idx, "data": providers[key]})
        idx += 1
    return slots


def render_key_pool(slots: list[dict], limits: dict[str, int]) -> list[str]:
    if not slots:
        return [c(C.DIM, "  키 정보 없음")]
    lines = []
    daily_limit = limits.get("twelve_data", 800)
    for slot in slots:
        d = slot["data"]
        used = int(d.get("calls_used_today", 0))
        limit = int(d.get("daily_limit", daily_limit))
        reserve = int(d.get("daily_reserve", 0))
        usable = max(0, limit - reserve)
        remaining = max(0, usable - used)
        pct = used / usable * 100 if usable else 100.0
        # 색상
        if remaining == 0:
            bar_color = C.RED
            status = c(C.RED, "소진")
        elif pct >= 80:
            bar_color = C.BYLW
            status = c(C.BYLW, "주의")
        else:
            bar_color = C.BGRN
            status = c(C.BGRN, "정상")
        bar = progress_bar(used, usable, width=20, color=bar_color)
        idx_str = c(C.BLU, f"KEY{slot['index']}")
        lines.append(f"  {idx_str} {bar}  {used}/{usable}  {status}")
    return lines


# ── 최근 실패 심볼 ─────────────────────────────────────────────────────────────
def recent_failures(state: dict, n: int = 5) -> list[str]:
    symbols = state.get("symbols", {})
    failed = []
    for sym, data in symbols.items():
        if not isinstance(data, dict):
            continue
        td = data.get("twelve_data", {})
        if isinstance(td, dict) and td.get("status") == "failed":
            err = td.get("error", "")[:40]
            updated = fmt_time(td.get("updated_at", ""))
            failed.append((updated, sym, err))
    failed.sort(reverse=True)
    return [f"  {c(C.DIM, t)}  {c(C.BRED, s):<12}  {c(C.DIM, e)}" for t, s, e in failed[:n]]


# ── 오늘 통계 ─────────────────────────────────────────────────────────────────
def today_stats(state: dict) -> tuple[int, int, int]:
    """오늘 twelve_data 기준 성공/실패/스킵 수 반환."""
    today = date.today().isoformat()
    symbols = state.get("symbols", {})
    done = failed = skipped = 0
    for data in symbols.values():
        if not isinstance(data, dict):
            continue
        td = data.get("twelve_data", {})
        if not isinstance(td, dict):
            continue
        if td.get("last_success_date") == today:
            done += 1
        elif td.get("status") == "failed":
            failed += 1
        elif td.get("status") == "skipped_rate_limit":
            skipped += 1
    return done, failed, skipped


# ── 화면 렌더링 ───────────────────────────────────────────────────────────────
def render(state: dict | None, limits: dict[str, int]) -> None:
    W = 55
    print(divider("═", W))
    print(c(C.BOLD + C.CYN, "  📊 data-scrapping 수집 모니터"))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(c(C.DIM, f"  {now_str}"))
    print(divider("═", W))

    if state is None:
        print(f"\n  {c(C.BYLW, '⚠  state.json 없음')}  수집 시작을 기다리는 중...")
        print()
        return

    current = state.get("current_run")

    # ── 수집 중 ───────────────────────────────────────────────────────────────
    if current:
        processed = int(current.get("processed", 0))
        failed    = int(current.get("failed", 0))
        skipped   = int(current.get("skipped_rate_limit", 0))
        total_done = processed + failed + skipped
        last_sym   = current.get("last_symbol") or "-"
        started_at = current.get("started_at", "")
        updated_at = fmt_time(current.get("updated_at", ""))
        elapsed    = fmt_elapsed(started_at)
        eta        = fmt_eta(processed, RUN_MAX_SYMBOLS, started_at)

        print(f"\n  상태   {c(C.BGRN, '● 수집 중')}")
        print(f"  경과   {c(C.BOLD, elapsed)}   ETA {c(C.CYN, eta)}")
        print(f"  갱신   {c(C.DIM, updated_at)}")
        print(f"  심볼   {c(C.BOLD, last_sym)}")

        print()
        print(c(C.DIM, "  ── 이번 실행 진행도 ──────────────────────────"))
        print(f"  {progress_bar(total_done, RUN_MAX_SYMBOLS)}")
        print(f"  {total_done} / {RUN_MAX_SYMBOLS}  "
              f"{c(C.BGRN, f'✓{processed}')}  "
              f"{c(C.BRED, f'✗{failed}')}  "
              f"{c(C.BYLW, f'⏭{skipped}')}")

    # ── 대기 중 ───────────────────────────────────────────────────────────────
    else:
        print(f"\n  상태   {c(C.DIM, '○ 대기 중 (다음 실행 예정)')}")

        runs = state.get("runs", [])
        last_run = runs[-1] if runs else None
        if last_run:
            s_time = fmt_time(last_run.get("started_at", ""))
            e_time = fmt_time(last_run.get("finished_at", ""))
            lp = int(last_run.get("processed", 0))
            lf = int(last_run.get("failed", 0))
            ls = int(last_run.get("skipped_rate_limit", 0))
            print()
            print(c(C.DIM, "  ── 마지막 실행 ────────────────────────────────"))
            print(f"  {s_time} → {e_time}   "
                  f"{c(C.BGRN, f'✓{lp}')}  "
                  f"{c(C.BRED, f'✗{lf}')}  "
                  f"{c(C.BYLW, f'⏭{ls}')}")

    # ── 오늘 전체 진행도 ───────────────────────────────────────────────────────
    done_today, fail_today, skip_today = today_stats(state)
    print()
    print(c(C.DIM, "  ── 오늘 전체 진행도 ──────────────────────────"))
    print(f"  {progress_bar(done_today, TOTAL_SYMBOLS, color=C.BBLU)}")
    print(f"  {done_today} / {TOTAL_SYMBOLS} 심볼 성공  "
          f"{c(C.BRED, f'✗{fail_today}')}  "
          f"{c(C.BYLW, f'⏭{skip_today}')}")

    # ── API 키 풀 상태 ─────────────────────────────────────────────────────────
    slots = read_key_pool(state)
    if slots:
        print()
        print(c(C.DIM, "  ── API 키 풀 (twelve_data) ────────────────────"))
        for line in render_key_pool(slots, limits):
            print(line)

    # ── 최근 실패 심볼 ─────────────────────────────────────────────────────────
    fails = recent_failures(state)
    if fails:
        print()
        print(c(C.DIM, "  ── 최근 실패 심볼 (최대 5개) ──────────────────"))
        for line in fails:
            print(line)

    print()


# ── 메인 ──────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="data-scrapping 수집 진행도 모니터")
    p.add_argument("-i", "--interval", type=float, default=REFRESH_SEC,
                   help=f"갱신 간격 (초, 기본 {REFRESH_SEC})")
    p.add_argument("--once", action="store_true",
                   help="한 번만 출력하고 종료")
    p.add_argument("--no-clear", action="store_true",
                   help="화면 지우기 비활성화 (로그 파이프용)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    limits = load_provider_limits()

    if args.once:
        render(load_state(), limits)
        return

    print("수집 진행도 모니터 시작... (Ctrl+C 로 종료)")
    try:
        while True:
            state = load_state()
            if not args.no_clear:
                clear()
            render(state, limits)
            W = 55
            print(divider("═", W))
            print(c(C.DIM, f"  {args.interval:.0f}초마다 갱신 | Ctrl+C 종료"))
            print(divider("═", W))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n종료.")


if __name__ == "__main__":
    main()
