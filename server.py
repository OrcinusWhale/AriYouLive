#!/usr/bin/env python3
"""AriYouLive Edge Monitor: local, persistent ICMP monitor and SQLite API."""
from __future__ import annotations
import base64, json, os, platform, re, shutil, sqlite3, subprocess, threading, time, uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
UI_ROOT = Path(os.environ.get("ARIYOULIVE_UI_DIR", str(ROOT)))
DB_PATH = Path(os.environ.get("ARIYOULIVE_DB", str(ROOT / "ariyou_live.db")))
AUDIO_DIR = Path(os.environ.get("ARIYOULIVE_AUDIO_DIR", str(Path.home() / ".ariyou_live" / "audio")))
BUNDLED_AUDIO_DIR = Path(os.environ.get("ARIYOULIVE_BUNDLED_AUDIO_DIR", str(ROOT / "assets" / "sounds")))
HOST, PORT = os.environ.get("ARIYOULIVE_HOST", "127.0.0.1"), int(os.environ.get("ARIYOULIVE_PORT", "8765"))
DEFAULTS = {"ping_every": 5, "timeout_ms": 1200, "fail_threshold": 3, "history_days": 30, "toast_seconds": 8, "audio_enabled": True, "global_sound_up": "f3989dbd37b44b03a191a4b98ab4e69c", "global_sound_down": "51d5992bbc014d77a440c362db83cf75", "sound_cooldown_seconds": 10, "sound_down_cooldown_seconds": 10, "sound_up_cooldown_seconds": 10, "sound_down_enabled": True, "sound_up_enabled": True}
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,sort_order INTEGER NOT NULL DEFAULT 0,parent_id INTEGER REFERENCES groups(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS group_mutes(group_id INTEGER PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE,muted_until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS devices(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,alias TEXT DEFAULT '',note TEXT NOT NULL DEFAULT '',group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,ips TEXT NOT NULL,settings TEXT NOT NULL DEFAULT '{}',muted_until REAL NOT NULL DEFAULT 0,last_state TEXT NOT NULL DEFAULT 'unknown',last_change REAL NOT NULL DEFAULT 0,fail_count INTEGER NOT NULL DEFAULT 0,flap_since REAL NOT NULL DEFAULT 0,sort_order INTEGER NOT NULL DEFAULT 0,width INTEGER,height INTEGER);
CREATE TABLE IF NOT EXISTS ip_status(device_id INTEGER NOT NULL,ip TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'unknown',fail_count INTEGER NOT NULL DEFAULT 0,last_ms REAL,last_seen REAL NOT NULL DEFAULT 0,PRIMARY KEY(device_id,ip),FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS buckets(device_id INTEGER NOT NULL,ip TEXT NOT NULL,bucket_start INTEGER NOT NULL,total_sent INTEGER NOT NULL DEFAULT 0,total_received INTEGER NOT NULL DEFAULT 0,total_ms REAL NOT NULL DEFAULT 0,min_ms REAL,max_ms REAL,PRIMARY KEY(device_id,ip,bucket_start),FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,device_id INTEGER NOT NULL,ip TEXT NOT NULL,state TEXT NOT NULL,message TEXT NOT NULL,created_at REAL NOT NULL,acknowledged INTEGER NOT NULL DEFAULT 0,muted_at_creation INTEGER NOT NULL DEFAULT 0,FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_alerts_device_created ON alerts(device_id,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_ack_created ON alerts(acknowledged,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_muted_created ON alerts(muted_at_creation,created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY AUTOINCREMENT,device_id INTEGER NOT NULL,ip TEXT NOT NULL,at REAL NOT NULL,ok INTEGER NOT NULL,ms REAL,FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_samples_device_at ON samples(device_id,at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_samples_at ON samples(at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_buckets_device_start ON buckets(device_id,bucket_start DESC);
CREATE INDEX IF NOT EXISTS idx_groups_parent_sort ON groups(parent_id,sort_order,id);
CREATE INDEX IF NOT EXISTS idx_devices_group_sort ON devices(group_id,sort_order,id);
CREATE INDEX IF NOT EXISTS idx_devices_sort ON devices(sort_order,id);
CREATE INDEX IF NOT EXISTS idx_alerts_device_ip_created ON alerts(device_id,ip,created_at,id,state);
CREATE INDEX IF NOT EXISTS idx_alerts_ack_muted_created ON alerts(acknowledged,muted_at_creation,created_at DESC,id DESC);
"""
LOCK = threading.RLock()

@contextmanager
def db():
    c=sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory=sqlite3.Row
    try:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA synchronous=NORMAL")
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()
def now(): return time.time()
def stamp(t): return datetime.fromtimestamp(t,timezone.utc).isoformat()
def sound_path(sound_id):
    if not isinstance(sound_id,str) or not re.fullmatch(r"[0-9a-f]{32}",sound_id): return None
    for ext in (".mp3",".wav"):
        path=AUDIO_DIR/(sound_id+ext)
        if path.is_file(): return path
    return None
def sound_library():
    AUDIO_DIR.mkdir(parents=True,exist_ok=True); sounds=[]
    for meta in AUDIO_DIR.glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{32}\.json",meta.name): continue
        sid=meta.stem; path=sound_path(sid)
        if not path: continue
        try: name=json.loads(meta.read_text(encoding="utf-8")).get("name",path.name)
        except (OSError,ValueError): name=path.name
        sounds.append({"id":sid,"name":name,"extension":path.suffix[1:],"url":f"/audio/{path.name}"})
    return sorted(sounds,key=lambda sound:str(sound["name"]).casefold())
def init():
    fresh_install=not DB_PATH.exists()
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    AUDIO_DIR.mkdir(parents=True,exist_ok=True)
    # Packaged sounds live beside the application, while the editable library
    # lives in the user's data directory. Seed any missing bundled files on
    # every launch so upgrades also populate an existing installation without
    # overwriting sounds the user has already uploaded or changed.
    if BUNDLED_AUDIO_DIR.is_dir():
        for asset in BUNDLED_AUDIO_DIR.iterdir():
            if asset.is_file() and asset.suffix.lower() in (".mp3",".wav",".json") and re.fullmatch(r"[0-9a-f]{32}.(mp3|wav|json)",asset.name):
                target=AUDIO_DIR/asset.name
                if not target.exists(): shutil.copy2(asset,target)
    with db() as c:
        c.executescript(SCHEMA)
        for table, column, declaration in (("groups","parent_id","INTEGER REFERENCES groups(id) ON DELETE SET NULL"),("devices","width","INTEGER"),("devices","height","INTEGER"),("devices","note","TEXT NOT NULL DEFAULT ''"),("alerts","snooze_until","REAL"),("alerts","muted_at_creation","INTEGER NOT NULL DEFAULT 0")):
            if column not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        existing_keys={r["key"] for r in c.execute("SELECT key FROM settings")}
        legacy_cooldown=c.execute("SELECT value FROM settings WHERE key='sound_cooldown_seconds'").fetchone()
        defaults=DEFAULTS.copy()
        if not fresh_install or not sound_path(defaults["global_sound_down"]): defaults["global_sound_down"]="system-default"
        if not fresh_install or not sound_path(defaults["global_sound_up"]): defaults["global_sound_up"]="system-default"
        for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",(k,json.dumps(v)))
        if legacy_cooldown:
            for key in ("sound_down_cooldown_seconds","sound_up_cooldown_seconds"):
                if key not in existing_keys: c.execute("UPDATE settings SET value=? WHERE key=?",(legacy_cooldown["value"],key))
        for row in c.execute("SELECT DISTINCT device_id FROM samples").fetchall():
            c.execute("DELETE FROM samples WHERE device_id=? AND id NOT IN (SELECT id FROM samples WHERE device_id=? ORDER BY at DESC,id DESC LIMIT 30)",(row[0],row[0]))
        c.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",("card_scale",json.dumps(1.0)))
        c.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",("global_mute_until",json.dumps(0)))

def effective(d, settings):
    own=json.loads(d["settings"] or "{}"); return {k:(own[k] if own.get(k) is not None else settings[k]) for k in DEFAULTS}
def ping(ip, timeout_ms):
    if platform.system()=="Windows": args=["ping","-n","1","-w",str(timeout_ms),ip]
    elif platform.system()=="Darwin": args=["ping","-n","-c","1","-W",str(timeout_ms),ip]
    else: args=["ping","-n","-c","1","-W",str(max(1,round(timeout_ms/1000))),ip]
    start=time.perf_counter()
    try:
        res=subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=max(2,timeout_ms/1000+1),check=False)
        return res.returncode==0,(time.perf_counter()-start)*1000 if res.returncode==0 else None
    except (OSError,subprocess.TimeoutExpired): return False,None

def monitor():
    next_probe={}
    next_housekeeping=0
    device_columns="id,name,alias,note,group_id,ips,settings,muted_until,last_state,last_change,fail_count,flap_since,sort_order,width,height"
    while True:
        started=now()
        try:
            with LOCK,db() as c:
                settings={r["key"]:json.loads(r["value"]) for r in c.execute("SELECT key,value FROM settings")}
                devices=[dict(r) for r in c.execute(f"SELECT {device_columns} FROM devices ORDER BY id")]
                status_map={(int(r["device_id"]),r["ip"]):dict(r) for r in c.execute("SELECT device_id,ip,state,fail_count FROM ip_status")}
                due=[]
                for device in devices:
                    cfg=effective(device,settings)
                    if started<next_probe.get(device["id"],0): continue
                    next_probe[device["id"]]=started+max(1,int(cfg["ping_every"]))
                    try: ips=json.loads(device["ips"])
                    except (TypeError,ValueError): continue
                    prior={ip:status_map.get((int(device["id"]),ip)) for ip in ips}
                    due.append((device,cfg,ips,prior))
                housekeeping_due=started>=next_housekeeping
                if housekeeping_due: next_housekeeping=started+60
            results=[]
            for device,cfg,ips,prior in due:
                per=[]
                for ip in ips:
                    ok,ms=ping(ip,int(cfg["timeout_ms"]))
                    old=prior.get(ip); fails=0 if ok else (int(old["fail_count"]) if old else 0)+1
                    ip_state="online" if ok else ("offline" if fails>=int(cfg["fail_threshold"]) else "warning")
                    per.append((ip,ok,ms,fails,ip_state,old))
                online=sum(1 for _,ok,_,_,_,_ in per if ok)
                device_state="online" if online==len(ips) and ips else ("degraded" if online>0 else ("offline" if all(item[4]=="offline" for item in per) else "warning"))
                results.append((device,cfg,ips,per,online,device_state))
            if results or housekeeping_due:
                with LOCK,db() as c:
                    latest_settings={r["key"]:json.loads(r["value"]) for r in c.execute("SELECT key,value FROM settings")}
                    for device,cfg,ips,per,online,device_state in results:
                        current_row=c.execute(f"SELECT {device_columns} FROM devices WHERE id=?",(device["id"],)).fetchone()
                        if not current_row: continue
                        current=dict(current_row)
                        if current["ips"]!=device["ips"] or current["settings"]!=device["settings"]: continue
                        current_cfg=effective(current,latest_settings)
                        if any(current_cfg[key]!=cfg[key] for key in ("ping_every","timeout_ms","fail_threshold")): continue
                        silenced=bool(device_json(c,current)["muted"])
                        for ip,ok,ms,fails,ip_state,old in per:
                            c.execute("INSERT INTO ip_status(device_id,ip,state,fail_count,last_ms,last_seen) VALUES(?,?,?,?,?,?) ON CONFLICT(device_id,ip) DO UPDATE SET state=excluded.state,fail_count=excluded.fail_count,last_ms=excluded.last_ms,last_seen=CASE WHEN excluded.state='online' THEN excluded.last_seen ELSE ip_status.last_seen END",(current["id"],ip,ip_state,fails,ms,started))
                            bucket=int(started//900)*900
                            c.execute("INSERT INTO buckets(device_id,ip,bucket_start,total_sent,total_received,total_ms,min_ms,max_ms) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(device_id,ip,bucket_start) DO UPDATE SET total_sent=total_sent+1,total_received=total_received+excluded.total_received,total_ms=total_ms+excluded.total_ms,min_ms=CASE WHEN excluded.min_ms IS NULL THEN min_ms WHEN min_ms IS NULL THEN excluded.min_ms ELSE MIN(min_ms,excluded.min_ms) END,max_ms=CASE WHEN excluded.max_ms IS NULL THEN max_ms WHEN max_ms IS NULL THEN excluded.max_ms ELSE MAX(max_ms,excluded.max_ms) END",(current["id"],ip,bucket,1,int(ok),ms or 0,ms,ms))
                            c.execute("INSERT INTO samples(device_id,ip,at,ok,ms) VALUES(?,?,?,?,?)",(current["id"],ip,started,int(ok),ms))
                            if ip_state=="offline" and (not old or old["state"]!="offline"):
                                c.execute("INSERT INTO alerts(device_id,ip,state,message,created_at,muted_at_creation) VALUES(?,?,?,?,?,?)",(current["id"],ip,"offline",f"{current['name']} is offline at {ip}",started,int(silenced)))
                            elif ip_state=="online" and old and old["state"] in ("offline","warning"):
                                c.execute("INSERT INTO alerts(device_id,ip,state,message,created_at,muted_at_creation) VALUES(?,?,?,?,?,?)",(current["id"],ip,"online",f"{current['name']} recovered at {ip}",started,int(silenced)))
                                c.execute("UPDATE alerts SET snooze_until=NULL WHERE device_id=? AND ip=? AND state='offline' AND acknowledged=0 AND snooze_until IS NOT NULL",(current["id"],ip))
                        c.execute("DELETE FROM samples WHERE device_id=? AND id NOT IN (SELECT id FROM samples WHERE device_id=? ORDER BY at DESC,id DESC LIMIT 30)",(current["id"],current["id"]))
                        if device_state!=current["last_state"]: c.execute("UPDATE devices SET last_state=?,last_change=? WHERE id=?",(device_state,started,current["id"]))
                    if housekeeping_due:
                        c.execute("UPDATE alerts SET snooze_until=NULL WHERE snooze_until IS NOT NULL AND snooze_until<=? AND acknowledged=0 AND state='offline' AND EXISTS(SELECT 1 FROM devices d WHERE d.id=alerts.device_id AND d.last_state IN ('offline','warning','degraded'))",(started,))
        except Exception as e: print(f"monitor cycle error: {e}",flush=True)
        time.sleep(.25)

def mute_active(until, at=None):
    return float(until or 0)<0 or float(until or 0)>(now() if at is None else at)
def device_json(c,d,mute_context=None,ip_status_map=None):
    out=dict(d); out["ips"]=json.loads(out["ips"]); out["settings"]=json.loads(out["settings"] or "{}"); group_ids=[]
    if out.get("group_id") is not None:
        group_id=int(out["group_id"]); group_ids.append(group_id)
        if mute_context is not None:
            parent_id=mute_context["group_parents"].get(group_id)
            if parent_id is not None: group_ids.append(int(parent_id))
        else:
            parent=c.execute("SELECT parent_id FROM groups WHERE id=?",(group_id,)).fetchone()
            if parent and parent["parent_id"] is not None: group_ids.append(int(parent["parent_id"]))
    if mute_context is not None:
        global_until=mute_context["global_until"]
        values=[mute_context["group_mutes"].get(group_id,0) for group_id in group_ids]
    else:
        global_row=c.execute("SELECT value FROM settings WHERE key='global_mute_until'").fetchone(); global_until=json.loads(global_row[0]) if global_row else 0
        marks=",".join("?" for _ in group_ids)
        values=[float(r[0]) for r in c.execute(f"SELECT muted_until FROM group_mutes WHERE group_id IN ({marks})",group_ids)] if group_ids else []
    group_until=-1 if any(float(v)<0 for v in values) else max([float(v) for v in values] or [0])
    own_until=out["muted_until"]; out["muted"]=mute_active(own_until) or mute_active(group_until) or mute_active(global_until)
    out["effective_mute_until"]=-1 if any(float(v or 0)<0 for v in (own_until,group_until,global_until)) and out["muted"] else max([float(v or 0) for v in (own_until,group_until,global_until) if mute_active(v)] or [0])
    out["ip_status"]=ip_status_map.get(int(out["id"]),[]) if ip_status_map is not None else [dict(r) for r in c.execute("SELECT device_id,ip,state,fail_count,last_ms,last_seen FROM ip_status WHERE device_id=?",(out["id"],))]
    return out
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def send(self,obj,status=200):
        b=json.dumps(obj).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(b)
    def body(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))) or b"{}")
    def do_OPTIONS(self): self.send_response(204); self.send_header("Access-Control-Allow-Origin","*"); self.send_header("Access-Control-Allow-Headers","Content-Type"); self.send_header("Access-Control-Allow-Methods","GET,POST,PUT,DELETE,OPTIONS"); self.end_headers()
    def do_GET(self):
        p=urlparse(self.path); parts=p.path.strip("/").split("/"); q=__import__('urllib.parse').parse.parse_qs(p.query)
        if p.path=="/api/sounds": self.send({"sounds":sound_library()}); return
        if p.path=="/api/sounds/export":
            pack=[]
            for sound in sound_library():
                path=sound_path(sound["id"])
                if path:
                    pack.append({"id":sound["id"],"name":sound["name"],"extension":sound["extension"],"data":base64.b64encode(path.read_bytes()).decode("ascii")})
            self.send({"sounds":pack}); return
        if p.path.startswith("/audio/"):
            filename=p.path.rsplit("/",1)[-1]
            if filename!=Path(filename).name or not re.fullmatch(r"[0-9a-f]{32}\.(mp3|wav)",filename): self.send({"error":"Sound not found"},404); return
            audio=AUDIO_DIR/filename
            if not audio.is_file(): self.send({"error":"Sound not found"},404); return
            content=audio.read_bytes(); self.send_response(200); self.send_header("Content-Type","audio/mpeg" if audio.suffix==".mp3" else "audio/wav"); self.send_header("Content-Length",str(len(content))); self.send_header("Cache-Control","public, max-age=3600"); self.end_headers(); self.wfile.write(content); return
        if p.path in ("/","/index.html","/style.css","/app.js","/alerts.html","/alerts.js","/assets/shield-logo.png"):
            path=UI_ROOT/('index.html' if p.path=='/' else p.path.lstrip('/'))
            if path.is_file():
                data=path.read_bytes(); self.send_response(200); self.send_header('Content-Type',{'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8'}.get(path.suffix,'application/octet-stream')); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
        with db() as c:
            if p.path=="/api/report":
                try:
                    start=float(q.get("start",[""])[0]); end=float(q.get("end",[""])[0])
                    if not (0<=start<end<=now()+86400) or end-start>366*86400: raise ValueError
                    device_arg=q.get("device_id",["all"])[0]
                    device_id=None if device_arg=="all" else int(device_arg)
                except (TypeError,ValueError,OverflowError): self.send({"error":"Choose a valid report date range and device"},400); return
                device_sql=" WHERE d.id=?" if device_id is not None else ""
                devices=[dict(row) for row in c.execute("SELECT d.id,d.name,d.alias,d.ips,d.group_id,CASE WHEN g.parent_id IS NULL THEN COALESCE(g.name,'Ungrouped') ELSE COALESCE(pg.name,'Ungrouped') END AS parent_group,CASE WHEN g.parent_id IS NOT NULL THEN g.name ELSE NULL END AS subgroup FROM devices d LEFT JOIN groups g ON g.id=d.group_id LEFT JOIN groups pg ON pg.id=g.parent_id"+device_sql+" ORDER BY d.sort_order,d.id",(device_id,) if device_id is not None else ())]
                if device_id is not None and not devices: self.send({"error":"Device not found"},404); return
                ids=[int(device["id"]) for device in devices]
                if not ids: self.send({"rows":[],"resolution_seconds":900}); return
                marks=",".join("?" for _ in ids); bucket_start=int(start//900)*900; bucket_end=((int(end)-1)//900+1)*900
                params=(*ids,bucket_start,bucket_end)
                accum={did:{"down_count":0,"downtime_seconds":0.0,"sent":0,"received":0} for did in ids}
                bin_count=48; bin_span=(end-start)/bin_count
                bins=[{"sent":0.0,"received":0.0,"down_count":0,"downtime_seconds":0.0} for _ in range(bin_count)]
                for row in c.execute(f"SELECT device_id,bucket_start,total_sent,total_received FROM buckets WHERE device_id IN ({marks}) AND bucket_start>=? AND bucket_start<?",params):
                    bucket=float(row["bucket_start"]); overlap=max(0.0,min(end,bucket+900)-max(start,bucket)); weight=overlap/900
                    sent=int(row["total_sent"] or 0)*weight; received=int(row["total_received"] or 0)*weight
                    accum[int(row["device_id"])]["sent"]+=sent; accum[int(row["device_id"])]["received"]+=received
                    if overlap:
                        first=max(0,min(bin_count-1,int((max(start,bucket)-start)/bin_span)))
                        last=max(0,min(bin_count-1,int((min(end,bucket+900)-start-1e-9)/bin_span)))
                        for idx in range(first,last+1):
                            part=max(0.0,min(end,bucket+900,start+(idx+1)*bin_span)-max(start,bucket,start+idx*bin_span))/900
                            bins[idx]["sent"]+=int(row["total_sent"] or 0)*part
                            bins[idx]["received"]+=int(row["total_received"] or 0)*part
                events=c.execute(f"SELECT device_id,ip,state,created_at,id FROM alerts WHERE device_id IN ({marks}) AND created_at<=? AND state IN ('offline','online') ORDER BY device_id,ip,created_at,id",(*ids,end)).fetchall()
                def add_downtime(begin,finish):
                    cursor=max(start,float(begin)); finish=min(end,float(finish))
                    while cursor<finish:
                        idx=max(0,min(bin_count-1,int((cursor-start)/bin_span))); boundary=min(finish,start+(idx+1)*bin_span)
                        bins[idx]["downtime_seconds"]+=boundary-cursor; cursor=boundary
                active_down={}; current_key=None
                for event in events:
                    did=int(event["device_id"]); key=(did,event["ip"]); at=float(event["created_at"])
                    if key!=current_key:
                        current_key=key
                    if at<start:
                        if event["state"]=="offline": active_down[key]=start
                        else: active_down.pop(key,None)
                    elif event["state"]=="offline":
                        if key not in active_down:
                            active_down[key]=at; accum[did]["down_count"]+=1
                            idx=max(0,min(bin_count-1,int((at-start)/bin_span))); bins[idx]["down_count"]+=1
                    elif key in active_down:
                        down_start=active_down.pop(key); duration=max(0.0,at-max(start,down_start))
                        accum[did]["downtime_seconds"]+=duration; add_downtime(down_start,at)
                for (did,_),down_start in active_down.items():
                    duration=max(0.0,end-max(start,down_start)); accum[did]["downtime_seconds"]+=duration; add_downtime(down_start,end)
                rows=[]
                for device in devices:
                    did=int(device["id"]); stats=accum[did]; sent=stats["sent"]; received=stats["received"]
                    rows.append({**device,"ips":json.loads(device["ips"] or "[]"),"down_count":stats["down_count"],"downtime_seconds":round(stats["downtime_seconds"],1),"availability_percent":round(received/sent*100,2) if sent else None,"probes_sent":round(sent,1),"probes_received":round(received,1)})
                series=[]
                for idx,point in enumerate(bins):
                    sent=point["sent"]; received=point["received"]
                    series.append({"start":start+idx*bin_span,"end":start+(idx+1)*bin_span,"down_count":point["down_count"],"downtime_seconds":round(point["downtime_seconds"],1),"availability_percent":round(received/sent*100,2) if sent else None})
                self.send({"rows":rows,"series":series,"resolution_seconds":900,"availability_note":"Availability prorates 15-minute aggregate bucket counts by the portion of each bucket overlapping the selected range."}); return
            if p.path=="/api/alerts":
                status=q.get("status",[""])[0]
                where=" WHERE a.acknowledged=0 AND a.muted_at_creation=0" if status=="active" else " WHERE a.acknowledged=1" if status=="history" else " WHERE a.muted_at_creation=1" if status=="muted" else ""
                try:
                    offset=max(0,int(q.get("offset",["0"])[0]))
                    limit=min(150,max(1,int(q.get("limit",["150"])[0]))) if status=="history" else 5000
                except (TypeError,ValueError): self.send({"error":"Invalid alert page"},400); return
                count=int(c.execute("SELECT COUNT(*) FROM alerts a"+where).fetchone()[0])
                total_count=int(c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])
                rows=[dict(r) for r in c.execute("SELECT a.id,a.device_id,a.ip,a.state,a.message,a.created_at,a.acknowledged,a.muted_at_creation,a.snooze_until,d.name,d.note,d.muted_until,d.group_id AS device_group_id,ips.state AS current_ip_state,gm.muted_until AS direct_group_mute,pm.muted_until AS parent_group_mute,CASE WHEN g.parent_id IS NULL THEN COALESCE(g.name,'Ungrouped') ELSE COALESCE(pg.name,'Ungrouped') END AS parent_group,CASE WHEN g.parent_id IS NOT NULL THEN g.name ELSE NULL END AS subgroup FROM alerts a JOIN devices d ON d.id=a.device_id LEFT JOIN ip_status ips ON ips.device_id=a.device_id AND ips.ip=a.ip LEFT JOIN groups g ON g.id=d.group_id LEFT JOIN groups pg ON pg.id=g.parent_id LEFT JOIN group_mutes gm ON gm.group_id=g.id LEFT JOIN group_mutes pm ON pm.group_id=g.parent_id"+where+" ORDER BY a.created_at DESC,a.id DESC LIMIT ? OFFSET ?",(limit,offset))]
                settings_row=c.execute("SELECT value FROM settings WHERE key='global_mute_until'").fetchone(); global_until=json.loads(settings_row[0]) if settings_row else 0
                for row in rows: row["muted"]=mute_active(global_until) or mute_active(row.get("muted_until")) or mute_active(row.get("direct_group_mute")) or mute_active(row.get("parent_group_mute"))
                self.send({"alerts":rows,"count":count,"total":total_count,"offset":offset,"limit":limit,"has_more":offset+len(rows)<count}); return
            if p.path=="/api/state":
                ss={r["key"]:json.loads(r["value"]) for r in c.execute("SELECT key,value FROM settings")}; groups=[dict(r) for r in c.execute("SELECT id,name,sort_order,parent_id FROM groups ORDER BY sort_order,id")]; group_mutes={int(r["group_id"]):float(r["muted_until"]) for r in c.execute("SELECT group_id,muted_until FROM group_mutes")}; active_mutes=[]; group_by_id={int(g["id"]):g for g in groups}; global_until=float(ss.get("global_mute_until",0) or 0)
                if mute_active(global_until): active_mutes.append({"scope":"global","id":None,"name":"Entire system","until":global_until})
                for group in groups:
                    group["muted_until"]=group_mutes.get(int(group["id"]),0)
                    if mute_active(group["muted_until"]):
                        parent=group_by_id.get(int(group["parent_id"])) if group["parent_id"] is not None else None; label=f"{parent['name']} › {group['name']}" if parent else group["name"]
                        active_mutes.append({"scope":"group","id":int(group["id"]),"name":label,"until":group["muted_until"]})
                group_parents={int(r["id"]):int(r["parent_id"]) if r["parent_id"] is not None else None for r in groups}
                mute_context={"global_until":global_until,"group_parents":group_parents,"group_mutes":group_mutes}
                ip_status_map={}
                for item in c.execute("SELECT device_id,ip,state,fail_count,last_ms,last_seen FROM ip_status ORDER BY device_id,ip"):
                    ip_status_map.setdefault(int(item["device_id"]),[]).append(dict(item))
                devs=[device_json(c,r,mute_context,ip_status_map) for r in c.execute("SELECT id,name,alias,note,group_id,ips,settings,muted_until,last_state,last_change,fail_count,flap_since,sort_order,width,height FROM devices ORDER BY sort_order,id")]
                for device in devs:
                    if mute_active(device["muted_until"]): active_mutes.append({"scope":"device","id":int(device["id"]),"name":device["name"],"until":device["muted_until"]})
                alerts=[dict(r) for r in c.execute("SELECT a.id,a.device_id,a.ip,a.state,a.message,a.created_at,a.acknowledged,a.muted_at_creation,a.snooze_until,d.name,d.muted_until,ips.state AS current_ip_state,CASE WHEN g.parent_id IS NULL THEN COALESCE(g.name,'Ungrouped') ELSE COALESCE(pg.name,'Ungrouped') END AS parent_group,CASE WHEN g.parent_id IS NOT NULL THEN g.name ELSE NULL END AS subgroup FROM alerts a JOIN devices d ON d.id=a.device_id LEFT JOIN ip_status ips ON ips.device_id=a.device_id AND ips.ip=a.ip LEFT JOIN groups g ON g.id=d.group_id LEFT JOIN groups pg ON pg.id=g.parent_id WHERE a.acknowledged=0 ORDER BY a.created_at DESC LIMIT 300")]; totals=c.execute("SELECT COALESCE(SUM(total_sent),0) sent,COALESCE(SUM(total_received),0) received FROM buckets WHERE bucket_start>=?",(int(now())-86400,)).fetchone(); availability=round(100*totals["received"]/totals["sent"],1) if totals["sent"] else None; unread=c.execute("SELECT COUNT(*) FROM alerts WHERE acknowledged=0 AND muted_at_creation=0").fetchone()[0]; self.send({"groups":groups,"devices":devs,"settings":ss,"alerts":alerts,"unread":unread,"availability":availability,"active_mutes":active_mutes}); return
            if len(parts)==3 and parts[0]=="api" and parts[1]=="device":
                did=int(parts[2]); ip=q.get("ip",[None])[0]; rows=list(c.execute("SELECT device_id,ip,bucket_start,total_sent,total_received,total_ms,min_ms,max_ms FROM buckets WHERE device_id=?"+(" AND ip=?" if ip else "")+" ORDER BY bucket_start",(did,ip) if ip else (did,))); samples=list(c.execute("SELECT id,device_id,ip,at,ok,ms FROM samples WHERE device_id=?"+(" AND ip=?" if ip else "")+" ORDER BY at DESC,id DESC LIMIT 30",(did,ip) if ip else (did,))); alerts=list(c.execute("SELECT id,device_id,ip,state,message,created_at,acknowledged,muted_at_creation,snooze_until FROM alerts WHERE device_id=? ORDER BY created_at DESC,id DESC LIMIT 200",(did,))); self.send({"buckets":[dict(x) for x in rows],"samples":[dict(x) for x in samples],"alerts":[dict(x) for x in alerts]}); return
            self.send({"error":"Not found"},404)
    def do_POST(self):
        p=urlparse(self.path); route=p.path.rstrip("/"); x=self.body()
        if p.path=="/api/sounds":
            try:
                raw_name=str(x.get("name", "")).replace("\\", "/").rsplit("/",1)[-1].strip()
                ext=Path(raw_name).suffix.lower()
                if ext not in (".mp3",".wav") or not raw_name: raise ValueError("Choose an MP3 or WAV file")
                encoded=str(x.get("data", "")); encoded=encoded.split(",",1)[-1]
                content=base64.b64decode(encoded,validate=True)
                if not content or len(content)>20*1024*1024: raise ValueError("Audio file must be between 1 byte and 20 MB")
                valid=(content.startswith(b"RIFF") and content[8:12]==b"WAVE") if ext==".wav" else (content.startswith(b"ID3") or (len(content)>1 and content[0]==0xff and content[1]&0xe0==0xe0))
                if not valid: raise ValueError("The file contents do not match the selected audio format")
                AUDIO_DIR.mkdir(parents=True,exist_ok=True); sid=uuid.uuid4().hex; target=AUDIO_DIR/(sid+ext); temp=AUDIO_DIR/(sid+ext+".tmp")
                temp.write_bytes(content); temp.replace(target)
                (AUDIO_DIR/(sid+".json")).write_text(json.dumps({"id":sid,"name":raw_name}),encoding="utf-8")
                self.send({"ok":True,"sound":{"id":sid,"name":raw_name,"extension":ext[1:],"url":f"/audio/{target.name}"}}); return
            except (TypeError,ValueError) as exc: self.send({"error":str(exc) or "Invalid audio upload"},400); return
            except OSError as exc: self.send({"error":f"Could not save audio: {exc}"},500); return
        with LOCK,db() as c:
            if route=="/api/alerts/acknowledge-all":
                cursor=c.execute("UPDATE alerts SET acknowledged=1 WHERE acknowledged=0 AND muted_at_creation=0")
                self.send({"ok":True,"count":cursor.rowcount}); return
            if route in ("/api/placement","/api/devices/move") or (route.startswith("/api/device/") and route.endswith("/placement")):
                placements=x.get("placements")
                if placements is None and route.startswith("/api/device/"):
                    parts=route.strip("/").split("/"); placements=[{"deviceId":parts[2],**x}]
                if placements is None:
                    placements=[x] if any(k in x for k in ("deviceId","device_id","id")) else []
                if not isinstance(placements,list): self.send({"error":"placements must be a list"},400); return
                normalized=[]
                try:
                    for item in placements:
                        did=int(item.get("deviceId",item.get("device_id",item.get("id"))))
                        subgroup=item.get("subGroupId",item.get("sub_group_id")); group=item.get("groupId",item.get("group_id"))
                        # A legacy group_id may itself identify a subgroup. New clients
                        # send the parent group and subgroup separately.
                        gid=subgroup if subgroup is not None else group; gid=None if gid is None else int(gid)
                        parent_id=None if group is None else int(group)
                        order=int(item.get("sortOrder",item.get("sort_order",0)))
                        if not c.execute("SELECT 1 FROM devices WHERE id=?",(did,)).fetchone(): raise ValueError("Device not found")
                        if gid is not None:
                            target=c.execute("SELECT id,parent_id FROM groups WHERE id=?",(gid,)).fetchone()
                            if not target: raise ValueError("Group or subgroup not found")
                            # If both IDs are supplied, require the subgroup to be
                            # nested under the requested main group. Legacy payloads
                            # with only group_id remain supported.
                            if subgroup is not None and (target["parent_id"] is None or parent_id is None or int(target["parent_id"])!=parent_id):
                                raise ValueError("Subgroup does not belong to the selected main group")
                            if subgroup is None and parent_id is not None and target["parent_id"] is not None:
                                raise ValueError("Select a main group for this subgroup")
                        normalized.append((gid,order,did))
                except (KeyError,TypeError,ValueError) as exc:
                    self.send({"error":str(exc) or "Invalid placement"},400); return
                c.executemany("UPDATE devices SET group_id=?,sort_order=? WHERE id=?",normalized)
                updated=[device_json(c,row) for _,_,did in normalized if (row:=c.execute("SELECT id,name,alias,note,group_id,ips,settings,muted_until,last_state,last_change,fail_count,flap_since,sort_order,width,height FROM devices WHERE id=?",(did,)).fetchone())]
                self.send({"ok":True,"devices":updated,"device":updated[0] if len(updated)==1 else None}); return
            if p.path=="/api/group":
                parent=x.get("parent_id")
                if parent is not None:
                    try: parent=int(parent)
                    except (TypeError,ValueError): self.send({"error":"Invalid parent group"},400); return
                    parent_row=c.execute("SELECT parent_id FROM groups WHERE id=?",(parent,)).fetchone()
                    if not parent_row: self.send({"error":"Parent group not found"},400); return
                    if parent_row["parent_id"] is not None: self.send({"error":"Subgroups must belong directly to a main group"},400); return
                cur=c.execute("INSERT INTO groups(name,parent_id) VALUES(?,?)",(x.get("name","New group"),parent)); self.send({"id":cur.lastrowid}); return
            if p.path=="/api/device":
                cur=c.execute("INSERT INTO devices(name,alias,note,group_id,ips,settings) VALUES(?,?,?,?,?,?)",(x.get("name","New device"),x.get("alias",""),x.get("note",""),x.get("group_id"),json.dumps(x.get("ips",[])),json.dumps(x.get("settings",{})))); self.send({"id":cur.lastrowid}); return
            if p.path=="/api/import":
                data=x.get("backup",x)
                try:
                    if not isinstance(data,dict) or not isinstance(data.get("groups"),list) or not isinstance(data.get("devices"),list): raise ValueError("Backup must contain groups and devices arrays")
                    settings=data.get("settings",{})
                    if not isinstance(settings,dict) or any(not isinstance(key,str) for key in settings): raise ValueError("Backup settings are invalid")
                    json.dumps(settings)
                    groups=data["groups"]; devices=data["devices"]; old_ids={}
                    for group in groups:
                        if not isinstance(group,dict) or not str(group.get("name","")).strip(): raise ValueError("A group is missing its name")
                        if group.get("id") is None or str(group["id"]) in old_ids: raise ValueError("Group IDs must be present and unique")
                        old_ids[str(group["id"])]=group
                    for group in groups:
                        parent=group.get("parent_id")
                        if parent is not None and (str(parent) not in old_ids or old_ids[str(parent)].get("parent_id") is not None): raise ValueError("Subgroups must reference a valid main group")
                    for device in devices:
                        if not isinstance(device,dict) or not str(device.get("name","")).strip(): raise ValueError("A device is missing its name")
                        if not isinstance(device.get("ips"),list) or not device["ips"] or any(not isinstance(ip,str) or not ip.strip() for ip in device["ips"]): raise ValueError("Each device must have at least one IP address")
                        if device.get("group_id") is not None and str(device["group_id"]) not in old_ids: raise ValueError("A device references a missing group")
                        if not isinstance(device.get("settings",{}),dict): raise ValueError("Device overrides are invalid")
                    sound_files=[]
                    if "sounds" in data:
                        if not isinstance(data["sounds"],list): raise ValueError("Sound library backup is invalid")
                        seen=set(); total=0
                        for sound in data["sounds"]:
                            if not isinstance(sound,dict) or not re.fullmatch(r"[0-9a-f]{32}",str(sound.get("id",""))): raise ValueError("A custom sound ID is invalid")
                            sid=sound["id"]; ext=sound.get("extension"); name=str(sound.get("name","")).strip()
                            if sid in seen or ext not in ("mp3","wav") or not name or len(name)>160: raise ValueError("Custom sound metadata is invalid")
                            content=base64.b64decode(sound.get("data",""),validate=True); total+=len(content)
                            valid=(content.startswith(b"RIFF") and content[8:12]==b"WAVE") if ext=="wav" else (content.startswith(b"ID3") or (len(content)>1 and content[0]==0xff and content[1]&0xe0==0xe0))
                            if not valid: raise ValueError("Custom sound contents do not match the selected audio format")
                            if len(content)>16*1024*1024 or total>64*1024*1024: raise ValueError("Custom sound backup exceeds the 64 MB import limit")
                            seen.add(sid); sound_files.append((sid,ext,name,content))
                except (TypeError,ValueError,KeyError) as exc:
                    self.send({"error":str(exc) or "Invalid configuration backup"},400); return
                staged=[]
                try:
                    if "sounds" in data:
                        AUDIO_DIR.mkdir(parents=True,exist_ok=True)
                        for sid,ext,name,content in sound_files:
                            audio_tmp=AUDIO_DIR/f".{sid}.{uuid.uuid4().hex}.tmp"; meta_tmp=AUDIO_DIR/f".{sid}.{uuid.uuid4().hex}.tmp"
                            audio_tmp.write_bytes(content); meta_tmp.write_text(json.dumps({"name":name}),encoding="utf-8"); staged.append((sid,ext,audio_tmp,meta_tmp))
                    c.execute("DELETE FROM devices"); c.execute("DELETE FROM groups"); c.execute("DELETE FROM settings")
                    merged={**DEFAULTS,"global_mute_until":0,"card_scale":1.0,**settings}
                    for key,value in merged.items(): c.execute("INSERT INTO settings VALUES(?,?)",(key,json.dumps(value)))
                    ids={}
                    for group in groups:
                        if group.get("parent_id") is None:
                            cur=c.execute("INSERT INTO groups(name,sort_order) VALUES(?,?)",(group["name"].strip(),int(group.get("sort_order",0))))
                            ids[str(group["id"])]=cur.lastrowid
                    for group in groups:
                        if group.get("parent_id") is not None:
                            cur=c.execute("INSERT INTO groups(name,sort_order,parent_id) VALUES(?,?,?)",(group["name"].strip(),int(group.get("sort_order",0)),ids[str(group["parent_id"])]))
                            ids[str(group["id"])]=cur.lastrowid
                            until=float(group.get("muted_until",0) or 0)
                            if until: c.execute("INSERT INTO group_mutes(group_id,muted_until) VALUES(?,?)",(cur.lastrowid,until))
                    for group in groups:
                        if group.get("parent_id") is None and float(group.get("muted_until",0) or 0): c.execute("INSERT INTO group_mutes(group_id,muted_until) VALUES(?,?)",(ids[str(group["id"])],float(group["muted_until"])))
                    for device in devices:
                        c.execute("INSERT INTO devices(name,alias,note,group_id,ips,settings,muted_until,sort_order,width,height) VALUES(?,?,?,?,?,?,?,?,?,?)",(device["name"].strip(),device.get("alias",""),device.get("note",""),ids.get(str(device.get("group_id"))) if device.get("group_id") is not None else None,json.dumps(device["ips"]),json.dumps(device.get("settings",{})),float(device.get("muted_until",0) or 0),int(device.get("sort_order",0)),device.get("width"),device.get("height")))
                    if "sounds" in data:
                        for sid,ext,audio_tmp,meta_tmp in staged:
                            os.replace(audio_tmp,AUDIO_DIR/f"{sid}.{ext}"); os.replace(meta_tmp,AUDIO_DIR/f"{sid}.json")
                        keep={sid for sid,_,_,_ in staged}
                        for file in AUDIO_DIR.iterdir():
                            if re.fullmatch(r"[0-9a-f]{32}\.(mp3|wav|json)",file.name) and file.stem not in keep: file.unlink(missing_ok=True)
                except (OSError,sqlite3.Error,TypeError,ValueError) as exc:
                    c.rollback()
                    for _,_,audio_tmp,meta_tmp in staged:
                        audio_tmp.unlink(missing_ok=True); meta_tmp.unlink(missing_ok=True)
                    self.send({"error":f"Could not import configuration: {exc}"},400); return
                self.send({"ok":True,"groups":len(groups),"devices":len(devices),"sounds":len(sound_files) if "sounds" in data else None}); return
            if p.path.startswith("/api/alert/"):
                if p.path.endswith("/snooze"):
                    parts=p.path.rstrip("/").split("/")
                    try:
                        alert_id=int(parts[-2]); seconds=int(x.get("seconds",3600))
                        if seconds<60 or seconds>604800: raise ValueError("Snooze duration must be between 1 minute and 7 days")
                    except (IndexError,TypeError,ValueError) as exc: self.send({"error":str(exc) or "Invalid snooze request"},400); return
                    if not c.execute("SELECT 1 FROM alerts a LEFT JOIN ip_status ips ON ips.device_id=a.device_id AND ips.ip=a.ip WHERE a.id=? AND a.acknowledged=0 AND a.state='offline' AND ips.state='offline'",(alert_id,)).fetchone(): self.send({"error":"Only unresolved DOWN alerts can be snoozed"},400); return
                    until=now()+seconds; c.execute("UPDATE alerts SET snooze_until=? WHERE id=?",(until,alert_id)); alert=dict(c.execute("SELECT id,device_id,ip,state,message,created_at,acknowledged,muted_at_creation,snooze_until FROM alerts WHERE id=?",(alert_id,)).fetchone()); self.send({"ok":True,"alert":alert}); return
                try: alert_id=int(p.path.split("/")[-1])
                except ValueError: self.send({"error":"Invalid alert ID"},400); return
                if not c.execute("SELECT 1 FROM alerts WHERE id=?",(alert_id,)).fetchone(): self.send({"error":"Alert not found"},404); return
                c.execute("UPDATE alerts SET acknowledged=1 WHERE id=?",(alert_id,)); alert=dict(c.execute("SELECT id,device_id,ip,state,message,created_at,acknowledged,muted_at_creation,snooze_until FROM alerts WHERE id=?",(alert_id,)).fetchone()); self.send({"ok":True,"alert":alert}); return
            if route=="/api/mute/global" or route.startswith("/api/mute/group/") or route.startswith("/api/mute/device/") or route.startswith("/api/mute/"):
                try:
                    if x.get("forever"): until=-1
                    else:
                        minutes=int(x.get("minutes",max(1,int(x.get("seconds",3600))//60)))
                        if minutes<1 or minutes>525600: raise ValueError("Duration must be between 1 minute and 1 year")
                        until=now()+minutes*60
                    if route=="/api/mute/global": c.execute("INSERT OR REPLACE INTO settings VALUES('global_mute_until',?)",(json.dumps(until),)); target={"scope":"global","id":None}
                    elif route.startswith("/api/mute/group/"):
                        gid=int(route.split("/")[-1])
                        if not c.execute("SELECT 1 FROM groups WHERE id=?",(gid,)).fetchone(): self.send({"error":"Group not found"},404); return
                        c.execute("INSERT OR REPLACE INTO group_mutes VALUES(?,?)",(gid,until)); target={"scope":"group","id":gid}
                    else:
                        did=int(route.split("/")[-1])
                        if not c.execute("SELECT 1 FROM devices WHERE id=?",(did,)).fetchone(): self.send({"error":"Device not found"},404); return
                        c.execute("UPDATE devices SET muted_until=? WHERE id=?",(until,did)); target={"scope":"device","id":did}
                    self.send({"ok":True,"muted_until":until,"target":target}); return
                except (TypeError,ValueError) as exc: self.send({"error":str(exc) or "Invalid mute duration"},400); return
            if route.startswith("/api/unmute/"):
                parts=route.strip("/").split("/")
                try:
                    if len(parts)==3 and parts[2]=="global": c.execute("INSERT OR REPLACE INTO settings VALUES('global_mute_until','0')")
                    elif len(parts)==4 and parts[2]=="group": c.execute("DELETE FROM group_mutes WHERE group_id=?",(int(parts[3]),))
                    elif len(parts)==4 and parts[2]=="device": c.execute("UPDATE devices SET muted_until=0 WHERE id=?",(int(parts[3]),))
                    else: self.send({"error":"Invalid mute target"},400); return
                    self.send({"ok":True}); return
                except ValueError: self.send({"error":"Invalid mute target"},400); return
            if p.path=="/api/settings":
                allowed={"ping_every":1,"timeout_ms":100,"fail_threshold":1,"history_days":1,"toast_seconds":1}
                try:
                    for k,v in x.items():
                        if k in ("global_sound_up","global_sound_down"):
                            v=str(v or "system-default")
                            if v!="system-default" and not sound_path(v): raise ValueError(f"Unknown sound selected for {k}")
                            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,json.dumps(v)))
                            continue
                        if k in ("sound_down_enabled","sound_up_enabled"):
                            v=bool(v)
                            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,json.dumps(v)))
                            continue
                        if k in ("sound_cooldown_seconds","sound_down_cooldown_seconds","sound_up_cooldown_seconds"):
                            v=int(v)
                            if not 0<=v<=3600: raise ValueError(f"{k} must be between 0 and 3600")
                            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,json.dumps(v)))
                            continue
                        if k=="card_scale":
                            v=float(v)
                            if not 0.55<=v<=1.75: raise ValueError("card_scale must be between 0.55 and 1.75")
                            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,json.dumps(v)))
                            continue
                        if k=="alert_cleanup_age":
                            if v!="custom":
                                v=int(v)
                                if not 1<=v<=3650: raise ValueError("alert_cleanup_age must be between 1 and 3650 days")
                            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,json.dumps(v)))
                            continue
                        if k=="alert_cleanup_custom_days":
                            v=int(v)
                            if not 1<=v<=3650: raise ValueError("alert_cleanup_custom_days must be between 1 and 3650 days")
                            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,json.dumps(v)))
                            continue
                        if k not in allowed and k!="audio_enabled": continue
                        if k=="audio_enabled": v=bool(v)
                        else:
                            v=int(v)
                            if v<allowed[k]: raise ValueError(f"{k} must be at least {allowed[k]}")
                        c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,json.dumps(v)))
                except (TypeError,ValueError) as exc: self.send({"error":str(exc) or "Invalid settings value"},400); return
                self.send({"ok":True}); return
        self.send({"error":"Not found"},404)
    def do_PUT(self):
        p=urlparse(self.path); x=self.body(); parts=p.path.strip("/").split("/")
        with LOCK,db() as c:
            if len(parts)==3 and parts[0]=="api" and parts[1]=="devices":
                try:
                    did=int(parts[2]); group_id=x.get("sub_group_id") if x.get("sub_group_id") is not None else x.get("group_id"); group_id=None if group_id is None else int(group_id); sort_order=int(x.get("sort_order",0))
                except (TypeError,ValueError): self.send({"error":"Invalid device placement"},400); return
                if not c.execute("SELECT 1 FROM devices WHERE id=?",(did,)).fetchone(): self.send({"error":"Device not found"},404); return
                if group_id is not None and not c.execute("SELECT 1 FROM groups WHERE id=?",(group_id,)).fetchone(): self.send({"error":"Group not found"},400); return
                c.execute("UPDATE devices SET group_id=?,sort_order=? WHERE id=?",(group_id,sort_order,did)); updated=device_json(c,c.execute("SELECT id,name,alias,note,group_id,ips,settings,muted_until,last_state,last_change,fail_count,flap_since,sort_order,width,height FROM devices WHERE id=?",(did,)).fetchone()); self.send({"ok":True,"device":updated}); return
            if len(parts)==4 and parts[1]=="device" and parts[3]=="size":
                c.execute("UPDATE devices SET width=?,height=? WHERE id=?",(x.get("width"),x.get("height"),int(parts[2]))); self.send({"ok":True}); return
            if len(parts)==4 and parts[1]=="device" and parts[3]=="placement":
                group_id=x.get("group_id"); c.execute("UPDATE devices SET group_id=?,sort_order=? WHERE id=?",(group_id,int(x.get("sort_order",0)),int(parts[2]))); self.send({"ok":True}); return
            if p.path.startswith("/api/device/"):
                did=int(p.path.split("/")[-1]); c.execute("UPDATE devices SET name=?,alias=?,note=?,group_id=?,ips=?,settings=? WHERE id=?",(x.get("name","Device"),x.get("alias",""),x.get("note",""),x.get("group_id"),json.dumps(x.get("ips",[])),json.dumps(x.get("settings",{})),did)); c.execute("DELETE FROM ip_status WHERE device_id=? AND ip NOT IN (%s)"%(",".join("?" for _ in x.get("ips",[])) or "''"),(did,*x.get("ips",[]))); self.send({"ok":True}); return
            if p.path.startswith("/api/group/"):
                gid=int(p.path.split("/")[-1]); parent=x.get("parent_id")
                if not c.execute("SELECT 1 FROM groups WHERE id=?",(gid,)).fetchone(): self.send({"error":"Group not found"},404); return
                if parent is not None:
                    try: parent=int(parent)
                    except (TypeError,ValueError): self.send({"error":"Invalid parent group"},400); return
                    if parent==gid: self.send({"error":"A group cannot be its own parent"},400); return
                    parent_row=c.execute("SELECT parent_id FROM groups WHERE id=?",(parent,)).fetchone()
                    if not parent_row: self.send({"error":"Parent group not found"},400); return
                    if parent_row["parent_id"] is not None: self.send({"error":"Subgroups must belong directly to a main group"},400); return
                    if c.execute("SELECT 1 FROM groups WHERE parent_id=? LIMIT 1",(gid,)).fetchone(): self.send({"error":"Move or delete this group's subgroups before changing its parent"},400); return
                c.execute("UPDATE groups SET name=?,parent_id=? WHERE id=?",(x.get("name","Group"),parent,gid)); self.send({"ok":True}); return
            if p.path.startswith("/api/order/"):
                c.execute("UPDATE devices SET sort_order=? WHERE id=?",(int(x.get("sort_order",0)),int(p.path.split("/")[-1]))); self.send({"ok":True}); return
        self.send({"error":"Not found"},404)
    def do_DELETE(self):
        p=urlparse(self.path); parts=p.path.split("/")
        with LOCK,db() as c:
            if len(parts)==4 and parts[1:3]==["api","sounds"]:
                sid=parts[3]
                path=sound_path(sid)
                if not path: self.send({"error":"Sound not found"},404); return
                reset_global=[]
                for key in ("global_sound_down","global_sound_up"):
                    row=c.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
                    if row and json.loads(row["value"])==sid:
                        c.execute("UPDATE settings SET value=? WHERE key=?",(json.dumps("system-default"),key)); reset_global.append(key)
                updated_devices=0
                for row in c.execute("SELECT id,settings FROM devices").fetchall():
                    try: settings=json.loads(row["settings"] or "{}")
                    except (TypeError,ValueError): continue
                    changed=False
                    for key in ("sound_down","sound_up"):
                        if settings.get(key)==sid: settings[key]="system-default"; changed=True
                    if changed:
                        c.execute("UPDATE devices SET settings=? WHERE id=?",(json.dumps(settings),row["id"])); updated_devices+=1
                try:
                    path.unlink()
                    (AUDIO_DIR/(sid+".json")).unlink(missing_ok=True)
                except OSError as exc:
                    self.send({"error":f"Could not delete sound file: {exc}"},500); return
                self.send({"ok":True,"id":sid,"reset_global":reset_global,"updated_devices":updated_devices}); return
            if p.path=="/api/alerts/history":
                try:
                    days=int(parse_qs(p.query).get("older_than_days",["30"])[0])
                    if days<1 or days>3650: raise ValueError
                except (TypeError,ValueError): self.send({"error":"Retention age must be between 1 and 3650 days"},400); return
                cursor=c.execute("DELETE FROM alerts WHERE acknowledged=1 AND created_at<?",(now()-days*86400,))
                self.send({"ok":True,"deleted":cursor.rowcount,"older_than_days":days}); return
            if len(parts)==4 and parts[2]=="device":
                c.execute("DELETE FROM devices WHERE id=?",(int(parts[3]),)); self.send({"ok":True}); return
            if len(parts)==4 and parts[2]=="group":
                gid=int(parts[3])
                group=c.execute("SELECT parent_id FROM groups WHERE id=?",(gid,)).fetchone()
                if not group: self.send({"error":"Group not found"},404); return
                if group["parent_id"] is None:
                    children=[r[0] for r in c.execute("SELECT id FROM groups WHERE parent_id=?",(gid,))]
                    ids=[gid,*children]
                    marks=",".join("?" for _ in ids)
                    c.execute(f"UPDATE devices SET group_id=NULL,sort_order=0 WHERE group_id IN ({marks})",ids)
                    c.execute("DELETE FROM groups WHERE parent_id=?",(gid,))
                else:
                    c.execute("UPDATE devices SET group_id=NULL,sort_order=0 WHERE group_id=?",(gid,))
                c.execute("DELETE FROM groups WHERE id=?",(gid,)); self.send({"ok":True}); return
        self.send({"error":"Not found"},404)

if __name__=="__main__":
    init(); threading.Thread(target=monitor,daemon=True,name="icmp-monitor").start(); print(f"AriYouLive service listening at http://{HOST}:{PORT}",flush=True); ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
