# CLAUDE.md

> ## Read [`.agents/AGENTS.md`](.agents/AGENTS.md) first. It is the canonical rules file for this repository and it is not optional.
>
> This file deliberately does **not** restate it. Rules copied into two places go stale in one of them, and a rules file that is wrong is worse than one that is missing. What follows is orientation and a quick-reference index only.

`.agents/AGENTS.md` follows the same convention as five sibling repositories, which all keep their workflow rules there. It covers branching and pull requests, keeping a PR description current, post-merge cleanup, ticket status, terminal and PowerShell gotchas, secrets, testing, documentation duties, and the PR checklist.

---

## What this repository is

Automatic worklog capture from Claude Code into Jira, so time reaches the board without anyone typing it in. Implements PROJ-62.

Three Python scripts, **no third-party dependencies**, standard library only:

- **`worklog.py`** (PROJ-66) — Claude Code hooks accumulate *active* session time per Jira issue into a local queue.
- **`post.py`** (PROJ-67) — drains that queue into Jira's worklog API.
- **`dashboard.py`** — writes a static, self-contained HTML view of all of it. Read-only; never touches the queue or Jira.

Planyway has no API of its own; it renders native Jira time tracking. `adjustEstimate=auto` on the worklog POST is what moves the Planyway timeline.

---

## The three rules most easily broken

Stated here as well as in `.agents/AGENTS.md` because breaking any of them puts a number on a real timesheet that nobody worked.

1. **Never invent a duration.** Rounding floors and carries the remainder. 39 minutes logs 35 and carries 4.
2. **Never mark a ticket Done on untested code, and never transition a ticket unasked.** Mock first, then real.
3. **Never work directly on `main`, and ask which branch before the first edit** — including documentation and config.

---

## Document index

| Document | What it covers |
|---|---|
| [.agents/AGENTS.md](.agents/AGENTS.md) | **The rules.** Branching, PRs, ticket status, secrets, testing, docs, checklists |
| [docs/USER_MANUAL.md](docs/USER_MANUAL.md) | Plain-English guide: install, map projects, what gets logged, troubleshooting |
| [docs/TECHNICAL_DOCUMENTATION.md](docs/TECHNICAL_DOCUMENTATION.md) | Architecture, state files, queue schema, retry model, command reference |
| [docs/DEVELOPMENT_NARRATIVE.md](docs/DEVELOPMENT_NARRATIVE.md) | The incidents that shaped the design. Read before changing the retry or locking logic. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contributing: the no-dependencies rule, what a good test looks like |
| [README.md](README.md) | Problem, solution, quick start, commands |

---

## Commands

Real as of `0.2.0`. Keep current from the first commit that changes one — the duty is in [`.agents/AGENTS.md`](.agents/AGENTS.md) under *Documentation*.

| Command | Does |
|---|---|
| `python worklog.py install` | Install session hooks into `~/.claude/settings.json` |
| `python worklog.py doctor` | Check hooks, config, runtime. **Run this first when anything looks wrong.** |
| `python worklog.py map <path> <KEY>` | Map a directory to a Jira issue key |
| `python worklog.py unmap <path>` | Remove that mapping |
| `python worklog.py resolve [path]` | Explain how a path resolves to an issue key |
| `python worklog.py status` | Active sessions, carry balances, today's total |
| `python post.py install-hook` | Drain the queue on every SessionStart |
| `python post.py check` | Verify credentials and that queued issues are reachable. **Read-only.** |
| `python post.py run --dry-run` | Show exactly what would be posted. **Always do this first.** |
| `python post.py run` | **Posts real worklogs to real Jira.** Not a casual command. |
| `python post.py status` | Queue health, blocked entries and why |
| `python dashboard.py --open` | Write and open a static HTML view of all state. No server. |
| `python dashboard.py --serve` | Same page on `127.0.0.1`, where its Refresh button works |
| `python tools/make_demo_gif.py` | Regenerate `docs/dashboard.gif`. Needs Edge or Chrome; no other dependency. |
| `python post.py retry <id\|all>` | Clear backoff / unblock and try again |
| `python tests/test_post.py` | post.py end to end against a mock Jira. No network or credentials needed. |
| `python tests/test_worklog.py` | worklog.py mapping commands |

### Notes that will save you time

- **The state directory is `~/.claude/worklog/`, not the repository.** Set `CLAUDE_WORKLOG_DIR` to redirect it — that is how the tests stay off your real queue.
- **`credentials.json` written by PowerShell 5.1 usually has a BOM**, which makes `json.load` throw; `read_json` swallows it and returns `{}`, so `post.py` reports missing credentials while the file looks fine. Write it BOM-free. This is in `.agents/AGENTS.md` under *Terminal Commands*.
- **Issue resolution order is env → `.jira-project` walking up → central map.** A marker beats the map, so `unmap` alone will not stop tracking a directory that has one. `unmap` warns when this is the case.
- **The resolution walk goes up.** A `.jira-project` at a parent directory captures every project beneath it.
- **Sessions under 30 seconds of active time produce nothing at all**, and anything under `minimum_minutes` carries forward rather than posting. A short session leaving no trace is correct behaviour, not a bug.
- **`post.py run` makes no network call when the queue is empty** — it checks for due entries before building a client. A SessionStart hook on an empty queue costs nothing.
- **A timeout on a POST says nothing about the server.** The request may have landed. This is why any entry with `attempts > 0` gets a duplicate check before it is retried, and why a *failed* duplicate check must park the entry rather than post.
