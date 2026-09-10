#!/usr/bin/env python3
"""
dashboard.py -- a static, self-contained view of what the worklog tools are doing.

Reads the state directory and writes one HTML file with the data baked into it.
No server, no CDN, no third-party dependencies: open the file with a file:// URL
and it works offline. A `file://` page cannot fetch a sibling data.json (CORS
forbids it), which is exactly why the data is embedded rather than loaded.

"Refresh" therefore means re-running this command; the page reloads itself on a
timer if you ask it to, so a terminal running `--watch` gives a live view.

Usage:
    dashboard.py                       write ~/.claude/worklog/dashboard/index.html
    dashboard.py --open                ...and open it in the browser
    dashboard.py --output <path>       write somewhere else
    dashboard.py --watch [seconds]     regenerate on a timer (default 30)
    dashboard.py --serve [port]        serve on 127.0.0.1 (default 8777) so the
                                       page's Refresh button really regenerates
"""

from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worklog  # noqa: E402

try:
    import post as postmod
except Exception:  # pragma: no cover - post.py is optional for a read-only view
    postmod = None

VERSION = "0.2.0"


# ---------------------------------------------------------------- collection

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _posted_path() -> Path:
    return worklog.state_dir() / "posted.jsonl"


def _tool(name: str) -> bool:
    return shutil.which(name) is not None


def collect_health(cfg: dict) -> dict:
    settings = Path.home() / ".claude" / "settings.json"
    data = worklog.read_json(settings, {})
    hooks = data.get("hooks", {})

    def installed(event: str, needle: str) -> bool:
        for group in hooks.get(event, []):
            for handler in group.get("hooks", []):
                if needle in json.dumps(handler):
                    return True
        return False

    creds = worklog.state_dir() / "credentials.json"
    creds_state = "missing"
    if creds.exists():
        raw = creds.read_bytes()
        if raw[:3] == b"\xef\xbb\xbf":
            creds_state = "BOM"           # json.load will throw; reads as "missing"
        else:
            have = worklog.read_json(creds, {})
            missing = [k for k in ("base_url", "email", "api_token") if not have.get(k)]
            creds_state = "ok" if not missing else "incomplete: " + ", ".join(missing)

    return {
        "settings_path": str(settings),
        "hooks": {
            "SessionStart (capture)": installed("SessionStart", "worklog.py"),
            "SessionStart (drain)": installed("SessionStart", "post.py"),
            "UserPromptSubmit": installed("UserPromptSubmit", "worklog.py"),
            "PostToolUse": installed("PostToolUse", "worklog.py"),
            "Stop": installed("Stop", "worklog.py"),
            "SessionEnd": installed("SessionEnd", "worklog.py"),
        },
        "credentials": creds_state,
        "credentials_path": str(creds),
        "git": _tool("git"),
        "gh": _tool("gh"),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "drain_lock_held": (worklog.state_dir() / "post-run.lock").exists(),
    }


