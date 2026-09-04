#!/usr/bin/env python3
"""End-to-end tests for post.py against the mock Jira in mock_jira.py.

Every test gets its own queue directory, its own mock-server state and its own
port, so nothing leaks between cases and the request log for a case contains
only that case's traffic.

Run:  python tests/test_post.py           all cases
      python tests/test_post.py 429       cases whose name contains "429"
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worklog  # noqa: E402

PYTHON = sys.executable
POST = str(ROOT / "post.py")
MOCK = str(Path(__file__).resolve().parent / "mock_jira.py")

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Harness:
    """One post.py world: a queue dir, a mock Jira, and helpers to drive both."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="worklog-test-"))
        self.state = self.tmp / "state"          # CLAUDE_WORKLOG_DIR
        self.mock_state = self.tmp / "mock"
        self.state.mkdir(parents=True)
        self.mock_state.mkdir(parents=True)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = None

    # ------------------------------------------------------------- lifecycle
    def start(self, scenario: dict | None = None):
        self.scenario(scenario or {})
        env = dict(os.environ, MOCK_JIRA_STATE=str(self.mock_state))
        self.proc = subprocess.Popen(
            [PYTHON, MOCK, str(self.port)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{self.base}/rest/api/3/myself", timeout=1).read()
                # clear the readiness probe from the request log
                (self.mock_state / "requests.jsonl").write_text("", encoding="utf-8")
                return self
            except urllib.error.HTTPError:
                (self.mock_state / "requests.jsonl").write_text("", encoding="utf-8")
                return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("mock jira did not come up")

    def stop(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait(timeout=10)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- mock side
    def scenario(self, spec: dict):
        (self.mock_state / "scenario.json").write_text(json.dumps(spec), encoding="utf-8")

    def requests(self, method: str | None = None, path_contains: str | None = None) -> list[dict]:
        path = self.mock_state / "requests.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if method and entry["method"] != method:
                continue
            if path_contains and path_contains not in entry["path"]:
                continue
            out.append(entry)
        return out

    def server_worklogs(self, issue_key: str) -> list[dict]:
        try:
            data = json.loads((self.mock_state / "worklogs.json").read_text(encoding="utf-8"))
        except Exception:
            return []
        return data.get(issue_key, [])

    def preload_worklog(self, issue_key: str, seconds: int, started: str,
                        account_id: str = "5f8a1b2c3d4e5f6a7b8c9d0e"):
        """Put a worklog on the issue as if a previous attempt had landed."""
        path = self.mock_state / "worklogs.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        entries = data.setdefault(issue_key, [])
        entries.append({
            "id": str(9000 + len(entries)),
            "issueId": "100001",
            "author": {"accountId": account_id, "displayName": "Test User"},
            "timeSpentSeconds": seconds,
            "timeSpent": f"{seconds // 60}m",
            "started": started,
            "created": started,
        })
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ------------------------------------------------------------- post side
    def env(self, **extra) -> dict:
        env = dict(os.environ)
        env.update({
            "CLAUDE_WORKLOG_DIR": str(self.state),
            "JIRA_BASE_URL": self.base,
            "JIRA_EMAIL": "test@example.com",
            "JIRA_API_TOKEN": "mock-token-123",
        })
        env.update(extra)
        return env

    def entry(self, issue_key="PROJ-67", seconds=1800, minutes_ago=90, **overrides) -> dict:
        started = worklog.now() - timedelta(minutes=minutes_ago)
        record = {
            "id": overrides.pop("id", f"{len(self.queue()):032x}"),
            "issue_key": issue_key,
            "time_spent_seconds": seconds,
            "started": worklog.jira_started(started),
            "comment": "worklog-automator | logged automatically from Claude Code",
            "comment_parts": {
                "repo": "worklog-automator",
                "branch": "main",
                "github_repo": None,
                "github_issue": None,
                "commits": [],
                "carried_seconds": 0,
                "session_seconds": seconds,
                "idle_drops": 0,
                "end_reason": "SessionEnd",
            },
            "session_id": "sess-test",
            "created": worklog.now().isoformat(),
            "status": "pending",
        }
        record.update(overrides)
        return record

    def seed(self, *entries: dict):
        path = self.state / "queue.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def queue(self) -> list[dict]:
        path = self.state / "queue.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def posted(self) -> list[dict]:
        path = self.state / "posted.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def run(self, *args: str, timeout: float = 120,
            env_extra: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([PYTHON, POST, *args], env=self.env(**(env_extra or {})),
                              capture_output=True, text=True, timeout=timeout,
                              cwd=str(ROOT))


# ---------------------------------------------------------------- assertions

class Failure(AssertionError):
    pass


def expect(condition, message: str):
    if not condition:
        raise Failure(message)


def expect_eq(actual, wanted, message: str):
    if actual != wanted:
        raise Failure(f"{message}\n      expected: {wanted!r}\n      actual:   {actual!r}")


# ---------------------------------------------------------------- the cases

@case
def test_happy_path_posts_and_archives(h: Harness):
    """A clean POST: entry leaves the queue, lands in posted.jsonl, and the
    request on the wire is the one Planyway needs."""
    h.start({"post_worklog": {"mode": "ok"}})
    entry = h.entry(seconds=1800)
    h.seed(entry)

    result = h.run("run")
    expect_eq(result.returncode, 0, f"run should succeed\n{result.stdout}{result.stderr}")

    expect_eq(len(h.queue()), 0, "queue should be empty after a successful post")
    archived = h.posted()
    expect_eq(len(archived), 1, "exactly one entry should be archived")
    expect_eq(archived[0]["status"], "posted", "archived entry status")
    expect(archived[0].get("jira_worklog_id"), "archived entry should carry the Jira worklog id")

    posts = h.requests("POST", "/worklog")
    expect_eq(len(posts), 1, "exactly one POST should have been made")
    post = posts[0]
    expect_eq(post["query"].get("adjustEstimate"), "auto",
              "adjustEstimate=auto is what moves the Planyway timeline")
    expect_eq(post["query"].get("notifyUsers"), "false", "notifyUsers should be false")
    expect(post["auth"].startswith("Basic "), "should send basic auth")
    expect_eq(post["content_type"], "application/json", "content type")
    body = post["body"]
    expect_eq(body["timeSpentSeconds"], 1800, "seconds on the wire")
    expect_eq(body["started"], entry["started"], "started should be sent verbatim")
    expect_eq(body["comment"]["type"], "doc", "comment must be ADF for REST v3")
    expect_eq(body["comment"]["version"], 1, "ADF version")
    expect(body["comment"]["content"][0]["content"][0]["text"],
           "ADF comment should carry text")

    server = h.server_worklogs("PROJ-67")
    expect_eq(len(server), 1, "server should hold exactly one worklog")


@case
def test_401_on_post_blocks_without_retry(h: Harness):
    """Bad credentials are permanent. The entry must park as blocked, keep its
    time, and never schedule a retry."""
    h.start({"post_worklog": {"mode": "status", "code": 401,
                              "body": {"errorMessages": ["Client must be authenticated"],
                                       "errors": {}}}})
    h.seed(h.entry())

    result = h.run("run")
    expect_eq(result.returncode, 1, "a failed run should exit non-zero")

    queue = h.queue()
    expect_eq(len(queue), 1, "blocked entry must stay in the queue")
    entry = queue[0]
    expect_eq(entry["status"], "blocked", "401 is permanent, not transient")
    expect(not entry.get("next_attempt"), "blocked entries must not be scheduled for retry")
    expect_eq(entry["attempts"], 1, "attempt should be counted")
    expect("401" in entry.get("last_error", ""), "error should record the status")
    expect_eq(entry["time_spent_seconds"], 1800, "time must not be lost")
    expect_eq(len(h.posted()), 0, "nothing should be archived")

    # A second run must not hammer a permanently broken credential.
    before = len(h.requests("POST", "/worklog"))
    h.run("run")
    expect_eq(len(h.requests("POST", "/worklog")), before,
              "blocked entries must not be retried on the next run")


@case
def test_401_on_myself_aborts_run_untouched(h: Harness):
    """If the identity probe fails there is nothing to be learned by posting.
    The queue must come out exactly as it went in."""
    h.start({"myself": {"mode": "status", "code": 401,
                        "body": {"errorMessages": ["Unauthorized"], "errors": {}}}})
    h.seed(h.entry())

    result = h.run("run")
    expect_eq(result.returncode, 1, "run should fail")

    queue = h.queue()
    expect_eq(len(queue), 1, "entry stays queued")
    expect_eq(queue[0]["status"], "pending", "entry must not be marked failed or blocked")
    expect_eq(queue[0].get("attempts", 0), 0, "no attempt was made against the worklog API")
    expect_eq(len(h.requests("POST", "/worklog")), 0, "must not POST when auth is dead")


@case
def test_429_backs_off_on_the_documented_ladder(h: Harness):
    """429 is transient. Each attempt should schedule the next one further out,
    following BACKOFF_SECONDS, and not be retried before it comes due."""
    h.start({"post_worklog": {"mode": "status", "code": 429,
                              "headers": {"Retry-After": "120"},
                              "body": {"errorMessages": ["Rate limit exceeded"], "errors": {}}}})
    h.seed(h.entry())

    ladder = [60, 300, 900]
    for index, expected_delay in enumerate(ladder, start=1):
        fired_at = worklog.now()
        result = h.run("run", *(["--force"] if index > 1 else []))
        expect_eq(result.returncode, 1, f"attempt {index} should report failure")

        entry = h.queue()[0]
        expect_eq(entry["status"], "failed", f"attempt {index}: 429 is transient")
        expect_eq(entry["attempts"], index, f"attempt {index}: attempts counter")
        scheduled = worklog.parse_ts(entry["next_attempt"])
        delay = (scheduled - fired_at).total_seconds()
        expect(abs(delay - expected_delay) <= 10,
               f"attempt {index}: next_attempt should be ~{expected_delay}s out, got {delay:.0f}s")

    # Not due yet: a plain run must leave it alone.
    before = len(h.requests("POST", "/worklog"))
    result = h.run("run")
    expect_eq(result.returncode, 0, "a run with nothing due should succeed quietly")
    expect_eq(len(h.requests("POST", "/worklog")), before,
              "an entry that is not due must not be retried")
    expect("Nothing due" in result.stdout, f"should say nothing is due, got: {result.stdout!r}")

    # Retry-After is what Jira actually tells us to wait. Record whether it is honoured.
    entry = h.queue()[0]
    scheduled = worklog.parse_ts(entry["next_attempt"])
    honoured = abs((scheduled - worklog.now()).total_seconds() - 120) <= 15
    h.note = ("Retry-After: 120 was %s"
              % ("honoured" if honoured else "IGNORED in favour of the fixed ladder"))


@case
def test_429_then_success_clears_the_entry(h: Harness):
    """The point of backing off is eventually getting through."""
    h.start({"post_worklog": {"mode": "sequence", "steps": [
        {"mode": "status", "code": 429, "body": {"errorMessages": ["slow down"], "errors": {}}},
        {"mode": "ok"},
    ]}})
    h.seed(h.entry())

    h.run("run")
    expect_eq(h.queue()[0]["status"], "failed", "first attempt is rate limited")

    result = h.run("run", "--force")
    expect_eq(result.returncode, 0, f"second attempt should succeed: {result.stderr}")
    expect_eq(len(h.queue()), 0, "queue should drain")
    expect_eq(len(h.posted()), 1, "entry should be archived")
    expect_eq(len(h.server_worklogs("PROJ-67")), 1,
              "exactly one worklog on the issue -- the 429 must not have left a partial")


@case
def test_timeout_mid_post_then_adopts_the_orphan(h: Harness):
    """The case the whole design exists for: the POST lands on the server, the
    client never sees the response. The retry must adopt the existing worklog,
    not create a second one."""
    h.start({"post_worklog": {"mode": "commit_then_hang", "seconds": 26}})
    h.seed(h.entry(seconds=2700))

    started = time.time()
    result = h.run("run", timeout=180)
    elapsed = time.time() - started
    expect(elapsed >= 18, f"the client should have waited out its 20s timeout, took {elapsed:.0f}s")
    expect_eq(result.returncode, 1, "a timed-out post should report failure")

    entry = h.queue()[0]
    expect_eq(entry["status"], "failed", "a timeout is transient, never permanent")
    expect_eq(entry["attempts"], 1, "attempt counted")
    expect("network failure" in entry.get("last_error", "").lower()
           or "timed out" in entry.get("last_error", "").lower(),
           f"error should name the network failure, got {entry.get('last_error')!r}")

    # The server did commit it, which is exactly the trap.
    expect_eq(len(h.server_worklogs("PROJ-67")), 1,
              "precondition: the mock committed the worklog before hanging")

    # Now let the next drain succeed if it wants to -- it should not need to.
    h.scenario({"post_worklog": {"mode": "ok"}})
    result = h.run("run", "--force")
    expect_eq(result.returncode, 0, f"the retry should succeed: {result.stderr}")

    expect_eq(len(h.server_worklogs("PROJ-67")), 1,
              "DOUBLE POST: the retry created a second worklog instead of adopting")
    expect_eq(len(h.queue()), 0, "queue should drain")
    archived = h.posted()
    expect_eq(len(archived), 1, "one archived entry")
    expect_eq(archived[0].get("adopted"), True, "entry should be marked as adopted")
    expect(archived[0].get("jira_worklog_id"), "adopted entry should carry the existing worklog id")
    expect("adopted" in result.stdout, f"run should report the adoption: {result.stdout!r}")


@case
def test_adoption_matches_on_author_start_and_duration(h: Harness):
    """Adoption must not grab somebody else's worklog, or one of a different
    length, just because it is on the same issue at the same time."""
    h.start({"post_worklog": {"mode": "ok"}})
    entry = h.entry(seconds=1800)
    # A decoy from another user, same instant and duration.
    h.preload_worklog("PROJ-67", 1800, entry["started"], account_id="somebody-else")
    # A decoy from us at the same instant but a different duration.
    h.preload_worklog("PROJ-67", 900, entry["started"])
    entry["attempts"] = 1
    entry["status"] = "failed"
    h.seed(entry)

    result = h.run("run", "--force")
    expect_eq(result.returncode, 0, f"run should succeed: {result.stderr}")

    archived = h.posted()
    expect_eq(len(archived), 1, "one entry processed")
    expect(not archived[0].get("adopted"),
           "must NOT adopt a decoy: wrong author or wrong duration")
    expect_eq(len(h.requests("POST", "/worklog")), 1,
              "should have posted a real worklog rather than adopting a decoy")


@case
def test_transient_duplicate_check_failure_does_not_post(h: Harness):
    """If the duplicate lookup itself fails, posting anyway is the exact
    double-post the lookup exists to prevent. It must back off instead."""
    h.start()
    entry = h.entry(seconds=3600)
    # The state after a post that landed but was never confirmed.
    h.preload_worklog("PROJ-67", 3600, entry["started"])
    entry["attempts"] = 1
    entry["status"] = "failed"
    h.seed(entry)

    # The duplicate check (GET) is rate-limited; the POST would happily succeed.
    h.scenario({"get_worklog": {"mode": "status", "code": 429,
                                "body": {"errorMessages": ["Rate limit"], "errors": {}}},
                "post_worklog": {"mode": "ok"}})

    result = h.run("run", "--force")
    expect_eq(result.returncode, 1, "an unresolvable entry should report failure")

    expect_eq(len(h.requests("POST", "/worklog")), 0,
              "must not POST while it is unknown whether the last attempt landed")
    expect_eq(len(h.server_worklogs("PROJ-67")), 1,
              "the issue must still hold exactly one worklog")

    queue = h.queue()
    expect_eq(len(queue), 1, "the entry stays queued")
    expect_eq(queue[0]["status"], "failed", "a rate-limited lookup is transient")
    expect_eq(queue[0]["attempts"], 2, "the failed check counts as an attempt")
    expect(queue[0].get("next_attempt"), "it must be scheduled to look again")
    expect("duplicate check" in queue[0].get("last_error", ""),
           f"the reason should say so: {queue[0].get('last_error')!r}")

    # Once the lookup works again it should adopt, still without posting.
    h.scenario({"get_worklog": {"mode": "ok"}, "post_worklog": {"mode": "ok"}})
    result = h.run("run", "--force")
    expect_eq(result.returncode, 0, f"the recheck should succeed: {result.stderr}")
    expect_eq(len(h.requests("POST", "/worklog")), 0, "still no POST -- it adopts")
    expect_eq(len(h.server_worklogs("PROJ-67")), 1, "still exactly one worklog")
    expect_eq(h.posted()[0].get("adopted"), True, "entry should be marked adopted")
    expect_eq(len(h.queue()), 0, "queue should drain")


@case
def test_permanent_duplicate_check_failure_blocks(h: Harness):
    """A lookup that fails permanently will not start working on its own, so
    the entry belongs in front of a human rather than in a retry loop."""
    h.start()
    entry = h.entry()
    entry["attempts"] = 1
    entry["status"] = "failed"
    h.seed(entry)

    h.scenario({"get_worklog": {"mode": "status", "code": 403,
                                "body": {"errorMessages": ["No permission to browse"],
                                         "errors": {}}},
                "post_worklog": {"mode": "ok"}})

    result = h.run("run", "--force")
    expect_eq(result.returncode, 1, "should report failure")
    expect_eq(len(h.requests("POST", "/worklog")), 0, "must not post blind")

    queue = h.queue()
    expect_eq(queue[0]["status"], "blocked", "403 on the lookup will not clear itself")
    expect(not queue[0].get("next_attempt"), "blocked entries must not stay scheduled")
    expect_eq(queue[0]["time_spent_seconds"], 1800, "time preserved")


@case
def test_first_attempt_skips_the_duplicate_check(h: Harness):
    """A never-attempted entry cannot have a duplicate, and must not pay for a
    lookup -- including when that lookup would fail."""
    h.start({"get_worklog": {"mode": "status", "code": 500},
             "post_worklog": {"mode": "ok"}})
    h.seed(h.entry())

    result = h.run("run")
    expect_eq(result.returncode, 0, f"a first attempt should post: {result.stderr}")
    expect_eq(len(h.requests("GET", "/worklog")), 0,
              "no duplicate lookup on a first attempt")
    expect_eq(len(h.requests("POST", "/worklog")), 1, "it should post")
    expect_eq(len(h.queue()), 0, "queue should drain")


@case
def test_concurrent_drains_post_once(h: Harness):
    """Reopening the app starts several sessions at once, and every SessionStart
    fires the drain hook. Without a run lock each process reads the same pending
    entry and posts it -- this actually happened, putting one 10-minute session
    on the board three times as worklogs 10041/10042/10043.

    The duplicate check does not cover this: it is gated on attempts > 0, and a
    fresh entry is attempts == 0 in every racing process.
    """
    h.start({"post_worklog": {"mode": "ok"}})
    h.seed(h.entry(seconds=600))

    # Fire several drains simultaneously, as three SessionStart hooks would.
    procs = [subprocess.Popen([PYTHON, POST, "run", "--from-hook"], env=h.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, cwd=str(ROOT))
             for _ in range(5)]
    for p in procs:
        p.wait(timeout=120)

    expect(all(p.returncode == 0 for p in procs),
           f"every drain should exit cleanly, got {[p.returncode for p in procs]}")
    expect_eq(len(h.requests('POST', '/worklog')), 1,
              "DOUBLE POST: concurrent drains each posted the same entry")
    expect_eq(len(h.server_worklogs("PROJ-67")), 1,
              "the issue must hold exactly one worklog")
    expect_eq(len(h.queue()), 0, "queue should drain")
    archived = h.posted()
    expect_eq(len(archived), 1,
              f"the entry must be archived once, not {len(archived)} times")


@case
def test_run_lock_is_released_for_the_next_drain(h: Harness):
    """The lock must not strand the queue: a later drain has to get through."""
    h.start({"post_worklog": {"mode": "ok"}})
    h.seed(h.entry(id="a" * 32, seconds=600))
    expect_eq(h.run("run").returncode, 0, "first drain")
    expect_eq(len(h.posted()), 1, "first entry posted")

    h.seed(h.entry(id="b" * 32, seconds=900))
    expect_eq(h.run("run").returncode, 0, "second drain should acquire the lock")
    expect_eq(len(h.posted()), 2, "second entry posted")
    expect(not run_lock_leftover(h), "the lock file must not be left behind")


def run_lock_leftover(h: Harness) -> bool:
    return any(p.name.startswith("post-run") and p.suffix == ".lock"
               for p in h.state.iterdir())


@case
def test_dry_run_is_not_blocked_by_a_held_lock(h: Harness):
    """--dry-run makes no request and changes nothing, so it must never be
    refused because a real drain happens to be running."""
    h.start({"post_worklog": {"mode": "ok"}})
    h.seed(h.entry())
    import post as post_mod
    lock = worklog.FileLock(h.state / "post-run", timeout=5, stale_after=900)
    with lock:
        result = h.run("run", "--dry-run")
    expect_eq(result.returncode, 0, f"dry run should succeed under a held lock: {result.stderr}")
    expect("PROJ-67" in result.stdout, f"and still report: {result.stdout!r}")
    expect_eq(len(h.requests()), 0, "still no requests")


@case
def test_dry_run_touches_nothing(h: Harness):
    """--dry-run must not make a request, not need credentials to work, and not
    change the queue."""
    h.start({"post_worklog": {"mode": "status", "code": 500}})
    h.seed(h.entry())

    result = h.run("run", "--dry-run")
    expect_eq(result.returncode, 0, f"dry run should succeed: {result.stderr}")
    expect_eq(len(h.requests()), 0, "dry run must make zero HTTP requests")
    expect_eq(len(h.queue()), 1, "dry run must leave the queue alone")
    expect_eq(h.queue()[0]["status"], "pending", "status unchanged")
    expect("PROJ-67" in result.stdout, "dry run should show the issue key")
    expect("30m" in result.stdout, f"dry run should show the duration: {result.stdout!r}")


@case
def test_limit_caps_the_batch(h: Harness):
    h.start({"post_worklog": {"mode": "ok"}})
    h.seed(h.entry(id="a" * 32, issue_key="PROJ-66"),
           h.entry(id="b" * 32, issue_key="PROJ-67"),
           h.entry(id="c" * 32, issue_key="PROJ-62"))

    result = h.run("run", "--limit", "2")
    expect_eq(result.returncode, 0, f"run should succeed: {result.stderr}")
    expect_eq(len(h.requests("POST", "/worklog")), 2, "--limit 2 should post exactly two")
    expect_eq(len(h.queue()), 1, "one entry should remain queued")
    expect_eq(len(h.posted()), 2, "two entries archived")


@case
def test_partial_batch_failure_isolates(h: Harness):
    """One bad entry must not take the good ones down with it."""
    h.start({"post_worklog": {"mode": "sequence", "steps": [
        {"mode": "ok"},
        {"mode": "status", "code": 400,
         "body": {"errorMessages": [], "errors": {"timeSpentSeconds": "must be positive"}}},
        {"mode": "ok"},
    ]}})
    h.seed(h.entry(id="a" * 32, issue_key="PROJ-66"),
           h.entry(id="b" * 32, issue_key="PROJ-67"),
           h.entry(id="c" * 32, issue_key="PROJ-62"))

    result = h.run("run")
    expect_eq(result.returncode, 1, "a batch with a failure should exit non-zero")
    expect_eq(len(h.posted()), 2, "the two good entries should be archived")
    queue = h.queue()
    expect_eq(len(queue), 1, "only the bad entry should remain")
    expect_eq(queue[0]["issue_key"], "PROJ-67", "the failing entry is the one left")
    expect_eq(queue[0]["status"], "blocked", "400 is a payload bug, not transient")


@case
def test_max_attempts_gives_up(h: Harness):
    """After MAX_ATTEMPTS a transient failure has to stop being transient, or
    the entry retries forever and nobody ever looks at it."""
    h.start({"post_worklog": {"mode": "status", "code": 503,
                              "body": {"errorMessages": ["upstream down"], "errors": {}}}})
    entry = h.entry()
    entry["attempts"] = 23  # MAX_ATTEMPTS is 24
    entry["status"] = "failed"
    h.seed(entry)

    h.run("run", "--force")
    result = h.queue()[0]
    expect_eq(result["attempts"], 24, "attempts should reach the cap")
    expect_eq(result["status"], "blocked", "at the cap the entry must block, not keep retrying")
    expect(not result.get("next_attempt"), "a blocked entry must not stay scheduled")


@case
def test_status_and_retry_commands(h: Harness):
    h.start({"post_worklog": {"mode": "status", "code": 403,
                              "body": {"errorMessages": ["no permission"], "errors": {}}}})
    h.seed(h.entry())
    h.run("run")

    status = h.run("status")
    expect_eq(status.returncode, 0, "status should succeed")
    expect("blocked" in status.stdout, f"status should list the blocked bucket: {status.stdout!r}")
    expect("BLOCKED" in status.stdout, "status should call out the blocked entry")
    expect("PROJ-67" in status.stdout, "status should name the issue")

    retry = h.run("retry", "all")
    expect_eq(retry.returncode, 0, "retry should succeed")
    entry = h.queue()[0]
    expect_eq(entry["status"], "pending", "retry should reset the status")
    expect_eq(entry["attempts"], 0, "retry should reset the attempt counter")
    expect(not entry.get("next_attempt"), "retry should clear the schedule")

    # And it should actually go out again once the permission is fixed.
    h.scenario({"post_worklog": {"mode": "ok"}})
    result = h.run("run")
    expect_eq(result.returncode, 0, "the reset entry should post")
    expect_eq(len(h.queue()), 0, "queue should drain")


@case
def test_check_reports_auth_and_issues(h: Harness):
    h.start()
    h.seed(h.entry(issue_key="PROJ-67"))
    result = h.run("check")
    expect_eq(result.returncode, 0, f"check should pass: {result.stdout}{result.stderr}")
    expect("auth   ok as Test User" in result.stdout, f"check should confirm auth: {result.stdout!r}")
    expect("ok   PROJ-67" in result.stdout, "check should verify the queued issue")

    h.scenario({"get_issue": {"mode": "status", "code": 404,
                              "body": {"errorMessages": ["Issue does not exist"], "errors": {}}}})
    result = h.run("check")
    expect_eq(result.returncode, 1, "check should fail when an issue is unreachable")
    expect("FAIL PROJ-67" in result.stdout, "check should name the unreachable issue")


@case
def test_offline_leaves_everything_recoverable(h: Harness):
    """Server down entirely. Nothing may be lost, nothing may be blocked."""
    h.start({"post_worklog": {"mode": "ok"}})
    h.seed(h.entry(seconds=5400))
    h.proc.kill()
    h.proc.wait(timeout=10)

    result = h.run("run")
    expect_eq(result.returncode, 1, "offline run should report failure")
    queue = h.queue()
    expect_eq(len(queue), 1, "the entry must survive")
    expect_eq(queue[0]["time_spent_seconds"], 5400, "the time must survive intact")
    expect_eq(queue[0].get("status"), "pending",
              "offline is not the entry's fault: it must stay pending, not fail or block")
    expect_eq(len(h.posted()), 0, "nothing archived")


@case
def test_github_title_reaches_the_adf_comment(h: Harness):
    """PROJ-62 wants the number *and* title in the comment. A cached title
    must survive all the way onto the wire, in the format worklog.py owns."""
    h.start({"post_worklog": {"mode": "ok"}})
    (h.state / "github-titles.json").write_text(
        json.dumps({"acme/worklog-automator#42": "Drain the queue into Jira"}),
        encoding="utf-8")

    entry = h.entry()
    entry["comment_parts"].update(github_repo="acme/worklog-automator", github_issue=42,
                                  commits=["abc1234 wire up backoff"])
    entry["comment"] = "stale pre-resolution text"
    h.seed(entry)

    result = h.run("run")
    expect_eq(result.returncode, 0, f"run should succeed: {result.stderr}")

    text = h.requests("POST", "/worklog")[0]["body"]["comment"]["content"][0]["content"][0]["text"]
    expect("acme/worklog-automator#42: Drain the queue into Jira" in text,
           f"resolved title should be in the comment, got {text!r}")
    expect("1 commit: abc1234 wire up backoff" in text, f"commits should survive: {text!r}")
    expect("logged automatically from Claude Code" in text, f"provenance suffix: {text!r}")
    expect("stale pre-resolution text" not in text,
           "the resolved comment should replace the one queued at SessionEnd")


@case
def test_missing_gh_falls_back_without_losing_the_post(h: Harness):
    """gh absent or unauthenticated must degrade to the queued comment, never
    fail the post and never poison the title cache."""
    h.start({"post_worklog": {"mode": "ok"}})
    empty = h.tmp / "no-tools"
    empty.mkdir()

    entry = h.entry()
    entry["comment_parts"].update(github_repo="acme/worklog-automator", github_issue=42)
    entry["comment"] = "acme/worklog-automator#42 | logged automatically from Claude Code"
    h.seed(entry)

    result = h.run("run", env_extra={"PATH": str(empty)})
    expect_eq(result.returncode, 0, f"a missing gh must not fail the post: {result.stderr}")

    text = h.requests("POST", "/worklog")[0]["body"]["comment"]["content"][0]["content"][0]["text"]
    expect_eq(text, entry["comment"], "should fall back to the comment queued at SessionEnd")
    expect(not (h.state / "github-titles.json").exists(),
           "a failed lookup must not write a cache entry that hides the title forever")
    expect_eq(len(h.posted()), 1, "the entry should still post")


@case
def test_started_timestamp_survives_the_round_trip(h: Harness):
    """started_to_ms parses Jira's colon-less offset; if it does not, adoption
    silently never matches and every retry double-posts."""
    sys.path.insert(0, str(ROOT))
    import post

    for offset in ("+0530", "-0800", "+0000"):
        stamp = f"2026-09-03T14:05:00.000{offset}"
        millis = post.started_to_ms(stamp)
        expect(isinstance(millis, int), f"{stamp} should parse to millis")
        rebuilt = datetime.fromtimestamp(millis / 1000).astimezone()
        expect_eq(worklog.jira_started(rebuilt)[-5:] != "", True, "sanity")
        # Same instant expressed with a colon must land on the same millisecond.
        with_colon = f"2026-09-03T14:05:00.000{offset[:3]}:{offset[3:]}"
        expect_eq(post.started_to_ms(with_colon), millis,
                  f"{stamp} and {with_colon} are the same instant")

    # And what worklog.py writes must be readable by what post.py compares.
    now = worklog.now()
    expect_eq(post.started_to_ms(worklog.jira_started(now)),
              int(now.timestamp() * 1000),
              "worklog.jira_started output must round-trip through post.started_to_ms")


# ---------------------------------------------------------------- runner

def main(argv: list[str]) -> int:
    selected = [c for c in CASES if not argv or any(a in c.__name__ for a in argv)]
    passed, failed = [], []
    notes = []

    for fn in selected:
        h = Harness()
        name = fn.__name__
        print(f"  {name} ... ", end="", flush=True)
        started = time.time()
        try:
            fn(h)
            print(f"ok ({time.time() - started:.1f}s)")
            passed.append(name)
        except Failure as exc:
            print(f"FAIL ({time.time() - started:.1f}s)")
            failed.append((name, str(exc), (fn.__doc__ or "").strip()))
        except Exception:
            print(f"ERROR ({time.time() - started:.1f}s)")
            failed.append((name, traceback.format_exc(), (fn.__doc__ or "").strip()))
        finally:
            if getattr(h, "note", None):
                notes.append(f"{name}: {h.note}")
            h.stop()

    print()
    for note in notes:
        print(f"note  {note}")
    if notes:
        print()
    for name, message, doc in failed:
        print(f"FAIL  {name}")
        if doc:
            print("      " + doc.splitlines()[0])
        for line in message.splitlines():
            print(f"      {line}")
        print()
    print(f"{len(passed)} passed, {len(failed)} failed, {len(selected)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
