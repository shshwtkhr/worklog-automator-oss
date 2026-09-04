# How this was built, and what went wrong

> This is the part that usually gets lost. The code shows what the tool does; it
> does not show which decisions were expensive, or which bugs only appeared once
> real usage found them. Every incident below actually happened.

---

## The premise

A worklog tool has an unusual failure mode: **it can put a number on a timesheet
that nobody worked.** Ordinary bugs produce a crash or a wrong answer you can
see. This one produces a plausible number that quietly becomes payroll, invoicing
or a sprint retrospective.

So the design rule is not "be accurate", it is "**never over-report**". Rounding
floors and carries the remainder. Idle gaps are discarded whole, not clipped.
Sessions below a noise floor vanish entirely. Every one of those choices loses a
little real time on purpose, because the alternative loses trust.

---

## The bug that a mock could never have caught

`post.py` was written with a duplicate check: a request can succeed and still
fail to return — a client-side timeout says nothing about the server side — so
any entry that has been attempted before is checked against the server before it
is retried, and a matching worklog is *adopted* rather than duplicated.

That was tested thoroughly. A scriptable mock committed a worklog and then hung
past the client timeout; the retry adopted the orphan; exactly one worklog
existed at the end. Twenty cases, all green.

Then the desktop app was reopened, and **one ten-minute session appeared on the
board three times**.

Reopening starts *several* sessions at once. Every `SessionStart` fires the drain
hook. Three processes read the same pending entry and each posted it. The
duplicate check did not help — and could not:

```python
if int(entry.get("attempts", 0)) > 0:      # <- the guard
    match = find_existing(jira, entry, account_id)
```

It is gated on `attempts > 0`, which defends a **sequential** retry after a
failure. In a race, every process holds a fresh entry at `attempts == 0`, so none
of them looks first.

The lesson is not "write more tests". It is that the test suite was thorough
about the axis the author had already thought of. The mock proved the state
machine; it could not disprove an assumption baked into the guard itself. The fix
is a lock held across the whole read → post → write cycle, and the test fires
five simultaneous drains and asserts exactly one POST — verified to fail with
five when the lock is removed, so it reproduces the reported failure rather than
merely passing.

---

## A near-identical bug, found by a test that did not exist yet

Earlier, the same duplicate check had a quieter flaw. `find_existing` caught
`JiraError` and returned `None` — indistinguishable from "checked, nothing
there". A lookup that was itself rate-limited fell straight through to a POST.

Reachable exactly when it matters most: on a retry after a rough patch, when the
next request is also likely to fail.

`find_existing` now raises instead, and the caller parks the entry. **"I checked
and it is not there" and "I could not check" must never collapse into the same
answer.** That sentence is in the code, because the type system will not say it
for you.

---

## Things only the real dependency could confirm

Before the first real request, the following were all *believed* to work and none
had been demonstrated:

- `adjustEstimate=auto` moving the remaining estimate
- the ADF comment body being accepted (REST v3 rejects a plain string)
- a colon-less `+0530` offset in `started` round-tripping to the same instant
- the duplicate lookup matching a real server response shape

All four passed on first contact. That is not the point — the point is that a
read-only credential check passing was *necessary and not sufficient*, and every
one of those four lives on the write path. A green test suite against a mock says
nothing about the wire format.

---

## What a browser will not let you do

The dashboard is a single self-contained HTML file: no server, no CDN, works
offline. That design falls out of one constraint — **a `file://` page cannot
fetch a sibling `data.json`**, because CORS forbids it. This is the usual reason
a "static dashboard" quietly still needs a server. Embedding the data removes the
need entirely.

The same constraint bites the Refresh button. A page opened as a file cannot run
a program; browsers forbid launching processes, and no JavaScript works around
it. So the button has two behaviours chosen from `location.protocol`: served, it
regenerates; unserved, it copies the command. When even the clipboard is
unavailable — it needs a user gesture *and* a permitted context — it selects the
text and says to press Ctrl-C. A button that can only report failure is worse
than no button.

One follow-on bug is worth recording because it looked like nothing at all: the
copied command originally omitted `--output`, so running it regenerated the
*default* file rather than the page you were reading. It reported success and
changed nothing visible.

---

## An inconsistency left in deliberately

`start_session` carries a branch commented *"resume / fork: keep accumulated
time"*. It cannot fire on a real resume: `end_session` unlinks the session record
unconditionally in a `finally`, so by the time `SessionStart` arrives there is
nothing to find. It serves fork, compaction and a same-day restart instead.

The consequence is that a suspended-and-resumed session produces two worklogs
rather than one. No time is lost, and sub-minimum fragments carry forward.

It was left as it is, and **pinned by a test asserting the current behaviour**,
because whether a resumed session should read as one continuous stretch is a
product question, not a code one. Characterisation tests make that a deliberate
choice rather than an accident — changing it now requires changing a test that
says what it does.

---

## Documentation drift, measured

Documentation went stale across roughly two hours of work, while the rule against
it sat in the file being edited. An audit found the README still saying *"Nothing
is sent to Jira"* about a feature that had worked for hours, both rules files
claiming "two Python scripts" with a third beside them, and test counts wrong in
two places.

The conclusion drawn was not "try harder". It was that a rule nothing enforces is
a rule that decays, and the check that caught all of it was a fifteen-line script
comparing every command in the code against every document.

---

## Why there are no dependencies

Everything here runs from a hook. A hook that fails because a virtualenv moved,
a package upgraded, or a wheel would not build is a hook that silently stops
recording your time — and you find out at the end of a sprint.

Standard library only means the whole thing installs by copying files. That
constraint held even where it cost something: the demo GIF is captured with a
browser that is already installed and assembled by a hand-written PNG reader,
median-cut quantiser and GIF89a/LZW encoder, rather than pulling in an imaging
library, `ffmpeg`, or a headless-browser package.

---

## The shape of the whole thing

Two processes with a file between them, and the file is the contract.

The capture side does local I/O only, because `SessionEnd` has a short budget in
Claude Code — no network, no subprocess that can hang. Everything expensive
happens later, in a separate process, where it is allowed to fail and retry.

That split is why an offline laptop loses nothing, why a killed terminal is
reclaimed by a sweeper, and why the slowest part of the system — a `gh` lookup
for an issue title — never delays a session by a millisecond.
