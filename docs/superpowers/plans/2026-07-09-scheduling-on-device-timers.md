# Scheduling via on-device Bot timers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the agent three in-process tools — `schedule_device`, `get_schedule`, `cancel_schedule` — that program timers into the SwitchBot Bots' own clocks (fire even when the computer is off), backed by a SQLite record the agent treats as source of truth.

**Architecture:** New `schedule_store.py` (SQLite record) + `schedules.py` (the tools). Every mutation rebuilds a device's FULL alarm set from the record and writes it to the Bot via `switchbot_scheduler.ble_writer.write_alarms` (empty list clears the Bot). The BLE write and the clock are injectable seams so the test suite touches no hardware and is time-deterministic.

**Tech Stack:** Python 3.11+, `python-telegram-bot`, `openai`, `bleak` (BLE, production only), `pytest`. Reuses `switchbot_scheduler` unchanged.

## Global Constraints

- Python **3.11+**.
- **No BLE and no network in the automated tests** — inject `write_fn(ble_id, alarms)` and `now_fn() -> datetime`.
- **Reuse `switchbot_scheduler`; make NO changes to it.** Reused API (verified):
  - `model.Event(time, action, days, once=False)`, `DeviceSchedule(device, events)`, `Schedule(schedules)`, `DAYS = ["sun","mon","tue","wed","thu","fri","sat"]`.
  - `encoder.encode_alarm(event, inverted=False) -> dict` (weekday bits, one-time bit 7, inversion swap).
  - `validator.validate(schedule, registry)` raises `validator.ScheduleError`; `validator.MAX_ALARMS == 5` per device.
  - `ble_writer.write_alarms(ble_id, alarms)` async — programs a Bot's full alarm set; **empty list clears it**.
  - `readback.readback(schedule) -> str`, `readback.describe_days(days) -> str`.
  - `registry.resolve(alias) -> name|None`, `known_names()`, `ble_id(name)`, `is_inverted(name)`.
- Tools are the existing `home_agent.tools.Tool(name, schema, impl)`; in-process (no MCP server).
- The SQLite record is the **source of truth**; `write_alarms` **replaces** a Bot's whole alarm set, so each op rebuilds the device's complete set from the record. Store lives in `config.db_path` (the existing `home_agent.db`).
- `.venv/bin/pytest` is the runner. Commit after every task.

---

### Task 1: `ScheduleStore` (SQLite record)

**Files:**
- Create: `src/home_agent/schedule_store.py`
- Test: `tests/home_agent/test_schedule_store.py`

**Interfaces:**
- Produces: `ScheduleStore(db_path)` with `add(device, action, time, days, once, fire_at=None) -> int` (row id), `list(device=None) -> list[dict]` (dict keys: device, action, time, days:list, once:bool, fire_at:str|None), `remove(device, time=None) -> int`, `remove_id(row_id) -> None`, `remove_expired(now_iso) -> int`. Thread-safe (connection-per-op), like `memory.Conversation`.

- [ ] **Step 1: Write the failing tests**

Create `tests/home_agent/test_schedule_store.py`:

```python
from home_agent.schedule_store import ScheduleStore


def test_add_list_and_remove(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    s.add("kitchen", "on", "18:00", ["mon", "tue"], False)
    s.add("kitchen", "off", "23:00", ["thu"], True, fire_at="2026-07-09T23:00:00")
    rows = s.list("kitchen")
    assert [r["time"] for r in rows] == ["18:00", "23:00"]
    assert rows[0]["days"] == ["mon", "tue"] and rows[0]["once"] is False
    assert rows[1]["once"] is True and rows[1]["fire_at"] == "2026-07-09T23:00:00"


def test_list_all_and_isolation(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    s.add("kitchen", "on", "18:00", ["mon"], False)
    s.add("garden", "on", "19:00", ["mon"], False)
    assert {r["device"] for r in s.list()} == {"kitchen", "garden"}
    assert [r["device"] for r in s.list("garden")] == ["garden"]


def test_remove_all_and_by_time(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    s.add("kitchen", "on", "18:00", ["mon"], False)
    s.add("kitchen", "off", "23:00", ["mon"], False)
    assert s.remove("kitchen", "18:00") == 1
    assert [r["time"] for r in s.list("kitchen")] == ["23:00"]
    assert s.remove("kitchen") == 1
    assert s.list("kitchen") == []


def test_remove_id_and_expired(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    rid = s.add("kitchen", "on", "18:00", ["mon"], False)
    s.remove_id(rid)
    assert s.list() == []
    s.add("kitchen", "on", "08:00", ["thu"], True, fire_at="2026-07-09T08:00:00")
    s.add("kitchen", "on", "20:00", ["thu"], True, fire_at="2026-07-09T20:00:00")
    assert s.remove_expired("2026-07-09T12:00:00") == 1   # only the 08:00 one is past
    assert [r["time"] for r in s.list("kitchen")] == ["20:00"]


def test_usable_from_a_different_thread(tmp_path):
    import threading
    s = ScheduleStore(str(tmp_path / "s.db"))
    errors = []

    def worker():
        try:
            s.add("kitchen", "on", "18:00", ["mon"], False)
            assert len(s.list("kitchen")) == 1
        except Exception as e:
            errors.append(repr(e))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'home_agent.schedule_store'`.

