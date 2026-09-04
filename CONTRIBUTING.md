# Contributing

Thanks for looking. This is a small, deliberately boring tool, and most of what
follows exists because something went wrong once.

---

## The rule that outranks everything else

**This tool writes to a real timesheet. Never put a number on the board that
nobody worked.**

Ordinary bugs produce a crash or a visibly wrong answer. This one produces a
plausible number that quietly becomes payroll, invoicing, or a sprint
retrospective. So the design rule is not "be accurate", it is "**never
over-report**":

- **Rounding floors and carries the remainder.** 39 minutes logs 35 and carries
  4. If you change the rounding, that direction is the invariant.
- **Idle gaps are discarded whole**, not clipped to the limit.
- **Short sessions carry forward** rather than being dropped or rounded up.

Each of those loses a little real time on purpose. A pull request that makes the
tool log *more* time needs to argue for itself very carefully.

---

## No dependencies. This one is not negotiable.

Everything here runs from a Claude Code hook. A hook that fails because a
virtualenv moved, a package upgraded, or a wheel would not build is a hook that
**silently stops recording your time** — and you find out at the end of a
sprint.

Standard library only, so the whole thing installs by copying files. That
constraint has held even where it cost something: the demo GIF is captured with
a browser that is already installed and assembled by a hand-written PNG reader,
median-cut quantiser and GIF89a/LZW encoder, rather than pulling in an imaging
library or `ffmpeg`.

If you genuinely cannot do something without a dependency, open an issue and
make the case before writing the code.

---

## Getting set up

There is no build step and nothing to install.

```bash
git clone https://github.com/userx/worklog-automator-oss
cd worklog-automator-oss
python tests/test_post.py               # should pass with no network at all
```

`CLAUDE_WORKLOG_DIR` redirects the state directory. Every test sets it to a
temporary path, which is how the suite stays off your real queue — **please keep
it that way in anything you add.**

---

## Tests

Four suites, no third-party test runner, no network, no Jira account:

```bash
python tests/test_post.py                 # posting, retry, duplicate adoption (23)
python tests/test_worklog.py              # project mapping commands (12)
python tests/test_session_lifecycle.py    # what each session end does to the clock (8)
python tests/test_dashboard.py            # the generated page (6)
```

Pass a substring to filter: `python tests/test_post.py 429`.

`tests/mock_jira.py` is a scriptable stand-in for Jira's REST v3 worklog API. It
re-reads its scenario on **every** request, so a case can change how the server
behaves between `post.py` invocations, and it logs every request so assertions
can be about what actually went over the wire.

**Please do not introduce pytest.** The suites are plain scripts so they run
anywhere Python does, which is the same reason the tool itself has no
dependencies.

### What a good test looks like here

**New behaviour ships with its test in the same commit.** Beyond that, two
habits that have repeatedly paid off:

- **Prove the test fails without the fix.** Every bug found so far was found by
  a test that did not exist yet, and at least one early test passed for the
  wrong reason. Temporarily revert your fix, watch the test fail, put it back.
  Say in the PR that you did.
- **When a test catches a real defect, keep the case and invert the assertion**
  to the fixed behaviour. Do not delete the test that caught it.

`tests/test_session_lifecycle.py` is a *characterisation* suite: it pins
behaviour a user has to be able to predict. Changing one of those assertions is
a deliberate product decision, not a test fix.

---

## Style

Match the surrounding code. Beyond that:

- Comments explain **why**, not what. Several comments in this codebase record a
  specific failure — those are load-bearing; do not tidy them away.
- `worklog.render_comment()` is the single source of truth for the worklog
  comment format. Nothing else builds that string.
- Hooks must never break a session: every entry point swallows exceptions, logs,
  and exits 0. Nothing prints to stdout from a hook, because Claude Code injects
  `SessionStart` and `UserPromptSubmit` stdout into the model's context.
- `dashboard.py` is **read-only**. It must never write to the queue, the carry
  balance, or Jira. If a change to it would need a write, the change belongs
  somewhere else.

---

## Branches, commits and pull requests

[`.agents/AGENTS.md`](.agents/AGENTS.md) is the full rules file. The short
version:

- Never commit to `main` directly.
- Branch as `GH-<issue-number>-<target-semver>`, e.g. `GH-14-0.3.0`.
- Reference the issue in the PR body with `Closes #14` — only if the PR genuinely
  completes it. For a partial fix, reference it without the keyword and say what
  remains.
- **The PR description is the current state; comments are the timeline.** If a
  commit closes something the description lists as open, update the description
  in the same push.

### Documentation is part of the change

Not follow-up work. In particular:

| If you change | Also update |
|---|---|
| A command | the script's `Usage:` docstring, `docs/USER_MANUAL.md`, `docs/TECHNICAL_DOCUMENTATION.md` §7 |
| A config key | `DEFAULT_CONFIG` and the settings table in the user manual |
| The queue entry shape | `docs/TECHNICAL_DOCUMENTATION.md` §4 — it is the contract between the two scripts |
| Retry classification | §6 **and** the plain-English explanation in the user manual |

Users make decisions based on how long something will keep trying. That belongs
in prose, not only in a constant.

---

## Reporting a bug

Useful reports include the output of:

```bash
python worklog.py doctor
python post.py status
```

**Please redact before pasting.** Those commands print directory paths and Jira
issue keys, and `post.py status` prints error text that may include your site
URL. Never paste `credentials.json`, an API token, or anything from it — a
Jira API token can do everything in Jira that you can.

If the bug involves time going missing, say which of the four stages it was last
seen in — posted, queued, carried, or on the clock. `python dashboard.py --open`
shows all four at once and is usually the fastest way to answer that.

---

## Things that are known, and deliberately open

Do not treat these as oversights — each is recorded on purpose, and two of them
are product questions rather than bugs:

- **`Retry-After` is ignored on a 429**, in favour of the fixed backoff ladder.
  So a server asking for 120 seconds is retried at 60 and earns another 429. Not
  data-losing, but worth fixing.
- **A resumed session produces two worklogs, not one**, because `end_session`
  finalizes on every reason. Whether it *should* read as one continuous stretch
  is a product question. It is pinned by a test, so changing it is a visible
  change.
- **Live rate-limiting is mock-only.** Atlassian cannot be made to 429 on demand.
- **Only Windows has been exercised end to end.** Nothing in the code is
  Windows-specific — the file locking deliberately avoids `fcntl` — but reports
  from macOS and Linux would be genuinely useful.

---

## Licence

By contributing you agree your contributions are licensed under the
[MIT Licence](LICENSE).
