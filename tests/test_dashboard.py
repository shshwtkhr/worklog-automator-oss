#!/usr/bin/env python3
"""Tests for dashboard.py.

The one that matters is the refresh command: a page that hands you a command
regenerating a *different* file looks exactly like a refresh that silently does
nothing. That shipped once.

Run:  python tests/test_dashboard.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worklog  # noqa: E402

PYTHON = sys.executable
DASH = str(ROOT / "dashboard.py")

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


class Failure(AssertionError):
    pass


def expect(condition, message: str):
    if not condition:
        raise Failure(message)


def expect_eq(actual, wanted, message: str):
    if actual != wanted:
        raise Failure(f"{message}\n      expected: {wanted!r}\n      actual:   {actual!r}")


class Harness:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dash-test-"))
        self.state = self.tmp / "state"
        self.state.mkdir(parents=True)
        self.proj = self.tmp / "project"
        self.proj.mkdir(parents=True)

    def stop(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self) -> dict:
        return dict(os.environ, CLAUDE_WORKLOG_DIR=str(self.state))

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([PYTHON, DASH, *args], env=self.env(),
                              capture_output=True, text=True, timeout=120, cwd=str(ROOT))

    def seed(self):
        """A queue entry and a carry balance, so the page has something to show."""
        entry = {
            "id": "a" * 32, "issue_key": "PROJ-67", "time_spent_seconds": 900,
            "started": worklog.jira_started(worklog.now()),
            "comment": "worklog-automator | logged automatically from Claude Code",
            "comment_parts": {"repo": "worklog-automator", "branch": "main",
                              "github_repo": None, "github_issue": None, "commits": [],
                              "carried_seconds": 0, "session_seconds": 900,
                              "idle_drops": 0, "end_reason": "clear"},
            "session_id": "s1", "created": worklog.now().isoformat(), "status": "pending",
        }
        (self.state / "queue.jsonl").write_text(
            json.dumps(entry, separators=(",", ":")) + "\n", encoding="utf-8")
        (self.state / "carry.json").write_text(
            json.dumps({"PROJ-22": {"seconds": 120.0,
                                      "updated": worklog.now().isoformat()}}),
            encoding="utf-8")

    def payload(self, page: Path) -> dict:
        html = page.read_text(encoding="utf-8")
        match = re.search(r"window\.__WORKLOG__ = (\{.*?\});</script>", html, re.S)
        expect(match, "the page must embed its data as window.__WORKLOG__")
        return json.loads(match.group(1))


# ---------------------------------------------------------------- the cases

@case
def test_refresh_command_regenerates_this_page_not_another(h: Harness):
    """The bug this suite exists for. A page written to a non-default path used
    to print `python dashboard.py`, which rewrites the DEFAULT file -- so
    running it appeared to do nothing at all."""
    page = h.tmp / "elsewhere" / "index.html"
    result = h.run("--output", str(page))
    expect_eq(result.returncode, 0, f"generate should succeed: {result.stderr}")
    expect(page.exists(), "the page should be written where asked")

    data = h.payload(page)
    expect_eq(data["output_path"], str(page.resolve()),
              "the page must know which file it is")
    expect("--output" in data["regen_command"],
           f"the refresh command must target this file: {data['regen_command']!r}")
    expect(str(page.resolve()) in data["regen_command"],
           f"...by full path: {data['regen_command']!r}")

    # And the command it gives must actually work.
    first = data["generated"]
    time.sleep(1.1)
    args = data["regen_command"].split()[1:]          # drop the leading "python"
    rerun = subprocess.run([PYTHON, *[a.strip('"') for a in args]],
                           env=h.env(), capture_output=True, text=True, timeout=120)
    expect_eq(rerun.returncode, 0, f"the printed command must run: {rerun.stderr}")
    expect(h.payload(page)["generated"] > first,
           "running the printed command must refresh THIS page")


@case
def test_default_output_keeps_the_short_command(h: Harness):
    """No --output noise when the page is the default one."""
    result = h.run()
    expect_eq(result.returncode, 0, f"generate should succeed: {result.stderr}")
    page = h.state / "dashboard" / "index.html"
    expect(page.exists(), "default output should be <state>/dashboard/index.html")
    data = h.payload(page)
    expect("--output" not in data["regen_command"],
           f"the default page needs no --output: {data['regen_command']!r}")
    expect_eq(data["output_path"], str(page.resolve()), "output_path")


@case
def test_command_picker_never_emits_a_bare_dot(h: Harness):
    """`map . KEY` copied out of the page means "wherever your terminal happens
    to be". Run from a home directory it silently maps the home directory and
    starts attributing every unmapped session to that issue. That happened."""
    h.seed()
    proj = h.tmp / "projects" / "needs-mapping"
    proj.mkdir(parents=True)
    (self_state := h.state / "unmapped.jsonl").write_text(json.dumps({
        "cwd": str(proj), "project_root": str(proj), "active_seconds": 900.0,
        "started": worklog.now().isoformat(), "ended": worklog.now().isoformat(),
        "session_id": "u1", "reason": "clear", "hint": "map it",
    }) + chr(10), encoding="utf-8")
    assert self_state.exists()

    page = h.tmp / "p" / "index.html"
    h.run("--output", str(page))
    cmds = {c["label"]: c["cmd"] for c in h.payload(page)["commands"]}

    for label, cmd in cmds.items():
        parts = cmd.split()
        expect(" ." not in cmd or not cmd.rstrip().endswith(" ."),
               f"{label!r} ends in a bare dot: {cmd!r}")

    mapcmd = next(c for k, c in cmds.items() if " map " in c)
    target = mapcmd.split(" map ")[1].rsplit(" ", 1)[0].strip('"')
    expect_eq(target, str(proj.resolve()),
              "map should target a directory that actually needs mapping")
    expect("<ISSUE-KEY>" in mapcmd, "the key stays a placeholder -- only the path is known")


@case
def test_map_target_is_genuinely_unmapped(h: Harness):
    """A session records its issue_key at SessionStart, so one begun before a
    mapping existed still reads as unmapped. Trusting that record would suggest
    mapping a directory that is already mapped."""
    import dashboard as dash
    proj = h.tmp / "projects" / "already-mapped"
    proj.mkdir(parents=True)
    # dashboard.py only ever reads config; build one rather than expecting a file
    cfg = {"projects": {str(proj.resolve()): {"issue_key": "PROJ-9"}},
           "idle_timeout_minutes": 15, "round_to_minutes": 5, "minimum_minutes": 5,
           "stale_session_hours": 12, "capture_git_context": True}

    stale_session = [{"project_root": str(proj), "issue_key": None}]   # frozen as unmapped
    targets = dash.pick_targets(stale_session, [], cfg)
    expect(targets["map"] != str(proj),
           "must not offer to map a directory the resolver already maps")
    expect_eq(targets["unmap"], str(proj.resolve()),
              "but it is exactly what unmap should offer")


@case
def test_unmap_target_is_in_the_central_map(h: Harness):
    """unmap only edits config.json. Offering a path held by a .jira-project
    marker would print `no mapping for ...` and exit 1."""
    import dashboard as dash
    marker_only = h.tmp / "projects" / "marker-only"
    marker_only.mkdir(parents=True)
    (marker_only / ".jira-project").write_text("PROJ-7" + chr(10), encoding="utf-8")
    cfg = {"projects": {}, "idle_timeout_minutes": 15, "round_to_minutes": 5,
           "minimum_minutes": 5, "stale_session_hours": 12, "capture_git_context": True}

    targets = dash.pick_targets(
        [{"project_root": str(marker_only), "issue_key": "PROJ-7"}], [], cfg)
    expect(targets["unmap"] != str(marker_only),
           "a marker-mapped path is not something unmap can remove")


@case
def test_page_is_self_contained(h: Harness):
    """No server, no CDN: it has to work offline from a file:// URL."""
    h.seed()
    page = h.tmp / "out" / "index.html"
    h.run("--output", str(page))
    html = page.read_text(encoding="utf-8")
    external = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', html)
    expect_eq(external, [], f"the page must reference nothing external: {external}")
    expect("<script>window.__WORKLOG__" in html, "data must be embedded, not fetched")
    expect("fetch('data.json'" not in html and 'fetch("data.json"' not in html,
           "must not try to fetch a sibling file -- CORS forbids it on file://")