- [ ] **Step 3: Create `src/home_agent/schedule_store.py`**

```python
import sqlite3
from contextlib import closing


class ScheduleStore:
    """Record of the timers the agent programmed onto the Bots. Source of truth, because Bots
    cannot be read back. Thread-safe: a fresh connection per operation (PTB runs handlers in a
    worker thread), mirroring memory.Conversation."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schedules ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT NOT NULL, action TEXT NOT NULL, "
                "time TEXT NOT NULL, days TEXT NOT NULL, once INTEGER NOT NULL DEFAULT 0, "
                "fire_at TEXT, set_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.commit()

    def add(self, device, action, time, days, once, fire_at=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO schedules (device, action, time, days, once, fire_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (device, action, time, ",".join(days), 1 if once else 0, fire_at),
            )
            conn.commit()
            return cur.lastrowid

    def list(self, device=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            if device is None:
                rows = conn.execute(
                    "SELECT device, action, time, days, once, fire_at FROM schedules "
                    "ORDER BY device, time"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT device, action, time, days, once, fire_at FROM schedules "
                    "WHERE device = ? ORDER BY time",
                    (device,),
                ).fetchall()
        return [{"device": d, "action": a, "time": t,
                 "days": [x for x in dd.split(",") if x], "once": bool(o), "fire_at": f}
                for d, a, t, dd, o, f in rows]

    def remove(self, device, time=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            if time is None:
                cur = conn.execute("DELETE FROM schedules WHERE device = ?", (device,))
            else:
                cur = conn.execute(
                    "DELETE FROM schedules WHERE device = ? AND time = ?", (device, time))
            conn.commit()
            return cur.rowcount

    def remove_id(self, row_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM schedules WHERE id = ?", (row_id,))
            conn.commit()

    def remove_expired(self, now_iso):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute(
                "DELETE FROM schedules WHERE once = 1 AND fire_at IS NOT NULL AND fire_at < ?",
                (now_iso,))
            conn.commit()
            return cur.rowcount
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedule_store.py tests/home_agent/test_schedule_store.py
git commit -m "feat(sched): ScheduleStore (SQLite record, thread-safe, expiry)"
```

---

### Task 2: `schedules.py` day/time helpers

**Files:**
- Create: `src/home_agent/schedules.py`
- Test: `tests/home_agent/test_schedule_tools.py`

**Interfaces:**
- Produces: `_normalize_days(days: list[str]) -> list[str]` (expands `daily`/`weekdays`/`weekends`, validates, returns a DAYS-ordered subset; raises `ValueError` on a bad day); `_one_time_target(time_str: str, now) -> tuple[str, str]` returning `(weekday_name, fire_at_iso)` for the next occurrence of `HH:MM` from `now`.

- [ ] **Step 1: Write the failing tests**

Create `tests/home_agent/test_schedule_tools.py`:

