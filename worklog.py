#!/usr/bin/env python3
"""
worklog-automator -- session time capture for Claude Code.

Implements PROJ-66: resolve a project directory to a Jira issue key, detect
session boundaries, accumulate active time, and write a rounded worklog record
to a local queue. Nothing is sent to Jira here -- that is PROJ-67's job, which
consumes queue.jsonl.

Design notes that matter:

  * Wall-clock session length is the wrong number. A session left open overnight
    would log 14h. We accumulate ACTIVE time instead: the gap between successive
    hook events is counted only if it is under `idle_timeout_minutes`.
  * SessionEnd hooks share a short budget in Claude Code, so finalize() does
    local file I/O only -- no network, no `gh`, no git calls that can hang.
  * SessionEnd does not fire if the terminal is killed. SessionStart runs a
    sweeper that finalizes orphaned sessions from previous runs.
  * Sessions shorter than `minimum_minutes` are not dropped, they accumulate in
    a per-issue carry balance so five 3-minute sessions become one 15-minute
    worklog rather than noise or lost time.
  * Hooks must never break the session. Every entry point swallows exceptions,
    logs to worklog.log, and exits 0. Nothing is printed to stdout, because
    Claude Code injects SessionStart/UserPromptSubmit stdout into context.

Usage:
    worklog.py install                install hooks into ~/.claude/settings.json
    worklog.py hook <event>           session-start | activity | session-end
    worklog.py status                 active sessions, carry balances, today
    worklog.py queue [--json]         pending worklogs
    worklog.py resolve [path]         explain issue-key resolution for a path
    worklog.py map <path> <KEY>       add a directory -> issue key mapping
    worklog.py unmap <path>           remove a directory -> issue key mapping
    worklog.py doctor                 check configuration
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "0.2.0"
DEFAULT_STATE_DIR = Path.home() / ".claude" / "worklog"
PROJECT_MARKER = ".jira-project"
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

DEFAULT_CONFIG = {
    "idle_timeout_minutes": 15,
    "round_to_minutes": 5,
    "minimum_minutes": 5,
    "stale_session_hours": 12,
    "capture_git_context": True,
    "projects": {},
}


# ---------------------------------------------------------------- paths / io

def state_dir() -> Path:
    override = os.environ.get("CLAUDE_WORKLOG_DIR")
    return Path(override).expanduser() if override else DEFAULT_STATE_DIR


def sessions_dir() -> Path:
    return state_dir() / "sessions"


def config_path() -> Path:
    return state_dir() / "config.json"


def queue_path() -> Path:
    return state_dir() / "queue.jsonl"


def unmapped_path() -> Path:
    return state_dir() / "unmapped.jsonl"


def carry_path() -> Path:
    return state_dir() / "carry.json"


def log_path() -> Path:
    return state_dir() / "worklog.log"


def ensure_dirs() -> None:
    sessions_dir().mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    try:
        ensure_dirs()
        with log_path().open("a", encoding="utf-8") as fh:
            fh.write(f"{now().isoformat()} {message}\n")
    except Exception:
        pass


def read_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def append_jsonl(path: Path, record: dict) -> None:
    """Locked because the PROJ-67 writer rewrites these files in place to
    update statuses, and an unlocked append during a rewrite loses the line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with FileLock(path):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


