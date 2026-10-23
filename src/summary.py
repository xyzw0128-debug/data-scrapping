"""Daily summary and host healthcheck helper."""
from __future__ import annotations
import argparse, json, os
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen
from src.atomic_io import atomic_write_json
from src.env import load_project_env
from src.lock import LockHeldError, lock_held_exit, pipeline_lock
from src.state import utc_today
from src.storage import ensure_data_dirs, get_db_path, get_snapshot_path
ROOT = Path(__file__).resolve().parents[1]

def parse_args() -> argparse.Namespace:
    p=argparse.ArgumentParser(description="Generate a daily collector summary and optional Discord notification.")
    p.add_argument("--data-dir", type=Path, default=ROOT / "data")
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--discord-webhook-env", default="DISCORD_WEBHOOK_URL")
    p.add_argument("--send-discord", action="store_true")
    return p.parse_args()

def read_state_summary(data_dir: Path) -> dict[str, object]:
    path=data_dir/"state"/"state.json"
    if not path.exists(): return {"exists": False}
    state=json.loads(path.read_text(encoding='utf-8')); providers=state.get('providers',{}) if isinstance(state,dict) else {}; runs=state.get('runs',[]) if isinstance(state,dict) else []
    return {"exists": True,"providers": providers,"run_count_kept": len(runs) if isinstance(runs,list) else 0,"last_run": runs[-1] if isinstance(runs,list) and runs else None}

CPU_SENSOR_NAMES = ("k10temp", "coretemp", "zenpower", "cpu_thermal")

def read_cpu_temp_c(hwmon_root: Path = Path('/sys/class/hwmon'), thermal_zone: Path = Path('/sys/class/thermal/thermal_zone0/temp')):
    """CPU temperature: the hwmon CPU sensor (k10temp on this Ryzen host), else thermal_zone0."""
    candidates = []
    for name_file in sorted(hwmon_root.glob('hwmon*/name')):
        try: name = name_file.read_text(encoding='utf-8').strip()
        except OSError: continue
        if name in CPU_SENSOR_NAMES: candidates.append(name_file.parent / 'temp1_input')
    candidates.append(thermal_zone)
    for p in candidates:
        try: return round(int(p.read_text(encoding='utf-8').strip())/1000,1)
        except (OSError, ValueError): continue
    return None

def read_disk_summary(data_dir: Path)->dict[str,object]:
    u=os.statvfs(data_dir); total=u.f_frsize*u.f_blocks; free=u.f_frsize*u.f_bavail; used=total-free
    return {"path": str(data_dir),"total_gb": round(total/1024**3,2),"used_gb": round(used/1024**3,2),"free_gb": round(free/1024**3,2),"used_percent": round((used/total)*100,2) if total else None}

def _db_counts(data_dir: Path)->dict[str,object]:
    db=get_db_path(data_dir)
    if not db.exists(): return {"db_path": str(db), "exists": False, "tables": {"ohlcv":0,"indicators":0,"macro":0,"news":0}}
    import duckdb
    con=duckdb.connect(str(db), read_only=True)
    try:
        tables={k:con.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0] for k in ["ohlcv","indicators","macro","news"]}
        has_runs=con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'collection_runs'").fetchone()[0]
        last_run=None
        if has_runs:
            row=con.execute("SELECT run_id, phase, CAST(session_date AS VARCHAR), status, core_expected, core_ok, fred_expected, fred_ok, finished_at FROM collection_runs ORDER BY finished_at DESC LIMIT 1").fetchone()
            if row: last_run=dict(zip(["run_id","phase","session_date","status","core_expected","core_ok","fred_expected","fred_ok","finished_at"], row))
    finally: con.close()
    return {"db_path": str(db), "exists": True, "tables": tables, "last_collection_run": last_run}

def _db_file_sizes(data_dir: Path)->dict[str,object]:
    """Byte sizes of market.db and market_snapshot.db (None when missing); daily full replacement grows them."""
    paths={"market_db_bytes": get_db_path(data_dir), "snapshot_bytes": get_snapshot_path(data_dir)}
    return {k:(p.stat().st_size if p.exists() else None) for k,p in paths.items()}

