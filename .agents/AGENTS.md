# Project Workflow Rules

Aggregated from five sibling repositories that all keep their rules at
`.agents/AGENTS.md`. The branching, PR and ticket discipline is carried over
unchanged, because the failures it was written to prevent are not
project-specific — several of the rules below exist because something went wrong
once, and the rule says which. Sections tied to those projects' stacks are replaced with the
equivalents for this Python CLI, and one section is new: this tool writes to a
real timesheet, which no sibling does.

---

## What this repository is

Three Python scripts with no third-party dependencies:

- **`worklog.py`** (PROJ-66) — Claude Code hooks accumulate *active* session
  time per Jira issue and write rounded entries to a local queue.
- **`post.py`** (PROJ-67) — drains that queue into Jira's worklog API, with
  backoff, permanent-vs-transient classification and duplicate adoption.
- **`dashboard.py`** — writes a static, self-contained HTML view of all state.
  **Read-only**: it must never write to the queue, the carry file or Jira. If a
  change to it would need a write, the change belongs somewhere else.

Both are documented in [`docs/`](../docs/). Read
[`docs/TECHNICAL_DOCUMENTATION.md`](../docs/TECHNICAL_DOCUMENTATION.md) before
changing behaviour, and [`docs/USER_MANUAL.md`](../docs/USER_MANUAL.md) to
understand what a user has been told to expect.

---

## The rule that matters most here

**This tool writes to a real timesheet. Never put a number on the board that
nobody worked.**

Everything else in this file is ordinary engineering discipline. This one is the
reason the project exists — PROJ-62 is about curing worklog drift, so a tool
that invents time defeats its own purpose.

- **Never invent a duration.** Rounding is *floor*, never round-to-nearest, and
  the remainder carries forward. A 39-minute session logs 35 minutes and carries
  4; it does not log 40. If you change the rounding, that direction is the
  invariant.
- **Never mark work Done on untested code.** Mock first, then real. A ticket
  marked Done because something compiled and dry-ran is exactly the fiction this
  repository exists to eliminate.
- **Never hand-edit `queue.jsonl` to make a problem go away.** It is the only
  record of time that has been worked but not yet posted. Use `post.py retry`.
- **Say what is not covered.** When a gap cannot be closed, record it explicitly
  rather than leaving it off the list. Live rate-limiting is mock-only because
  Atlassian cannot be made to 429 on demand, and both the tests and the ticket
  say so.

---

## Branching and Pull Requests

- **Never work directly on the `main` branch.**
- **Before the first edit of any request, ask the user which branch to work on.**
  This applies to every request, not only new tasks — continuing a multi-step
  piece of work on the branch that happens to be checked out is exactly how the
  wrong branch gets used. Check whether the current branch already has an open or
  recently merged PR and say so, because that usually changes the answer.
  - **Scope:** this covers anything that changes a file in this repository,
    including documentation and config. It does **not** cover writes outside the
    repository — scratch files, temporary scripts, an agent's own notes — which
    never reach a branch and so need no confirmation.
  - Asking costs one question; committing to the wrong branch costs a rebase, a
    reopened PR, or work stranded on a branch nobody is reviewing.
- Branch names depend on **where the work was tracked**. Pick the scheme that
  matches the tracker the task came from — they are not interchangeable, and the
  prefix is how anyone later finds the ticket the branch belongs to.

  | Work tracked in | Format | Example |
  |---|---|---|
  | **GitHub issue** | **`GH-<issue-no>-<semver>`** | `GH-2-0.2.0` |
  | **Jira ticket** | **`PROJ-<ticket>-<semver>`** | `PROJ-67-0.2.0` |

  - `<issue-no>` is the GitHub issue number, without a `#`.
  - `<semver>` is the **target** release version, not the current one. Several
    branches may share a version; that is expected, not a clash.
  - When the work spans multiple tickets, chain them in ticket order before the
    version: `PROJ-66-PROJ-67-0.2.0`. Include a ticket only if the branch
    actually delivers it; a ticket merely touched belongs in the PR body.
  - Read the prefix back to the user before creating the branch.
    `TEHCDL-10-1.2.0` reached `main` as a typo on a sibling project and is now
    permanent in three merge commit messages.
- Once finished, commit and push the branch.
- Remind the user to raise a PR to `main`, or raise it yourself if GitHub tools
  are available.
- Ask the user for the PR title prefix. With multiple tickets, bracket each one:
  `[PROJ-66][PROJ-67]`.

### Keeping the PR current

**The description is the current state; comments are the timeline.** A reviewer
opens a PR, reads the description, and may never scroll to comment nine.