def collect(output: Path | None = None) -> dict:
    cfg = worklog.load_config()
    now = worklog.now()
    floor = max(int(cfg["minimum_minutes"]), int(cfg["round_to_minutes"])) * 60
    idle_limit = float(cfg["idle_timeout_minutes"]) * 60

    # live sessions
    sessions = []
    for path in sorted(worklog.sessions_dir().glob("*.json")):
        rec = worklog.read_json(path, None)
        if not rec:
            continue
        try:
            idle = (now - worklog.parse_ts(rec["last_activity"])).total_seconds()
        except Exception:
            idle = 0.0
        sessions.append({
            "session_id": rec.get("session_id", path.stem),
            "issue_key": rec.get("issue_key"),
            "cwd": rec.get("cwd"),
            "project_root": rec.get("project_root"),
            "source": rec.get("issue_source"),
            "started": rec.get("started"),
            "active_seconds": float(rec.get("active_seconds", 0)),
            "events": int(rec.get("events", 0)),
            "idle_drops": int(rec.get("idle_drops", 0)),
            "resumed": int(rec.get("resumed", 0)),
            "idle_seconds": idle,
            "going_stale": idle > float(cfg["stale_session_hours"]) * 3600,
            "branch": (rec.get("git_start") or {}).get("branch"),
        })

    queue = _read_jsonl(worklog.queue_path())
    posted = _read_jsonl(_posted_path())
    carry = worklog.read_json(worklog.carry_path(), {})

    # unmapped directories, aggregated
    unmapped_rows = _read_jsonl(worklog.unmapped_path())
    unmapped: dict[str, dict] = {}
    for row in unmapped_rows:
        key = row.get("project_root") or row.get("cwd") or "?"
        slot = unmapped.setdefault(key, {"path": key, "sessions": 0, "seconds": 0.0, "last": ""})
        slot["sessions"] += 1
        slot["seconds"] += float(row.get("active_seconds", 0))
        if row.get("ended", "") > slot["last"]:
            slot["last"] = row.get("ended", "")
    unmapped_list = sorted(unmapped.values(), key=lambda r: -r["seconds"])

    # per-issue rollups
    today = now.date().isoformat()
    posted_by_issue: dict[str, dict] = defaultdict(lambda: {"seconds": 0, "count": 0,
                                                            "today": 0, "last": ""})
    for entry in posted:
        slot = posted_by_issue[entry["issue_key"]]
        slot["seconds"] += int(entry.get("time_spent_seconds", 0))
        slot["count"] += 1
        when = str(entry.get("posted_at", ""))[:10]
        if when == today:
            slot["today"] += int(entry.get("time_spent_seconds", 0))
        if str(entry.get("posted_at", "")) > slot["last"]:
            slot["last"] = str(entry.get("posted_at", ""))

    queued_by_issue: dict[str, int] = defaultdict(int)
    blocked_by_issue: dict[str, int] = defaultdict(int)
    for entry in queue:
        queued_by_issue[entry["issue_key"]] += int(entry.get("time_spent_seconds", 0))
        if entry.get("status") == "blocked":
            blocked_by_issue[entry["issue_key"]] += int(entry.get("time_spent_seconds", 0))

    live_by_issue: dict[str, float] = defaultdict(float)
    for s in sessions:
        if s["issue_key"]:
            live_by_issue[s["issue_key"]] += s["active_seconds"]

    # projects: mapped directories, plus any issue with data but no mapping
    projects = []
    for path, entry in (cfg.get("projects") or {}).items():
        key = entry.get("issue_key") if isinstance(entry, dict) else entry
        projects.append({"path": path, "issue_key": key, "source": "config map",
                        "github": (entry or {}).get("github") if isinstance(entry, dict) else None})
    seen_paths = {p["path"] for p in projects}
    for s in sessions:
        root = s.get("project_root")
        if root and root not in seen_paths and s.get("issue_key"):
            projects.append({"path": root, "issue_key": s["issue_key"],
                             "source": "marker" if str(s.get("source", "")).endswith(".jira-project")
                             else str(s.get("source")), "github": None})
            seen_paths.add(root)

    mapped_keys = {p["issue_key"] for p in projects}
    for key in sorted(set(posted_by_issue) | set(queued_by_issue) | set(carry) | set(live_by_issue)):
        if key not in mapped_keys:
            projects.append({"path": None, "issue_key": key, "source": "history only",
                             "github": None})

    for p in projects:
        key = p["issue_key"]
        p["live_seconds"] = live_by_issue.get(key, 0.0)
        p["carry_seconds"] = float((carry.get(key) or {}).get("seconds", 0))
        p["queued_seconds"] = queued_by_issue.get(key, 0)
        p["blocked_seconds"] = blocked_by_issue.get(key, 0)
        p["posted_seconds"] = posted_by_issue[key]["seconds"] if key in posted_by_issue else 0
        p["posted_count"] = posted_by_issue[key]["count"] if key in posted_by_issue else 0
        p["posted_today"] = posted_by_issue[key]["today"] if key in posted_by_issue else 0
        p["last_posted"] = posted_by_issue[key]["last"] if key in posted_by_issue else ""
        p["unposted_seconds"] = p["live_seconds"] + p["carry_seconds"] + p["queued_seconds"]
        p["needs_for_floor"] = max(0.0, floor - (p["carry_seconds"] + p["live_seconds"])) \
            if p["queued_seconds"] == 0 else 0.0
    projects.sort(key=lambda p: (-(p["unposted_seconds"] + p["posted_seconds"]), str(p["issue_key"])))

    totals = {
        "live": sum(live_by_issue.values()),
        "carry": sum(float(v.get("seconds", 0)) for v in carry.values()),
        "queued": sum(int(e.get("time_spent_seconds", 0)) for e in queue),
        "blocked": sum(int(e.get("time_spent_seconds", 0)) for e in queue
                       if e.get("status") == "blocked"),
        "failed_count": sum(1 for e in queue if e.get("status") == "failed"),
        "blocked_count": sum(1 for e in queue if e.get("status") == "blocked"),
        "pending_count": sum(1 for e in queue if e.get("status") == "pending"),
        "posted_all": sum(int(e.get("time_spent_seconds", 0)) for e in posted),
        "posted_today": sum(v["today"] for v in posted_by_issue.values()),
        "posted_count": len(posted),
        "unmapped": sum(r["seconds"] for r in unmapped_list),
    }
    totals["unposted"] = totals["live"] + totals["carry"] + totals["queued"]

    return {
        "generated": now.isoformat(),
        "generated_human": now.strftime("%A %d %B %Y, %H:%M:%S %Z").strip(),
        "version": VERSION,
        "state_dir": str(worklog.state_dir()),
        "config": cfg,
        "floor_seconds": floor,
        "idle_limit_seconds": idle_limit,
        "noise_floor_seconds": 30,
        "sessions": sessions,
        "queue": queue,
        "posted": sorted(posted, key=lambda e: str(e.get("posted_at", "")), reverse=True),
        "carry": carry,
        "unmapped": unmapped_list,
        "projects": projects,
        "totals": totals,
        "health": collect_health(cfg),
        "output_path": str((output or default_output()).resolve()),
        "regen_command": regen_command(output),
        "commands": command_menu(output, pick_targets(sessions, unmapped_list, cfg)),
    }


# ---------------------------------------------------------------- rendering

