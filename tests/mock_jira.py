#!/usr/bin/env python3
"""A scriptable stand-in for Jira Cloud's REST v3 worklog API.

Behaviour is read from a scenario JSON file on *every* request, so a test can
change how the server responds between post.py invocations without restarting
it. Every request is appended to requests.jsonl so tests can assert on exactly
what went over the wire, which is the whole point: post.py has never made a
real request, so "it did not crash" is not evidence of anything.

Scenario keys ("myself", "post_worklog", "get_worklog", "get_issue") each take:

    {"mode": "ok"}                 normal response
    {"mode": "status", "code": 429, "body": {...}, "headers": {...}}
    {"mode": "hang", "seconds": 25}              no response, then close
    {"mode": "commit_then_hang", "seconds": 25}  store it, THEN hang
    {"mode": "commit_then_drop"}   store it, then kill the connection
    {"mode": "sequence", "steps": [ ...any of the above... ]}
                                   step N applies to the Nth matching request,
                                   the last step repeats once exhausted
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATE = Path(os.environ.get("MOCK_JIRA_STATE", "."))
SCENARIO = STATE / "scenario.json"
REQUESTS = STATE / "requests.jsonl"
WORKLOGS = STATE / "worklogs.json"

ACCOUNT_ID = "5f8a1b2c3d4e5f6a7b8c9d0e"
LOCK = threading.Lock()
COUNTERS: dict[str, int] = {}


def scenario_for(name: str) -> dict:
    try:
        data = json.loads(SCENARIO.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    spec = data.get(name) or {"mode": "ok"}
    if spec.get("mode") == "sequence":
        steps = spec.get("steps") or [{"mode": "ok"}]
        with LOCK:
            index = COUNTERS.get(name, 0)
            COUNTERS[name] = index + 1
        return steps[min(index, len(steps) - 1)]
    return spec


def load_worklogs() -> dict:
    try:
        return json.loads(WORKLOGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_worklogs(data: dict) -> None:
    WORKLOGS.write_text(json.dumps(data, indent=2), encoding="utf-8")


def jira_stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}" + dt.strftime("%z")


def store_worklog(issue_key: str, body: dict, author: str = ACCOUNT_ID) -> dict:
    """Commit a worklog exactly as Jira would, and hand back what it returns."""
    with LOCK:
        data = load_worklogs()
        record = {
            "id": str(10000 + sum(len(v) for v in data.values())),
            "issueId": "100001",
            "author": {"accountId": author, "displayName": "Test User"},
            "timeSpentSeconds": int(body.get("timeSpentSeconds", 0)),
            "timeSpent": f"{int(body.get('timeSpentSeconds', 0)) // 60}m",
            "started": body.get("started"),
            "comment": body.get("comment"),
            "created": jira_stamp(datetime.now().astimezone()),
        }
        data.setdefault(issue_key, []).append(record)
        save_worklogs(data)
    return record


def to_ms(started: str) -> int | None:
    if not started:
        return None
    text = started
    if len(text) > 5 and text[-5] in "+-" and ":" not in text[-5:]:
        text = text[:-5] + text[-5:-2] + ":" + text[-2:]
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    # ------------------------------------------------------------- plumbing
    def record(self, method: str, body: str | None) -> None:
        parsed = urlparse(self.path)
        entry = {
            "at": time.time(),
            "method": method,
            "path": parsed.path,
            "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
            "auth": self.headers.get("Authorization"),
            "user_agent": self.headers.get("User-Agent"),
            "content_type": self.headers.get("Content-Type"),
        }
        if body:
            try:
                entry["body"] = json.loads(body)
            except Exception:
                entry["body_raw"] = body[:2000]
        with LOCK:
            with REQUESTS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")

    def send_json(self, code: int, payload, headers: dict | None = None) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def apply(self, spec: dict, on_commit=None) -> bool:
        """Returns True when the caller still needs to send a normal response."""
        mode = spec.get("mode", "ok")
        if mode == "ok":
            return True
        if mode == "status":
            self.send_json(int(spec.get("code", 500)),
                           spec.get("body", {"errorMessages": ["mock failure"], "errors": {}}),
                           spec.get("headers"))
            return False
        if mode in ("hang", "commit_then_hang"):
            if mode == "commit_then_hang" and on_commit:
                on_commit()
            time.sleep(float(spec.get("seconds", 25)))
            self.close_connection = True
            return False
        if mode == "commit_then_drop":
            if on_commit:
                on_commit()
            self.close_connection = True
            try:
                self.connection.close()
            except Exception:
                pass
            return False
        return True

    # ------------------------------------------------------------- routes
    def do_GET(self):
        self.record("GET", None)
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if path == "/rest/api/3/myself":
            if not self.apply(scenario_for("myself")):
                return
            return self.send_json(200, {
                "accountId": ACCOUNT_ID,
                "displayName": "Test User",
                "emailAddress": "test@example.com",
            })

        parts = path.strip("/").split("/")
        # rest/api/3/issue/<KEY>/worklog
        if len(parts) == 6 and parts[3] == "issue" and parts[5] == "worklog":
            if not self.apply(scenario_for("get_worklog")):
                return
            issue_key = parts[4]
            entries = load_worklogs().get(issue_key, [])
            after = query.get("startedAfter")
            if after is not None:
                try:
                    cutoff = int(after)
                    entries = [e for e in entries
                               if (to_ms(e.get("started", "")) or 0) > cutoff]
                except Exception:
                    pass
            return self.send_json(200, {
                "startAt": 0, "maxResults": int(query.get("maxResults", 100)),
                "total": len(entries), "worklogs": entries,
            })

        # rest/api/3/issue/<KEY>
        if len(parts) == 5 and parts[3] == "issue":
            if not self.apply(scenario_for("get_issue")):
                return
            return self.send_json(200, {
                "key": parts[4], "id": "100001",
                "fields": {"summary": f"Mock issue {parts[4]}",
                           "timetracking": {"remainingEstimateSeconds": 36000}},
            })

        return self.send_json(404, {"errorMessages": [f"no route {path}"], "errors": {}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        self.record("POST", raw)
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")

        if len(parts) == 6 and parts[3] == "issue" and parts[5] == "worklog":
            issue_key = parts[4]
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                return self.send_json(400, {"errorMessages": ["bad json"], "errors": {}})
            spec = scenario_for("post_worklog")
            if not self.apply(spec, on_commit=lambda: store_worklog(issue_key, body)):
                return
            return self.send_json(201, store_worklog(issue_key, body))

        return self.send_json(404, {"errorMessages": [f"no route {parsed.path}"], "errors": {}})


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8731
    STATE.mkdir(parents=True, exist_ok=True)
    REQUESTS.touch()
    if not WORKLOGS.exists():
        save_worklogs({})
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"mock jira on http://127.0.0.1:{port} state={STATE}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
