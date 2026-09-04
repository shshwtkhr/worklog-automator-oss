# User Manual

> **Version:** 0.2.0
> Plain English. No code knowledge assumed. If you want to know how it works
> inside, read [TECHNICAL_DOCUMENTATION.md](TECHNICAL_DOCUMENTATION.md).

---

## What this does, in one paragraph

You work in Claude Code. It quietly notices how long you actually spent, works
out which Jira ticket that time belongs to based on which folder you were in, and
posts it to Jira as a worklog. Planyway then shows it on your timeline. You never
type a number into a timesheet.

## What it does not do

- It does not watch you. It only notices that a Claude Code session was active,
  and only in folders you have explicitly mapped to a ticket.
- It does not log time for folders you have not mapped. Those are noted in a
  local file so you can decide later, but nothing is sent anywhere.
- It does not round up. Ever. See [Why the numbers look
  low](#why-the-numbers-look-low).
- It does not send anything outside your machine except the worklog itself, to
  your own Jira.

---

## Setting it up

You do this once. Four steps, five minutes.

### 1. Install the hooks

```bash
python worklog.py install
python post.py install-hook
```

The first tells Claude Code to notify the tool when sessions start, when you do
things, and when sessions end. The second tells it to send anything waiting
whenever a new session starts.

Check it worked:

```bash
python worklog.py doctor
```

Everything should say `ok`. A warning about "0 mapped projects" is expected at
this stage — that is the next step.

### 2. Give it your Jira details

You need three things: your Jira site address, your email, and an **API token**.
The token is not your password. Create one at
[id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens).

Put all three in a file called `credentials.json` inside `~/.claude/worklog/`
(on Windows that is `C:\Users\<you>\.claude\worklog\`):

```json
{
  "base_url": "https://your-site.atlassian.net",
  "email": "you@example.com",
  "api_token": "your-token-here"
}
```

> **Windows warning, and this one genuinely bites.** If you create this file with
> PowerShell's `Set-Content -Encoding utf8`, it adds an invisible marker at the
> start that makes the file unreadable to the tool. You will be told your
> credentials are *missing* while staring at a file that looks perfectly correct.
> Save it as **UTF-8 without BOM** — Notepad's "UTF-8" (not "UTF-8 with BOM") is
> fine, and so is any code editor.

Then check it:

```bash
python post.py check
```

You should see your name and `auth ok`.

### 3. Tell it which folder is which ticket

For each project you want tracked:

```bash
python worklog.py map C:\Projects\example-app PROJ-22
```

To stop tracking one:

```bash
python worklog.py unmap C:\Projects\example-app
```

Not sure what a folder will do? Ask it:

```bash
python worklog.py resolve C:\Projects\example-app
```

**Two things that surprise people:**

- **Subfolders are included.** Mapping `C:\Projects\example-app` also covers
  `C:\Projects\example-app\src\components`. That is usually what you want.
- **It searches upward.** If you map `C:\Projects` itself, *every* project
  underneath it gets that same ticket. Map the individual project folders, not
  the folder that contains them.

### 4. Restart Claude Code

Hooks are read at startup. Until you restart, nothing is being recorded.

---

## Using it day to day

There is nothing to use. That is the point. Work normally.

If you want to look:

| I want to know | Command |
|---|---|
| What is waiting to be sent | `python post.py status` |
| What is being tracked right now | `python worklog.py status` |
| Is my Jira connection healthy | `python post.py check` |
| What *would* be sent, without sending | `python post.py run --dry-run` |
| Send it now, don't wait for the next session | `python post.py run` |
| See everything at once, in a browser | `python dashboard.py --open` |

**`python post.py run --dry-run` is the safe one.** It shows you exactly what
would go to Jira and touches nothing. Get in the habit of running it first.

---

## Seeing what it is doing

```bash
python dashboard.py --open
```

That writes a single HTML file and opens it. No server, nothing to install, works
offline — it is just a file, so you can bookmark it.

![Every tab of the dashboard, then dark mode](dashboard.gif)

It shows, in one page: how much time is unposted and exactly which of the four
stages it is sitting in, every project and what it has logged, live sessions with
their event counts, the queue with any errors, what has been posted, which
unmapped folders are accumulating time, and whether your hooks and credentials
are healthy.

The data is baked into the file when it is written, so it is a snapshot rather
than a live feed.

### The command picker

Under the header is a **Command** dropdown with everything worth running,
grouped: refreshing this page, looking at state, mapping folders, and posting.
Pick one and it is copied to your clipboard, with the real full paths already
filled in, so it runs from any folder.

Two things it does deliberately:

- Anything that **writes to Jira** is marked `⚠ writes to Jira` and turns the
  command red. `post.py run` is the only one that actually sends.
- If your browser refuses clipboard access — which some `file://` pages do — it
  **selects** the command instead and tells you to press Ctrl-C. It never leaves
  you with a button that just says it failed.

### Making the Refresh button actually work

There is a Refresh button at the top. What it does depends on how you opened the
page, and the reason is worth one sentence: **a page opened as a file cannot run
a program.** Browsers forbid it, deliberately — otherwise any web page could run
anything on your machine.

So:

- **Opened as a file** — Refresh says *Copy refresh command*. One click puts
  `python dashboard.py` on your clipboard; paste it in a terminal, then reload
  the page.
- **Opened via `python dashboard.py --serve`** — Refresh genuinely refreshes.
  The page is served from your own machine on `127.0.0.1:8777`, so the button
  can ask it to regenerate. Nothing is exposed to your network.

A third option if you would rather not click anything: run
`python dashboard.py --watch` in a terminal and turn on Auto-reload in the page.
It then updates itself every 30 seconds.

## Why the numbers look low

This is the most common surprise, and it is deliberate.

**It measures active time, not elapsed time.** If you leave a session open and go
to lunch, the gap is ignored. Only stretches where you were actually doing things
count — with a 15-minute grace period, so thinking time still counts.

**It rounds down, never up.** Work 39 minutes and it logs 35, not 40. The
leftover 4 minutes is not thrown away — it is held and added to your next session
on that same ticket.

**Short sessions do not post immediately.** Anything under 5 minutes is held over
rather than posted. Five 3-minute sessions become one 15-minute worklog instead
of five entries or, worse, nothing.

**Sessions under 30 seconds vanish entirely.** Opening a folder to check
something is not work.

The bias is always toward under-reporting. A timesheet that overstates is worse
than one that understates, so when in doubt the tool logs less.

---

## Where your time went if you cannot find it

Time goes to one of four places. In order:

1. **Posted to Jira.** Check the ticket, or `python post.py status`.
2. **Waiting in the queue** because Jira was unreachable, or because it is not due
   for a retry yet. `python post.py status` shows this.
3. **Held as carry-forward** because the session was under the 5-minute minimum.
   `python worklog.py status` shows the balances.
4. **In `unmapped.jsonl`** because the folder is not mapped to a ticket. Nothing
   is lost, but nothing was sent either.

That fourth file is genuinely useful — it is a ranked list of which folders are
eating your time, so you can see which ones deserve a ticket.

---

## Getting your time onto the board sooner

Your time only reaches Jira when a session **ends**. Until then it sits on the
clock, counting up. So if you work all afternoon in one long session, nothing
appears on the board until that session finishes and the next one starts.

Here is what each way of ending a session actually does. This is measured, not
assumed — `tests/test_session_lifecycle.py` checks every row.

| What you do | Time banked to Jira? | Your conversation |
|---|---|---|
| `/clear` | **yes** | New empty one. The old one is still there — recover it from the `/resume` picker |
| `/resume` | **yes** | You move to whichever earlier conversation you pick |
| Quit or log out | **yes** | Ends |
| `/compact` | **no** | Kept, just summarised |
| Fork the session | **no** | Kept |
| Force-kill, same day | **no**, not yet | Ends; the clock is picked up again next time |
| Force-kill, next day | **yes**, swept automatically | Ends |

**`/compact` is the one that surprises people.** It looks like a session
boundary and it isn't. It will push out anything already waiting in the queue,
but the session you are in keeps counting — nothing new gets banked.

**Nothing is ever lost in any of these.** Every second is either posted, waiting
in the queue, held as carry-forward, or still on the clock. That is the one thing
the tests check for every route.

### If you want the time banked without leaving the conversation

There is no command for this yet. The way to do it is to send the session-end
hook by hand. Find your session id first:

```bash
python worklog.py status
```

Then, replacing the id with yours:

```bash
echo '{"session_id":"YOUR-SESSION-ID","cwd":"E:\\Projects\\your-project","reason":"manual-flush"}' | python worklog.py hook session-end
python post.py run --dry-run
python post.py run
```

The conversation is completely untouched. Your time so far is banked as one
worklog, and the clock restarts from zero — so the rest of the session becomes a
second entry. Nothing is double-counted.

Being honest about the cost: this is fiddly, and it is the only route that keeps
you in place. A proper `worklog.py flush` command would be the obvious fix.

## When something goes wrong

### "missing Jira credentials"

Either the file is not there, or it has the invisible BOM marker described above.
Re-save it as UTF-8 without BOM.

### I see the same worklog two or three times

This was a real bug, now fixed. Opening the app could start several
sessions at once, and each one tried to send the queue at the same moment, so
the same entry reached Jira more than once.

If you have duplicates from before the fix, they have to be deleted in Jira by
hand — the tool will not remove a worklog it did not mean to create. Look for
entries with an identical duration and start time, created within the same
second. Keep one, delete the rest.

Only one send can run at a time now, so this cannot recur.

### An entry says `blocked`

Blocked means the tool stopped trying on purpose, because retrying would not
help. `python post.py status` tells you why. Usually one of:

- **401** — your API token is wrong or expired. Make a new one.
- **403** — you do not have permission to log work on that ticket.
- **404** — the ticket does not exist, usually a typo in a mapping.

Fix the cause, then:

```bash
python post.py retry all
python post.py run
```

Nothing is lost while an entry is blocked. It sits in the queue until you deal
with it.

### Something is failing but keeps retrying

That is a `failed` entry, not a blocked one — a temporary problem like Jira being
down or rate-limiting you. It backs off: one minute, then five, then fifteen,
then an hour, then longer. It will keep trying for about a day before giving up
and blocking. You do not need to do anything.

To make it try right now instead of waiting:

```bash
python post.py run --force
```

### Nothing is being recorded at all

In order:

1. `python worklog.py doctor` — are the hooks installed?
2. Did you restart Claude Code after installing them?
3. `python worklog.py resolve <the folder>` — does it find a ticket?
4. Was the session longer than 30 seconds?

### I logged time to the wrong ticket

The tool cannot undo a posted worklog. Delete it in Jira directly, then fix the
mapping with `unmap` and `map` so it does not happen again.

If a folder keeps resolving to a ticket you did not expect after unmapping it,
there is probably a `.jira-project` file in that folder or one above it. `unmap`
will tell you when this is the case, and name the file.

---

## Settings

Edit `~/.claude/worklog/config.json`. All times in minutes.

| Setting | Default | What it means |
|---|---|---|
| `idle_timeout_minutes` | 15 | Gaps longer than this are not counted as work |
| `round_to_minutes` | 5 | Worklogs are a multiple of this, rounded down |
| `minimum_minutes` | 5 | Below this, time is carried forward rather than posted |
| `stale_session_hours` | 12 | Abandoned sessions older than this are closed automatically |
| `capture_git_context` | true | Whether to record branch and commits in the comment |
| `projects` | `{}` | Your folder → ticket mappings. Use `map` rather than editing this. |

---

## What ends up in the Jira comment

Something like:

```
userx/example-app#42: Fix scoring on partial answers | 2 commits: a1b2c3d ...; d4e5f6a ... | logged automatically from Claude Code
```

It includes the GitHub issue number and title when your branch name contains an
issue number, the repository, and up to three commits made during the session.
The last part is always there, so anyone reading the ticket knows the entry was
not typed by hand.

---

## Privacy

Everything except the worklog itself stays on your machine.

What **is** sent to your Jira: the ticket key, the duration, the start time, and
the comment above.

What is **not** sent anywhere: your folder paths, the contents of your files,
what you asked Claude, or anything about unmapped projects.

Two local files worth knowing about, because nothing prunes them: `unmapped.jsonl`
records which folders you worked in and for how long, and `worklog.log` is a
diagnostic log. Both are on your machine only. Delete them whenever you like —
nothing depends on them.

Your Jira API token sits in `credentials.json`. Treat it like a password: it can
do anything in Jira that you can. On Mac or Linux, `chmod 600` it — the tool will
warn you if you have not.
