"""Daily summary and Raspberry Pi healthcheck helper."""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from src.state import utc_today
from src.storage import ensure_data_dirs


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a daily collector summary and optional Discord notification.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=None, help="Summary JSON path; defaults to data/logs/daily/<date>.json")
    parser.add_argument("--discord-webhook-env", default="DISCORD_WEBHOOK_URL")
    parser.add_argument("--send-discord", action="store_true", help="Send summary to Discord webhook")
    return parser.parse_args()


def _count_files(path: Path, pattern: str = "*") -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.glob(pattern) if item.is_file())


def _read_csv_stats(path: Path) -> dict[str, object]:
    rows = 0
    first_date = ""
    last_date = ""
    if not path.exists():
        return {"rows": 0, "first_date": "", "last_date": ""}

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows += 1
            date_value = str(row.get("date", ""))
            if date_value and not first_date:
                first_date = date_value
            if date_value:
                last_date = date_value
    return {"rows": rows, "first_date": first_date, "last_date": last_date}


def summarize_csv_dir(path: Path, prefix_to_strip: str = "") -> dict[str, object]:
    """Summarize symbol/series-level CSV files in a directory."""
    files = sorted(path.glob("*.csv")) if path.exists() else []
    items: dict[str, dict[str, object]] = {}
    for csv_path in files:
        name = csv_path.stem
        if prefix_to_strip and name.startswith(prefix_to_strip):
            name = name[len(prefix_to_strip) :]
        items[name] = _read_csv_stats(csv_path)
    return {"file_count": len(files), "items": items}


def read_state_summary(data_dir: Path) -> dict[str, object]:
    """Read minimal state.json summary without failing if it is missing."""
    path = data_dir / "state" / "state.json"
    if not path.exists():
        return {"exists": False}
    state = json.loads(path.read_text(encoding="utf-8"))
    providers = state.get("providers", {}) if isinstance(state, dict) else {}
    runs = state.get("runs", []) if isinstance(state, dict) else []
    return {
        "exists": True,
        "providers": providers,
        "run_count_kept": len(runs) if isinstance(runs, list) else 0,
        "last_run": runs[-1] if isinstance(runs, list) and runs else None,
    }


def read_cpu_temp_c() -> float | None:
    """Read Raspberry Pi CPU temperature when available."""
    temp_path = Path("/sys/class/thermal/thermal_zone0/temp")
    if not temp_path.exists():
        return None
    try:
        return round(int(temp_path.read_text(encoding="utf-8").strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def read_disk_summary(data_dir: Path) -> dict[str, object]:
    """Return disk usage for the filesystem containing data_dir."""
    usage = os.statvfs(data_dir)
    total = usage.f_frsize * usage.f_blocks
    free = usage.f_frsize * usage.f_bavail
    used = total - free
    return {
        "path": str(data_dir),
        "total_gb": round(total / 1024**3, 2),
        "used_gb": round(used / 1024**3, 2),
        "free_gb": round(free / 1024**3, 2),
        "used_percent": round((used / total) * 100, 2) if total else None,
    }


def build_summary(data_dir: Path) -> dict[str, object]:
    """Build a daily operational summary for collected data and host health."""
    ensure_data_dirs(data_dir)
    (data_dir / "logs" / "daily").mkdir(parents=True, exist_ok=True)

    return {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "date": utc_today(),
        "state": read_state_summary(data_dir),
        "files": {
            "raw_json_count": _count_files(data_dir / "raw", "**/*.json"),
            "ohlcv": summarize_csv_dir(data_dir / "ohlcv"),
            "indicators": summarize_csv_dir(data_dir / "indicators"),
            "macro": summarize_csv_dir(data_dir / "macro", "FRED_"),
            "news": summarize_csv_dir(data_dir / "news", "FINNHUB_"),
        },
        "hardware": {
            "disk": read_disk_summary(data_dir),
            "cpu_temp_c": read_cpu_temp_c(),
        },
    }


def save_summary(path: Path, summary: dict[str, object]) -> Path:
    """Write summary JSON atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path


def format_discord_message(summary: dict[str, object]) -> str:
    """Format a compact Discord message."""
    files = summary["files"]
    hardware = summary["hardware"]
    disk = hardware["disk"]
    return (
        f"[data-scrapping] {summary['date']} summary\n"
        f"raw_json={files['raw_json_count']} "
        f"ohlcv={files['ohlcv']['file_count']} "
        f"indicators={files['indicators']['file_count']} "
        f"macro={files['macro']['file_count']} "
        f"news={files['news']['file_count']}\n"
        f"disk_free_gb={disk['free_gb']} cpu_temp_c={hardware['cpu_temp_c']}"
    )


def send_discord(webhook_url: str, message: str) -> None:
    """Send a plain Discord webhook message."""
    payload = json.dumps({"content": message}).encode("utf-8")
    request = Request(webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=15) as response:
        response.read()


def main() -> int:
    args = parse_args()
    summary = build_summary(args.data_dir)
    output = args.output or args.data_dir / "logs" / "daily" / f"{summary['date']}.json"
    summary_path = save_summary(output, summary)
    summary["summary_path"] = str(summary_path)

    if args.send_discord:
        webhook_url = os.environ.get(args.discord_webhook_env, "")
        if not webhook_url:
            raise RuntimeError(f"Missing Discord webhook environment variable: {args.discord_webhook_env}")
        send_discord(webhook_url, format_discord_message(summary))

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