CSS = """
:root{color-scheme:light;
--plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
--grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
--good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
--s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;
--plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
--grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
--s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;}}
:root[data-theme=dark]{color-scheme:dark;
--plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
--grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
--s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:20px;margin:0 0 2px;letter-spacing:-.01em}
h2{font-size:14px;margin:0 0 12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.sub{color:var(--ink2);font-size:13px;margin:0}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start;justify-content:space-between;margin-bottom:22px}
.hbtns{display:flex;gap:8px;align-items:center}
button{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--ring);
border-radius:7px;padding:6px 11px;cursor:pointer}
button:hover{border-color:var(--axis)}
button[aria-pressed=true]{background:var(--s1);border-color:var(--s1);color:#fff}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:11px;padding:16px 18px}
.cmdbar{display:flex;gap:9px;align-items:center;margin:0 0 14px;flex-wrap:wrap}
.cmdlabel{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.cmdbar select{font:inherit;font-size:13px;color:var(--ink);background:var(--surface);
border:1px solid var(--ring);border-radius:7px;padding:6px 9px;max-width:340px}
.cmdout{flex:1 1 300px;min-width:0;white-space:pre-wrap;word-break:break-all;
background:var(--surface);border:1px solid var(--ring);border-radius:7px;
padding:6px 10px;color:var(--ink2);line-height:1.45}
.cmdout.danger{color:var(--critical);border-color:var(--critical)}
.cmdbar button{white-space:nowrap}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px;margin-bottom:14px}
.tile .k{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:5px}
.tile .v{font-size:25px;line-height:1.1;letter-spacing:-.02em}
.tile .n{font-size:12px;color:var(--ink2);margin-top:4px}
.tile.crit .v{color:var(--critical)} .tile.warn .v{color:var(--warning)} .tile.good .v{color:var(--good)}
.pipe{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin:10px 0 4px}
.stage{padding:12px 14px;border:1px solid var(--ring);border-right:0;position:relative;background:var(--surface)}
.stage:first-child{border-radius:9px 0 0 9px}
.stage:last-child{border-right:1px solid var(--ring);border-radius:0 9px 9px 0}
.stage .sk{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.stage .sv{font-size:19px;margin-top:3px;font-variant-numeric:tabular-nums}
.stage .sn{font-size:11.5px;color:var(--ink2);margin-top:5px;line-height:1.35}
.bar{height:8px;border-radius:4px;background:var(--grid);overflow:hidden;display:flex;margin:12px 0 2px}
.bar i{display:block;height:100%}
.bar i+i{margin-left:2px}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--ink2);margin-top:8px}
.legend b{display:inline-block;width:9px;height:9px;border-radius:2.5px;margin-right:5px;vertical-align:-1px}
nav.tabs{display:flex;flex-wrap:wrap;gap:5px;margin:26px 0 12px;border-bottom:1px solid var(--grid)}
nav.tabs button{border:0;background:0;border-radius:7px 7px 0 0;padding:8px 13px;color:var(--ink2);
border-bottom:2px solid transparent;margin-bottom:-1px}
nav.tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--s1);font-weight:600}
nav.tabs button:hover{color:var(--ink)}
.count{display:inline-block;min-width:17px;padding:0 5px;margin-left:6px;border-radius:9px;
background:var(--grid);color:var(--ink2);font-size:11px;text-align:center}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:11px;letter-spacing:.05em;
text-transform:uppercase;padding:0 10px 8px;border-bottom:1px solid var(--grid);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--grid);vertical-align:top}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.nw{white-space:nowrap}
.dim{color:var(--muted)}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;
border:1px solid var(--ring);white-space:nowrap}
.pill.good{color:var(--good);border-color:var(--good)}
.pill.warn{color:var(--warning);border-color:var(--warning)}
.pill.crit{color:var(--critical);border-color:var(--critical)}
.pill.info{color:var(--s1);border-color:var(--s1)}
.track{display:block;width:88px;height:7px;border-radius:3.5px;background:var(--grid);overflow:hidden}
.hbar{height:7px;border-radius:3.5px;background:var(--s1);min-width:2px;display:block}
td.share{width:100px}
.err{color:var(--critical);font-size:12px;margin-top:4px;word-break:break-word}
.empty{color:var(--muted);padding:22px 2px;font-style:italic}
.note{font-size:12.5px;color:var(--ink2);margin-top:10px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;
background:var(--grid);padding:1.5px 5px;border-radius:4px}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;font-size:13px}
.kv dt{color:var(--muted)} .kv dd{margin:0}
.sec{margin-bottom:26px}
@media(max-width:760px){.pipe{grid-template-columns:1fr}
.stage{border-right:1px solid var(--ring);border-bottom:0;border-radius:0}
.stage:first-child{border-radius:9px 9px 0 0}
.stage:last-child{border-bottom:1px solid var(--ring);border-radius:0 0 9px 9px}
table,thead,tbody,th,td,tr{display:block}
thead{display:none} td{border:0;padding:3px 0}
tbody tr{border-bottom:1px solid var(--grid);padding:10px 0}
td.num{text-align:left}}
"""