```python
from datetime import datetime, timezone
from home_agent.schedules import _normalize_days, _one_time_target


def _thu_1824():
    # Thursday 2026-07-09 18:24 (fixed clock for deterministic tests)
    return datetime(2026, 7, 9, 18, 24, tzinfo=timezone.utc)


def test_normalize_days_words_and_explicit():
    assert _normalize_days(["daily"]) == ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    assert _normalize_days(["weekdays"]) == ["mon", "tue", "wed", "thu", "fri"]
    assert _normalize_days(["weekends"]) == ["sat", "sun"]
    assert _normalize_days(["tue", "sun"]) == ["sun", "tue"]      # DAYS order
    assert _normalize_days(["mon", "mon"]) == ["mon"]            # dedupe


def test_normalize_days_bad_day_raises():
    import pytest
    with pytest.raises(ValueError):
        _normalize_days(["funday"])


def test_one_time_target_today_when_time_ahead():
    day, fire_at = _one_time_target("18:29", _thu_1824())
    assert day == "thu"
    assert fire_at.startswith("2026-07-09T18:29")


def test_one_time_target_rolls_to_next_day_when_past():
    day, fire_at = _one_time_target("18:00", _thu_1824())   # already past 18:24
    assert day == "fri"
    assert fire_at.startswith("2026-07-10T18:00")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'home_agent.schedules'`.

- [ ] **Step 3: Create `src/home_agent/schedules.py`**

```python
from datetime import timedelta

from switchbot_scheduler.model import DAYS

_DAY_WORDS = {
    "daily": list(DAYS),
    "weekdays": ["mon", "tue", "wed", "thu", "fri"],
    "weekends": ["sat", "sun"],
}
# python's datetime.weekday(): Mon=0..Sun=6
_PY_WEEKDAY = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


def _normalize_days(days):
    """Expand convenience words, validate, and return a DAYS-ordered, deduped subset."""
    seen = set()
    for d in days:
        key = str(d).strip().lower()
        if key in _DAY_WORDS:
            seen.update(_DAY_WORDS[key])
        elif key in DAYS:
            seen.add(key)
        else:
            raise ValueError(f"unknown day '{d}'")
    return [d for d in DAYS if d in seen]


def _one_time_target(time_str, now):
    """(weekday_name, fire_at_iso) of the next occurrence of HH:MM from `now`
    (today if still ahead, else tomorrow)."""
    hh, mm = (int(x) for x in time_str.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return _PY_WEEKDAY[target.weekday()], target.isoformat()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/test_schedule_tools.py
git commit -m "feat(sched): day-normalization + one-time-target helpers"
```

---

### Task 3: `schedule_device` tool + rebuild-and-write

**Files:**
- Modify: `src/home_agent/schedules.py`
- Test: `tests/home_agent/test_schedule_tools.py`

**Interfaces:**
- Produces: `build_schedule_tools(registry, store, *, write_fn=None, now_fn=None) -> list[Tool]` (this task returns only `schedule_device`; Tasks 4–5 append the others); `_program_device(device, store, registry, write_fn)`; `_program_bot(ble_id, alarms)` (real BLE, lazy imports). `write_fn(ble_id, alarms)` default = `_program_bot`; `now_fn()` default = local aware now.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_schedule_tools.py`:

```python
from switchbot_scheduler.registry import Registry, Device
from home_agent.schedule_store import ScheduleStore
from home_agent.schedules import build_schedule_tools


def _registry():
    return Registry([
        Device(name="living_room", aliases=["סלון"], ble_id="ID1", inverted=True),
        Device(name="ac", aliases=["מזגן"], ble_id="ID2", mode="press"),
        Device(name="dining", aliases=["פינת אוכל"], ble_id="ID3"),
    ])


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _tools(tmp_path, writes, now=None):
    store = ScheduleStore(str(tmp_path / "s.db"))
    return build_schedule_tools(
        _registry(), store,
        write_fn=lambda ble_id, alarms: writes.append((ble_id, alarms)),
        now_fn=(now or _thu_1824)), store


def test_schedule_one_time_sets_once_bit_and_correct_time(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    out = _tool(tools, "schedule_device").impl({"device": "פינת אוכל", "action": "on", "time": "18:29"})
    assert "dining" in out and "✅" in out
    ble_id, alarms = writes[-1]
    assert ble_id == "ID3" and len(alarms) == 1
    assert alarms[0]["hour"] == 18 and alarms[0]["minute"] == 29
    assert alarms[0]["repeat_byte"] & 0x80          # one-time bit set
    row = store.list("dining")[0]
    assert row["once"] is True and row["days"] == ["thu"]


def test_schedule_recurring_expands_days_no_once_bit(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["weekdays"]})
    _, alarms = writes[-1]
    assert not (alarms[0]["repeat_byte"] & 0x80)    # not one-time


