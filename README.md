# worklog-automator

**Your Jira board says the work took four hours. It took nine. Nobody logged the
other five.**

This fixes that, by never asking you to log anything.

---

## The problem

Manual time tracking fails in a specific, predictable way.

You finish a task, you mean to log it, and you don't. A day later you log
something — a round number, remembered generously, and rounded up because that
feels fair. Multiply that across a board and estimates stop meaning anything:
every ticket looks like it took the time it was estimated to take, because the
estimate is what got typed in.

The usual fixes make it worse. A timer you have to start is one more thing to
forget. A tool that logs wall-clock time bills you for lunch. A tool that rounds
to the nearest half hour invents time nobody worked, which is the same disease
with better manners.

## The solution

Claude Code already fires hooks when a session starts, when you send a message,
on every tool call, and when the session ends. That is enough to measure how long
you were *actually working* — and to attribute it, because the working directory
identifies the project, and the project maps to a Jira issue.

So: no timer to start, no form to fill in, and no number invented on your behalf.

![The dashboard: every tab, the command picker, then dark mode](docs/dashboard.gif)

Planyway needs no integration — it has no API and no custom fields, it renders
native Jira time tracking. A standard worklog with `adjustEstimate=auto` is what
moves its timeline.

---

## How it works

Three scripts, standard library only, **no third-party dependencies**.

| Script | Does |
|---|---|
| **`worklog.py`** | Hooks accumulate *active* session time per Jira issue into a local queue |
| **`post.py`** | Drains that queue into Jira, with backoff and duplicate protection |
| **`dashboard.py`** | Writes a static HTML page showing where every minute is |

Time moves through four stages, and nothing advances on its own except by the
rule stated beside it:

| Stage | Moves on |
|---|---|
| **On the clock** | Gaps between hook events, counted only when under 15 minutes |
| **Carried** | A session under 5 minutes is held, not dropped, and added to the next one on that issue |
| **Queued** | Written at session end, floored to 5 minutes. Never rounded up |
| **In Jira** | Drained on the next `SessionStart`. One drain at a time |

The two halves are separate processes with a file between them. `SessionEnd` has
a short budget in Claude Code, so the hook path does local file I/O only — no
network, no `gh`, no git call that can hang. Everything that can block happens
later, in `post.py`, where it is allowed to fail and retry.

---

## Quick start

```bash
python worklog.py install           # session hooks -> ~/.claude/settings.json
python post.py install-hook         # drain the queue on every SessionStart
```

Put your Jira details in `~/.claude/worklog/credentials.json`:

```json
{
  "base_url": "https://your-site.atlassian.net",
  "email": "you@example.com",
  "api_token": "..."
}
```

