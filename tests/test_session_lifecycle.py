#!/usr/bin/env python3
"""What each way of ending a Claude Code session does to accumulated time.

Drives the exact hook payloads Claude Code sends for each documented
SessionEnd `reason` and SessionStart `source`, and asserts what happens to the
clock. These are characterisation tests: they pin down behaviour a user has to
be able to predict, because the answer decides whether their time reaches the
board today or tomorrow.

The invariant that matters most is the last case: **no path loses time.** Every
scenario must still account for every second, whether it is queued, carried, or
still on the clock.

Run:  python tests/test_session_lifecycle.py
      python tests/test_session_lifecycle.py compact
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worklog  # noqa: E402

PYTHON = sys.executable
SID = "sess-under-test"
BANKED = 1500.0          # 25 minutes already on the clock when the scenario fires

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


class World:
    """One isolated state directory with a project mapped to an issue."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sess-lifecycle-"))
        self.state = self.tmp / "state"
        self.proj = self.tmp / "project"
        self.state.mkdir(parents=True)
        self.proj.mkdir(parents=True)
        (self.proj / ".jira-project").write_text("PROJ-67\n", encoding="utf-8")

    def stop(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self) -> dict:
        return dict(os.environ, CLAUDE_WORKLOG_DIR=str(self.state))

    def hook(self, event: str, **extra) -> int:
        payload = {"session_id": SID, "cwd": str(self.proj), **extra}
        return subprocess.run([PYTHON, "worklog.py", "hook", event],
                              input=json.dumps(payload), capture_output=True,
                              text=True, cwd=str(ROOT), env=self.env(),
                              timeout=60).returncode

    def seed(self, active: float = BANKED, idle_age_s: float = 5) -> dict:
        """A session already running, with time on the clock."""
        self.hook("session-start", source="startup")
        path = self.session_path()
        record = json.loads(path.read_text(encoding="utf-8"))
        record["active_seconds"] = active
        record["events"] = 40
        record["last_activity"] = (worklog.now() - timedelta(seconds=idle_age_s)).isoformat()
        path.write_text(json.dumps(record), encoding="utf-8")
        return record

    def session_path(self) -> Path:
        return self.state / "sessions" / f"{SID}.json"

    def session(self) -> dict | None:
        path = self.session_path()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def queue(self) -> list[dict]:
        path = self.state / "queue.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def queued_seconds(self) -> int:
        return sum(e["time_spent_seconds"] for e in self.queue())

    def carried_seconds(self) -> float:
        return sum(v["seconds"] for v in
                   worklog.read_json(self.state / "carry.json", {}).values())

    def on_the_clock(self) -> float:
        live = self.session()
        return float(live["active_seconds"]) if live else 0.0

    def accounted(self) -> float:
        return self.queued_seconds() + self.carried_seconds() + self.on_the_clock()


def expect_nothing_lost(w: World, tolerance: float = 60.0):
    """Every second must be queued, carried, or still on the clock. The
    tolerance absorbs the sub-minute the floor leaves as carry."""
    total = w.accounted()
    expect(abs(total - BANKED) <= tolerance,
           f"time went missing: started with {BANKED:.0f}s, can account for {total:.0f}s "
           f"(queued {w.queued_seconds()}s + carried {w.carried_seconds():.0f}s "
           f"+ clock {w.on_the_clock():.0f}s)")


# ------------------------------------------------- scenarios that BANK the time

@case
def test_clear_banks_the_time_and_restarts_the_clock(w: World):
    """/clear fires SessionEnd(clear) then SessionStart(clear)."""
    w.seed()
    w.hook("session-end", reason="clear")
    w.hook("session-start", source="clear")

    expect_eq(w.queued_seconds(), 1500, "25m should be queued")
    expect(w.session(), "a fresh session should exist after SessionStart")
    expect_eq(w.on_the_clock(), 0.0, "the new session's clock starts at zero")
    expect_nothing_lost(w)


@case
def test_resume_banks_the_time_the_same_way_clear_does(w: World):
    """/resume fires SessionEnd(resume) then SessionStart(resume). end_session
    deletes the record whatever the reason, so a resumed conversation starts a
    NEW worklog session -- the time splits across two entries."""
    w.seed()
    w.hook("session-end", reason="resume")
    w.hook("session-start", source="resume")

    expect_eq(w.queued_seconds(), 1500, "25m should be queued")
    expect_eq(w.on_the_clock(), 0.0,
              "accumulated time does NOT carry across a resume -- the clock restarts")
    expect_eq(w.session().get("resumed"), None,
              "start_session's resume branch cannot fire: end_session already "
              "removed the record it looks for")
    expect_nothing_lost(w)


@case
def test_manual_session_end_banks_without_touching_the_conversation(w: World):
    """Driving session-end by hand is the only way to bank time while staying
    in the same conversation. The next tool call re-creates the session."""
    w.seed()
    w.hook("session-end", reason="manual-flush")
    expect(w.session() is None, "session record is removed by session-end")

    w.hook("activity")          # the next tool call in the same conversation
    expect_eq(w.queued_seconds(), 1500, "25m should be queued")
    expect(w.session(), "the next activity hook re-creates the session")
    expect_eq(w.on_the_clock(), 0.0, "and it starts from zero, so nothing double counts")
    expect_nothing_lost(w)


