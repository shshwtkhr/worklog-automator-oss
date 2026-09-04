#!/usr/bin/env python3
"""
post.py -- drain the local worklog queue into Jira.

Implements PROJ-67. Consumes queue.jsonl written by worklog.py (PROJ-66)
and POSTs each entry to Jira's worklog API, which is what Planyway reads:
Planyway has no API and no custom fields of its own, it renders native Jira
time tracking. `adjustEstimate=auto` is what moves the remaining estimate and
therefore what makes the Planyway timeline move.

What this handles that a naive POST loop does not:

  * Offline and failure. Nothing leaves the queue until Jira confirms it.
    Transient failures back off exponentially; permanent ones (bad auth, issue
    gone, malformed payload) are parked as `blocked` and surfaced, never
    silently retried forever and never dropped.
  * Double-posting. A request can succeed and still fail to return -- a timeout
    on the client side says nothing about the server side. Before retrying any
    entry that has been attempted before, we look for a matching worklog
    already on the issue and adopt it instead of creating a second one.
  * GitHub issue titles. PROJ-62 wants the number *and* title in the comment.
    SessionEnd cannot afford the lookup, so it happens here, cached, and the
    format itself stays owned by worklog.render_comment().

Auth (Jira Cloud, basic auth with an API token from
id.atlassian.com/manage-profile/security/api-tokens):

    export JIRA_BASE_URL=https://your-site.atlassian.net
    export JIRA_EMAIL=you@example.com
    export JIRA_API_TOKEN=...

or the same three keys in ~/.claude/worklog/credentials.json, chmod 600.

Usage:
    post.py run [--dry-run] [--limit N] [--force]
    post.py install-hook          drain on every Claude Code SessionStart
    post.py status                queue health
    post.py retry <id|all>        clear backoff / unblock and try again
    post.py check                 verify credentials and permissions
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worklog  # noqa: E402  -- shares state paths, locking and comment format

VERSION = "0.2.0"

# Transient: worth retrying. Everything else is a bug or a permission problem
# that retrying will not fix.
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}
BACKOFF_SECONDS = [60, 300, 900, 3600, 3 * 3600, 6 * 3600]
MAX_ATTEMPTS = 24


def credentials_path() -> Path:
    return worklog.state_dir() / "credentials.json"


def posted_path() -> Path:
    return worklog.state_dir() / "posted.jsonl"


def gh_cache_path() -> Path:
    return worklog.state_dir() / "github-titles.json"


# ---------------------------------------------------------------- credentials

def load_credentials() -> dict:
    creds = {
        "base_url": os.environ.get("JIRA_BASE_URL", ""),
        "email": os.environ.get("JIRA_EMAIL", ""),
        "api_token": os.environ.get("JIRA_API_TOKEN", ""),
    }
    path = credentials_path()
    if path.exists():
        if os.name == "posix" and (path.stat().st_mode & 0o077):
            worklog.log(f"WARNING: {path} is group/world readable; chmod 600 it")
        stored = worklog.read_json(path, {})
        for key in creds:
            if not creds[key]:
                creds[key] = stored.get(key, "")
    creds["base_url"] = creds["base_url"].rstrip("/")
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise RuntimeError(
            "missing Jira credentials: " + ", ".join(missing)
            + f"\nSet JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN, or fill {path}"
        )
    return creds


class JiraError(Exception):
    def __init__(self, message: str, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


class Jira:
    def __init__(self, creds: dict, timeout: float = 20.0):
        self.base = creds["base_url"]
        self.timeout = timeout
        raw = f"{creds['email']}:{creds['api_token']}".encode()
        self.auth = "Basic " + base64.b64encode(raw).decode()

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self.auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"worklog-automator/{VERSION}",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode() or "{}"
                return json.loads(payload) if payload.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:400]
            except Exception:
                pass
            raise JiraError(f"HTTP {exc.code} on {method} {path}: {detail}",
                            status=exc.code,
                            transient=exc.code in RETRY_STATUS) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # No response at all -- offline, DNS, TLS, timeout. Always retryable,
            # but the request may still have landed, hence the duplicate check.
            raise JiraError(f"network failure on {method} {path}: {exc}",
                            status=None, transient=True) from None

    def myself(self) -> dict:
        return self.request("GET", "/rest/api/3/myself")

    def add_worklog(self, issue_key: str, seconds: int, started: str,
                    comment_adf: dict) -> dict:
        return self.request(
            "POST", f"/rest/api/3/issue/{issue_key}/worklog",
            params={"adjustEstimate": "auto", "notifyUsers": "false"},
            body={"timeSpentSeconds": seconds, "started": started,
                  "comment": comment_adf},
        )

    def worklogs_since(self, issue_key: str, since_ms: int) -> list[dict]:
        result = self.request("GET", f"/rest/api/3/issue/{issue_key}/worklog",
                              params={"startedAfter": since_ms, "maxResults": 100})
        return result.get("worklogs", [])


# ---------------------------------------------------------------- formatting

def adf(text: str) -> dict:
    """Jira REST v3 requires the worklog comment as an Atlassian Document, not
    a plain string. v2 takes a string; this targets v3."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": text}]}]}


