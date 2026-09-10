#!/usr/bin/env python3
"""Tests for worklog.py's project mapping commands.

Each case gets its own CLAUDE_WORKLOG_DIR and its own temp project tree, so the
real config is never touched.

Run:  python tests/test_worklog.py           all cases
      python tests/test_worklog.py unmap     cases whose name contains "unmap"
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import worklog  # noqa: E402

PYTHON = sys.executable
WORKLOG = str(ROOT / "worklog.py")

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
        self.tmp = Path(tempfile.mkdtemp(prefix="worklog-map-test-"))
        self.state = self.tmp / "state"
        self.projects = self.tmp / "projects"
        self.state.mkdir(parents=True)
        self.projects.mkdir(parents=True)

    def stop(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def project(self, name: str) -> Path:
        path = self.projects / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def env(self) -> dict:
        return dict(os.environ, CLAUDE_WORKLOG_DIR=str(self.state))

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([PYTHON, WORKLOG, *args], env=self.env(),
                              capture_output=True, text=True, timeout=60, cwd=str(ROOT))

    def config(self) -> dict:
        path = self.state / "config.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def mappings(self) -> dict:
        return {p: (e.get("issue_key") if isinstance(e, dict) else e)
                for p, e in (self.config().get("projects") or {}).items()}

    def resolve(self, path: Path | str) -> dict:
        """Resolve through worklog.py in a subprocess so it reads the same
        config the commands wrote."""
        result = self.run("resolve", str(path))
        return {"stdout": result.stdout, "code": result.returncode}


# ---------------------------------------------------------------- the cases

@case
def test_map_adopts_a_running_unresolved_session(h: Harness):
    """The reason the advice used to be "restart Claude Code". A session records
    its issue key once, at SessionStart, so a mapping added later was invisible
    to it -- and real work landed in unmapped.jsonl because the map arrived an
    hour too late."""
    project = h.project("late-mapped")
    sessions = h.state / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "live1.json").write_text(json.dumps({
        "session_id": "live1", "cwd": str(project), "project_root": str(project),
        "issue_key": None, "issue_source": "unresolved",
        "started": worklog.now().isoformat(),
        "last_activity": worklog.now().isoformat(),
        "active_seconds": 1727.0, "events": 96, "idle_drops": 0,
    }), encoding="utf-8")

    result = h.run("map", str(project), "PROJ-85")
    expect_eq(result.returncode, 0, f"map should succeed: {result.stderr}")
    expect("live1" in result.stdout and "PROJ-85" in result.stdout,
           f"map should say which running session it rescued: {result.stdout!r}")
    expect("no restart needed" in result.stdout,
           "and should say a restart is not required, since it is not")

    rec = json.loads((sessions / "live1.json").read_text(encoding="utf-8"))
    expect_eq(rec["issue_key"], "PROJ-85", "the running session should adopt the mapping")
    expect("adopted mid-session" in rec["issue_source"],
           f"provenance should say it was adopted, not resolved at start: {rec['issue_source']!r}")
    expect_eq(rec["active_seconds"], 1727.0, "accumulated time must not be disturbed")


@case
def test_map_never_reattributes_an_already_resolved_session(h: Harness):
    """Adoption rescues unattributed time. Moving time that already has an
    issue would silently shift hours between tickets."""
    project = h.project("already-known")
    sessions = h.state / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "live2.json").write_text(json.dumps({
        "session_id": "live2", "cwd": str(project), "project_root": str(project),
        "issue_key": "PROJ-1", "issue_source": "marker",
        "started": worklog.now().isoformat(),
        "last_activity": worklog.now().isoformat(),
        "active_seconds": 600.0, "events": 12, "idle_drops": 0,
    }), encoding="utf-8")

    h.run("map", str(project), "PROJ-999")
    rec = json.loads((sessions / "live2.json").read_text(encoding="utf-8"))
    expect_eq(rec["issue_key"], "PROJ-1",
              "a session that already has an issue must keep it")
    expect_eq(rec["issue_source"], "marker", "and its provenance")


@case
def test_map_reports_nothing_when_no_session_needs_adopting(h: Harness):
    project = h.project("quiet")
    result = h.run("map", str(project), "PROJ-5")
    expect_eq(result.returncode, 0, "map should succeed")
    expect("no restart needed" not in result.stdout,
           f"nothing was adopted, so nothing should be claimed: {result.stdout!r}")


@case
def test_unmap_removes_the_mapping(h: Harness):
    project = h.project("example-app")
    h.run("map", str(project), "PROJ-22", "userx/example-app")
    expect_eq(h.mappings().get(str(project.resolve())), "PROJ-22", "precondition: mapped")

    result = h.run("unmap", str(project))
    expect_eq(result.returncode, 0, f"unmap should succeed: {result.stderr}")
    expect("PROJ-22" in result.stdout,
           f"should report what was removed: {result.stdout!r}")
    expect_eq(h.mappings(), {}, "the mapping should be gone from config.json")


@case
def test_unmap_leaves_other_mappings_alone(h: Harness):
    a, b, c = h.project("alpha"), h.project("beta"), h.project("gamma")
    h.run("map", str(a), "PROJ-1")
    h.run("map", str(b), "PROJ-2")
    h.run("map", str(c), "PROJ-3")
    expect_eq(len(h.mappings()), 3, "precondition: three mappings")

    result = h.run("unmap", str(b))
    expect_eq(result.returncode, 0, "unmap should succeed")

    remaining = h.mappings()
    expect_eq(sorted(remaining.values()), ["PROJ-1", "PROJ-3"],
              "only the targeted mapping should go")
    expect(str(b.resolve()) not in remaining, "beta should be gone")


@case
def test_unmap_of_unmapped_path_fails_cleanly(h: Harness):
    """Must not create the key, not wipe the config, and not exit 0 -- a script
    that unmaps in a loop needs to know which ones were actually there."""
    h.run("map", str(h.project("alpha")), "PROJ-1")
    before = h.mappings()

    result = h.run("unmap", str(h.project("never-mapped")))
    expect_eq(result.returncode, 1, "unmapping something absent should exit 1")
    expect("no mapping" in result.stderr.lower(),
           f"should say so on stderr: {result.stderr!r}")
    expect_eq(h.mappings(), before, "config must be unchanged")


@case
def test_unmap_without_argument_is_a_usage_error(h: Harness):
    result = h.run("unmap")
    expect_eq(result.returncode, 2, "missing argument should be a usage error, not a crash")
    expect("usage" in result.stderr.lower(), f"should print usage: {result.stderr!r}")


@case
def test_unmap_normalises_paths_the_same_way_map_does(h: Harness):
    """map stores a resolved absolute path. unmap has to resolve identically or
    it silently fails to find mappings the user can plainly see."""
    project = h.project("example-app")
    h.run("map", str(project), "PROJ-22")
    stored = str(project.resolve())
    expect_eq(list(h.mappings()), [stored], "precondition")

    # Same directory, spelled differently.
    awkward = str(project) + os.sep + "." + os.sep
    result = h.run("unmap", awkward)
    expect_eq(result.returncode, 0,
              f"unmap should resolve {awkward!r} to the stored path: {result.stderr}")
    expect_eq(h.mappings(), {}, "the mapping should be gone")


@case
def test_unmap_then_resolve_is_unresolved(h: Harness):
    """The point of unmapping: sessions there stop being attributed."""
    project = h.project("example-app")
    h.run("map", str(project), "PROJ-22")
    before = h.resolve(project)
    expect("PROJ-22" in before["stdout"], f"precondition: resolves: {before['stdout']!r}")

    h.run("unmap", str(project))
    after = h.resolve(project)
    expect("PROJ-22" not in after["stdout"],
           f"must no longer resolve to the issue: {after['stdout']!r}")


@case
def test_unmap_warns_when_a_marker_still_wins(h: Harness):
    """A .jira-project marker beats the central map, so removing the mapping
    alone changes nothing. Silently reporting success would be a lie."""
    project = h.project("worklog-automator")
    (project / ".jira-project").write_text("PROJ-67\n", encoding="utf-8")
    h.run("map", str(project), "PROJ-99")

    result = h.run("unmap", str(project))
    expect_eq(result.returncode, 0, "unmap should still succeed")
    expect_eq(h.mappings(), {}, "the central mapping should be gone")
    expect("PROJ-67" in result.stdout,
           f"should warn that the marker still maps it: {result.stdout!r}")
    expect(".jira-project" in result.stdout, "should name the marker file")

    # And it genuinely still resolves, which is what the warning is about.
    after = h.resolve(project)
    expect("PROJ-67" in after["stdout"],
           f"marker should still win after unmap: {after['stdout']!r}")


@case
def test_unmap_warns_on_a_marker_in_a_parent(h: Harness):
    """Resolution walks up, so a marker above the directory captures it too."""
    parent = h.project("monorepo")
    (parent / ".jira-project").write_text("PROJ-50\n", encoding="utf-8")
    child = parent / "packages" / "api"
    child.mkdir(parents=True)
    h.run("map", str(child), "PROJ-51")

    result = h.run("unmap", str(child))
    expect_eq(result.returncode, 0, "unmap should succeed")
    expect("PROJ-50" in result.stdout,
           f"should warn about the parent marker: {result.stdout!r}")


@case
def test_unmap_no_warning_when_no_marker(h: Harness):
    project = h.project("plain")
    h.run("map", str(project), "PROJ-1")
    result = h.run("unmap", str(project))
    expect_eq(result.returncode, 0, "unmap should succeed")
    expect("NOTE:" not in result.stdout,
           f"no marker exists, so there is nothing to warn about: {result.stdout!r}")


@case
def test_unmap_tolerates_a_bare_string_mapping(h: Harness):
    """resolve_issue accepts a hand-edited {path: "KEY"} form, so unmap must
    report it rather than printing None."""
    project = h.project("hand-edited")
    h.run("map", str(project), "PROJ-1")          # creates config + dirs
    cfg = h.config()
    cfg["projects"] = {str(project.resolve()): "PROJ-77"}
    (h.state / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    result = h.run("unmap", str(project))
    expect_eq(result.returncode, 0, f"unmap should succeed: {result.stderr}")
    expect("PROJ-77" in result.stdout,
           f"should report the bare-string key, not None: {result.stdout!r}")
    expect_eq(h.mappings(), {}, "mapping removed")


@case
def test_map_unmap_round_trip_is_idempotent(h: Harness):
    project = h.project("cycle")
    for _ in range(3):
        expect_eq(h.run("map", str(project), "PROJ-5").returncode, 0, "map should succeed")
        expect_eq(len(h.mappings()), 1, "map should not duplicate the entry")
        expect_eq(h.run("unmap", str(project)).returncode, 0, "unmap should succeed")
        expect_eq(h.mappings(), {}, "unmap should clear it")
    expect_eq(h.run("unmap", str(project)).returncode, 1,
              "a fourth unmap has nothing to remove")


@case
def test_unmap_appears_in_help(h: Harness):
    result = h.run("--help")
    expect_eq(result.returncode, 0, "help should succeed")
    expect("unmap" in result.stdout, f"unmap should be documented: {result.stdout[:400]!r}")


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
