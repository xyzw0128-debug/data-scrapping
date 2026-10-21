"""실시간 수집 진행도 표시 스크립트 (개선판).

사용법:
    python scripts/progress.py          # 60초 갱신 (기본)
    python scripts/progress.py -i 5    # 5초 갱신
    python scripts/progress.py --once  # 한 번만 출력
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.env import load_project_env  # noqa: E402
from src.finnhub_news import universe_news_symbols  # noqa: E402
from src.lock import PIPELINE_LOCK_NAME, pipeline_running  # noqa: E402
from src.market_calendar import expected_session  # noqa: E402
from src.universe import load_universe  # noqa: E402

DATA_DIR       = ROOT / "data"
STATE_PATH     = DATA_DIR / "state" / "state.json"
PROVIDERS_PATH = ROOT / "config" / "providers.yaml"
PROC_ROOT      = Path("/proc")

# ── 상수 ──────────────────────────────────────────────────────────────────────
BAR_WIDTH       = 38
REFRESH_SEC     = 60

# run_daily.sh 단계 모듈 → 표시 이름 (src.discord_status는 단계가 아니라 제외)
STAGE_NAMES = {
    "src.universe":        "종목 목록 갱신",
    "src.main":            "Twelve Data",
    "src.fred":            "FRED",
    "src.snapshot":        "스냅숏",
    "src.alpha_vantage":   "Alpha Vantage",
    "src.indicators":      "지표 계산",
    "src.finnhub_news":    "뉴스(Finnhub)",
    "src.summary":         "요약",
    "src.validate_duckdb": "DuckDB 검증",
}
NEWS_STAGE = STAGE_NAMES["src.finnhub_news"]


# ── ANSI 컬러 ─────────────────────────────────────────────────────────────────
class C:
    RST  = "\033[0m"
    BOLD = "\033[1m"
    DIM  = "\033[2m"
    RED  = "\033[31m"
    BLU  = "\033[34m"
    BRED = "\033[91m"
    BGRN = "\033[92m"
    BYLW = "\033[93m"
    BBLU = "\033[94m"
    BCYN = "\033[96m"


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


def universe_counts() -> tuple[list[str], int]:
    """(핵심 종목 목록, 핵심+순환 활성 종목 수). 목록 파일을 못 읽으면 ([], 0)."""
    try:
        universe = load_universe(DATA_DIR)
    except Exception:
        return [], 0
    core = universe.core_symbols()
    return core, len(core) + len(universe.rotation_symbols())


def parse_iso(value: object) -> datetime | None:
    """ISO 시각을 시간대 있는 datetime으로(시간대가 없으면 UTC). 못 읽으면 None."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def load_provider_limits() -> dict[str, int]:
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
    if done <= 0 or total <= 0:
        return "?"
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        elapsed_s = (datetime.now(UTC) - start).total_seconds()
        rate = done / elapsed_s
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
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M:%S")
    except Exception:
        return iso[:19] if iso else "-"


def divider(char: str = "─", width: int = 55) -> str:
    return c(C.DIM, char * width)


# ── 키풀 상태 읽기 ─────────────────────────────────────────────────────────────
def read_key_pool(state: dict, provider: str = "twelve_data") -> list[dict]:
    providers = state.get("providers", {})
    slots = []
    idx = 0
    while True:
        key = f"{provider}:key{idx}"
        if key not in providers:
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

    # 현재 사용 중인 키 찾기 (잔량 있는 첫 번째 키)
    active_index = None
    for slot in slots:
        d = slot["data"]
        used = int(d.get("calls_used_today", 0))
        limit = int(d.get("daily_limit", daily_limit))
        reserve = int(d.get("daily_reserve", 0))
        usable = max(0, limit - reserve)
        remaining = max(0, usable - used)
        if remaining > 0:
            active_index = slot["index"]
            break

    for slot in slots:
        d = slot["data"]
        used = int(d.get("calls_used_today", 0))
        limit = int(d.get("daily_limit", daily_limit))
        reserve = int(d.get("daily_reserve", 0))
        usable = max(0, limit - reserve)
        remaining = max(0, usable - used)
        pct = used / usable * 100 if usable else 100.0

        if remaining == 0:
            bar_color = C.BRED
            status = c(C.BRED, "소진")
        elif pct >= 80:
            bar_color = C.BYLW
            status = c(C.BYLW, "주의")
        else:
            bar_color = C.BGRN
            status = c(C.BGRN, "정상")

        bar = progress_bar(used, usable, width=20, color=bar_color)
        idx_str = c(C.BLU, f"KEY{slot['index']}")
        active_marker = c(C.BGRN, " ◀ 사용중") if slot["index"] == active_index else ""
        lines.append(f"  {idx_str} {bar}  {used}/{usable}  {status}{active_marker}")
    return lines