class FileLock:
    """Minimal cross-platform lock for the shared state files.

    `stale_after` is how long a lock may exist before it is assumed to belong to
    a killed process and broken. Keep it comfortably longer than the work the
    lock protects: a lock broken while its holder is still working is worse than
    no lock, because both processes then believe they hold it.
    """

    def __init__(self, target: Path, timeout: float = 5.0, stale_after: float = 30.0):
        self.lock = target.with_suffix(target.suffix + ".lock")
        self.timeout = timeout
        self.stale_after = stale_after
        self.fd = None

    def __enter__(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:  # break a lock abandoned by a killed process
                    if time.time() - self.lock.stat().st_mtime > self.stale_after:
                        self.lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not acquire {self.lock}")
                time.sleep(0.05)

    def __exit__(self, *_exc):
        if self.fd is not None:
            os.close(self.fd)
        self.lock.unlink(missing_ok=True)
        return False


# ---------------------------------------------------------------- time utils

def now() -> datetime:
    return datetime.now().astimezone()


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def jira_started(dt: datetime) -> str:
    """Jira's worklog API wants 2026-09-03T14:05:00.000+0530 -- no colon in the
    offset, milliseconds required."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}" + dt.strftime("%z")


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json(config_path(), {}))
    cfg.setdefault("projects", {})
    return cfg


def save_config(cfg: dict) -> None:
    write_json_atomic(config_path(), cfg)


# ------------------------------------------------------- issue key resolution

def parse_marker_file(path: Path) -> dict:
    """Accepts a bare key (`PROJ-22`) or `key: value` lines."""
    result: dict = {}
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return result
    if not text:
        return result
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip().lower()] = value.strip()
        elif ISSUE_KEY_RE.match(line):
            result["issue_key"] = line
    return result


def resolve_issue(cwd: str, cfg: dict) -> dict:
    """Resolution order: env override, .jira-project walking up, central map.

    Returns {issue_key, source, github, project_root} with issue_key possibly None.
    """
    env_key = os.environ.get("CLAUDE_WORKLOG_ISSUE", "").strip()
    if ISSUE_KEY_RE.match(env_key):
        return {"issue_key": env_key, "source": "env:CLAUDE_WORKLOG_ISSUE",
                "github": None, "project_root": cwd}

    try:
        start = Path(cwd).expanduser().resolve()
    except Exception:
        start = Path(cwd)

    for directory in [start, *start.parents]:
        marker = directory / PROJECT_MARKER
        if marker.is_file():
            data = parse_marker_file(marker)
            key = data.get("issue_key") or data.get("issue") or data.get("key")
            if key and ISSUE_KEY_RE.match(key):
                return {"issue_key": key, "source": str(marker),
                        "github": data.get("github"), "project_root": str(directory)}

    # Central map: longest matching path prefix wins.
    best_path, best_entry = None, None
    for mapped, entry in (cfg.get("projects") or {}).items():
        try:
            mapped_resolved = Path(mapped).expanduser().resolve()
        except Exception:
            continue
        if start == mapped_resolved or mapped_resolved in start.parents:
            if best_path is None or len(str(mapped_resolved)) > len(str(best_path)):
                best_path, best_entry = mapped_resolved, entry
    if best_entry:
        entry = {"issue_key": best_entry} if isinstance(best_entry, str) else dict(best_entry)
        key = entry.get("issue_key")
        if key and ISSUE_KEY_RE.match(key):
            return {"issue_key": key, "source": f"config.json:{best_path}",
                    "github": entry.get("github"), "project_root": str(best_path)}

    return {"issue_key": None, "source": "unresolved", "github": None,
            "project_root": str(start)}


# ---------------------------------------------------------------- git context

def git(args: list[str], cwd: str, timeout: float = 2.0) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


GH_ISSUE_PATTERNS = [
    re.compile(r"(?:^|/)(?:gh-|issue[-/]?|#)(\d{1,6})(?:[-_/]|$)", re.IGNORECASE),
    re.compile(r"(?:^|/)(\d{1,6})[-_]"),
]


def github_issue_from_branch(branch: str | None) -> int | None:
    if not branch:
        return None
    for pattern in GH_ISSUE_PATTERNS:
        match = pattern.search(branch)
        if match:
            return int(match.group(1))
    return None


def github_slug(cwd: str) -> str | None:
    url = git(["remote", "get-url", "origin"], cwd)
    if not url:
        return None
    match = re.search(r"github\.com[:/](?P<slug>[\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    return match.group("slug") if match else None


def collect_git_context(cwd: str, since: datetime) -> dict:
    """Best effort, short timeouts. Called at session start and end only."""
    context: dict = {}
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if branch and branch != "HEAD":
        context["branch"] = branch
    slug = github_slug(cwd)
    if slug:
        context["github_repo"] = slug
    commits = git(["log", f"--since={since.isoformat()}", "--pretty=%h %s", "-n", "10"], cwd)
    if commits:
        context["commits"] = [line for line in commits.splitlines() if line.strip()]
    return context


# ---------------------------------------------------------------- sessions

def session_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "unknown")[:120]
    return sessions_dir() / f"{safe}.json"


def read_hook_input() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def start_session(payload: dict, cfg: dict) -> None:
    ensure_dirs()
    sweep_stale(cfg)

    session_id = payload.get("session_id") or f"anon-{uuid.uuid4().hex[:8]}"
    cwd = payload.get("cwd") or os.getcwd()
    path = session_file(session_id)

    if path.exists():  # resume / fork: keep accumulated time, just re-arm the clock
        record = read_json(path, {})
        record["last_activity"] = now().isoformat()
        record["resumed"] = record.get("resumed", 0) + 1
        record["cwd"] = cwd
        write_json_atomic(path, record)
        return

    resolved = resolve_issue(cwd, cfg)
    started = now()
    record = {
        "session_id": session_id,
        "cwd": cwd,
        "project_root": resolved["project_root"],
        "issue_key": resolved["issue_key"],
        "issue_source": resolved["source"],
        "github_repo": resolved.get("github"),
        "started": started.isoformat(),
        "last_activity": started.isoformat(),
        "active_seconds": 0.0,
        "events": 0,
        "idle_drops": 0,
    }
    if cfg.get("capture_git_context", True):
        record["git_start"] = collect_git_context(cwd, started)
        record.setdefault("github_repo", None)
        record["github_repo"] = record["github_repo"] or record["git_start"].get("github_repo")
    write_json_atomic(path, record)


def heartbeat(payload: dict, cfg: dict) -> None:
    session_id = payload.get("session_id")
    if not session_id:
        return
    path = session_file(session_id)
    if not path.exists():
        # Hooks installed mid-session, or SessionStart never fired.
        start_session(payload, cfg)
        return
    record = read_json(path, None)
    if not record:
        return

    current = now()
    try:
        last = parse_ts(record["last_activity"])
    except Exception:
        last = current
    gap = (current - last).total_seconds()
    idle_limit = float(cfg["idle_timeout_minutes"]) * 60

    if 0 <= gap <= idle_limit:
        record["active_seconds"] = float(record.get("active_seconds", 0)) + gap
    elif gap > idle_limit:
        record["idle_drops"] = record.get("idle_drops", 0) + 1

    record["last_activity"] = current.isoformat()
    record["events"] = record.get("events", 0) + 1
    write_json_atomic(path, record)


def finalize_record(record: dict, cfg: dict, reason: str) -> dict | None:
    """Close out one session: apply the final idle rule, fold into the carry
    balance, and emit a queue entry when the balance clears the minimum."""
    current = now()
    try:
        last = parse_ts(record["last_activity"])
    except Exception:
        last = current
    gap = (current - last).total_seconds()
    idle_limit = float(cfg["idle_timeout_minutes"]) * 60
    active = float(record.get("active_seconds", 0))
    if 0 <= gap <= idle_limit:
        active += gap

    issue_key = record.get("issue_key")
    cwd = record.get("cwd") or os.getcwd()
    try:
        started = parse_ts(record["started"])
    except Exception:
        started = current

    if active < 30:  # below the noise floor, not worth recording at all
        return None

    if not issue_key:
        append_jsonl(unmapped_path(), {
            "cwd": cwd,
            "project_root": record.get("project_root"),
            "active_seconds": round(active, 1),
            "started": started.isoformat(),
            "ended": current.isoformat(),
            "session_id": record.get("session_id"),
            "reason": reason,
            "hint": f"run: worklog.py map {record.get('project_root')} <ISSUE-KEY>",
        })
        return None

    git_end = {}
    if cfg.get("capture_git_context", True) and reason != "stale":
        git_end = collect_git_context(cwd, started)

    round_to = max(1, int(cfg["round_to_minutes"])) * 60
    minimum = max(0, int(cfg["minimum_minutes"])) * 60

    with FileLock(carry_path()):
        carry = read_json(carry_path(), {})
        balance = float(carry.get(issue_key, {}).get("seconds", 0)) + active

        # The minimum is judged on real accumulated time, never on the rounded
        # figure -- otherwise a 3-minute session rounds up to 5 and defeats the
        # whole point of having a minimum.
        if balance < max(minimum, round_to):
            carry[issue_key] = {"seconds": round(balance, 1),
                                "updated": current.isoformat()}
            write_json_atomic(carry_path(), carry)
            return None

        # Floor, not round-to-nearest: rounding up invents time that was never
        # worked. The remainder carries forward, so nothing is lost either.
        rounded = int(balance // round_to) * round_to

        leftover = max(0.0, balance - rounded)
        carried = max(0.0, balance - active)
        if leftover >= 30:  # sub-30s residue is rounding noise, not time
            carry[issue_key] = {"seconds": round(leftover, 1),
                                "updated": current.isoformat()}
        else:
            carry.pop(issue_key, None)
        write_json_atomic(carry_path(), carry)

    branch = git_end.get("branch") or (record.get("git_start") or {}).get("branch")
    gh_repo = (record.get("github_repo") or git_end.get("github_repo")
               or (record.get("git_start") or {}).get("github_repo"))
    parts = {
        "repo": Path(record.get("project_root") or cwd).name,
        "branch": branch,
        "github_repo": gh_repo,
        "github_issue": github_issue_from_branch(branch),
        "commits": git_end.get("commits", []),
        "carried_seconds": round(carried, 1),
        "session_seconds": round(active, 1),
        "idle_drops": record.get("idle_drops", 0),
        "end_reason": reason,
    }
    entry = {
        "id": uuid.uuid4().hex,
        "issue_key": issue_key,
        "time_spent_seconds": int(rounded),
        "started": jira_started(started),
        "comment": render_comment(parts),
        "comment_parts": parts,
        "session_id": record.get("session_id"),
        "created": current.isoformat(),
        "status": "pending",
    }
    append_jsonl(queue_path(), entry)
    return entry


def render_comment(parts: dict, github_title: str | None = None) -> str:
    """PROJ-62 wants the GitHub issue number and title in the worklog comment.

    The title needs a network call, which SessionEnd cannot afford, so the
    number is captured here and PROJ-67 passes the resolved title back in
    when it posts. Keep this the single source of truth for the format.
    """
    bits = []
    issue = parts.get("github_issue")
    repo = parts.get("github_repo") or parts.get("repo")
    if issue:
        label = f"{repo}#{issue}" if repo else f"#{issue}"
        bits.append(f"{label}: {github_title}" if github_title else label)
    elif parts.get("repo"):
        bits.append(str(parts["repo"]))
    if parts.get("branch") and not issue:
        bits.append(f"branch {parts['branch']}")
    commits = parts.get("commits") or []
    if commits:
        bits.append(f"{len(commits)} commit{'s' if len(commits) > 1 else ''}: "
                    + "; ".join(commits[:3]))
    bits.append("logged automatically from Claude Code")
    return " | ".join(bits)


def end_session(payload: dict, cfg: dict) -> None:
    session_id = payload.get("session_id")
    if not session_id:
        return
    path = session_file(session_id)
    record = read_json(path, None)
    if not record:
        return
    reason = payload.get("reason") or payload.get("source") or "session_end"
    try:
        finalize_record(record, cfg, reason)
    finally:
        path.unlink(missing_ok=True)


def sweep_stale(cfg: dict) -> int:
    """SessionEnd does not fire on a killed terminal. Reclaim orphans."""
    ensure_dirs()
    cutoff = now() - timedelta(hours=float(cfg.get("stale_session_hours", 12)))
    swept = 0
    for path in sessions_dir().glob("*.json"):
        record = read_json(path, None)
        if not record:
            path.unlink(missing_ok=True)
            continue
        try:
            last = parse_ts(record["last_activity"])
        except Exception:
            last = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        if last < cutoff:
            try:
                finalize_record(record, cfg, "stale")
            except Exception as exc:
                log(f"sweep failed for {path.name}: {exc}")
            path.unlink(missing_ok=True)
            swept += 1
    return swept


# ---------------------------------------------------------------- commands

def cmd_hook(argv: list[str]) -> int:
    event = argv[0] if argv else ""
    payload = read_hook_input()
    cfg = load_config()
    if event == "session-start":
        start_session(payload, cfg)
    elif event == "activity":
        heartbeat(payload, cfg)
    elif event == "session-end":
        end_session(payload, cfg)
    else:
        log(f"unknown hook event: {event!r}")
    return 0


def read_queue() -> list[dict]:
    if not queue_path().exists():
        return []
    entries = []
    for line in queue_path().read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries


def cmd_status(argv: list[str]) -> int:
    cfg = load_config()
    ensure_dirs()
    print(f"worklog-automator {VERSION}   state: {state_dir()}")

    live = sorted(sessions_dir().glob("*.json"))
    print(f"\nActive sessions ({len(live)}):")
    if not live:
        print("  none")
    for path in live:
        record = read_json(path, {})
        key = record.get("issue_key") or "UNMAPPED"
        active = human_duration(record.get("active_seconds", 0))
        print(f"  {key:<14} {active:>7} active  {Path(record.get('cwd','?')).name}"
              f"  (last seen {record.get('last_activity','?')[11:19]})")

    carry = read_json(carry_path(), {})
    print(f"\nCarry balances (below the {cfg['minimum_minutes']}m minimum):")
    if not carry:
        print("  none")
    for key, value in sorted(carry.items()):
        print(f"  {key:<14} {human_duration(value.get('seconds', 0)):>7}")

    entries = read_queue()
    pending = [e for e in entries if e.get("status") == "pending"]
    today = now().date().isoformat()
    todays = [e for e in entries if e.get("created", "").startswith(today)]
    total = sum(e["time_spent_seconds"] for e in todays)
    print(f"\nQueue: {len(pending)} pending of {len(entries)} total")
    print(f"Logged today: {human_duration(total)} across {len(todays)} worklog(s)")

    if unmapped_path().exists():
        count = sum(1 for line in unmapped_path().read_text(encoding="utf-8").splitlines() if line.strip())
        if count:
            print(f"\n{count} unmapped session(s) -- see {unmapped_path()}")
    return 0


def cmd_queue(argv: list[str]) -> int:
    entries = [e for e in read_queue() if e.get("status") == "pending"]
    if "--json" in argv:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("Queue is empty.")
        return 0
    for entry in entries:
        print(f"{entry['issue_key']:<14} {human_duration(entry['time_spent_seconds']):>7}"
              f"  {entry['started']}")
        print(f"  {entry['comment']}")
    return 0


def cmd_resolve(argv: list[str]) -> int:
    cwd = argv[0] if argv else os.getcwd()
    cfg = load_config()
    result = resolve_issue(cwd, cfg)
    print(f"path        {cwd}")
    print(f"issue key   {result['issue_key'] or '(unresolved)'}")
    print(f"source      {result['source']}")
    print(f"root        {result['project_root']}")
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if branch:
        print(f"branch      {branch}")
        issue = github_issue_from_branch(branch)
        print(f"gh issue    {issue if issue else '(none detected in branch name)'}")
    if not result["issue_key"]:
        print(f"\nTo map it:  worklog.py map {result['project_root']} PROJ-00")
        return 1
    return 0


def cmd_map(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: worklog.py map <path> <ISSUE-KEY> [github-slug]", file=sys.stderr)
        return 2
    path, key = argv[0], argv[1].upper()
    if not ISSUE_KEY_RE.match(key):
        print(f"'{key}' does not look like a Jira key (expected e.g. PROJ-22)", file=sys.stderr)
        return 2
    resolved = str(Path(path).expanduser().resolve())
    cfg = load_config()
    entry = {"issue_key": key}
    if len(argv) > 2:
        entry["github"] = argv[2]
    cfg["projects"][resolved] = entry
    save_config(cfg)
    print(f"mapped {resolved} -> {key}")
    return 0


def mapped_key(entry) -> str | None:
    """Map values are {"issue_key": ...} but a hand-edited config may hold a
    bare string, which resolve_issue already tolerates."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("issue_key")
    return None


def cmd_unmap(argv: list[str]) -> int:
    if not argv:
        print("usage: worklog.py unmap <path>", file=sys.stderr)
        return 2
    resolved = str(Path(argv[0]).expanduser().resolve())
    cfg = load_config()
    entry = cfg["projects"].pop(resolved, None)
    if entry is None:
        print(f"no mapping for {resolved}", file=sys.stderr)
        return 1
    save_config(cfg)
    print(f"unmapped {resolved} (was {mapped_key(entry)})")

    # Removing the central mapping is not enough if a marker file still wins:
    # resolution checks .jira-project before the map, walking up from the path.
    start = Path(resolved)
    for directory in [start, *start.parents]:
        marker = directory / PROJECT_MARKER
        if marker.is_file():
            data = parse_marker_file(marker)
            key = data.get("issue_key") or data.get("issue") or data.get("key")
            if key and ISSUE_KEY_RE.match(key):
                print(f"NOTE: {marker} still maps this to {key}; delete it to stop tracking.")
            break
    return 0


def hook_config(script: str) -> dict:
    python = sys.executable or "python3"

    def handler(event: str, **extra) -> dict:
        return {"type": "command", "command": python,
                "args": [script, "hook", event], **extra}

    return {
        "SessionStart": [{"hooks": [handler("session-start", **{"async": True})]}],
        "UserPromptSubmit": [{"hooks": [handler("activity", **{"async": True})]}],
        "PostToolUse": [{"matcher": "*", "hooks": [handler("activity", **{"async": True})]}],
        "Stop": [{"hooks": [handler("activity", **{"async": True})]}],
        # Not async: this one must finish. timeout raises Claude Code's
        # 1.5s SessionEnd budget so the finalize write always lands.
        "SessionEnd": [{"hooks": [handler("session-end", timeout=10)]}],
    }


def is_ours(handler: dict) -> bool:
    blob = json.dumps(handler)
    return "worklog.py" in blob


def cmd_install(argv: list[str]) -> int:
    script = str(Path(__file__).resolve())
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = read_json(settings, {})
    hooks = data.setdefault("hooks", {})

    for event, groups in hook_config(script).items():
        existing = hooks.get(event, [])
        cleaned = []
        for group in existing:  # drop any previous install of ours
            kept = [h for h in group.get("hooks", []) if not is_ours(h)]
            if kept:
                group = dict(group, hooks=kept)
                cleaned.append(group)
        hooks[event] = cleaned + groups

    write_json_atomic(settings, data)

    ensure_dirs()
    if not config_path().exists():
        save_config(dict(DEFAULT_CONFIG))

    print(f"Installed hooks into {settings}")
    print(f"Config:  {config_path()}")
    print(f"State:   {state_dir()}")
    print("\nNext: map your repos, e.g.")
    print("  worklog.py map ~/code/example-app PROJ-22 userx/example-app")
    print("or drop a .jira-project file at each repo root containing the key.")
    print("\nRestart Claude Code, then check with: worklog.py doctor")
    return 0


def cmd_doctor(argv: list[str]) -> int:
    ok = True
    cfg = load_config()
    settings = Path.home() / ".claude" / "settings.json"
    data = read_json(settings, {})
    events = data.get("hooks", {})
    wanted = ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"]

    print("hooks")
    for event in wanted:
        found = any(is_ours(h) for g in events.get(event, []) for h in g.get("hooks", []))
        print(f"  {'ok  ' if found else 'MISS'} {event}")
        ok = ok and found

    print("\nconfig")
    print(f"  {'ok  ' if config_path().exists() else 'MISS'} {config_path()}")
    projects = cfg.get("projects") or {}
    print(f"  {'ok  ' if projects else 'warn'} {len(projects)} mapped project(s)")
    for path, entry in projects.items():
        key = entry if isinstance(entry, str) else entry.get("issue_key")
        exists = Path(path).expanduser().exists()
        print(f"       {'  ' if exists else '! '}{key:<14} {path}"
              f"{'' if exists else '   (path not found)'}")

    print("\nruntime")
    print(f"  ok   python {sys.version.split()[0]} at {sys.executable}")
    print(f"  {'ok  ' if state_dir().exists() else 'warn'} state dir {state_dir()}")
    git_ok = git(["--version"], os.getcwd()) is not None
    print(f"  {'ok  ' if git_ok else 'warn'} git available (branch/commit context)")

    swept = sweep_stale(cfg)
    if swept:
        print(f"\nswept {swept} stale session(s)")
    return 0 if ok else 1


COMMANDS = {
    "hook": cmd_hook,
    "install": cmd_install,
    "status": cmd_status,
    "queue": cmd_queue,
    "resolve": cmd_resolve,
    "map": cmd_map,
    "unmap": cmd_unmap,
    "doctor": cmd_doctor,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if argv[0] in ("-V", "--version"):
        print(VERSION)
        return 0
    handler = COMMANDS.get(argv[0])
    if not handler:
        print(f"unknown command: {argv[0]}", file=sys.stderr)
        return 2
    return handler(argv[1:])


if __name__ == "__main__":
    is_hook = len(sys.argv) > 1 and sys.argv[1] == "hook"
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:  # a hook must never take the session down
        log(f"error in {' '.join(sys.argv[1:])}: {exc!r}")
        if not is_hook:
            print(f"worklog: {exc}", file=sys.stderr)
        sys.exit(0 if is_hook else 1)