The token comes from
[id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
— it is not your password. **Save the file as UTF-8 without a BOM**; PowerShell's
`Set-Content -Encoding utf8` adds one, and the result reports as *missing
credentials* rather than as a malformed file.

Map each project:

```bash
python worklog.py map C:\Projects\example-app PROJ-22 userx/example-app
python worklog.py doctor            # everything should say ok
python post.py check                # verifies credentials, read-only
```

That is the whole setup. Work normally.

**Restarting is only needed once**, after `install`, because Claude Code reads
hooks at startup. Mapping a project takes effect immediately — `map` hands the
new key to any session already running in that directory, and a session end
re-resolves anyway, so time worked before the mapping existed is not lost.

---

## What gets logged — and what does not

The single rule everything else follows: **never over-report.** Every decision
below loses a little real time on purpose, because a timesheet that overstates is
worse than one that understates.

- **Active time, not elapsed time.** Only gaps between hook events under
  `idle_timeout_minutes` count. A longer gap contributes **nothing** — the whole
  gap is discarded, not just the excess.
- **Rounding floors.** 39 minutes logs 35. The remaining 4 is not thrown away; it
  carries to your next session on that issue.
- **Short sessions carry rather than post.** Five 3-minute sessions become one
  15-minute worklog instead of five noisy entries — or, worse, nothing.
- **Under 30 seconds of activity produces nothing at all.** Opening a folder to
  check something is not work.

This is why the numbers look lower than your day felt. That is the design, not a
defect.

### Nothing is ever silently dropped

If time is not on the board it is in one of four places, and
`python dashboard.py --open` shows all four at once:

1. **Posted** to Jira
2. **Queued** — Jira was unreachable, or the entry is not due for a retry yet
3. **Carried** — the session was under the minimum
4. **In `unmapped.jsonl`** — the folder resolves to no issue, so nothing was sent

That last file doubles as a ranked list of which folders are earning enough time
to deserve a ticket.

---

## How a directory becomes an issue key

Checked in order, first hit wins:

1. `CLAUDE_WORKLOG_ISSUE` — a per-shell override
2. A **`.jira-project`** file, walking *up* from the working directory
3. The **`projects`** map in `~/.claude/worklog/config.json`, longest path prefix wins

`.jira-project` accepts a bare key or key–value lines:

```
issue_key: PROJ-22
github: userx/example-app
```

**Two things that surprise people.** Subdirectories are included, which is
usually what you want. And the search goes *upward* — a marker at
`C:\Projects\` would capture every project beneath it, so map the individual
project folders rather than the folder containing them.

A marker **beats** the central map, so `unmap` alone will not stop tracking a
directory that has one. `unmap` says so when that is the case.

---

## Commands

### `worklog.py` — capture

| Command | What it does |
| :-- | :-- |
| `install` | Merge hooks into `~/.claude/settings.json` |
| `status` | Live sessions, carry balances, today's total |
| `queue [--json]` | Pending worklogs waiting to be posted |
| `resolve [path]` | Explain how a path maps to an issue key |
| `map <path> <KEY> [gh-slug]` | Add a directory mapping |
| `unmap <path>` | Remove one; warns if a `.jira-project` marker still captures it |
| `doctor` | Verify hooks, config and runtime |

### `post.py` — send

| Command | What it does |
| :-- | :-- |
| `install-hook` | Drain the queue on every `SessionStart` |
| `check` | Verify credentials and that queued issues are reachable. Read-only. |
| `run --dry-run` | Show exactly what would be posted. **Do this first.** |
| `run [--limit N] [--force]` | **Posts real worklogs to real Jira.** |
| `status` | Queue health, blocked entries and why |
| `retry <id\|all>` | Clear backoff, unblock, and try again |

### `dashboard.py` — see

| Command | What it does |
| :-- | :-- |
| *(no args)* | Write `<state>/dashboard/index.html` |
| `--open` | ...and open it |
| `--watch [seconds]` | Regenerate on a timer |
| `--serve [port]` | Serve on `127.0.0.1` so the page's Refresh button really works |

---

## When something goes wrong

**An entry says `blocked`.** That means retrying would not help, so it stopped on
purpose. `python post.py status` says why — usually a bad token (401), no
permission to log work (403), or a typo'd issue key (404). Fix the cause, then
`post.py retry all`. Nothing is lost while an entry is blocked; it waits.

**An entry says `failed`.** A temporary problem. It backs off — one minute, five,
fifteen, an hour, then longer — for about a day before giving up and blocking.
You need do nothing. `post.py run --force` tries immediately.

**Nothing is being recorded.** In order: `worklog.py doctor` (are the hooks
installed?), did you restart Claude Code, `worklog.py resolve <folder>` (does it
find a ticket?), and was the session longer than 30 seconds?

Deeper troubleshooting is in [docs/USER_MANUAL.md](docs/USER_MANUAL.md).

---

## Design decisions worth knowing

**A timeout on a POST says nothing about the server.** The request may have
landed. So any entry that has been attempted before is checked against Jira and
an existing match is *adopted* rather than duplicated — and a duplicate check
that itself fails parks the entry instead of posting, because "I checked and it
is not there" and "I could not check" must not collapse into the same answer.

**Only one drain runs at a time.** Reopening the app starts several sessions at
once and every `SessionStart` fires the drain hook. Without a lock, each process
reads the same pending entry and posts it.

**A killed terminal fires no hook at all.** `SessionStart` sweeps sessions whose
last heartbeat is older than `stale_session_hours` and finalises them against
their original start time, so the time still lands on the right day.

**Hooks never break a session.** Every entry point swallows exceptions, logs to
`worklog.log`, and exits 0. Nothing prints to stdout, because Claude Code injects
`SessionStart` and `UserPromptSubmit` stdout into the model's context.

**No dependencies, deliberately.** A hook that fails because a virtualenv moved
is a hook that silently stops recording your time, and you find out at the end of
a sprint.

---

## What it does not do

- It does not watch you. It notices that a session was active, and only in
  folders you explicitly mapped.
- It does not send your paths, file contents or prompts anywhere. What reaches
  Jira is an issue key, a duration, a start time, and a comment naming the repo,
  branch and up to three commit subjects.
- It does not honour Jira's `Retry-After` on a 429 — it uses its own backoff
  ladder, so a server asking for 120s is retried at 60.
- A **resumed** session produces two worklogs rather than one, because
  `end_session` finalises on every reason. Pinned by a test, so changing it is a
  deliberate change.
- Live rate-limiting is mock-only; Atlassian cannot be made to 429 on demand.
- Only Windows 11 with Python 3.14 has been exercised end to end. Nothing is
  Windows-specific — the file locking deliberately avoids `fcntl` — but that is
  the only platform actually proven.

---

## Tests

Four suites, no third-party runner, no network, no Jira account.
`tests/mock_jira.py` is a scriptable stand-in for Jira's REST v3 worklog API.

    python tests/test_post.py                 # posting, retry, duplicate adoption (24)
    python tests/test_worklog.py              # project mapping commands (15)
    python tests/test_session_lifecycle.py    # what each session end does to the clock (10)
    python tests/test_dashboard.py            # the generated page (9)

Pass a substring to filter: `python tests/test_post.py 429`.

---

## Contributing

Issues and pull requests welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

Two things worth knowing before you start: **no third-party dependencies** is a
hard rule rather than a preference, and a change that makes the tool log *more*
time has to argue for itself. Both are explained there, along with the handful of
issues left deliberately open.

Reports from macOS and Linux would be especially useful.

## Licence

MIT — see [LICENSE](LICENSE).

## Documentation

| Document | For |
|---|---|
| [docs/USER_MANUAL.md](docs/USER_MANUAL.md) | Using it: setup, mapping, why numbers look low, troubleshooting |
| [docs/TECHNICAL_DOCUMENTATION.md](docs/TECHNICAL_DOCUMENTATION.md) | Changing it: architecture, state files, the queue schema, the retry model |
| [docs/DEVELOPMENT_NARRATIVE.md](docs/DEVELOPMENT_NARRATIVE.md) | Why it looks like this: the bugs that shaped it, and what a mock could not catch |
| [.agents/AGENTS.md](.agents/AGENTS.md) | Working on it: branching, PRs, ticket status, secrets, checklists |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contributing: the no-dependencies rule, what a good test looks like |

`tools/make_demo_gif.py` regenerates the GIF above — headless Chromium for the
frames, then a standard-library PNG reader, median-cut quantiser and GIF89a/LZW
writer. No Pillow, no ffmpeg, no `node_modules`.