# ── 파이프라인 잠금과 실행 중인 단계 ───────────────────────────────────────────
def lock_info(lock_path: Path) -> dict:
    """잠금 파일의 pid·holder와 잠금을 잡은 시각(파일 수정 시각).

    run_daily.sh와 단독 실행 CLI는 잠금을 잡을 때 내용을 새로 쓰고, run_daily.sh가 띄운
    단계는 잠금 파일을 쓰지 않으므로 수정 시각이 곧 이번 실행의 시작 시각이다.
    """
    try:
        text = lock_path.read_text(encoding="utf-8")
        since = datetime.fromtimestamp(lock_path.stat().st_mtime, UTC).replace(microsecond=0)
    except OSError:
        return {}
    info: dict = {"since": since}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key == "pid" and value.strip().isdigit():
            info["pid"] = int(value)
        elif key == "holder":
            info["holder"] = value.strip()
    return info


def _cmdline(proc_root: Path, pid: int) -> list[str]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _children(proc_root: Path, pid: int) -> list[int]:
    children = []
    for stat in proc_root.glob("[0-9]*/stat"):
        try:
            # "pid (comm) state ppid ..." — comm에 공백·괄호가 있을 수 있어 마지막 ')' 뒤를 쓴다.
            fields = stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 1 and fields[1] == str(pid):
            children.append(int(stat.parent.name))
    return sorted(children)


def stage_of(argv: list[str]) -> str | None:
    """명령줄의 단계 이름. `python -m src.snapshot --phase core` → "스냅숏 (core)"."""
    if any(Path(arg).name == "rclone_backup.sh" for arg in argv):
        return "백업"
    if "-m" not in argv:
        return None
    index = argv.index("-m")
    name = STAGE_NAMES.get(argv[index + 1] if index + 1 < len(argv) else "")
    if name and "--phase" in argv:
        phase_index = argv.index("--phase")
        if phase_index + 1 < len(argv):
            name = f"{name} ({argv[phase_index + 1]})"
    return name


def running_stage(pid: int | None, proc_root: Path = PROC_ROOT) -> str | None:
    """잠금 보유 프로세스(단독 실행 CLI) 또는 그 자식(run_daily.sh가 띄운 단계)의 단계 이름."""
    if pid is None:
        return None
    for candidate in [pid, *_children(proc_root, pid)]:
        name = stage_of(_cmdline(proc_root, candidate))
        if name:
            return name
    return None


# ── 뉴스 진행도 ───────────────────────────────────────────────────────────────
def news_progress(since: datetime, path: Path) -> tuple[int, int, datetime | None]:
    """since 이후 뉴스 수집을 시도한 종목 수, 그중 실패 수, 가장 이른 시도 시각.

    finnhub_news.json은 10종목마다 저장되므로 화면 숫자는 최대 9종목 늦을 수 있다.
    """
    try:
        symbols = json.loads(path.read_text(encoding="utf-8")).get("symbols", {})
    except (OSError, ValueError, AttributeError):
        return 0, 0, None
    attempted = failed = 0
    first: datetime | None = None
    for entry in symbols.values() if isinstance(symbols, dict) else []:
        if not isinstance(entry, dict):
            continue
        stamp = parse_iso(entry.get("last_attempt_at"))
        if stamp is None or stamp < since:
            continue
        attempted += 1
        failed += entry.get("status") == "failed"
        first = stamp if first is None or stamp < first else first
    return attempted, failed, first


def news_planned() -> int:
    """뉴스 단계 대상 종목 수(src.finnhub_news 기본 선택, FINNHUB_LIMIT 반영). 못 세면 0."""
    try:
        total = len(universe_news_symbols(load_universe(DATA_DIR), {}, {}, None))
    except Exception:
        return 0
    limit = os.environ.get("FINNHUB_LIMIT", "").strip()
    return min(total, int(limit)) if limit.isdigit() else total