def test_schedule_applies_inversion_and_press(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    _tool(tools, "schedule_device").impl({"device": "סלון", "action": "on", "time": "18:00"})
    assert writes[-1][1][0]["action"] == 2          # inverted on -> off code 2
    _tool(tools, "schedule_device").impl({"device": "מזגן", "action": "on", "time": "18:00"})
    assert writes[-1][1][0]["action"] == 0          # press-mode -> press code 0


def test_schedule_rewrites_full_set_for_device(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    st = _tool(tools, "schedule_device")
    st.impl({"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]})
    st.impl({"device": "פינת אוכל", "action": "off", "time": "23:00", "days": ["mon"]})
    assert len(writes[-1][1]) == 2                  # 2nd write carries BOTH timers


def test_schedule_rejects_over_five_cap_and_rolls_back(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    st = _tool(tools, "schedule_device")
    for i in range(5):
        st.impl({"device": "פינת אוכל", "action": "on", "time": f"0{i}:00", "days": ["mon"]})
    out = st.impl({"device": "פינת אוכל", "action": "on", "time": "06:00", "days": ["mon"]})
    assert "5" in out or "max" in out.lower()
    assert len(store.list("dining")) == 5           # 6th rolled back
    assert len(writes) == 5                         # no write for the rejected 6th


def test_schedule_write_failure_rolls_back(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db"))

    def boom(ble_id, alarms):
        raise RuntimeError("out of range")

    tools = build_schedule_tools(_registry(), store, write_fn=boom, now_fn=_thu_1824)
    out = _tool(tools, "schedule_device").impl({"device": "פינת אוכל", "action": "on", "time": "18:00"})
    assert "dining" in out and ("range" in out or "couldn't" in out.lower())
    assert store.list("dining") == []               # nothing persisted


def test_schedule_unknown_device(tmp_path):
    tools, _ = _tools(tmp_path, [])
    out = _tool(tools, "schedule_device").impl({"device": "garage", "action": "on", "time": "18:00"})
    assert "unknown device" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_tools.py -k schedule_ -v`
Expected: FAIL — `ImportError: cannot import name 'build_schedule_tools'`.

- [ ] **Step 3: Implement in `src/home_agent/schedules.py`**

Add imports at the top (alongside the existing ones):

```python
import logging
from datetime import datetime

from switchbot_scheduler.model import Event, DeviceSchedule, Schedule
from switchbot_scheduler.encoder import encode_alarm
from switchbot_scheduler.validator import validate, ScheduleError
from switchbot_scheduler.readback import describe_days

from .tools import Tool

log = logging.getLogger("home_agent")
```

Add the schema, the write helpers, the impl, and the factory:

```python
_SCHEDULE_SCHEMA = {"type": "function", "function": {
    "name": "schedule_device",
    "description": (
        "Schedule a SwitchBot device to turn on/off (or press) at a clock time, programmed into the "
        "device's own timer so it fires even if this computer is off. `time` is 24-hour \"HH:MM\". "
        "Omit `days` for a ONE-TIME timer (fires at the next occurrence of that time); give `days` for "
        "a RECURRING timer. For relative requests like 'in 5 minutes', first call get_current_time and "
        "compute the HH:MM. Each device holds at most 5 timers. Report what you scheduled, in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "Room/device name or alias, Hebrew or English."},
        "action": {"type": "string", "enum": ["on", "off", "press"],
                   "description": "on, off, or press; the AC only honors press."},
        "time": {"type": "string", "description": "24-hour clock time, \"HH:MM\"."},
        "days": {"type": "array", "items": {"type": "string"},
                 "description": "Any of sun mon tue wed thu fri sat, or the words daily/weekdays/weekends. Omit for a one-time timer."},
    }, "required": ["device", "action", "time"], "additionalProperties": False},
}}


def _program_bot(ble_id, alarms):
    import asyncio
    from switchbot_scheduler.ble_writer import write_alarms
    asyncio.run(write_alarms(ble_id, alarms))


def _program_device(device, store, registry, write_fn):
    """Rebuild `device`'s full alarm set from the store and write it to the Bot (empty list clears it)."""
    rows = store.list(device)
    events = [Event(r["time"], r["action"], r["days"], r["once"]) for r in rows]
    if events:
        validate(Schedule([DeviceSchedule(device, events)]), registry)
    alarms = [encode_alarm(e, inverted=registry.is_inverted(device)) for e in events]
    write_fn(registry.ble_id(device), alarms)


def _schedule_impl(args, *, registry, store, write_fn, now_fn):
    spoken = (args.get("device") or "").strip()
    action = (args.get("action") or "").strip().lower()
    time_str = (args.get("time") or "").strip()
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    if action not in ("on", "off", "press"):
        return f"unknown action '{action}'. Use on, off, or press."
    try:
        raw_days = args.get("days") or []
        if raw_days:
            days, once, fire_at = _normalize_days(raw_days), False, None
        else:
            day, fire_at = _one_time_target(time_str, now_fn())
            days, once = [day], True
    except (ValueError, AttributeError) as e:
        return f"couldn't set the timer: {e}"
    row_id = store.add(name, action, time_str, days, once, fire_at)
    try:
        _program_device(name, store, registry, write_fn)
    except ScheduleError as e:
        store.remove_id(row_id)
        return f"can't schedule that: {e}"
    except Exception as e:
        store.remove_id(row_id)
        return f"couldn't reach {name} — timer not set ({e})"
    when = "one-time" if once else describe_days(days)
    return f"{name}: {action} at {time_str} ({when}) ✅"


def _now():
    return datetime.now().astimezone()


def build_schedule_tools(registry, store, *, write_fn=None, now_fn=None):
    write_fn = write_fn or _program_bot
    now_fn = now_fn or _now
    return [
        Tool(name="schedule_device", schema=_SCHEDULE_SCHEMA,
             impl=lambda args: _schedule_impl(
                 args, registry=registry, store=store, write_fn=write_fn, now_fn=now_fn)),
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/test_schedule_tools.py
git commit -m "feat(sched): schedule_device tool (one-time + recurring, 5-cap, rollback)"
```

---

### Task 4: `get_schedule` tool

**Files:**
- Modify: `src/home_agent/schedules.py`
- Test: `tests/home_agent/test_schedule_tools.py`

**Interfaces:**
- Produces: a `get_schedule` tool added to `build_schedule_tools`'s returned list; `_get_schedule_impl(args, *, registry, store, write_fn, now_fn)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_schedule_tools.py`:

```python
def test_get_schedule_lists_and_reports_device(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]})
    out = _tool(tools, "get_schedule").impl({"device": "פינת אוכל"})
    assert "dining" in out and "18:00" in out


def test_get_schedule_empty(tmp_path):
    tools, _ = _tools(tmp_path, [])
    assert "nothing" in _tool(tools, "get_schedule").impl({}).lower()


def test_get_schedule_expires_past_one_time(tmp_path):
    writes = []
    store = ScheduleStore(str(tmp_path / "s.db"))
    # a one-time that already fired (fire_at before our frozen now)
    store.add("dining", "on", "08:00", ["thu"], True, fire_at="2026-07-09T08:00:00+00:00")
    tools = build_schedule_tools(_registry(), store,
                                 write_fn=lambda b, a: writes.append((b, a)), now_fn=_thu_1824)
    out = _tool(tools, "get_schedule").impl({})
    assert "nothing" in out.lower()                 # expired, not shown
    assert store.list("dining") == []               # and removed from the record
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_tools.py -k get_schedule -v`
Expected: FAIL — `StopIteration` (no `get_schedule` tool yet).

- [ ] **Step 3: Implement in `src/home_agent/schedules.py`**

Add the schema and impl, and add the tool to `build_schedule_tools`:

```python
_GET_SCHEDULE_SCHEMA = {"type": "function", "function": {
    "name": "get_schedule",
    "description": (
        "List the timers currently programmed (from what I have set). Use when the user asks what's "
        "scheduled — for one device or all. Reflects what I programmed; timers set outside me (e.g. the "
        "SwitchBot app) won't appear."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "One device name/alias; omit for all."},
    }, "additionalProperties": False},
}}


def _get_schedule_impl(args, *, registry, store, write_fn, now_fn):
    spoken = (args.get("device") or "").strip()
    device = None
    if spoken:
        device = registry.resolve(spoken)
        if device is None:
            return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    store.remove_expired(now_fn().isoformat())      # drop fired one-time timers from the record
    rows = store.list(device)
    if not rows:
        return "nothing scheduled" if device is None else f"{device}: nothing scheduled"
    by_dev = {}
    for r in rows:
        by_dev.setdefault(r["device"], []).append(
            Event(r["time"], r["action"], r["days"], r["once"]))
    return readback(Schedule([DeviceSchedule(d, evs) for d, evs in by_dev.items()]))
```

Add `readback` to the `switchbot_scheduler.readback` import line so it reads:
```python
from switchbot_scheduler.readback import describe_days, readback
```

In `build_schedule_tools`, add to the returned list (after `schedule_device`):
```python
        Tool(name="get_schedule", schema=_GET_SCHEDULE_SCHEMA,
             impl=lambda args: _get_schedule_impl(
                 args, registry=registry, store=store, write_fn=write_fn, now_fn=now_fn)),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/test_schedule_tools.py
git commit -m "feat(sched): get_schedule tool (reads record, expires fired one-timers)"
```

---

### Task 5: `cancel_schedule` tool

**Files:**
- Modify: `src/home_agent/schedules.py`
- Test: `tests/home_agent/test_schedule_tools.py`

**Interfaces:**
- Produces: a `cancel_schedule` tool added to `build_schedule_tools`; `_cancel_impl(args, *, registry, store, write_fn, now_fn)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_schedule_tools.py`:

```python
def test_cancel_all_clears_the_bot(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]})
    out = _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל"})
    assert "dining" in out
    assert store.list("dining") == []
    assert writes[-1] == ("ID3", [])                # empty write clears the Bot