- When a commit closes something the description lists as open, or changes what
  the PR delivers, **update the description in the same push** — not at the end,
  not when review is requested.
- Post an update comment per meaningful batch of work. It does not discharge the
  obligation above.
- When later work contradicts an earlier claim, **say so explicitly**. Correct
  the description and state the correction in a comment rather than silently
  editing the claim away — anyone who already read it needs to know.
- Before requesting review, re-read the description end to end against the
  commit list.

### Post-merge cleanup

Do this **immediately after a PR merges**, before cutting the next branch. A
stale local `main` is the failure that matters: branching from it silently
re-introduces the merge and produces a PR that appears to revert work.

1. `git checkout main`
2. `git pull --ff-only` — if this refuses to fast-forward, something was
   committed to `main` directly. Stop and resolve it; do not force.
3. `git branch -d <merged-branch>` — lowercase `-d` is deliberate. It refuses to
   delete anything not fully merged. Never reach for `-D` to silence the error.
4. `git push origin --delete <merged-branch>`
5. Confirm with `git log --oneline -1` and `git branch -a`.

If in doubt whether something landed, check containment rather than reading the
diff — a merged PR shows its full diff under *Files changed* forever:

```
git merge-base --is-ancestor <branch-sha> origin/main
```

### Issue and ticket status

Tracker status is part of the change, not an afterthought — a board that lies
about what is in flight is worse than no board.

**Jira (PROJ-\*, UP-\*)**

- Move to **In Progress** when work actually starts, i.e. when the branch is cut.
- Move to **Done** only when the PR delivering it has **merged**, and only when
  the code has actually run against the real dependency. See *The rule that
  matters most here*. A ticket marked Done against an open PR is a lie the moment
  review asks for changes.
- A ticket spanning several phases stays **In Progress** until its final phase
  merges. Note the delivered slice in the ticket rather than closing it early.
- **Never transition a ticket without being asked.** Recording findings in a
  comment is not the same as moving the board, and the board is the user's call.

**GitHub issues**

- Reference the issue in the PR body with a closing keyword — `Closes #2` — so
  the merge closes it. Do not close issues by hand; the link records *which*
  change resolved it.
- Only use a closing keyword if the PR genuinely completes the issue. For a
  partial fix, reference it without the keyword and say what remains.

**Both**

- State the status transitions you made in the PR description.

---

## Terminal Commands

- No permission is needed to run terminal commands impacting this project while
  performing a task. Run project-related shell commands proactively. This covers
  the test suites, `post.py --dry-run`, `post.py check`, `post.py status` and
  `worklog.py doctor`.
- It does **not** extend to **`post.py run` without `--dry-run`**, which posts
  real worklogs to a real Jira instance. Dry-run first, show the user what will
  be sent, and only then post.
- This repository targets Windows. Prefer PowerShell; the code itself must not
  assume a POSIX shell, and no tracked file may contain an absolute machine path.
- PowerShell 5.1 gotchas that have already cost time on this project and its
  siblings:
  - `&&` and `||` are parser errors. Use `;` or `if ($?) { }`.
  - `Get-Content -Raw` on a BOM-less UTF-8 file guesses ANSI and silently mangles
    non-ASCII. **Always pass `-Encoding UTF8`.**
  - **`Set-Content -Encoding utf8` writes a BOM.** Use
    `[System.IO.File]::WriteAllText($p, $s, (New-Object System.Text.UTF8Encoding $false))`.
    This bites *this* repository specifically: `credentials.json` written with a
    BOM makes `json.load` throw, `read_json` swallows the exception and returns
    `{}`, and `post.py` reports "missing Jira credentials" while a perfectly good
    file sits on disk. The failure looks like a wrong token.
  - `foreach (...) { } | Format-Table` is an empty-pipe-element error. Collect
    into a variable first.
  - **Prefer the editing tools over shell rewrites for tracked files.** Shell
    round-trips introduce BOMs, normalise line endings, and turn a one-word
    change into a whole-file diff.
- Git Bash gotcha: a quoted heredoc (`<<'EOF'`) carrying Python with mixed
  quoting can fail to parse. Write the file with an editing tool instead of
  fighting the shell.

---

## Secrets and what must never be committed

- **Never commit a Jira API token, and never hardcode the user's email address,
  Atlassian cloud ID or site URL into source.** All of it is read from
  environment variables or `~/.claude/worklog/credentials.json`.
- **Never print a token**, not even truncated, and never into a commit message,
  an issue body, a PR description or a Jira comment. When credentials must be
  set up, have the user write them via a silent prompt so the value never enters
  a transcript or shell history.