def render_pipeline_stage() -> None:
    """잠금은 잡혀 있는데 current_run이 없을 때: Twelve Data 외 단계(FRED·스냅숏·지표·뉴스 등)."""
    info = lock_info(DATA_DIR / "state" / PIPELINE_LOCK_NAME)
    since = info.get("since")
    stage = running_stage(info.get("pid")) or "Twelve Data 외 단계"
    print(f"\n  상태   {c(C.BGRN, '● 실행 중')}   단계 {c(C.BOLD, stage)}")
    if since is not None:
        print(f"  경과   {c(C.BOLD, fmt_elapsed(since.isoformat()))}   시작 {c(C.DIM, fmt_time(since.isoformat()))}")
    if info.get("pid"):
        holder = f"{info.get('holder') or '-'} (pid {info['pid']})"
        print(f"  잠금   {c(C.DIM, holder)}")
    if since is None:
        return
    attempted, failed, first = news_progress(since, DATA_DIR / "state" / "finnhub_news.json")
    if not attempted and stage != NEWS_STAGE:
        return
    planned = max(news_planned(), attempted, 1)
    if attempted >= planned:
        eta = "완료"
    elif first is not None:
        eta = fmt_eta(attempted, planned, first.isoformat())
    else:
        eta = "?"
    print()
    print(c(C.DIM, "  ── 뉴스 진행도 (이번 실행) ────────────────────"))
    print(f"  {progress_bar(attempted, planned, color=C.BYLW)}")
    print(f"  {attempted} / {planned} 종목 시도  {c(C.BRED, f'✗{failed}')}   ETA {c(C.BCYN, eta)}")


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
    # state.json의 날짜는 UTC 기준이다(한국 시간 00:00~09:00에도 맞게 센다).
    today = datetime.now(UTC).date().isoformat()
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
    print(c(C.BOLD + C.BCYN, "  📊 data-scrapping 수집 모니터"))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(c(C.DIM, f"  {now_str}"))
    print(divider("═", W))

    if state is None:
        print(f"\n  {c(C.BYLW, '⚠  state.json 없음')}  수집 시작을 기다리는 중...")
        print()
        return

    current = state.get("current_run")
    lock_exists = pipeline_running(DATA_DIR)
    core, total_symbols = universe_counts()

    # ── 수집 중 (current_run + 파이프라인 잠금 둘 다 있음) ─────────────────────
    if current and lock_exists:
        processed  = int(current.get("processed", 0))
        failed     = int(current.get("failed", 0))
        skipped    = int(current.get("skipped_rate_limit", 0))
        total_done = processed + failed + skipped
        last_sym   = current.get("last_symbol") or "-"
        started_at = current.get("started_at", "")
        updated_at = fmt_time(current.get("updated_at", ""))
        elapsed    = fmt_elapsed(started_at)
        planned    = int(current.get("planned") or 0) or max(total_done, 1)
        phase      = {"core": "핵심 종목", "rotation": "순환 종목"}.get(current.get("phase"), current.get("phase") or "-")
        eta        = fmt_eta(total_done, planned, started_at)

        print(f"\n  상태   {c(C.BGRN, '● 수집 중')}   단계 {c(C.BOLD, phase)}")
        print(f"  경과   {c(C.BOLD, elapsed)}   ETA {c(C.BCYN, eta)}")
        print(f"  갱신   {c(C.DIM, updated_at)}")
        print(f"  심볼   {c(C.BOLD, last_sym)}")
        print()
        print(c(C.DIM, "  ── 이번 실행 진행도 ──────────────────────────"))
        print(f"  {progress_bar(total_done, planned)}")
        print(f"  {total_done} / {planned}  "
              f"{c(C.BGRN, f'✓{processed}')}  "
              f"{c(C.BRED, f'✗{failed}')}  "
              f"{c(C.BYLW, f'⏭{skipped}')}")

    # ── 실행 중 (잠금은 있는데 current_run 없음: Twelve Data 외 단계) ───────────
    elif lock_exists:
        render_pipeline_stage()

    # ── 비정상 종료 (lock 없는데 current_run 남아있음) ────────────────────────
    elif current:
        updated_at = fmt_time(current.get("updated_at", ""))
        print(f"\n  상태   {c(C.BYLW, '⚠  비정상 종료 감지')}")
        print(f"  {c(C.DIM, '└ 파이프라인 잠금 없음 — 프로세스가 중간에 종료된 것 같습니다')}")
        print(f"  마지막 갱신   {c(C.DIM, updated_at)}")
        print()
        print(c(C.DIM, "  다음 실행이 이어서 처리합니다(state.json 기준)."))

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
    if core:
        session = expected_session(datetime.now(UTC)).isoformat()
        symbols = state.get("symbols", {})
        core_ok = sum(1 for s in core if (symbols.get(s, {}).get("twelve_data") or {}).get("core_session") == session)
        print()
        print(c(C.DIM, f"  ── 핵심 종목 (기준 거래일 {session}) ──────────────"))
        print(f"  {progress_bar(core_ok, len(core), color=C.BCYN)}")
        print(f"  {core_ok} / {len(core)} 종목 완료")

    done_today, fail_today, skip_today = today_stats(state)
    print()
    print(c(C.DIM, "  ── 오늘(UTC) 전체 진행도 ─────────────────────"))
    print(f"  {progress_bar(done_today, total_symbols, color=C.BBLU)}")
    print(f"  {done_today} / {total_symbols} 심볼 성공  "
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
    load_project_env(ROOT)  # FINNHUB_LIMIT 같은 실행 설정을 run_daily.sh와 같게 읽는다
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