JS = """
const D = window.__WORKLOG__;
const $ = (s,r)=> (r||document).querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function dur(sec){
  sec = Math.round(Number(sec)||0);
  if(!sec) return '0m';
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  if(h && m) return h+'h '+m+'m';
  if(h) return h+'h';
  if(m) return m+'m';
  return s+'s';
}
function when(iso){
  if(!iso) return '—';
  try{ const d=new Date(iso); if(isNaN(d)) return String(iso).slice(0,19).replace('T',' ');
    return d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  }catch(e){ return String(iso).slice(0,19).replace('T',' '); }
}
function ago(sec){
  sec=Math.round(sec||0);
  if(sec<60) return sec+'s ago';
  if(sec<3600) return Math.floor(sec/60)+'m ago';
  return Math.floor(sec/3600)+'h ago';
}
function statusPill(s){
  const map={posted:['good','posted'],pending:['info','pending'],
             failed:['warn','retrying'],blocked:['crit','blocked']};
  const [cls,label]=map[s]||['',''+s];
  return '<span class="pill '+cls+'">'+esc(label)+'</span>';
}

/* ---- tabs ---- */
function initTabs(){
  const btns=[...document.querySelectorAll('nav.tabs button')];
  btns.forEach(b=>b.addEventListener('click',()=>{
    btns.forEach(x=>x.setAttribute('aria-selected', x===b));
    document.querySelectorAll('[data-panel]').forEach(p=>{
      p.hidden = p.dataset.panel !== b.dataset.tab;
    });
    try{ localStorage.setItem('wl-tab', b.dataset.tab); }catch(e){}
  }));
  let want='projects';
  try{ want = localStorage.getItem('wl-tab') || 'projects'; }catch(e){}
  (btns.find(b=>b.dataset.tab===want)||btns[0]).click();
}

/* ---- theme ---- */
function initTheme(){
  const b=$('#theme'); if(!b) return;
  let t=null; try{ t=localStorage.getItem('wl-theme'); }catch(e){}
  if(t) document.documentElement.setAttribute('data-theme',t);
  b.addEventListener('click',()=>{
    const cur=document.documentElement.getAttribute('data-theme');
    const dark = cur ? cur==='dark' : matchMedia('(prefers-color-scheme:dark)').matches;
    const next = dark?'light':'dark';
    document.documentElement.setAttribute('data-theme',next);
    try{ localStorage.setItem('wl-theme',next); }catch(e){}
  });
}

/* ---- auto reload (pairs with `dashboard.py --watch`) ---- */
function initReload(){
  const b=$('#auto'); if(!b) return;
  let on=false; try{ on = localStorage.getItem('wl-auto')==='1'; }catch(e){}
  let timer=null;
  const apply=()=>{
    b.setAttribute('aria-pressed', on);
    b.textContent = on ? 'Auto-reload: on' : 'Auto-reload: off';
    if(timer) clearInterval(timer);
    if(on) timer=setInterval(()=>location.reload(), 30000);
  };
  b.addEventListener('click',()=>{ on=!on; try{localStorage.setItem('wl-auto',on?'1':'0');}catch(e){} apply(); });
  apply();
}

/* ---- command picker ----
   Same constraint as the Refresh button: a page cannot run a program, so the
   most it can honestly do is hand you the exact command with real paths. */
function selectText(el){
  try{ const r=document.createRange(); r.selectNodeContents(el);
       const s=window.getSelection(); s.removeAllRanges(); s.addRange(r);
       return true; }catch(e){ return false; }
}
function copyText(text){
  return (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
    .catch(()=> new Promise((res,rej)=>{
      try{ const ta=document.createElement('textarea');
           ta.value=text; ta.style.position='fixed'; ta.style.opacity='0';
           document.body.appendChild(ta); ta.select();
           const ok=document.execCommand('copy'); ta.remove();
           ok?res():rej(); }catch(e){ rej(); }
    }));
}
function initCommands(){
  const sel=$('#cmd'), out=$('#cmdout'), btn=$('#cmdcopy');
  if(!sel||!D.commands) return;
  const groups=[];
  D.commands.forEach(c=>{ if(!groups.includes(c.group)) groups.push(c.group); });
  groups.forEach(g=>{
    const og=document.createElement('optgroup'); og.label=g;
    D.commands.forEach((c,i)=>{
      if(c.group!==g) return;
      const o=document.createElement('option');
      o.value=String(i);
      o.textContent = c.label + (c.danger ? '  ⚠ writes to Jira' : '');
      og.appendChild(o);
    });
    sel.appendChild(og);
  });
  const show=()=>{
    const c=D.commands[Number(sel.value)];
    out.textContent=c.cmd;
    out.classList.toggle('danger', !!c.danger);
    btn.textContent = c.danger ? 'Copy (careful)' : 'Copy';
  };
  const flash=ok=>{
    const was = D.commands[Number(sel.value)].danger ? 'Copy (careful)' : 'Copy';
    if(ok){ btn.textContent='Copied'; }
    else { selectText(out); btn.textContent='Selected — press Ctrl-C'; }
    setTimeout(()=>{ btn.textContent=was; }, ok ? 1800 : 3200);
  };
  sel.addEventListener('change',()=>{ show(); copyText(out.textContent).then(()=>flash(true),()=>flash(false)); });
  btn.addEventListener('click',()=> copyText(out.textContent).then(()=>flash(true),()=>flash(false)));
  sel.value='0'; show();
}

/* ---- refresh ----
   Served over http (dashboard.py --serve): the button really regenerates.
   Opened as a file:// page: a browser cannot launch a process, so the honest
   fallback is to hand you the command. */
function initRefresh(){
  const b=$('#refresh'); if(!b) return;
  const served = location.protocol==='http:' || location.protocol==='https:';
  if(served){
    b.title='Regenerate from the current state and reload';
    b.addEventListener('click', async ()=>{
      b.disabled=true; b.textContent='Refreshing…';
      try{ await fetch('regenerate',{cache:'no-store'}); }catch(e){}
      location.reload();
    });
    return;
  }
  b.textContent='Copy refresh command';
  b.title=D.regen_command+'  (a file:// page cannot run it for you)';
  b.addEventListener('click', ()=>{
    copyText(D.regen_command).then(
      ()=>{ b.textContent='Copied — run it, then reload'; },
      ()=>{ // clipboard unavailable: fall back to the visible command bar
            const out=$('#cmdout');
            if(out){ out.textContent=D.regen_command; selectText(out); }
            b.textContent='Selected below — press Ctrl-C'; }
    ).finally(()=> setTimeout(()=>{ b.textContent='Copy refresh command'; }, 3200));
  });
}

/* ---- age of the data ---- */
function initAge(){
  const el=$('#age'); if(!el) return;
  const t0=new Date(D.generated);
  const tick=()=>{
    const s=Math.max(0,(Date.now()-t0.getTime())/1000);
    el.textContent = s<45 ? 'just now' : ago(s);
    el.className = s>600 ? 'pill warn' : 'pill';
  };
  tick(); setInterval(tick,10000);
}

/* ---- table helper ---- */
function table(cols, rows, emptyMsg){
  if(!rows.length) return '<div class="empty">'+esc(emptyMsg)+'</div>';
  let h='<table><thead><tr>';
  cols.forEach(c=> h+='<th class="'+((c.num?'num ':'')+(c.cls||'')).trim()+'">'+esc(c.label)+'</th>');
  h+='</tr></thead><tbody>';
  rows.forEach(r=>{
    h+='<tr>';
    cols.forEach(c=> h+='<td class="'+((c.num?'num ':'')+(c.cls||'')).trim()+'">'+c.cell(r)+'</td>');
    h+='</tr>';
  });
  return h+'</tbody></table>';
}

function render(){
  /* projects */
  const maxTotal = Math.max(1,...D.projects.map(p=>p.posted_seconds+p.unposted_seconds));
  $('#p-projects').innerHTML = table([
    {label:'Issue', cls:'nw', cell:p=>'<span class="mono">'+esc(p.issue_key)+'</span>'
      + (p.blocked_seconds? ' <span class="pill crit">blocked</span>':'')},
    {label:'Directory', cell:p=> p.path
      ? '<span class="mono dim">'+esc(p.path)+'</span>'
      : '<span class="dim">— no current mapping</span>'},
    {label:'Source', cls:'nw', cell:p=>'<span class="dim">'+esc(p.source)+'</span>'},
    {label:'On clock', num:1, cell:p=> p.live_seconds? dur(p.live_seconds):'<span class="dim">—</span>'},
    {label:'Carried', num:1, cell:p=> p.carry_seconds? dur(p.carry_seconds):'<span class="dim">—</span>'},
    {label:'Queued', num:1, cell:p=> p.queued_seconds? dur(p.queued_seconds):'<span class="dim">—</span>'},
    {label:'Posted', num:1, cell:p=> p.posted_seconds? dur(p.posted_seconds):'<span class="dim">—</span>'},
    {label:'Share', cls:'share', cell:p=>{
      const w = Math.round(100*(p.posted_seconds+p.unposted_seconds)/maxTotal);
      return '<span class="track" title="'+dur(p.posted_seconds+p.unposted_seconds)
        +' of '+dur(maxTotal)+'"><span class="hbar" style="width:'+Math.max(2,w)+'%"></span></span>';
    }},
  ], D.projects, 'No projects mapped yet. Run: worklog.py map <path> <ISSUE-KEY>');

  /* sessions */
  $('#p-sessions').innerHTML = table([
    {label:'Session', cell:s=>'<span class="mono">'+esc(String(s.session_id).slice(0,8))+'</span>'},
    {label:'Issue', cls:'nw', cell:s=> s.issue_key
      ? '<span class="mono">'+esc(s.issue_key)+'</span>'
      : '<span class="pill">unmapped — will not post</span>'},
    {label:'Directory', cell:s=>'<span class="mono dim">'+esc(s.cwd||'—')+'</span>'
      + (s.branch? '<div class="dim">branch '+esc(s.branch)+'</div>':'')},
    {label:'On clock', num:1, cell:s=> dur(s.active_seconds)},
    {label:'Events', num:1, cell:s=> s.events},
    {label:'Idle gaps', num:1, cell:s=> s.idle_drops
      ? s.idle_drops : '<span class="dim">0</span>'},
    {label:'Last activity', num:1, cell:s=> ago(s.idle_seconds)
      + (s.going_stale? ' <span class="pill warn">stale</span>':'')},
  ], D.sessions, 'No live sessions. Nothing is accumulating right now.');

  /* queue */
  $('#p-queue').innerHTML = table([
    {label:'Entry', cell:e=>'<span class="mono">'+esc(String(e.id).slice(0,8))+'</span>'},
    {label:'Issue', cls:'nw', cell:e=>'<span class="mono">'+esc(e.issue_key)+'</span>'},
    {label:'Duration', num:1, cell:e=> dur(e.time_spent_seconds)},
    {label:'Status', cell:e=> statusPill(e.status)
      + (e.attempts? ' <span class="dim">attempt '+e.attempts+'</span>':'')
      + (e.last_error? '<div class="err">'+esc(String(e.last_error).slice(0,220))+'</div>':'')},
    {label:'Started', num:1, cell:e=> when(e.started)},
    {label:'Next try', num:1, cell:e=> e.next_attempt? when(e.next_attempt)
      : (e.status==='blocked'? '<span class="dim">needs a human</span>':'<span class="dim">now</span>')},
  ], D.queue, 'Queue is empty — everything captured has been posted.');

  /* posted */
  $('#p-posted').innerHTML = table([
    {label:'Entry', cell:e=>'<span class="mono">'+esc(String(e.id).slice(0,8))+'</span>'},
    {label:'Issue', cls:'nw', cell:e=>'<span class="mono">'+esc(e.issue_key)+'</span>'},
    {label:'Duration', num:1, cell:e=> dur(e.time_spent_seconds)},
    {label:'Jira worklog', cell:e=>'<span class="mono">'+esc(e.jira_worklog_id||'—')+'</span>'
      + (e.adopted? ' <span class="pill info">adopted</span>':'')},
    {label:'Posted', num:1, cell:e=> when(e.posted_at)},
    {label:'Comment', cell:e=>'<span class="dim">'+esc(String(e.comment||'').slice(0,110))+'</span>'},
  ], D.posted.slice(0,60), 'Nothing has been posted yet.');

  /* unmapped */
  $('#p-unmapped').innerHTML = table([
    {label:'Directory', cell:r=>'<span class="mono">'+esc(r.path)+'</span>'},
    {label:'Sessions', num:1, cell:r=> r.sessions},
    {label:'Time not logged', num:1, cell:r=> dur(r.seconds)},
    {label:'Last seen', num:1, cell:r=> when(r.last)},
    {label:'To start tracking', cell:r=>'<code>worklog.py map '+esc(r.path)+' KEY</code>'},
  ], D.unmapped, 'No unmapped activity recorded.');

  /* health */
  const h=D.health;
  const yn = v => v ? '<span class="pill good">yes</span>' : '<span class="pill crit">no</span>';
  let rows='';
  Object.entries(h.hooks).forEach(([k,v])=> rows+='<dt>'+esc(k)+'</dt><dd>'+yn(v)+'</dd>');
  const credCls = h.credentials==='ok' ? 'good' : (h.credentials==='BOM'?'crit':'warn');
  const credTxt = h.credentials==='BOM'
    ? 'file has a BOM — json.load throws, so this reads as “missing credentials”'
    : h.credentials;
  $('#p-health').innerHTML =
    '<div class="card"><h2>Hooks installed</h2><dl class="kv">'+rows+'</dl></div>'
    + '<div class="card" style="margin-top:12px"><h2>Environment</h2><dl class="kv">'
    + '<dt>Jira credentials</dt><dd><span class="pill '+credCls+'">'+esc(credTxt)+'</span>'
    + '<div class="dim mono">'+esc(h.credentials_path)+'</div></dd>'
    + '<dt>git</dt><dd>'+yn(h.git)+' <span class="dim">branch & commit context</span></dd>'
    + '<dt>gh</dt><dd>'+yn(h.gh)+' <span class="dim">GitHub issue titles in the comment</span></dd>'
    + '<dt>Python</dt><dd>'+esc(h.python)+'</dd>'
    + '<dt>Drain lock</dt><dd>'+(h.drain_lock_held
        ? '<span class="pill warn">held</span> <span class="dim">a drain is running</span>'
        : '<span class="pill good">free</span>')+'</dd>'
    + '<dt>State directory</dt><dd class="mono">'+esc(D.state_dir)+'</dd>'
    + '</dl></div>'
    + '<div class="card" style="margin-top:12px"><h2>Settings</h2><dl class="kv">'
    + '<dt>Idle timeout</dt><dd>'+D.config.idle_timeout_minutes+' min <span class="dim">— a longer gap counts as nothing</span></dd>'
    + '<dt>Round to</dt><dd>'+D.config.round_to_minutes+' min <span class="dim">— always down, never up</span></dd>'
    + '<dt>Minimum</dt><dd>'+D.config.minimum_minutes+' min <span class="dim">— below this, time carries forward</span></dd>'
    + '<dt>Stale after</dt><dd>'+D.config.stale_session_hours+' h <span class="dim">— abandoned sessions are reclaimed</span></dd>'
    + '<dt>Git context</dt><dd>'+yn(D.config.capture_git_context)+'</dd>'
    + '</dl></div>';
}

document.addEventListener('DOMContentLoaded',()=>{ render(); initTabs(); initTheme(); initCommands(); initRefresh(); initReload(); initAge(); });
"""


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _fmt(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    if not seconds:
        return "0m"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def render(data: dict) -> str:
    t = data["totals"]
    floor = data["floor_seconds"]

    # unposted split, for the stacked bar
    parts = [("On the clock", t["live"], "var(--s3)"),
             ("Carried", t["carry"], "var(--s4)"),
             ("Queued", t["queued"], "var(--s2)")]
    total = max(1.0, sum(p[1] for p in parts))
    bar = "".join(
        f'<i style="width:{max(0.0, 100*v/total):.4f}%;background:{c}" title="{html.escape(k)}"></i>'
        for k, v, c in parts if v > 0)
    legend = "".join(
        f'<span><b style="background:{c}"></b>{html.escape(k)} — {_fmt(v)}</span>'
        for k, v, c in parts)

    def tile(key, value, note, cls=""):
        return (f'<div class="card tile {cls}"><div class="k">{html.escape(key)}</div>'
                f'<div class="v">{html.escape(value)}</div>'
                f'<div class="n">{note}</div></div>')

    tiles = "".join([
        tile("Unposted", _fmt(t["unposted"]), "not yet on the board"),
        tile("On the clock", _fmt(t["live"]), _plural(len(data["sessions"]), "live session")),
        tile("Carried", _fmt(t["carry"]), f'under the {floor//60}m floor'),
        tile("Queued", _fmt(t["queued"]), _plural(len(data["queue"]), "entry", "entries")),
        tile("Blocked", _fmt(t["blocked"]),
             "needs attention" if t["blocked_count"] else "nothing stuck",
             "crit" if t["blocked_count"] else ""),
        tile("Posted today", _fmt(t["posted_today"]), f'{_fmt(t["posted_all"])} all time', "good"),
    ])

    stages = "".join([
        f'<div class="stage"><div class="sk">1 · On the clock</div>'
        f'<div class="sv">{_fmt(t["live"])}</div>'
        f'<div class="sn">Gaps between hook events, counted only when under '
        f'{int(data["idle_limit_seconds"]//60)} min.</div></div>',
        f'<div class="stage"><div class="sk">2 · Carried</div>'
        f'<div class="sv">{_fmt(t["carry"])}</div>'
        f'<div class="sn">A session under {floor//60} min is held, not dropped, and '
        f'added to the next one on that issue.</div></div>',
        f'<div class="stage"><div class="sk">3 · Queued</div>'
        f'<div class="sv">{_fmt(t["queued"])}</div>'
        f'<div class="sn">Written at session end, floored to '
        f'{int(data["config"]["round_to_minutes"])} min. Never rounded up.</div></div>',
        f'<div class="stage"><div class="sk">4 · In Jira</div>'
        f'<div class="sv">{_fmt(t["posted_all"])}</div>'
        f'<div class="sn">Drained on the next SessionStart. One drain at a time.</div></div>',
    ])

    def tab(tid, label, n=None):
        badge = f'<span class="count">{n}</span>' if n is not None else ""
        return (f'<button role="tab" data-tab="{tid}" aria-selected="false">'
                f'{html.escape(label)}{badge}</button>')

    tabs = "".join([
        tab("projects", "Projects", len(data["projects"])),
        tab("sessions", "Live sessions", len(data["sessions"])),
        tab("queue", "Queue", len(data["queue"])),
        tab("posted", "Posted", len(data["posted"])),
        tab("unmapped", "Unmapped", len(data["unmapped"])),
        tab("health", "Health"),
    ])

    payload = json.dumps(data, default=str).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Worklog · under the hood</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1>Worklog — under the hood</h1>
    <p class="sub">Generated {html.escape(data["generated_human"])} ·
      <span id="age" class="pill">just now</span></p>
  </div>
  <div class="hbtns">
    <button id="refresh">Refresh</button>
    <button id="auto" aria-pressed="false">Auto-reload: off</button>
    <button id="theme">Theme</button>
  </div>
</header>

<div class="cmdbar">
  <label for="cmd" class="cmdlabel">Command</label>
  <select id="cmd"></select>
  <code id="cmdout" class="cmdout">—</code>
  <button id="cmdcopy">Copy</button>
</div>

<div class="tiles">{tiles}</div>

<div class="sec">
  <h2>Where your time is right now</h2>
  <div class="pipe">{stages}</div>
  <div class="bar">{bar}</div>
  <div class="legend">{legend}</div>
  <p class="note">Nothing moves to the next stage on its own except by these rules.
    Time under {data["noise_floor_seconds"]}s of activity is discarded entirely;
    everything else is either on the clock, carried, queued, or in Jira.</p>
</div>

<nav class="tabs" role="tablist">{tabs}</nav>
<div data-panel="projects" hidden><div class="card" id="p-projects"></div></div>
<div data-panel="sessions" hidden><div class="card" id="p-sessions"></div></div>
<div data-panel="queue" hidden><div class="card" id="p-queue"></div></div>
<div data-panel="posted" hidden><div class="card" id="p-posted"></div></div>
<div data-panel="unmapped" hidden>
  <div class="card" id="p-unmapped"></div>
  <p class="note">These directories accumulated time but resolve to no Jira issue, so
    nothing was sent. This is a ranked list of what is worth mapping —
    {_fmt(data["totals"]["unmapped"])} in total.</p>
</div>
<div data-panel="health" hidden><div id="p-health"></div></div>

<p class="note">Data is baked into this file — a <code>file://</code> page cannot read a
  sibling JSON. This page is <code>{html.escape(data["output_path"])}</code>; refresh it with
  <code>{html.escape(data["regen_command"])}</code>
  (add <code>--watch</code> to regenerate on a timer, and turn Auto-reload on).</p>
</div>
<script>window.__WORKLOG__ = {payload};</script>
<script>{JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------- cli

def _q(path) -> str:
    text = str(path)
    return f'"{text}"' if " " in text else text


def pick_targets(sessions: list[dict], unmapped: list[dict], cfg: dict) -> dict:
    """Choose concrete absolute paths for the commands that take one.

    A bare `.` in a copied command means "wherever your terminal happens to be",
    which is almost never the project you meant. `map . KEY` run from a home
    directory silently maps the home directory and starts attributing every
    unmapped session to that issue. So the picker resolves a real path instead:
    the project you are working in now, or the best mapping candidate.
    """
    here = str(Path.cwd())
    central = {str(Path(p).resolve()): p for p in (cfg.get("projects") or {})}
    live = [s["project_root"] for s in sessions if s.get("project_root")]

    def resolves(path: str) -> bool:
        """Ask the resolver, not the session record. A session freezes its
        issue_key at SessionStart, so one started before a mapping was added
        still reads as unmapped -- which would suggest mapping it twice."""
        try:
            return bool(worklog.resolve_issue(path, cfg)["issue_key"])
        except Exception:
            return False

    def in_central(path: str) -> bool:
        """unmap only touches the central map. Suggesting a path held by a
        .jira-project marker would produce `no mapping for ...` and exit 1."""
        try:
            return str(Path(path).resolve()) in central
        except Exception:
            return False

    unmappable = [p for p in live if not resolves(p)]         + [r["path"] for r in unmapped if not resolves(r["path"])]
    removable = [p for p in live if in_central(p)] + list(cfg.get("projects") or {})

    return {
        "map": (unmappable or [here])[0],
        "unmap": (removable or [here])[0],
        "resolve": (live or [here])[0],
    }


def command_menu(output: Path | None, targets: dict | None = None) -> list[dict]:
    """Every command worth copying, with real absolute paths so they run from
    any working directory. `danger` marks the ones that write to Jira."""
    targets = targets or {"map": ".", "unmap": ".", "resolve": "."}
    here = Path(__file__).resolve().parent
    dash, wl, post_py = here / "dashboard.py", here / "worklog.py", here / "post.py"
    target = (output or default_output()).resolve()
    out = "" if target == default_output().resolve() else f" --output {_q(target)}"
    return [
        {"group": "This page", "label": "Refresh it once",
         "cmd": f"python {_q(dash)}{out}"},
        {"group": "This page", "label": "Refresh every 30s (then turn Auto-reload on)",
         "cmd": f"python {_q(dash)}{out} --watch"},
        {"group": "This page", "label": "Serve it, so the Refresh button works",
         "cmd": f"python {_q(dash)}{out} --serve"},
        {"group": "This page", "label": "Serve it and open a browser",
         "cmd": f"python {_q(dash)}{out} --serve --open"},

        {"group": "Look at state", "label": "Live sessions, carry balances, today",
         "cmd": f"python {_q(wl)} status"},
        {"group": "Look at state", "label": "Check hooks, config and runtime",
         "cmd": f"python {_q(wl)} doctor"},
        {"group": "Look at state", "label": "Queue health and why things are blocked",
         "cmd": f"python {_q(post_py)} status"},
        {"group": "Look at state", "label": "Verify Jira credentials (read-only)",
         "cmd": f"python {_q(post_py)} check"},
        {"group": "Look at state", "label": "Explain how a folder resolves to an issue",
         "cmd": f"python {_q(wl)} resolve {_q(targets['resolve'])}"},

        {"group": "Mapping", "label": "Map this folder to an issue",
         "cmd": f"python {_q(wl)} map {_q(targets['map'])} <ISSUE-KEY>"},
        {"group": "Mapping", "label": "Stop tracking a folder",
         "cmd": f"python {_q(wl)} unmap {_q(targets['unmap'])}"},

        {"group": "Posting", "label": "Preview exactly what would be sent",
         "cmd": f"python {_q(post_py)} run --dry-run"},
        {"group": "Posting", "label": "Send it to Jira for real", "danger": True,
         "cmd": f"python {_q(post_py)} run"},
        {"group": "Posting", "label": "Unblock everything and try again",
         "cmd": f"python {_q(post_py)} retry all"},
    ]


def regen_command(output: Path | None) -> str:
    """The command that refreshes *this* file. Without --output it would rewrite
    the default one, which looks like a refresh that does nothing."""
    script = Path(__file__).resolve()
    target = (output or default_output()).resolve()
    quote = lambda p: f'"{p}"' if " " in str(p) else str(p)
    if target == default_output().resolve():
        return f"python {quote(script)}"
    return f"python {quote(script)} --output {quote(target)}"


def serve(output: Path, port: int) -> int:
    """Optional. The static file is the default and needs nothing; this exists
    only so the Refresh button can actually do something. Bound to 127.0.0.1,
    because the page carries your directory paths and issue keys."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def _send(self, code: int, body: bytes, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path in ("/", "/index.html"):
                # regenerate on every load, so a plain browser refresh is enough
                body = render(collect(output)).encode("utf-8")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(body)
                return self._send(200, body)
            if path == "/regenerate":
                build(output)
                return self._send(200, b'{"ok":true}', "application/json")
            return self._send(404, b"not found", "text/plain; charset=utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"serving {url}   (regenerates on every load; Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print("stopped")
    return 0


def default_output() -> Path:
    return worklog.state_dir() / "dashboard" / "index.html"


def build(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(collect(output)), encoding="utf-8")
    return output


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0

    output = default_output()
    if "--output" in argv:
        try:
            output = Path(argv[argv.index("--output") + 1]).expanduser()
        except Exception:
            print("--output needs a path", file=sys.stderr)
            return 2

    interval = None
    if "--watch" in argv:
        interval = 30
        idx = argv.index("--watch") + 1
        if idx < len(argv) and not argv[idx].startswith("-"):
            try:
                interval = max(2, int(argv[idx]))
            except ValueError:
                pass

    if "--serve" in argv:
        port = 8777
        idx = argv.index("--serve") + 1
        if idx < len(argv) and not argv[idx].startswith("-"):
            try:
                port = int(argv[idx])
            except ValueError:
                pass
        build(output)
        if "--open" in argv:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        return serve(output, port)

    build(output)
    url = output.resolve().as_uri()
    print(f"wrote {output}")
    print(f"open  {url}")
    if "--open" in argv:
        webbrowser.open(url)

    if interval:
        print(f"watching -- regenerating every {interval}s, Ctrl-C to stop")
        try:
            while True:
                time.sleep(interval)
                build(output)
                print(f"  {worklog.now():%H:%M:%S} regenerated", flush=True)
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
