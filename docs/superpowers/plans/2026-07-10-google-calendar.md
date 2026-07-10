# Google Calendar (family-mcp) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read + manage the couple's Google Calendar from Telegram — `find_events` across both spouses' calendars + a shared Family calendar, and create/update/delete on the Family calendar behind a deterministic, cross-turn confirm gate.

**Architecture:** A new `gcal.py` (Google client bootstrap + four tools) + a `CalendarPending` SQLite store. The Google API client is an injectable seam (fake in tests → no network). Writes are staged by `prepare_calendar_change` and executed only by `commit_calendar_change`, which refuses a change staged in the *same* turn (via a turn-start `committable_id` snapshot taken in `handle_message`). Auth is a **service account** (`calendar.events` scope); "both see it" comes from a shared Family calendar both subscribe to (no attendees).

**Tech Stack:** Python 3.11+, `sqlite3` + `statistics`/`datetime` (stdlib), `pytest`; runtime `google-api-python-client` + `google-auth` (imported lazily; **tests don't need them**).

## Global Constraints

- Python **3.11+**. **No network in the automated tests** — inject a fake Google `service` and a frozen `now_fn`; the real Google libs are imported **lazily** inside `load_calendar_service` only.
- Tools are `home_agent.tools.Tool`; datetimes ISO 8601 in **`Asia/Jerusalem`**.
- **Reads** span all `CALENDAR_IDS`, deduped by **`(iCalUID, start)`** (so recurring instances aren't collapsed), preferring the `CALENDAR_WRITE_ID` copy. **Writes** happen only on `CALENDAR_WRITE_ID`; `update`/`delete` refuse a `ref` not on it.
- **Confirm is deterministic + cross-turn:** the only executor is `commit_calendar_change`; it runs a pending change **only if its id == the `committable_id` snapshotted at the start of this `handle_message`** (⇒ staged in a *prior* turn). Pending changes **expire ~15 min**.
- Calendar tools are **chat-scoped** → built **per turn** inside `handle_message` (chat_id + committable_id captured in a closure, omitted from schemas). If `GOOGLE_SA_KEYFILE` is unset, they don't load (bot still runs).
- Module is `gcal.py` (NOT `calendar.py` — avoids shadowing stdlib). Make NO changes to `switchbot_scheduler`.
- `.venv/bin/pytest` is the runner. Commit after every task.

---

### Task 1: Config keys + dependencies

**Files:**
- Modify: `src/home_agent/config.py`, `pyproject.toml`
- Test: `tests/home_agent/test_home_agent_config.py`

**Interfaces:**
- Produces: `Config.google_sa_keyfile: str`, `Config.calendar_ids: list[str]`, `Config.calendar_write_id: str`
  (defaults to the first of `calendar_ids`). Env: `GOOGLE_SA_KEYFILE`, `CALENDAR_IDS`, `CALENDAR_WRITE_ID`.

- [ ] **Step 1: Write the failing test**

Append to `tests/home_agent/test_home_agent_config.py` (and add `CALENDAR_IDS`, `CALENDAR_WRITE_ID`,
`GOOGLE_SA_KEYFILE` to the `_clean_env` tuple):

```python
def test_load_config_calendar_keys(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN=tok\n"
                   "GOOGLE_SA_KEYFILE=/k.json\nCALENDAR_IDS=fam@g.com, me@g.com\n")
    from home_agent.config import load_config
    cfg = load_config(str(env))
    assert cfg.google_sa_keyfile == "/k.json"
    assert cfg.calendar_ids == ["fam@g.com", "me@g.com"]
    assert cfg.calendar_write_id == "fam@g.com"          # defaults to first
    monkeypatch.setenv("CALENDAR_WRITE_ID", "me@g.com")
    assert load_config(str(env)).calendar_write_id == "me@g.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_home_agent_config.py::test_load_config_calendar_keys -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'google_sa_keyfile'`.

- [ ] **Step 3: Implement in `config.py`**

Change the import to `from dataclasses import dataclass, field`. Add fields to `Config`:

```python
    google_sa_keyfile: str = ""
    calendar_ids: list[str] = field(default_factory=list)
    calendar_write_id: str = ""
```

In `load_config`, before the `return`, compute the id list, then add the three kwargs:

```python
    cal_ids = [x for x in os.environ.get("CALENDAR_IDS", "").replace(",", " ").split() if x.strip()]
```
```python
        google_sa_keyfile=os.environ.get("GOOGLE_SA_KEYFILE", ""),
        calendar_ids=cal_ids,
        calendar_write_id=os.environ.get("CALENDAR_WRITE_ID", "") or (cal_ids[0] if cal_ids else ""),
```

Add to `pyproject.toml` `dependencies`: `"google-api-python-client>=2.0"`, `"google-auth>=2.0"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/home_agent/test_home_agent_config.py -v`
Expected: PASS. (No need to `pip install` the google libs — nothing imports them yet.)

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/config.py pyproject.toml tests/home_agent/test_home_agent_config.py
git commit -m "feat(calendar): config keys (GOOGLE_SA_KEYFILE/CALENDAR_IDS/CALENDAR_WRITE_ID) + google deps"
```

---

### Task 2: `CalendarPending` store

**Files:**
- Create: `src/home_agent/calendar_pending.py`
- Test: `tests/home_agent/test_calendar_pending.py`

**Interfaces:**
- Produces: `CalendarPending(db_path)` with `stage(chat_id, payload: dict, created_at: str) -> int` (fresh
  autoincrement id; replaces any prior row for the chat), `current(chat_id) -> {id, payload, created_at} | None`,
  `clear(chat_id)`. Thread-safe (connection-per-op).

- [ ] **Step 1: Write the failing tests**

Create `tests/home_agent/test_calendar_pending.py`:

```python
from home_agent.calendar_pending import CalendarPending


def test_stage_current_clear(tmp_path):
    p = CalendarPending(str(tmp_path / "c.db"))
    assert p.current(1) is None
    i1 = p.stage(1, {"action": "create", "title": "x"}, "2026-07-10T08:00:00+03:00")
    cur = p.current(1)
    assert cur["id"] == i1 and cur["payload"]["title"] == "x"
    assert cur["created_at"] == "2026-07-10T08:00:00+03:00"
    p.clear(1)
    assert p.current(1) is None


def test_stage_replaces_and_new_id(tmp_path):
    p = CalendarPending(str(tmp_path / "c.db"))
    i1 = p.stage(1, {"action": "create"}, "2026-07-10T08:00:00+03:00")
    i2 = p.stage(1, {"action": "delete"}, "2026-07-10T08:05:00+03:00")
    assert i2 != i1                       # fresh id per staging (the same-turn guard depends on this)
    assert p.current(1)["payload"]["action"] == "delete"   # only the latest survives


def test_chats_isolated(tmp_path):
    p = CalendarPending(str(tmp_path / "c.db"))
    p.stage(1, {"a": 1}, "t")
    p.stage(2, {"a": 2}, "t")
    assert p.current(1)["payload"] == {"a": 1}
    assert p.current(2)["payload"] == {"a": 2}


def test_usable_from_a_different_thread(tmp_path):
    import threading
    p = CalendarPending(str(tmp_path / "c.db"))
    errs = []

    def worker():
        try:
            p.stage(7, {"a": 1}, "t")
            assert p.current(7)["payload"] == {"a": 1}
        except Exception as e:
            errs.append(repr(e))

    t = threading.Thread(target=worker); t.start(); t.join()
    assert errs == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_calendar_pending.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'home_agent.calendar_pending'`.

- [ ] **Step 3: Create `src/home_agent/calendar_pending.py`**

```python
import json
import sqlite3
from contextlib import closing


class CalendarPending:
    """One staged, unconfirmed calendar change per chat (SQLite). Each stage gets a fresh autoincrement
    id — the same-turn confirm guard compares it to a turn-start snapshot. Thread-safe: connection-per-op."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pending_calendar_changes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                "payload_json TEXT NOT NULL, created_at TEXT NOT NULL)")
            conn.commit()

    def stage(self, chat_id, payload, created_at):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM pending_calendar_changes WHERE chat_id = ?", (chat_id,))
            cur = conn.execute(
                "INSERT INTO pending_calendar_changes (chat_id, payload_json, created_at) "
                "VALUES (?, ?, ?)", (chat_id, json.dumps(payload), created_at))
            conn.commit()
            return cur.lastrowid

    def current(self, chat_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT id, payload_json, created_at FROM pending_calendar_changes WHERE chat_id = ?",
                (chat_id,)).fetchone()
        return None if not row else {"id": row[0], "payload": json.loads(row[1]), "created_at": row[2]}

    def clear(self, chat_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM pending_calendar_changes WHERE chat_id = ?", (chat_id,))
            conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_calendar_pending.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/calendar_pending.py tests/home_agent/test_calendar_pending.py
git commit -m "feat(calendar): CalendarPending store (fresh id per stage, thread-safe)"
```

---

### Task 3: `gcal.py` bootstrap + `find_events`

**Files:**
- Create: `src/home_agent/gcal.py`
- Test: `tests/home_agent/test_gcal.py`

**Interfaces:**
- Produces: `load_calendar_service(config) -> service|None` (lazy google import); `build_calendar_tools(service, pending_store, chat_id, committable_id, *, calendar_ids, write_id, now_fn=None) -> list[Tool]` (this task returns only `find_events`; Tasks 4–5 append the rest); helpers `_start_of`, `_end_of`. `ref` format = `"{calendar_id}|{event_id}"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/home_agent/test_gcal.py`:

```python
from datetime import datetime, timezone
from home_agent.gcal import build_calendar_tools
from home_agent.calendar_pending import CalendarPending


class _Exec:
    def __init__(self, r): self._r = r
    def execute(self): return self._r


class _Events:
    def __init__(self, by_cal): self.by_cal = by_cal; self.calls = []
    def list(self, **kw): self.calls.append(("list", kw)); return _Exec({"items": self.by_cal.get(kw["calendarId"], [])})
    def insert(self, **kw): self.calls.append(("insert", kw)); return _Exec({"id": "newid"})
    def patch(self, **kw): self.calls.append(("patch", kw)); return _Exec({})
    def delete(self, **kw): self.calls.append(("delete", kw)); return _Exec({})


class _Service:
    def __init__(self, by_cal): self._e = _Events(by_cal)
    def events(self): return self._e


def _now():
    return datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)


def _ev(uid, start, summary, eid):
    return {"iCalUID": uid, "id": eid, "summary": summary,
            "start": {"dateTime": start}, "end": {"dateTime": start}}


def _tools(by_cal, tmp_path, cal_ids=("fam", "me"), write="fam"):
    svc = _Service(by_cal)
    store = CalendarPending(str(tmp_path / "c.db"))
    return {t.name: t for t in build_calendar_tools(svc, store, 1, None, calendar_ids=list(cal_ids),
                                                     write_id=write, now_fn=_now)}, svc, store


def test_find_events_dedups_same_instance_prefers_write_calendar(tmp_path):
    # same event (uid A, same start) on both calendars → one result, from the write calendar
    ev_fam = _ev("A", "2026-07-11T10:00:00+03:00", "Dentist", "efam")
    ev_me = _ev("A", "2026-07-11T10:00:00+03:00", "Dentist", "eme")
    tools, _, _ = _tools({"fam": [ev_fam], "me": [ev_me]}, tmp_path)
    out = tools["find_events"].impl({})
    assert out.count("Dentist") == 1
    assert "ref:fam|efam" in out            # preferred the write-calendar copy


def test_find_events_keeps_distinct_recurring_instances(tmp_path):
    i1 = _ev("W", "2026-07-11T18:00:00+03:00", "Class", "w1")
    i2 = _ev("W", "2026-07-18T18:00:00+03:00", "Class", "w2")   # same uid, different start
    tools, _, _ = _tools({"fam": [i1, i2], "me": []}, tmp_path)
    out = tools["find_events"].impl({})
    assert out.count("Class") == 2          # both instances kept


def test_find_events_query_uses_wide_range(tmp_path):
    tools, svc, _ = _tools({"fam": [], "me": []}, tmp_path)
    tools["find_events"].impl({"query": "dentist"})
    kw = svc.events().calls[0][1]
    # query → now-30d .. now+180d
    assert kw["timeMin"].startswith("2026-06-10") and kw["timeMax"].startswith("2027-01-06")
    assert kw["q"] == "dentist"


def test_find_events_default_range_one_week(tmp_path):
    tools, svc, _ = _tools({"fam": [], "me": []}, tmp_path)
    tools["find_events"].impl({})
    kw = svc.events().calls[0][1]
    assert kw["timeMin"].startswith("2026-07-10") and kw["timeMax"].startswith("2026-07-17")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_gcal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'home_agent.gcal'`.

- [ ] **Step 3: Create `src/home_agent/gcal.py`**

```python
import logging
from datetime import datetime, timedelta

from .tools import Tool

log = logging.getLogger("home_agent")
_TZ = "Asia/Jerusalem"


def _now():
    return datetime.now().astimezone()


def _start_of(e):
    s = e.get("start") or {}
    return s.get("dateTime") or s.get("date") or ""


def _end_of(e):
    en = e.get("end") or {}
    return en.get("dateTime") or en.get("date") or ""


_FIND_SCHEMA = {"type": "function", "function": {
    "name": "find_events",
    "description": "Look up calendar events across the family's calendars. Use for 'what do we have this "
                   "week?', 'are we free Saturday?', 'when's the dentist?'. Pass ISO datetimes time_min/"
                   "time_max to bound the range, and/or a text query. Returns matching events, each ending "
                   "with a [ref:…] handle you pass to prepare_calendar_change for update/delete.",
    "parameters": {"type": "object", "properties": {
        "time_min": {"type": "string", "description": "ISO datetime lower bound (optional)."},
        "time_max": {"type": "string", "description": "ISO datetime upper bound (optional)."},
        "query": {"type": "string", "description": "Free-text search (optional)."},
    }, "additionalProperties": False},
}}


def _find_impl(args, *, service, calendar_ids, write_id, now_fn):
    query = (args.get("query") or "").strip() or None
    now = now_fn()
    time_min = args.get("time_min") or ((now - timedelta(days=30)) if query else now).isoformat()
    time_max = args.get("time_max") or ((now + timedelta(days=180)) if query else now + timedelta(days=7)).isoformat()
    chosen = {}
    for cal_id in calendar_ids:
        resp = service.events().list(calendarId=cal_id, timeMin=time_min, timeMax=time_max,
                                     singleEvents=True, orderBy="startTime", q=query).execute()
        for e in resp.get("items", []):
            key = (e.get("iCalUID"), _start_of(e))
            if key not in chosen or cal_id == write_id:
                chosen[key] = (cal_id, e)
    items = sorted(chosen.values(), key=lambda ce: _start_of(ce[1]))
    if not items:
        return "no events found"
    return "\n".join(
        f"{_start_of(e)} – {_end_of(e)}: {e.get('summary', '(no title)')} [ref:{cal_id}|{e['id']}]"
        for cal_id, e in items)


def build_calendar_tools(service, pending_store, chat_id, committable_id, *,
                         calendar_ids, write_id, now_fn=None):
    now_fn = now_fn or _now
    return [
        Tool(name="find_events", schema=_FIND_SCHEMA,
             impl=lambda a: _find_impl(a, service=service, calendar_ids=calendar_ids,
                                       write_id=write_id, now_fn=now_fn)),
    ]


def load_calendar_service(config):
    """Build the real Google Calendar client from the service-account key, or None if unconfigured."""
    import os
    if not config.google_sa_keyfile or not os.path.exists(config.google_sa_keyfile):
        log.warning("GOOGLE_SA_KEYFILE not set/found — calendar tools disabled")
        return None
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        config.google_sa_keyfile, scopes=["https://www.googleapis.com/auth/calendar.events"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_gcal.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/gcal.py tests/home_agent/test_gcal.py
git commit -m "feat(calendar): gcal service bootstrap + find_events ((iCalUID,start) dedup, query range)"
```

---

### Task 4: `prepare_calendar_change` (stage-only)

**Files:**
- Modify: `src/home_agent/gcal.py`
- Test: `tests/home_agent/test_gcal.py`

**Interfaces:**
- Produces: a `prepare_calendar_change` tool + `_prepare_impl(args, *, pending_store, chat_id, write_id, now_fn)`.
  Stages a payload (`create|update|delete`); **no Google call**. `update`/`delete` require a `ref` on `write_id`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_gcal.py`:

```python
def test_prepare_create_stages_and_no_google_call(tmp_path):
    tools, svc, store = _tools({"fam": [], "me": []}, tmp_path)
    out = tools["prepare_calendar_change"].impl(
        {"action": "create", "title": "רופא שיניים", "start": "2026-07-14T15:00:00+03:00"})
    assert "כן" in out or "confirm" in out.lower()
    assert store.current(1)["payload"]["title"] == "רופא שיניים"
    assert svc.events().calls == []          # nothing sent to Google


def test_prepare_create_requires_title_and_start(tmp_path):
    tools, _, store = _tools({"fam": [], "me": []}, tmp_path)
    out = tools["prepare_calendar_change"].impl({"action": "create", "title": "x"})   # no start
    assert "title" in out or "start" in out
    assert store.current(1) is None


def test_prepare_update_delete_require_write_calendar_ref(tmp_path):
    tools, _, store = _tools({"fam": [], "me": []}, tmp_path)
    # ref on a personal calendar → refused
    out = tools["prepare_calendar_change"].impl({"action": "delete", "ref": "me|e1"})
    assert "family" in out.lower()
    assert store.current(1) is None
    # ref on the write calendar → staged
    ok = tools["prepare_calendar_change"].impl({"action": "delete", "ref": "fam|e1"})
    assert store.current(1)["payload"] == {"action": "delete", "ref": "fam|e1"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_gcal.py -k prepare -v`
Expected: FAIL — `KeyError: 'prepare_calendar_change'`.

- [ ] **Step 3: Implement in `gcal.py`**

Add the schema:

```python
_PREPARE_SCHEMA = {"type": "function", "function": {
    "name": "prepare_calendar_change",
    "description": "STAGE a calendar change for the user to confirm (does not apply it yet). action is "
                   "create/update/delete. For create: give title + start (ISO). For update/delete: give a "
                   "ref from find_events (only Family-calendar events can be changed). After staging, tell "
                   "the user the exact change and wait; it is applied only when they confirm and you call "
                   "commit_calendar_change.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["create", "update", "delete"]},
        "title": {"type": "string"}, "start": {"type": "string", "description": "ISO datetime."},
        "end": {"type": "string", "description": "ISO datetime (optional)."},
        "all_day": {"type": "boolean"},
        "notes": {"type": "string"},
        "ref": {"type": "string", "description": "Event ref from find_events (for update/delete)."},
    }, "required": ["action"], "additionalProperties": False},
}}


def _prepare_impl(args, *, pending_store, chat_id, write_id, now_fn):
    action = (args.get("action") or "").strip().lower()
    if action == "create":
        title = (args.get("title") or "").strip()
        start = (args.get("start") or "").strip()
        if not title or not start:
            return "to create an event I need both a title and a start time"
        payload = {"action": "create", "title": title, "start": start, "end": args.get("end"),
                   "all_day": bool(args.get("all_day")), "notes": args.get("notes")}
        when = start + (" (all day)" if payload["all_day"] else "")
        summary = f"create '{title}' at {when}"
    elif action in ("update", "delete"):
        ref = (args.get("ref") or "").strip()
        if "|" not in ref:
            return "which event? use find_events first and pass its ref"
        if ref.rsplit("|", 1)[0] != write_id:
            return "I can only change events on the Family calendar; that one is on a personal calendar."
        if action == "delete":
            payload = {"action": "delete", "ref": ref}
            summary = f"delete event {ref}"
        else:
            payload = {"action": "update", "ref": ref, "title": args.get("title"),
                       "start": args.get("start"), "end": args.get("end"), "notes": args.get("notes")}
            summary = f"update event {ref}"
    else:
        return "unknown action; use create, update, or delete"
    pending_store.stage(chat_id, payload, now_fn().isoformat())
    return f"Ready to {summary}. Reply כן to confirm."
```

Append to `build_calendar_tools`'s returned list:

```python
        Tool(name="prepare_calendar_change", schema=_PREPARE_SCHEMA,
             impl=lambda a: _prepare_impl(a, pending_store=pending_store, chat_id=chat_id,
                                          write_id=write_id, now_fn=now_fn)),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_gcal.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/gcal.py tests/home_agent/test_gcal.py
git commit -m "feat(calendar): prepare_calendar_change (stage-only; write-calendar-ref guard)"
```

---

### Task 5: `commit_calendar_change` + `cancel_calendar_change`

**Files:**
- Modify: `src/home_agent/gcal.py`
- Test: `tests/home_agent/test_gcal.py`

**Interfaces:**
- Produces: `commit_calendar_change` (same-turn + expiry guards; executes via the fake/real service) and
  `cancel_calendar_change` tools; helper `_apply(service, payload, write_id)`. Module const `_EXPIRY_MINUTES = 15`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_gcal.py`:

```python
def test_commit_refuses_same_turn(tmp_path):
    # committable_id snapshot is None (nothing staged before the turn); staging now → id != None → refuse
    tools, svc, store = _tools({"fam": [], "me": []}, tmp_path)   # committable_id=None
    tools["prepare_calendar_change"].impl(
        {"action": "create", "title": "x", "start": "2026-07-14T15:00:00+03:00"})
    out = tools["commit_calendar_change"].impl({})
    assert "כן" in out or "reply" in out.lower()
    assert not any(c[0] == "insert" for c in svc.events().calls)   # NOT applied
    assert store.current(1) is not None                            # still staged


def test_commit_applies_when_staged_in_prior_turn(tmp_path):
    svc = _Service({"fam": [], "me": []})
    store = CalendarPending(str(tmp_path / "c.db"))
    sid = store.stage(1, {"action": "create", "title": "x", "start": "2026-07-14T15:00:00+03:00",
                          "end": None, "all_day": False, "notes": None}, _now().isoformat())
    # committable_id == the prior-turn staged id
    tools = {t.name: t for t in build_calendar_tools(svc, store, 1, sid, calendar_ids=["fam", "me"],
                                                     write_id="fam", now_fn=_now)}
    out = tools["commit_calendar_change"].impl({})
    assert "✅" in out
    ins = [c for c in svc.events().calls if c[0] == "insert"]
    assert ins and ins[0][1]["calendarId"] == "fam"
    assert ins[0][1]["body"]["summary"] == "x"
    assert store.current(1) is None                                # cleared


def test_commit_all_day_uses_exclusive_end(tmp_path):
    svc = _Service({"fam": [], "me": []})
    store = CalendarPending(str(tmp_path / "c.db"))
    sid = store.stage(1, {"action": "create", "title": "trip", "start": "2026-07-14T00:00:00+03:00",
                          "end": None, "all_day": True, "notes": None}, _now().isoformat())
    tools = {t.name: t for t in build_calendar_tools(svc, store, 1, sid, calendar_ids=["fam"],
                                                     write_id="fam", now_fn=_now)}
    tools["commit_calendar_change"].impl({})
    body = [c for c in svc.events().calls if c[0] == "insert"][0][1]["body"]
    assert body["start"] == {"date": "2026-07-14"}
    assert body["end"] == {"date": "2026-07-15"}                   # exclusive: one-day → +1


def test_commit_expired(tmp_path):
    from datetime import timedelta
    svc = _Service({"fam": [], "me": []})
    store = CalendarPending(str(tmp_path / "c.db"))
    old = (_now() - timedelta(minutes=30)).isoformat()
    sid = store.stage(1, {"action": "delete", "ref": "fam|e1"}, old)
    tools = {t.name: t for t in build_calendar_tools(svc, store, 1, sid, calendar_ids=["fam"],
                                                     write_id="fam", now_fn=_now)}
    out = tools["commit_calendar_change"].impl({})
    assert "expired" in out.lower()
    assert not any(c[0] == "delete" for c in svc.events().calls)


def test_cancel_clears(tmp_path):
    tools, svc, store = _tools({"fam": [], "me": []}, tmp_path)
    tools["prepare_calendar_change"].impl({"action": "create", "title": "x", "start": "2026-07-14T15:00:00+03:00"})
    tools["cancel_calendar_change"].impl({})
    assert store.current(1) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_gcal.py -k "commit or cancel" -v`
Expected: FAIL — `KeyError: 'commit_calendar_change'`.

- [ ] **Step 3: Implement in `gcal.py`**

Add the constant `_EXPIRY_MINUTES = 15` near the top, the schemas, `_apply`, the impls, and the tools.

```python
_COMMIT_SCHEMA = {"type": "function", "function": {
    "name": "commit_calendar_change",
    "description": "Apply the change the user just confirmed. Only call this after you staged a change with "
                   "prepare_calendar_change on a PREVIOUS turn and the user has now confirmed (e.g. 'כן').",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}

_CANCEL_SCHEMA = {"type": "function", "function": {
    "name": "cancel_calendar_change",
    "description": "Discard the staged (unconfirmed) calendar change.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def _apply(service, payload, write_id):
    action = payload["action"]
    if action == "create":
        body = {"summary": payload["title"]}
        if payload.get("notes"):
            body["description"] = payload["notes"]
        if payload.get("all_day"):
            from datetime import date, timedelta
            start_date = payload["start"][:10]
            end_date = (payload["end"][:10] if payload.get("end")
                        else (date.fromisoformat(start_date) + timedelta(days=1)).isoformat())
            body["start"] = {"date": start_date}
            body["end"] = {"date": end_date}
        else:
            end = payload.get("end")
            if not end:
                end = (datetime.fromisoformat(payload["start"]) + timedelta(hours=1)).isoformat()
            body["start"] = {"dateTime": payload["start"], "timeZone": _TZ}
            body["end"] = {"dateTime": end, "timeZone": _TZ}
        service.events().insert(calendarId=write_id, body=body).execute()
        return f"created '{payload['title']}' ✅"
    cal_id, event_id = payload["ref"].rsplit("|", 1)
    if action == "delete":
        service.events().delete(calendarId=cal_id, eventId=event_id).execute()
        return "deleted ✅"
    body = {}
    if payload.get("title"):
        body["summary"] = payload["title"]
    if payload.get("notes"):
        body["description"] = payload["notes"]
    if payload.get("start"):
        body["start"] = {"dateTime": payload["start"], "timeZone": _TZ}
    if payload.get("end"):
        body["end"] = {"dateTime": payload["end"], "timeZone": _TZ}
    service.events().patch(calendarId=cal_id, eventId=event_id, body=body).execute()
    return "updated ✅"


def _commit_impl(args, *, service, pending_store, chat_id, committable_id, write_id, now_fn):
    p = pending_store.current(chat_id)
    if not p:
        return "nothing staged to confirm"
    if p["id"] != committable_id:
        return "I've noted the change — reply כן to apply it."      # staged this turn; confirm on the next
    if (now_fn() - datetime.fromisoformat(p["created_at"])).total_seconds() > _EXPIRY_MINUTES * 60:
        pending_store.clear(chat_id)
        return "that change expired — tell me again."
    try:
        result = _apply(service, p["payload"], write_id)
    except Exception as e:
        return f"couldn't apply the change ({e})"
    pending_store.clear(chat_id)
    return result


def _cancel_impl(args, *, pending_store, chat_id):
    pending_store.clear(chat_id)
    return "canceled the pending calendar change"
```

Append to `build_calendar_tools`'s returned list:

```python
        Tool(name="commit_calendar_change", schema=_COMMIT_SCHEMA,
             impl=lambda a: _commit_impl(a, service=service, pending_store=pending_store, chat_id=chat_id,
                                         committable_id=committable_id, write_id=write_id, now_fn=now_fn)),
        Tool(name="cancel_calendar_change", schema=_CANCEL_SCHEMA,
             impl=lambda a: _cancel_impl(a, pending_store=pending_store, chat_id=chat_id)),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_gcal.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/gcal.py tests/home_agent/test_gcal.py
git commit -m "feat(calendar): commit (same-turn + expiry guards, all-day exclusive end) + cancel"
```

---

### Task 6: Prompt policy + per-turn chat-scoped wiring

**Files:**
- Modify: `src/home_agent/prompts.py`, `src/home_agent/telegram_app.py`
- Test: `tests/home_agent/test_system_prompt.py`, `tests/home_agent/test_telegram_handler.py`

**Interfaces:**
- Consumes: `load_calendar_service`, `build_calendar_tools`, `CalendarPending`.
- Produces: `build_application` builds `service` + `CalendarPending` at startup (if configured) and passes
  them to `handle_message`; `handle_message` gains `calendar_service=None, calendar_pending=None`, snapshots
  `committable_id` **before** `run_turn`, and appends the per-turn calendar tools. Prompt gains a calendar policy.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_system_prompt.py` (inside `test_prompt_is_nonempty_and_stable`, keep the
existing asserts): `assert "calendar" in FAMILY_SYSTEM_PROMPT.lower()`.

Append to `tests/home_agent/test_telegram_handler.py`:

```python
def test_calendar_same_turn_prepare_then_commit_is_not_applied(tmp_path, make_fake_client):
    from home_agent.calendar_pending import CalendarPending

    class _Exec:
        def __init__(self, r): self._r = r
        def execute(self): return self._r

    class _Events:
        def __init__(self): self.calls = []
        def list(self, **k): self.calls.append(("list", k)); return _Exec({"items": []})
        def insert(self, **k): self.calls.append(("insert", k)); return _Exec({"id": "x"})
        def patch(self, **k): self.calls.append(("patch", k)); return _Exec({})
        def delete(self, **k): self.calls.append(("delete", k)); return _Exec({})

    class _Svc:
        def __init__(self): self._e = _Events()
        def events(self): return self._e

    svc, pend = _Svc(), CalendarPending(str(tmp_path / "c.db"))
    # model tries prepare THEN commit in one turn
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "prepare_calendar_change",
                         "arguments": {"action": "create", "title": "רופא", "start": "2026-07-14T15:00:00+03:00"}}]},
        {"tool_calls": [{"id": "c2", "name": "commit_calendar_change", "arguments": {}}]},
        {"content": "רשמתי, תאשרו"},
    ])
    conv = Conversation(str(tmp_path / "m.db"))
    cfg = _cfg(tmp_path, {1})
    cfg.calendar_ids = ["fam"]; cfg.calendar_write_id = "fam"
    reply = handle_message(1, "תוסיף רופא שיניים", config=cfg, conversation=conv, client=client,
                           calendar_service=svc, calendar_pending=pend)
    assert not any(c[0] == "insert" for c in svc.events().calls)   # same-turn commit did NOT apply
    assert pend.current(1) is not None                             # still staged for a later confirm
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_telegram_handler.py -k calendar_same_turn -v`
Expected: FAIL — `handle_message` doesn't accept `calendar_service`/`calendar_pending` yet.

- [ ] **Step 3: Add the prompt policy (`prompts.py`)**

Insert before the final "If a request is ambiguous…" line (digit-free):

```python
    "For calendar changes, first find the event with find_events, tell the user the exact change, and apply "
    "it only after they confirm — by staging with prepare_calendar_change and, on a later confirming "
    "message, calling commit_calendar_change. "
```

- [ ] **Step 4: Wire `telegram_app.py`**

Add imports:
```python
from .calendar_pending import CalendarPending
from .gcal import build_calendar_tools, load_calendar_service
```
In `build_application`, after `conversation` is set up:
```python
    cal_service = load_calendar_service(config)
    cal_pending = CalendarPending(config.db_path) if cal_service is not None else None
```
Pass them into the `handle_message` call inside `on_message`:
```python
        reply = await asyncio.to_thread(
            handle_message, chat_id, message.text or "",
            config=config, conversation=conversation, client=client, tools=tools,
            calendar_service=cal_service, calendar_pending=cal_pending)
```
Extend `handle_message`'s signature with `calendar_service=None, calendar_pending=None`, and — right after
the allow-list checks, **before** `history = conversation.load(...)` — build the per-turn calendar tools:
```python
    turn_tools = list(tools)
    if calendar_service is not None and calendar_pending is not None:
        committable_id = (calendar_pending.current(chat_id) or {}).get("id")
        turn_tools = turn_tools + build_calendar_tools(
            calendar_service, calendar_pending, chat_id, committable_id,
            calendar_ids=config.calendar_ids, write_id=config.calendar_write_id)
```
Then pass `tools=turn_tools` (not `tools`) into the `run_turn(...)` call.

- [ ] **Step 5: Run the full suite to verify it passes**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (all tests, incl. the same-turn handler test and the prompt anchor).

- [ ] **Step 6: Commit**

```bash
git add src/home_agent/prompts.py src/home_agent/telegram_app.py tests/home_agent/
git commit -m "feat(calendar): prompt policy + per-turn chat-scoped wiring (committable_id snapshot)"
```

---

### Task 7: Live smoke test (manual)

**Files:** none (verification + docs).

- [ ] **Step 1: Install the Google deps + set up the service account**

`.venv/bin/pip install google-api-python-client google-auth`. Then do the Google Cloud setup from the spec
(enable Calendar API → service account + JSON key → create a Family calendar, share it with your wife + the
SA email → optionally share your personal calendars with the SA). Set `GOOGLE_SA_KEYFILE`, `CALENDAR_IDS`,
`CALENDAR_WRITE_ID` in `.env`.

- [ ] **Step 2: Full automated suite**

Run: `.venv/bin/pytest -q --ignore=integration_tests` → PASS.

- [ ] **Step 3: Drive it from Telegram (one instance only)**

Start: `PYTHONPATH=src .venv/bin/python -m home_agent`. In the chat:
- `מה יש לנו השבוע?` → lists real events across the calendars.
- `תוסיף רופא שיניים יום שלישי ב-3` → bot summarizes + asks; **it should NOT create yet**.
- `כן` → now it creates on the Family calendar; confirm it appears on **both** your and your wife's calendars.
- `תבטל את זה` → finds it, asks, and on `כן` deletes it.
Stop with Ctrl+C.

- [ ] **Step 4: Update the roadmap**

In `docs/ROADMAP.md`, mark the family-mcp **calendar** item ✅ (find/create/update/delete via a service
account + shared Family calendar, confirm-gated).

- [ ] **Step 5: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "docs(calendar): Google Calendar tools shipped"
```