def read_validation(data_dir: Path, run_id: str | None)->dict[str,object] | None:
    """This run's src.validate_duckdb result; None when the report is missing or from another run (e.g. skipped in dry-run)."""
    path=data_dir/"logs"/"validation"/"duckdb_latest.json"
    if not run_id or not path.exists(): return None
    try: report=json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError): return None
    if not isinstance(report,dict) or report.get('run_id')!=run_id: return None
    warnings={w['type']:w['count'] for w in report.get('warnings') or [] if isinstance(w,dict) and 'type' in w}
    return {"status": report.get('status'),"issue_count": report.get('issue_count'),"warning_count": report.get('warning_count'),"warnings": warnings,"generated_at": report.get('generated_at')}

def build_summary(data_dir: Path, run_id: str | None = None)->dict[str,object]:
    ensure_data_dirs(data_dir); (data_dir/"logs"/"daily").mkdir(parents=True,exist_ok=True)
    raw_json_count=sum(1 for i in (data_dir/"raw").glob("**/*.json*") if i.is_file())
    return {"generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00','Z'),"date": utc_today(),"state": read_state_summary(data_dir),"files": {"raw_json_count": raw_json_count,"db": _db_counts(data_dir),"db_files": _db_file_sizes(data_dir)},"validation": read_validation(data_dir, run_id),"hardware": {"disk": read_disk_summary(data_dir),"cpu_temp_c": read_cpu_temp_c()}}

def save_summary(path:Path, summary:dict[str,object])->Path:
    return atomic_write_json(path, summary)

def format_discord_message(summary:dict[str,object])->str:
    t=summary['files']['db']['tables']; d=summary['hardware']['disk']; r=summary['files']['db'].get('last_collection_run') or {}
    run=f"\nrun={r.get('status')} session={r.get('session_date')} core={r.get('core_ok')}/{r.get('core_expected')} fred={r.get('fred_ok')}/{r.get('fred_expected')}" if r else ""
    sizes=summary['files'].get('db_files') or {}; mb=lambda b: "-" if b is None else round(b/1e6)
    v=summary.get('validation'); counts=",".join(f"{k}:{n}" for k,n in sorted(((v or {}).get('warnings') or {}).items()))
    check=f"\nvalidation={v.get('status')} warnings={counts or 0}" if v else "\nvalidation=N/A"
    return f"[data-scrapping] {summary['date']} summary\nraw_json={summary['files']['raw_json_count']} ohlcv={t['ohlcv']} indicators={t['indicators']} macro={t['macro']} news={t['news']}{run}{check}\ndb_mb={mb(sizes.get('market_db_bytes'))} snapshot_mb={mb(sizes.get('snapshot_bytes'))} disk_free_gb={d['free_gb']} cpu_temp_c={summary['hardware']['cpu_temp_c']}"

def send_discord(url:str,msg:str)->None:
    req=Request(
        url,
        data=json.dumps({"content":msg}).encode('utf-8'),
        headers={"Content-Type":"application/json", "User-Agent": "data-scrapping-summary/0.1"},
        method='POST',
    )
    with urlopen(req,timeout=15) as r: r.read()

def main()->int:
    load_project_env(ROOT)
    a=parse_args()
    try:
        with pipeline_lock(a.data_dir): s=build_summary(a.data_dir, os.environ.get("DATA_SCRAPPING_RUN_ID"))
    except LockHeldError as exc:
        return lock_held_exit(exc)
    # Saved before the optional Discord message so a webhook failure never loses the summary file.
    out=a.output or a.data_dir/"logs"/"daily"/f"{s['date']}.json"; s['summary_path']=str(save_summary(out,s))
    if a.send_discord:
        u=os.environ.get(a.discord_webhook_env,"")
        if not u: raise RuntimeError(f"Missing Discord webhook environment variable: {a.discord_webhook_env}")
        send_discord(u, format_discord_message(s))
    print(json.dumps(s,ensure_ascii=False,indent=2,sort_keys=True)); return 0

if __name__=='__main__': raise SystemExit(main())
