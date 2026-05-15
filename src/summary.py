"""Daily summary and Raspberry Pi healthcheck helper."""
from __future__ import annotations
import argparse, json, os
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen
from src.state import utc_today
from src.storage import ensure_data_dirs, get_db_path
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

def read_cpu_temp_c():
    p=Path('/sys/class/thermal/thermal_zone0/temp')
    if not p.exists(): return None
    try: return round(int(p.read_text(encoding='utf-8').strip())/1000,1)
    except: return None

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
    finally: con.close()
    return {"db_path": str(db), "exists": True, "tables": tables}

def build_summary(data_dir: Path)->dict[str,object]:
    ensure_data_dirs(data_dir); (data_dir/"logs"/"daily").mkdir(parents=True,exist_ok=True)
    raw_json_count=sum(1 for i in (data_dir/"raw").glob("**/*.json") if i.is_file())
    return {"generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00','Z'),"date": utc_today(),"state": read_state_summary(data_dir),"files": {"raw_json_count": raw_json_count,"db": _db_counts(data_dir)},"hardware": {"disk": read_disk_summary(data_dir),"cpu_temp_c": read_cpu_temp_c()}}

def save_summary(path:Path, summary:dict[str,object])->Path:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+'.tmp'); tmp.write_text(json.dumps(summary,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding='utf-8'); os.replace(tmp,path); return path

def format_discord_message(summary:dict[str,object])->str:
    t=summary['files']['db']['tables']; d=summary['hardware']['disk']
    return f"[data-scrapping] {summary['date']} summary\nraw_json={summary['files']['raw_json_count']} ohlcv={t['ohlcv']} indicators={t['indicators']} macro={t['macro']} news={t['news']}\ndisk_free_gb={d['free_gb']} cpu_temp_c={summary['hardware']['cpu_temp_c']}"

def send_discord(url:str,msg:str)->None:
    req=Request(
        url,
        data=json.dumps({"content":msg}).encode('utf-8'),
        headers={"Content-Type":"application/json", "User-Agent": "data-scrapping-summary/0.1"},
        method='POST',
    )
    with urlopen(req,timeout=15) as r: r.read()

def main()->int:
    a=parse_args(); s=build_summary(a.data_dir); out=a.output or a.data_dir/"logs"/"daily"/f"{s['date']}.json"; s['summary_path']=str(save_summary(out,s))
    if a.send_discord:
        u=os.environ.get(a.discord_webhook_env,"")
        if not u: raise RuntimeError(f"Missing Discord webhook environment variable: {a.discord_webhook_env}")
        send_discord(u, format_discord_message(s))
    print(json.dumps(s,ensure_ascii=False,indent=2,sort_keys=True)); return 0

if __name__=='__main__': raise SystemExit(main())