def test_cancel_one_by_time_keeps_the_rest(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    st = _tool(tools, "schedule_device")
    st.impl({"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]})
    st.impl({"device": "פינת אוכל", "action": "off", "time": "23:00", "days": ["mon"]})
    _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל", "time": "18:00"})
    assert [r["time"] for r in store.list("dining")] == ["23:00"]
    assert len(writes[-1][1]) == 1                  # rewrote the remaining one


def test_cancel_nothing_matched(tmp_path):
    tools, _ = _tools(tmp_path, [])
    out = _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל", "time": "09:00"})
    assert "nothing" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_tools.py -k cancel -v`
Expected: FAIL — `StopIteration` (no `cancel_schedule` tool yet).

- [ ] **Step 3: Implement in `src/home_agent/schedules.py`**

Add the schema and impl, and add the tool to `build_schedule_tools`:

```python
_CANCEL_SCHEMA = {"type": "function", "function": {
    "name": "cancel_schedule",
    "description": (
        "Cancel scheduled timers. Give a device to clear all its timers, or a device + time to cancel "
        "just that one. Reprograms the device so the cancelled timer no longer fires."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "Device name or alias."},
        "time": {"type": "string", "description": "24-hour \"HH:MM\" to cancel one timer; omit to clear all for the device."},
    }, "required": ["device"], "additionalProperties": False},
}}


def _cancel_impl(args, *, registry, store, write_fn, now_fn):
    spoken = (args.get("device") or "").strip()
    time_str = (args.get("time") or "").strip() or None
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    removed = store.remove(name, time_str)
    if removed == 0:
        return f"nothing scheduled matched for {name}."
    try:
        _program_device(name, store, registry, write_fn)
    except Exception as e:
        return f"cancelled in my records, but couldn't reprogram {name} ({e}) — try again."
    return f"{name}: cancelled {removed} timer(s) ✅"
```

In `build_schedule_tools`, add to the returned list (after `get_schedule`):
```python
        Tool(name="cancel_schedule", schema=_CANCEL_SCHEMA,
             impl=lambda args: _cancel_impl(
                 args, registry=registry, store=store, write_fn=write_fn, now_fn=now_fn)),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/test_schedule_tools.py
git commit -m "feat(sched): cancel_schedule tool (one or all; empty-clears the Bot)"
```

---

### Task 6: Wire schedule tools into the running bot

**Files:**
- Modify: `src/home_agent/telegram_app.py`
- Test: `tests/home_agent/test_telegram_app.py`, `tests/home_agent/test_telegram_handler.py`

**Interfaces:**
- Consumes: `build_schedule_tools`, `ScheduleStore`, `Config.db_path`, `load_home_tools`.
- Produces: `build_application` composes `list(DEFAULT_TOOLS) + load_home_tools(config) + build_schedule_tools(registry, ScheduleStore(config.db_path))` and threads it through `on_message`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_telegram_handler.py`:

```python
def test_handle_message_runs_schedule_device_through_composed_tools(tmp_path, make_fake_client):
    from home_agent.schedules import build_schedule_tools
    from home_agent.schedule_store import ScheduleStore
    from home_agent.tools import DEFAULT_TOOLS
    from switchbot_scheduler.registry import Registry, Device
    reg = Registry([Device(name="dining", aliases=["פינת אוכל"], ble_id="ID3")])
    store = ScheduleStore(str(tmp_path / "s.db"))
    writes = []
    sched = build_schedule_tools(reg, store, write_fn=lambda b, a: writes.append((b, a)))
    tools = list(DEFAULT_TOOLS) + sched
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "schedule_device",
                         "arguments": {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]}}]},
        {"content": "קבעתי"},
    ])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "תזמן", config=_cfg(tmp_path, {1}),
                           conversation=conv, client=client, tools=tools)
    assert writes and writes[-1][0] == "ID3"
    assert reply == "קבעתי"
```

Update `tests/home_agent/test_telegram_app.py`'s `test_build_application_registers_one_text_handler` to also assert the schedule tools are present is NOT possible via the handler count (tools live in a closure); instead add a focused test:

```python
def test_build_application_composes_schedule_tools(tmp_path, make_fake_client):
    # devices file present so home + schedule tools load; assert no crash and one handler
    dev = tmp_path / "devices.yaml"
    dev.write_text("devices:\n  dining:\n    aliases: [פינת אוכל]\n    ble_id: ID3\n")
    from home_agent.config import Config
    cfg = Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                 allowed_chat_ids={1}, db_path=str(tmp_path / "m.db"), devices_path=str(dev))
    app = build_application(cfg, client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    assert sum(len(hs) for hs in app.handlers.values()) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_telegram_handler.py -k schedule_device_through -v`
Expected: FAIL — the composed `tools` in `build_application` don't yet include schedule tools (the handler test) / import error if referencing new wiring.

- [ ] **Step 3: Wire into `telegram_app.build_application`**

Add imports at the top of `telegram_app.py`:
```python
from .schedule_store import ScheduleStore
from .schedules import build_schedule_tools
```
Inside `build_application`, replace the tools-composition line with:
```python
    schedule_tools = build_schedule_tools(load_registry(config), ScheduleStore(config.db_path)) \
        if load_home_tools(config) else []
```
That is fragile (double-load). Instead, add a small helper in `home.py` to expose the registry, OR compose directly. Use this concrete approach: in `home.py` add
```python
def load_registry(config):
    """Return the device Registry, or None if the devices file is absent."""
    import os
    from switchbot_scheduler.registry import Registry
    return Registry.load(config.devices_path) if os.path.exists(config.devices_path) else None
```
Then in `telegram_app.build_application`:
```python
    from .home import build_home_tools, load_registry
    registry = load_registry(config)
    tools = list(DEFAULT_TOOLS)
    if registry is not None:
        tools += build_home_tools(registry)
        tools += build_schedule_tools(registry, ScheduleStore(config.db_path))
    else:
        log.warning("devices file not found at %s — home control + scheduling disabled", config.devices_path)
```
Remove the now-redundant `tools = list(DEFAULT_TOOLS) + load_home_tools(config)` line. Keep the `from .home import load_home_tools` import only if still used elsewhere; otherwise replace it with the `build_home_tools, load_registry` import. (Leave `load_home_tools` in `home.py` — it is still covered by its own tests.)

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (all tests, including the new wiring + handler tests).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/telegram_app.py src/home_agent/home.py tests/home_agent/
git commit -m "feat(sched): compose schedule tools into the bot at startup"
```

---

### Task 7: Real-hardware smoke test

**Files:** none (verification + docs).

**Interfaces:** none.

- [ ] **Step 1: Full automated suite**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS.

- [ ] **Step 2: Live one-time timer (the box-free proof)**

Ensure `.env` is set. Start the bot: `PYTHONPATH=src .venv/bin/python -m home_agent`.
In Telegram send: `תזמן את המטבח להידלק בעוד 2 דקות` ("schedule the kitchen to turn on in 2 minutes").
Then **stop the bot process** (Ctrl+C). Wait for the time.
Expected: the kitchen Bot fires **on its own** ~2 minutes later, with the process stopped — proving the timer lives on the Bot.

- [ ] **Step 3: Live list + cancel**

Restart the bot. Send `מה מתוזמן?` ("what's scheduled?") → lists the timers. Send `בטל את התזמון של המטבח` ("cancel the kitchen schedule") → confirms cancellation.

- [ ] **Step 4: Update the roadmap**

In `docs/ROADMAP.md`, note under Epic D (or a new line) that on-device-timer scheduling shipped: `schedule_device`/`get_schedule`/`cancel_schedule`, in-process, verified live.

- [ ] **Step 5: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "docs(sched): on-device-timer scheduling shipped"
```