@case
def test_generating_never_writes_to_the_state_it_reads(h: Harness):
    """The dashboard is read-only. It must never touch the queue, the carry
    balance or the archive."""
    h.seed()
    before = {p.name: p.read_bytes()
              for p in h.state.iterdir() if p.is_file()}
    h.run("--output", str(h.tmp / "ro" / "index.html"))
    after = {p.name: p.read_bytes()
             for p in h.state.iterdir() if p.is_file()}
    for name, blob in before.items():
        expect(name in after, f"{name} disappeared")
        expect_eq(after[name], blob, f"{name} was modified by generating the dashboard")


@case
def test_payload_carries_what_the_page_needs(h: Harness):
    h.seed()
    page = h.tmp / "p" / "index.html"
    h.run("--output", str(page))
    data = h.payload(page)
    for key in ("generated", "totals", "projects", "sessions", "queue", "posted",
                "carry", "unmapped", "health", "config", "output_path",
                "regen_command", "floor_seconds"):
        expect(key in data, f"payload is missing {key!r}")
    expect_eq(len(data["queue"]), 1, "the seeded queue entry should be there")
    expect_eq(data["totals"]["queued"], 900, "queued seconds")
    expect(any(p["issue_key"] == "PROJ-22" for p in data["projects"]),
           "an issue with only a carry balance should still appear as a project")


@case
def test_empty_state_still_renders(h: Harness):
    """A fresh install has no queue, no sessions and no archive."""
    page = h.tmp / "empty" / "index.html"
    result = h.run("--output", str(page))
    expect_eq(result.returncode, 0, f"should not crash on empty state: {result.stderr}")
    data = h.payload(page)
    expect_eq(data["totals"]["unposted"], 0, "nothing unposted")
    expect_eq(data["queue"], [], "no queue")
    expect(page.stat().st_size > 5000, "the page should still be a real page")


# ---------------------------------------------------------------- runner

def main(argv: list[str]) -> int:
    selected = [c for c in CASES if not argv or any(a in c.__name__ for a in argv)]
    passed, failed = [], []
    for fn in selected:
        h = Harness()
        print(f"  {fn.__name__} ... ", end="", flush=True)
        started = time.time()
        try:
            fn(h)
            print(f"ok ({time.time() - started:.1f}s)")
            passed.append(fn.__name__)
        except Failure as exc:
            print(f"FAIL ({time.time() - started:.1f}s)")
            failed.append((fn.__name__, str(exc), (fn.__doc__ or "").strip()))
        except Exception:
            print(f"ERROR ({time.time() - started:.1f}s)")
            failed.append((fn.__name__, traceback.format_exc(), (fn.__doc__ or "").strip()))
        finally:
            h.stop()

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