def gh_issue_title(repo: str | None, number: int | None) -> str | None:
    """Resolve via the gh CLI, cached forever -- issue titles rarely change and
    a stale title is better than a failed post."""
    if not repo or not number:
        return None
    cache_key = f"{repo}#{number}"
    cache = worklog.read_json(gh_cache_path(), {})
    if cache_key in cache:
        return cache[cache_key] or None
    title = None
    try:
        out = subprocess.run(
            ["gh", "issue", "view", str(number), "--repo", repo,
             "--json", "title", "-q", ".title"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            title = out.stdout.strip()
    except Exception:
        return None  # gh missing or not authed: skip, do not poison the cache
    cache[cache_key] = title or ""
    worklog.write_json_atomic(gh_cache_path(), cache)
    return title


def build_comment(entry: dict) -> str:
    parts = entry.get("comment_parts") or {}
    title = gh_issue_title(parts.get("github_repo"), parts.get("github_issue"))
    if title:
        return worklog.render_comment(parts, github_title=title)
    return entry.get("comment") or worklog.render_comment(parts)


# ---------------------------------------------------------------- queue state

def load_queue() -> list[dict]:
    path = worklog.queue_path()
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            worklog.log(f"skipping unparseable queue line: {line[:120]}")
    return entries


def save_queue(entries: list[dict]) -> None:
    """Rewrite in place, holding the same lock worklog.py takes when appending,
    so a session ending mid-drain cannot lose its worklog."""
    path = worklog.queue_path()
    with worklog.FileLock(path):
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        os.replace(tmp, path)


def archive(entries: list[dict]) -> None:
    for entry in entries:
        worklog.append_jsonl(posted_path(), entry)


def due(entry: dict, force: bool) -> bool:
    if entry.get("status") not in ("pending", "failed"):
        return False
    if force:
        return True
    nxt = entry.get("next_attempt")
    if not nxt:
        return True
    try:
        return worklog.parse_ts(nxt) <= worklog.now()
    except Exception:
        return True


def schedule_retry(entry: dict, error: str) -> None:
    attempts = int(entry.get("attempts", 0)) + 1
    entry["attempts"] = attempts
    entry["last_error"] = error[:500]
    entry["last_attempt"] = worklog.now().isoformat()
    if attempts >= MAX_ATTEMPTS:
        entry["status"] = "blocked"
        entry.pop("next_attempt", None)
        return
    delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
    entry["status"] = "failed"
    entry["next_attempt"] = (worklog.now() + timedelta(seconds=delay)).isoformat()


def record_failure(entry: dict, exc: JiraError, message: str) -> None:
    """Park one entry after a failed call: back off if the failure could clear
    on its own, block it for a human if it cannot."""
    if exc.transient:
        schedule_retry(entry, message)
    else:
        entry.update(status="blocked", last_error=message[:500],
                     attempts=int(entry.get("attempts", 0)) + 1,
                     last_attempt=worklog.now().isoformat())
        entry.pop("next_attempt", None)


def started_to_ms(started: str) -> int:
    """'2026-09-03T14:05:00.000+0530' -> epoch millis."""
    normalized = started
    if len(started) > 5 and started[-5] in "+-" and ":" not in started[-5:]:
        normalized = started[:-5] + started[-5:-2] + ":" + started[-2:]
    return int(worklog.parse_ts(normalized).timestamp() * 1000)


def find_existing(jira: Jira, entry: dict, account_id: str | None) -> dict | None:
    """Has this worklog already landed? Matches on issue, author, exact start
    second and exact duration -- the tuple worklog.py guarantees is unique per
    entry, since two sessions cannot start in the same second on one issue.

    Returns the match, or None for a definite miss. A failed *lookup* raises
    instead of returning None: "I checked and it is not there" and "I could not
    check" must not collapse into the same answer, or a rate-limited duplicate
    check silently becomes the duplicate it exists to prevent.
    """
    try:
        target_ms = started_to_ms(entry["started"])
    except Exception:
        # An unparseable start time can never match, and can never start to
        # match later. Jira will reject the same value on the POST below, which
        # blocks the entry for a human -- the right outcome for a malformed row.
        return None
    existing = jira.worklogs_since(entry["issue_key"], target_ms - 2000)
    for candidate in existing:
        if int(candidate.get("timeSpentSeconds", -1)) != int(entry["time_spent_seconds"]):
            continue
        if account_id and candidate.get("author", {}).get("accountId") != account_id:
            continue
        try:
            if abs(started_to_ms(candidate.get("started", "")) - target_ms) <= 1000:
                return candidate
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- commands

def run_lock_path() -> Path:
    return worklog.state_dir() / "post-run"


def cmd_run(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    force = "--force" in argv
    limit = None
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except Exception:
            print("--limit needs a number", file=sys.stderr)
            return 2

    if dry:
        return _run(argv, dry=True, force=force, limit=limit)

    # One drain at a time. Reopening the app starts several sessions at once and
    # every SessionStart fires this hook, so without the lock each process reads
    # the same pending entry and posts it. The duplicate check does not save us:
    # it is gated on attempts > 0, and a fresh entry is attempts == 0 in every
    # racing process. That is how one 10-minute session reached Jira three times.
    #
    # A second drain has nothing useful to add, so it exits rather than waits.
    try:
        with worklog.FileLock(run_lock_path(), timeout=0.5, stale_after=900):
            return _run(argv, dry=False, force=force, limit=limit)
    except TimeoutError:
        worklog.log("post: another drain is already running, skipping")
        if not from_hook(argv):
            print("Another drain is already running; nothing to do.")
        return 0


def _run(argv: list[str], dry: bool, force: bool, limit: int | None) -> int:
    entries = load_queue()
    todo = [e for e in entries if due(e, force)]
    if limit:
        todo = todo[:limit]
    if not todo:
        if not from_hook(argv):
            print("Nothing due.")
        return 0

    if dry:
        for entry in todo:
            print(f"[dry-run] {entry['issue_key']:<14} "
                  f"{worklog.human_duration(entry['time_spent_seconds']):>7}  "
                  f"{entry['started']}")
            print(f"          {build_comment(entry)}")
        return 0

    try:
        jira = Jira(load_credentials())
        account_id = jira.myself().get("accountId")
    except Exception as exc:
        message = str(exc)
        worklog.log(f"post: cannot start ({message})")
        if not from_hook(argv):
            print(f"post: {message}", file=sys.stderr)
        return 1

    posted, failed, adopted = 0, 0, 0
    for entry in todo:
        # Only worth a lookup if a previous attempt could have half-succeeded.
        if int(entry.get("attempts", 0)) > 0:
            try:
                match = find_existing(jira, entry, account_id)
            except JiraError as exc:
                # We do not know whether the earlier attempt landed. Posting now
                # is how you get two worklogs for one session, so wait and look
                # again rather than guessing.
                failed += 1
                message = f"duplicate check failed, not posting: {exc}"
                record_failure(entry, exc, message)
                worklog.log(f"post: {entry['issue_key']} {entry['id'][:8]} -> {message}")
                continue
            if match:
                entry.update(status="posted", jira_worklog_id=match.get("id"),
                             posted_at=worklog.now().isoformat(), adopted=True)
                entry.pop("next_attempt", None)
                adopted += 1
                continue
        try:
            result = jira.add_worklog(entry["issue_key"],
                                      int(entry["time_spent_seconds"]),
                                      entry["started"],
                                      adf(build_comment(entry)))
            entry.update(status="posted", jira_worklog_id=result.get("id"),
                         posted_at=worklog.now().isoformat())
            entry.pop("next_attempt", None)
            posted += 1
        except JiraError as exc:
            failed += 1
            record_failure(entry, exc, str(exc))
            worklog.log(f"post: {entry['issue_key']} {entry['id'][:8]} -> {exc}")

    done = [e for e in entries if e.get("status") == "posted"]
    remaining = [e for e in entries if e.get("status") != "posted"]
    # Archive first. Dropping an entry from the queue before its record is
    # durable loses the evidence that the time was ever logged; the other order
    # only risks re-reading it next run, where the duplicate check adopts it.
    archive(done)
    save_queue(remaining)

    if not from_hook(argv):
        summary = f"posted {posted}"
        if adopted:
            summary += f", adopted {adopted} already-present"
        if failed:
            summary += f", {failed} failed"
        print(summary + f"; {len(remaining)} left in queue")
    return 0 if failed == 0 else 1


def from_hook(argv: list[str]) -> bool:
    return "--quiet" in argv or "--from-hook" in argv


def cmd_check(argv: list[str]) -> int:
    try:
        creds = load_credentials()
    except Exception as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"site   {creds['base_url']}")
    jira = Jira(creds)
    try:
        me = jira.myself()
    except JiraError as exc:
        print(f"auth   FAILED -- {exc}", file=sys.stderr)
        return 1
    print(f"auth   ok as {me.get('displayName')} <{me.get('emailAddress')}>")
    print(f"       accountId {me.get('accountId')}")

    keys = sorted({e["issue_key"] for e in load_queue()})
    if not keys:
        print("issues no queued issues to check")
        return 0
    print("issues")
    ok = True
    for key in keys:
        try:
            jira.request("GET", f"/rest/api/3/issue/{key}",
                         params={"fields": "summary,timetracking"})
            print(f"  ok   {key}")
        except JiraError as exc:
            ok = False
            print(f"  FAIL {key} -- {exc}")
    return 0 if ok else 1


def cmd_status(argv: list[str]) -> int:
    entries = load_queue()
    buckets: dict[str, list[dict]] = {}
    for entry in entries:
        buckets.setdefault(entry.get("status", "pending"), []).append(entry)

    print(f"queue at {worklog.queue_path()}")
    for status in ("pending", "failed", "blocked"):
        items = buckets.get(status, [])
        total = sum(e["time_spent_seconds"] for e in items)
        print(f"  {status:<8} {len(items):>3}  {worklog.human_duration(total)}")

    for entry in buckets.get("failed", []):
        print(f"\n  retry {entry['id'][:8]} {entry['issue_key']} "
              f"attempt {entry.get('attempts')} next {entry.get('next_attempt','?')[:19]}")
        print(f"        {entry.get('last_error','')[:160]}")
    for entry in buckets.get("blocked", []):
        print(f"\n  BLOCKED {entry['id'][:8]} {entry['issue_key']} "
              f"{worklog.human_duration(entry['time_spent_seconds'])}")
        print(f"          {entry.get('last_error','')[:200]}")
        print(f"          fix, then: post.py retry {entry['id'][:8]}")

    if posted_path().exists():
        count = sum(1 for l in posted_path().read_text(encoding="utf-8").splitlines() if l.strip())
        print(f"\nposted   {count} archived in {posted_path().name}")
    return 0


def cmd_retry(argv: list[str]) -> int:
    if not argv:
        print("usage: post.py retry <id-prefix|all>", file=sys.stderr)
        return 2
    target = argv[0]
    entries = load_queue()
    touched = 0
    for entry in entries:
        if entry.get("status") == "posted":
            continue
        if target == "all" or entry["id"].startswith(target):
            entry["status"] = "pending"
            entry.pop("next_attempt", None)
            entry["attempts"] = 0
            touched += 1
    save_queue(entries)
    print(f"reset {touched} entr{'y' if touched == 1 else 'ies'} to pending")
    return 0


def cmd_install_hook(argv: list[str]) -> int:
    """Drain on SessionStart. The queue is written at SessionEnd, so the next
    session ships it -- no cron, no daemon, and it is async so it costs the
    session nothing."""
    script = str(Path(__file__).resolve())
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = worklog.read_json(settings, {})
    hooks = data.setdefault("hooks", {})

    handler = {"type": "command", "command": sys.executable or "python3",
               "args": [script, "run", "--from-hook"], "async": True}

    cleaned = []
    for group in hooks.get("SessionStart", []):
        kept = [h for h in group.get("hooks", []) if "post.py" not in json.dumps(h)]
        if kept:
            cleaned.append(dict(group, hooks=kept))
    hooks["SessionStart"] = cleaned + [{"hooks": [handler]}]

    worklog.write_json_atomic(settings, data)
    print(f"Queue will drain on every SessionStart ({settings})")
    print("Credentials must be visible to that process -- put them in")
    print(f"  {credentials_path()}   (chmod 600)")
    print("rather than a shell rc, since hooks do not source your interactive shell.")
    return 0


COMMANDS = {
    "run": cmd_run,
    "check": cmd_check,
    "status": cmd_status,
    "retry": cmd_retry,
    "install-hook": cmd_install_hook,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    handler = COMMANDS.get(argv[0])
    if not handler:
        print(f"unknown command: {argv[0]}", file=sys.stderr)
        return 2
    return handler(argv[1:])


if __name__ == "__main__":
    hooked = "--from-hook" in sys.argv
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:
        worklog.log(f"post error: {exc!r}")
        if not hooked:
            print(f"post: {exc}", file=sys.stderr)
        sys.exit(0 if hooked else 1)