@case
def test_stale_sweep_reclaims_a_killed_session_after_the_cutoff(w: World):
    """A force-killed terminal fires no hook at all. The next SessionStart more
    than stale_session_hours later finalizes the orphan."""
    w.seed(idle_age_s=13 * 3600)          # default cutoff is 12h
    w.hook("session-start", source="startup")

    expect_eq(w.queued_seconds(), 1500, "the orphaned session should be reclaimed")
    entry = w.queue()[0]
    expect_eq(entry["comment_parts"]["end_reason"], "stale",
              "it should be recorded as stale, not as a clean end")
    expect_nothing_lost(w)


# --------------------------------------------- scenarios that KEEP accumulating

@case
def test_compact_does_not_bank_anything(w: World):
    """/compact fires SessionStart(compact) with NO SessionEnd. This is the trap:
    it drains an existing queue but does not finalize the current session."""
    w.seed()
    w.hook("session-start", source="compact")

    expect_eq(w.queue(), [], "nothing should be queued -- SessionEnd never fired")
    expect_eq(w.on_the_clock(), 1500.0, "the clock keeps running, untouched")
    expect_nothing_lost(w)


@case
def test_fork_keeps_accumulating(w: World):
    """A forked session: SessionStart(fork), no SessionEnd."""
    w.seed()
    w.hook("session-start", source="fork")

    expect_eq(w.queue(), [], "nothing banked")
    expect_eq(w.on_the_clock(), 1500.0, "accumulated time survives a fork")
    expect_nothing_lost(w)


@case
def test_force_kill_same_day_resumes_the_same_record(w: World):
    """Killed and restarted within the stale cutoff: SessionStart finds the
    existing record and re-arms it rather than starting over."""
    w.seed(idle_age_s=120)
    w.hook("session-start", source="startup")

    expect_eq(w.queue(), [], "too recent to be swept, so nothing is banked yet")
    expect_eq(w.on_the_clock(), 1500.0, "the earlier time is still on the clock")
    expect_eq(w.session().get("resumed"), 1,
              "this is the branch start_session's resume handling was written for")
    expect_nothing_lost(w)


@case
def test_session_end_resolves_a_mapping_that_arrived_late(w: World):
    """The safety net. Even with no `map` command run in between -- config edited
    by hand, or another process -- time must not be filed as unmapped when the
    directory resolves by the time the session ends."""
    w.seed()
    path = w.session_path()
    record = json.loads(path.read_text(encoding="utf-8"))
    record["issue_key"] = None                 # as if mapped after SessionStart
    record["issue_source"] = "unresolved"
    path.write_text(json.dumps(record), encoding="utf-8")

    w.hook("session-end", reason="clear")

    queued = w.queue()
    expect_eq(len(queued), 1,
              "the time should reach the queue, not unmapped.jsonl")
    expect_eq(queued[0]["issue_key"], "PROJ-67",
              "resolved from the directory at session end, from its .jira-project")
    unmapped = w.state / "unmapped.jsonl"
    expect(not unmapped.exists() or not unmapped.read_text(encoding="utf-8").strip(),
           "nothing should have been filed as unmapped")


@case
def test_session_end_still_files_genuinely_unmapped_time(w: World):
    """The net must not catch everything -- a directory that resolves to nothing
    still belongs in unmapped.jsonl."""
    w.seed()
    (w.proj / ".jira-project").unlink()        # remove the only mapping
    path = w.session_path()
    record = json.loads(path.read_text(encoding="utf-8"))
    record["issue_key"] = None
    path.write_text(json.dumps(record), encoding="utf-8")

    w.hook("session-end", reason="clear")

    expect_eq(w.queue(), [], "nothing should be queued")
    unmapped = w.state / "unmapped.jsonl"
    expect(unmapped.exists() and unmapped.read_text(encoding="utf-8").strip(),
           "it should be recorded as unmapped instead")


# ------------------------------------------------------------------- invariant

@case
def test_no_path_loses_time(w: World):
    """The invariant across every route out of a session."""
    routes = [
        ("clear",   [("session-end", {"reason": "clear"}), ("session-start", {"source": "clear"})]),
        ("resume",  [("session-end", {"reason": "resume"}), ("session-start", {"source": "resume"})]),
        ("logout",  [("session-end", {"reason": "logout"})]),
        ("exit",    [("session-end", {"reason": "prompt_input_exit"})]),
        ("other",   [("session-end", {"reason": "other"})]),
        ("compact", [("session-start", {"source": "compact"})]),
        ("fork",    [("session-start", {"source": "fork"})]),
    ]
    for name, steps in routes:
        world = World()
        try:
            world.seed()
            for event, extra in steps:
                world.hook(event, **extra)
            total = world.accounted()
            expect(abs(total - BANKED) <= 60,
                   f"route {name!r} lost time: accounted {total:.0f}s of {BANKED:.0f}s")
        finally:
            world.stop()


# ---------------------------------------------------------------- runner

def main(argv: list[str]) -> int:
    selected = [c for c in CASES if not argv or any(a in c.__name__ for a in argv)]
    passed, failed = [], []
    for fn in selected:
        w = World()
        print(f"  {fn.__name__} ... ", end="", flush=True)
        started = time.time()
        try:
            fn(w)
            print(f"ok ({time.time() - started:.1f}s)")
            passed.append(fn.__name__)
        except Failure as exc:
            print(f"FAIL ({time.time() - started:.1f}s)")
            failed.append((fn.__name__, str(exc), (fn.__doc__ or "").strip()))
        except Exception:
            print(f"ERROR ({time.time() - started:.1f}s)")
            failed.append((fn.__name__, traceback.format_exc(), (fn.__doc__ or "").strip()))
        finally:
            w.stop()

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