- `credentials.json` lives in the **state directory, not the repository**. It is
  never tracked here at all. On POSIX it should be `chmod 600`; `post.py` warns
  when it is not.
- `.jira-project` is gitignored. It is per-developer local config, and committing
  it would map the repository to one person's ticket for everybody.
- Nothing under the state directory (`~/.claude/worklog/`) is ever committed:
  `queue.jsonl`, `posted.jsonl`, `unmapped.jsonl` and `worklog.log` contain
  working patterns and directory paths.
- `.claude/settings.local.json` is per-developer and gitignored.

---

## Testing

- **`python tests/test_post.py`** — post.py end to end against a scriptable mock
  Jira on `127.0.0.1`. No network, no credentials, no Jira account needed.
- **`python tests/test_worklog.py`** — worklog.py's mapping commands.
- **`python tests/test_session_lifecycle.py`** — what each way of ending a
  session does to the clock. Characterisation tests: they pin behaviour a user
  has to be able to predict, so changing one is a deliberate act.
- **`python tests/test_dashboard.py`** — the generated page: that it is
  self-contained, that generating it never writes to the state it reads, and
  that the refresh command it prints regenerates *that* page rather than a
  different one.
- Both are standalone runners with no third-party dependencies, matching the
  scripts themselves. **Do not introduce pytest** — the whole point is that this
  runs anywhere Python does.
- A case name substring filters: `python tests/test_post.py 429`.
- **New behaviour ships with its test in the same commit.** Every bug found so
  far was found by a test that did not exist until someone wrote it.
- When a test proves a real defect, keep the case and invert the assertion to
  the fixed behaviour. Do not delete the case that caught it.
- Tests must not touch the real state directory. Set `CLAUDE_WORKLOG_DIR` to a
  temp path, as both suites do.

---

## Documentation

Documentation drift is treated as part of the change, not follow-up work.

- Whenever a **command is added, renamed or removed** in any script, update
  three things in the same commit: the `Usage:` block in that script's module
  docstring, the command table in `docs/USER_MANUAL.md`, and the command
  reference in `docs/TECHNICAL_DOCUMENTATION.md` §7.
- Whenever a **config key** is added, renamed or removed, update `DEFAULT_CONFIG`
  and the settings table in `docs/USER_MANUAL.md`.
- Whenever the **queue entry shape** changes, update
  `docs/TECHNICAL_DOCUMENTATION.md` §4. Both scripts read that structure, and it
  is the contract between them.
- Whenever **retry classification** changes (`RETRY_STATUS`, `BACKOFF_SECONDS`,
  `MAX_ATTEMPTS`), update `docs/TECHNICAL_DOCUMENTATION.md` §6 **and** the
  plain-English explanation in `docs/USER_MANUAL.md`. Users make decisions based
  on how long something will keep trying.
- Whenever the **worklog comment format** changes, `worklog.render_comment()`
  stays the single source of truth. Do not build the string anywhere else.
- Whenever a **command is added to `dashboard.py`**, or a new field is surfaced,
  update its module docstring and the dashboard sections in
  `docs/USER_MANUAL.md` and `README.md`. The dashboard is how anyone sees state
  without reading jsonl by hand; a field it does not show may as well not exist.
- Whenever a **version** is released, `VERSION` in all three scripts and the git
  tag move together. The git tag is canonical.
- Documents describing repository *status* should link to the ticket rather than
  restate it. Restated status goes stale.

---

## PR Checklist

Confirm each of these before raising a PR:

- [ ] Branch name matches the scheme for its tracker, spelled correctly
- [ ] `python tests/test_post.py` passes with no network access
- [ ] `python tests/test_worklog.py` passes
- [ ] `python tests/test_session_lifecycle.py` passes
- [ ] `python tests/test_dashboard.py` passes
- [ ] New behaviour has a test in the same commit
- [ ] `python worklog.py doctor` is green
- [ ] Docs updated per the rules above, if commands, config keys, the queue
      shape, retry classification or the comment format changed
- [ ] No secrets, no tokens, no personal identifiers, no absolute machine paths
      staged
- [ ] No BOM in any tracked text file
- [ ] `.jira-project` not staged
- [ ] No third-party dependency introduced
- [ ] Rounding still floors and still carries the remainder
- [ ] Jira tickets delivered by this PR are **In Progress**, and any ticket whose
      description this PR contradicts has been updated in the same PR
- [ ] GitHub issues fully resolved are referenced with `Closes #N`
- [ ] Previous branch cleaned up before this one was cut

## After the PR merges

- [ ] Run the Post-merge cleanup steps
- [ ] Move delivered Jira tickets to **Done** — only now, only if the code has
      run against real Jira, and only if the user has asked for the transition
