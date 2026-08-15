# Schedule Date Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "תזמן את האור למחר ב-18:30" schedule the light for **tomorrow**, one-time, with the resolved date visible in every reply — instead of firing today and silently becoming a weekly recurring timer.

**Architecture:** All relative-day resolution moves into Python (`_resolve_fire_at`) driven by a required `when` enum that the model fills from language only. Recurring moves to a separate tool so a one-time weekday request has exactly one representation. `fire_at` becomes UTC so expiry comparisons are correct, and the scheduler and the model share one HOME_TZ clock.

**Tech Stack:** Python 3.11+, SQLite (stdlib `sqlite3`), `zoneinfo`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-15-schedule-date-resolution-design.md`

## Global Constraints

- **Test command (the whole CI gate):** `.venv/bin/pytest -q --ignore=integration_tests`. There is no ruff/mypy in this project. From this worktree the venv lives at `/Users/netanelsade/smart-home/.venv/bin/pytest`.
- **Baseline before starting:** 346 tests passing.
- **`src/switchbot_scheduler/` is NOT modified.** It is a reused library (CLAUDE.md). All new date logic lives in `home_agent`.
- **No network, no BLE, no OpenAI in tests.** Every side effect goes through an injectable seam (`now_fn`, `write_fn`, `make_fake_client`).
- **`FAMILY_SYSTEM_PROMPT` must stay digit-free and byte-stable.** Enforced by `tests/home_agent/test_system_prompt.py`.
- **`fire_at` is stored as UTC ISO** (`+00:00` suffix) everywhere, and rendered in HOME_TZ for display only.
- **`remove_expired(now_iso)` takes a UTC ISO string.** Both call sites convert.
- **Timers are one-time by default.** Recurring is a separate tool and is rare.
- **Max horizon is +7 days.** Beyond that is not encodable on BLE.
- **Stores are thread-safe by connection-per-operation** (`contextlib.closing` per method). Mirror the existing pattern.
- **Append-only mindset:** never widen a `DELETE` beyond what is being cancelled.
- **Commit after every task.**

---

# Phase 1 — Grounding and safety

No model-facing API change. The existing 346-test suite is a real regression net for this phase.

### Task 1: `fire_at` becomes UTC, and `remove_expired` returns the rows it deleted

**Files:**
- Modify: `src/home_agent/schedule_store.py:63-69`
- Test: `tests/home_agent/test_schedule_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ScheduleStore.remove_expired(now_iso: str) -> list[dict]` — returns the deleted rows (same dict shape as `list()`), instead of an `int` rowcount. `now_iso` MUST be a UTC ISO string.

- [ ] **Step 1: Write the failing test**

In `tests/home_agent/test_schedule_store.py`, replace the existing `remove_expired` assertions and add the UTC ordering case:

```python
def test_remove_expired_returns_deleted_rows(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    s.add("kitchen", "on", "08:00", ["thu"], True, fire_at="2026-07-09T05:00:00+00:00")
    s.add("kitchen", "on", "20:00", ["thu"], True, fire_at="2026-07-09T17:00:00+00:00")
    removed = s.remove_expired("2026-07-09T09:00:00+00:00")
    assert [r["time"] for r in removed] == ["08:00"]
    assert removed[0]["device"] == "kitchen"
    assert [r["time"] for r in s.list("kitchen")] == ["20:00"]


def test_remove_expired_orders_by_instant_not_wall_clock(tmp_path):
    # A UTC row that fires at 01:00 local on the 16th must NOT be expired by a
    # "now" that is 30 minutes earlier in real time. With local-tz strings this
    # comparison silently deleted live timers.
    s = ScheduleStore(str(tmp_path / "s.db"))
    s.add("kitchen", "on", "01:00", ["sun"], True, fire_at="2026-08-15T22:00:00+00:00")
    removed = s.remove_expired("2026-08-15T21:30:00+00:00")
    assert removed == []
    assert len(s.list("kitchen")) == 1
```

These use pytest's `tmp_path` fixture, matching the existing style in that file.

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_schedule_store.py -q`
Expected: FAIL — `remove_expired` returns an `int`, so `[r["time"] for r in removed]` raises `TypeError`.

- [ ] **Step 3: Write minimal implementation**

Replace `remove_expired` in `src/home_agent/schedule_store.py`:

```python
    def remove_expired(self, now_iso):
        """Delete fired one-time rows and return them. `now_iso` MUST be a UTC ISO
        string — fire_at is stored UTC precisely so this string comparison orders
        by instant rather than by wall clock."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT id, device, action, time, days, once, fire_at FROM schedules "
                "WHERE once = 1 AND fire_at IS NOT NULL AND fire_at < ?",
                (now_iso,),
            ).fetchall()
            if rows:
                conn.execute(
                    "DELETE FROM schedules WHERE id IN (%s)" % ",".join("?" * len(rows)),
                    [r[0] for r in rows],
                )
                conn.commit()
        return [{"id": i, "device": d, "action": a, "time": t,
                 "days": [x for x in dd.split(",") if x], "once": bool(o), "fire_at": f}
                for i, d, a, t, dd, o, f in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_schedule_store.py -q`
Expected: PASS.

Then run the full suite: `/Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: failures in `test_schedule_tools.py` (the `get_schedule` expiry test) and possibly `test_cloud_scheduler.py`, because callers still pass local-tz `now`. That is expected and fixed in Task 2/3 — do not "fix" them by reverting this task.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedule_store.py tests/home_agent/test_schedule_store.py
git commit -m "fix(schedules): remove_expired returns rows and compares UTC instants"
```

---

### Task 2: Pin the clock — HOME_TZ for both the scheduler and the model

**Files:**
- Modify: `src/home_agent/tools.py:13-30`
- Modify: `src/home_agent/schedules.py:193-195` (`_now`), and the two `remove_expired` call sites
- Modify: `src/home_agent/telegram_app.py:111` (hoist `ZoneInfo`), `:123` (use the factory), `:127` (pass `now_fn`)
- Test: `tests/home_agent/test_time_tool.py`, `tests/home_agent/test_schedule_tools.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `tools.build_time_tools(tz) -> list[Tool]` — returns a `get_current_time` bound to `tz` (a `ZoneInfo`). `tools.DEFAULT_TOOLS` is **kept unchanged** as a host-tz fallback, because `telegram_app.handle_message`'s default argument (`telegram_app.py:48`) and several tests import it.

- [ ] **Step 1: Write the failing test**

Add to `tests/home_agent/test_time_tool.py`:

```python
def test_build_time_tools_uses_given_timezone():
    from zoneinfo import ZoneInfo
    from home_agent.tools import build_time_tools

    tools = build_time_tools(ZoneInfo("Asia/Jerusalem"))
    out = tools[0].impl({})
    # Israel is never UTC; the reply must not carry a UTC marker.
    assert "UTC" not in out
    assert tools[0].name == "get_current_time"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_time_tool.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_time_tools'`.

- [ ] **Step 3: Write minimal implementation**

In `src/home_agent/tools.py`, refactor `_now_string` to take a tz and add the factory. Keep `get_current_time` and `DEFAULT_TOOLS` exactly as they are:

```python
def _now_string(args: dict, tz=None) -> str:
    # Include the weekday and timezone so the model never has to infer the day of week
    # (it guessed wrong when only "YYYY-MM-DD HH:MM:SS" was returned).
    now = datetime.now(tz) if tz is not None else datetime.now().astimezone()
    return now.strftime("%A, %Y-%m-%d %H:%M:%S %Z")


_TIME_SCHEMA = {"type": "function", "function": {
    "name": "get_current_time",
    "description": "Return the current local date and time, including the day of the week.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

get_current_time = Tool(name="get_current_time", schema=_TIME_SCHEMA, impl=_now_string)

DEFAULT_TOOLS = [get_current_time]


def build_time_tools(tz):
    """get_current_time bound to the home timezone. This is the clock the MODEL reads,
    so it must agree with the scheduler's clock — it drives the model's choice of weekday."""
    return [Tool(name="get_current_time", schema=_TIME_SCHEMA,
                 impl=lambda args: _now_string(args, tz=tz))]
```

In `src/home_agent/schedules.py`, make the module default tz-aware and convert both expiry calls to UTC. Add near the other imports:

```python
from datetime import datetime, timedelta, timezone
```

and add a helper plus update `_get_schedule_impl`'s sweep:

```python
def _utc_iso(dt):
    """UTC ISO for storage and for every remove_expired comparison."""
    return dt.astimezone(timezone.utc).isoformat()
```

Change `schedules.py:155` from `store.remove_expired(now_fn().isoformat())` to:

```python
    store.remove_expired(_utc_iso(now_fn()))     # drop fired one-time timers from the record
```

In `src/home_agent/cloud_scheduler.py:21`, change `self.store.remove_expired(self.now_fn().isoformat())` to use the same conversion:

```python
        self.store.remove_expired(self.now_fn().astimezone(_dt_timezone.utc).isoformat())
```

adding `from datetime import timezone as _dt_timezone` to that module's imports.

In `src/home_agent/telegram_app.py`, hoist the `ZoneInfo` import to the top of the module (it is currently inside the cloud-creds `if` block at line 111, but is now needed unconditionally), then change line 123 and 127:

```python
    tools = list(build_time_tools(ZoneInfo(config.home_tz)))
```

```python
        tools += build_schedule_tools(
            registry, ScheduleStore(config.db_path), scheduler=scheduler,
            now_fn=lambda: datetime.now(ZoneInfo(config.home_tz)))
```

Update the import line at `telegram_app.py:23` to `from .tools import DEFAULT_TOOLS, build_time_tools` and ensure `datetime` is imported in that module.

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_time_tool.py tests/home_agent/test_schedule_tools.py tests/home_agent/test_telegram_app.py -q`
Expected: PASS. `test_telegram_app.py:53` spies with `**kw`, so the new `now_fn=` kwarg does not break it.

Then update `test_cloud_scheduler.py:43, 50, 69, 87` fixtures from `"2026-07-16T09:00:00+03:00"` style to the UTC equivalent (`"2026-07-16T06:00:00+00:00"` for 09:00 Israel, `"2026-07-16T15:00:00+00:00"` for 18:00), and run:

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS, full suite.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/tools.py src/home_agent/schedules.py src/home_agent/cloud_scheduler.py src/home_agent/telegram_app.py tests/home_agent/
git commit -m "fix(schedules): pin scheduler and model clocks to HOME_TZ, compare expiry in UTC"
```

---

### Task 3: `CloudScheduler.schedule_row` raises instead of silently dropping a past job

**Files:**
- Modify: `src/home_agent/cloud_scheduler.py:20-24` (`reconcile`), `:26-40` (`schedule_row`)
- Test: `tests/home_agent/test_cloud_scheduler.py`

**Interfaces:**
- Consumes: `ScheduleStore.remove_expired` from Task 1.
- Produces: `CloudScheduler.schedule_row(row)` raises `ValueError` when `row["once"]` and `fire_at <= now`. `CloudScheduler.reconcile()` skips such rows without raising.

- [ ] **Step 1: Write the failing test**

```python
def test_schedule_row_raises_for_past_one_time(tmp_path):
    import pytest
    sched, store, _ = _make_scheduler()          # match the file's existing helper
    row = {"id": 1, "device": "garden", "action": "on", "time": "09:00",
           "days": ["thu"], "once": True, "fire_at": "2026-07-16T06:00:00+00:00"}
    with pytest.raises(ValueError):
        sched.schedule_row(row)                   # now_fn is after that instant


def test_reconcile_skips_past_rows_without_raising(tmp_path):
    sched, store, _ = _make_scheduler()
    store.add("garden", "on", "09:00", ["thu"], True, "2026-07-16T06:00:00+00:00")
    sched.reconcile()                             # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_cloud_scheduler.py -q`
Expected: FAIL — `schedule_row` currently returns `None` for a past row, so `pytest.raises` fails with `DID NOT RAISE`.

- [ ] **Step 3: Write minimal implementation**

In `src/home_agent/cloud_scheduler.py`:

```python
    def reconcile(self):
        self.store.remove_expired(self.now_fn().astimezone(_dt_timezone.utc).isoformat())
        for row in self.store.list():
            if self.registry.is_cloud(row["device"]):
                try:
                    self.schedule_row(row)
                except ValueError:
                    continue          # already-past rows are expected on restart

    def schedule_row(self, row):
        name = _job_name(row["id"])
        if row["once"]:
            when = datetime.fromisoformat(row["fire_at"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=self.tz)
            if when <= self.now_fn():
                # Must raise, not return: _schedule_impl rolls the store row back on
                # failure, and a silent return reports ✅ for a timer that never fires.
                raise ValueError("that moment has already passed")
            self.jq.run_once(self._make_cb(row), when=when, name=name)
        else:
            hh, mm = (int(x) for x in row["time"].split(":"))
            days = tuple(_DAY_NUM[d] for d in row["days"])
            self.jq.run_daily(self._make_cb(row), time=dtime(hh, mm, tzinfo=self.tz),
                              days=days, name=name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS, full suite.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/cloud_scheduler.py tests/home_agent/test_cloud_scheduler.py
git commit -m "fix(schedules): raise on past cloud job so the store rolls back"
```

---

### Task 4: Fired one-time timers stop resurrecting

**Files:**
- Modify: `src/home_agent/schedules.py` — `_schedule_impl` and `_cancel_impl`
- Test: `tests/home_agent/test_schedule_tools.py`

**Interfaces:**
- Consumes: `ScheduleStore.remove_expired -> list[dict]` (Task 1).
- Produces: `_expire_and_reprogram(store, registry, write_fn, now_fn)` — sweeps expired rows and reprograms every affected **BLE** device. Returns nothing.

Why: `_program_device` rebuilds a Bot's whole alarm set from the store, and BLE has no fire callback, so a fired one-time row is re-armed by the next unrelated write to that device and silently eats one of the 5 alarm slots.

- [ ] **Step 1: Write the failing test**

```python
def test_fired_one_time_row_is_not_rearmed_by_a_later_write(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    # a one-time row that has already fired (before the frozen clock _thu_1824)
    store.add("dining", "on", "08:00", ["thu"], True, "2026-07-09T05:00:00+00:00")
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "off", "time": "23:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    assert [r["time"] for r in store.list("dining")] == ["23:00"]
    # only the new alarm was written to the Bot, not the stale 08:00 one
    assert len(writes[-1][1]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_schedule_tools.py -q -k rearmed`
Expected: FAIL — two alarms are written, and the stale row is still in the store.

- [ ] **Step 3: Write minimal implementation**

Add to `src/home_agent/schedules.py`:

```python
def _expire_and_reprogram(store, registry, write_fn, now_fn):
    """Drop fired one-time rows, and rewrite the alarm set of every BLE device that lost
    one — clearing the store alone leaves the Bot still holding the alarm."""
    for device in {r["device"] for r in store.remove_expired(_utc_iso(now_fn()))}:
        if registry.resolve(device) is None or registry.is_cloud(device):
            continue          # cloud one-time jobs self-remove in CloudScheduler._make_cb
        try:
            _program_device(device, store, registry, write_fn)
        except Exception as e:
            # An unrelated Bot's dead battery must not block the call the user made.
            log.warning("could not reprogram %s after expiry: %s", device, type(e).__name__)
```

Add `import logging` / `log = logging.getLogger("home_agent")` at the top of the module if not present.

Call it as the first statement of `_schedule_impl` and `_cancel_impl` (after the arg parsing, before any store write):

```python
    _expire_and_reprogram(store, registry, write_fn, now_fn)
```

Do **not** add it to `_get_schedule_impl` — that tool keeps its existing `remove_expired` sweep but stays read-only; a "what's scheduled?" question must never trigger BLE writes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS, full suite.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/test_schedule_tools.py
git commit -m "fix(schedules): expire fired one-time rows before writing, and reprogram affected bots"
```

---

### Task 5: Enforce the 5-timer cap on the cloud path too

**Files:**
- Modify: `src/home_agent/schedules.py` — `_schedule_impl`
- Test: `tests/home_agent/test_schedules_cloud.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_schedule_impl` returns an error string when a device already holds `MAX_ALARMS` timers, for **both** routes.

Why: `validate()` runs only inside `_program_device`, i.e. BLE only, so cloud devices could accumulate unbounded rows.

- [ ] **Step 1: Write the failing test**

```python
def test_cloud_device_respects_the_five_timer_cap(tmp_path):
    tools, store, _ = _cloud_tools()             # match the file's existing helper
    for i in range(5):
        _tool(tools, "schedule_device").impl(
            {"device": "גינה", "action": "on", "time": f"0{i+1}:00", "days": ["mon"]})
    out = _tool(tools, "schedule_device").impl(
        {"device": "גינה", "action": "on", "time": "07:00", "days": ["mon"]})
    assert "max" in out.lower() or "5" in out
    assert len(store.list("garden")) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_schedules_cloud.py -q -k cap`
Expected: FAIL — a 6th row is stored, since `validate` never runs for cloud devices.

- [ ] **Step 3: Write minimal implementation**

In `_schedule_impl`, after resolving `name` and before `store.add(...)`:

```python
    from switchbot_scheduler.validator import MAX_ALARMS
    if len(store.list(name)) >= MAX_ALARMS:
        return (f"{name} already has {MAX_ALARMS} timers, which is the maximum. "
                f"Cancel one first.")
```

(Import `MAX_ALARMS` at module top alongside the existing `validate, ScheduleError` import instead of inline.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS, full suite.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/test_schedules_cloud.py
git commit -m "fix(schedules): enforce the 5-timer cap on cloud devices too"
```

**PHASE 1 GATE:** full suite green, no model-facing change yet. Review before continuing.

---

# Phase 2 — The API change

### Task 6: `_resolve_fire_at` — the `when` enum

**Files:**
- Modify: `src/home_agent/schedules.py` — replace `_one_time_target`
- Test: `tests/home_agent/test_schedule_tools.py`

**Interfaces:**
- Consumes: `_utc_iso` (Task 2).
- Produces: `_resolve_fire_at(when: str, time_str: str, now: datetime) -> datetime` — a tz-aware datetime in `now`'s zone. Raises `ValueError` for an unknown token or for `today` with a passed time. Also `_WHEN_TOKENS: list[str]` for the schema enum.

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from home_agent.schedules import _resolve_fire_at

_TZ = ZoneInfo("Asia/Jerusalem")

def _fri_1721():
    # Friday 2026-08-14 17:21 local — the exact moment of the reported bug
    return datetime(2026, 8, 14, 17, 21, tzinfo=_TZ)


def test_tomorrow_never_resolves_to_today():
    got = _resolve_fire_at("tomorrow", "18:30", _fri_1721())
    assert got.date().isoformat() == "2026-08-15"      # Saturday, not Friday


def test_soonest_is_today_when_ahead_and_tomorrow_when_passed():
    assert _resolve_fire_at("soonest", "18:30", _fri_1721()).date().isoformat() == "2026-08-14"
    late = datetime(2026, 8, 14, 19, 0, tzinfo=_TZ)
    assert _resolve_fire_at("soonest", "18:30", late).date().isoformat() == "2026-08-15"


def test_today_errors_when_the_time_has_passed():
    import pytest
    late = datetime(2026, 8, 14, 19, 0, tzinfo=_TZ)
    with pytest.raises(ValueError):
        _resolve_fire_at("today", "18:30", late)


def test_weekday_counts_today_only_when_the_time_is_ahead():
    # today IS Friday
    assert _resolve_fire_at("fri", "18:30", _fri_1721()).date().isoformat() == "2026-08-14"
    late = datetime(2026, 8, 14, 19, 0, tzinfo=_TZ)
    assert _resolve_fire_at("fri", "18:30", late).date().isoformat() == "2026-08-21"


def test_day_after_tomorrow_and_in_a_week():
    assert _resolve_fire_at("day_after_tomorrow", "18:30", _fri_1721()).date().isoformat() == "2026-08-16"
    assert _resolve_fire_at("in_a_week", "18:30", _fri_1721()).date().isoformat() == "2026-08-21"


def test_unknown_token_raises():
    import pytest
    with pytest.raises(ValueError):
        _resolve_fire_at("friday", "18:30", _fri_1721())


def test_resolution_survives_the_dst_boundary():
    # Israel ends DST overnight 2026-10-24/25. "tomorrow 18:30" must be 18:30 WALL CLOCK
    # on the 25th, not 17:30 — which is what fixed-offset arithmetic would produce.
    before = datetime(2026, 10, 24, 12, 0, tzinfo=_TZ)
    got = _resolve_fire_at("tomorrow", "18:30", before)
    assert got.date().isoformat() == "2026-10-25"
    assert (got.hour, got.minute) == (18, 30)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_schedule_tools.py -q`
Expected: FAIL — `ImportError: cannot import name '_resolve_fire_at'`.

- [ ] **Step 3: Write minimal implementation**

Replace `_one_time_target` in `src/home_agent/schedules.py` with:

```python
from datetime import time as _dtime

# Fixed day offsets. "soonest" is conditional, so it is handled separately.
_WHEN_OFFSETS = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2, "in_a_week": 7}
_WHEN_TOKENS = ["soonest", *_WHEN_OFFSETS, *DAYS]


def _resolve_fire_at(when, time_str, now):
    """Resolve a `when` token to a tz-aware datetime in `now`'s zone.

    All date arithmetic lives here, never in the model: the model maps language to a
    token ("מחר" -> "tomorrow") and nothing else. Built from date+time+tzinfo rather
    than timedelta on an aware value, so a target across a DST boundary keeps its
    wall-clock time.
    """
    key = str(when or "").strip()
    if key not in _WHEN_TOKENS:
        raise ValueError(f"unknown when '{when}'. Use one of: {', '.join(_WHEN_TOKENS)}")
    hh, mm = (int(x) for x in time_str.split(":"))
    tz = now.tzinfo

    def at(d):
        return datetime.combine(d, _dtime(hh, mm), tzinfo=tz)

    if key == "soonest":
        target = at(now.date())
        return target if target > now else at(now.date() + timedelta(days=1))

    if key in _WHEN_OFFSETS:
        target = at(now.date() + timedelta(days=_WHEN_OFFSETS[key]))
        if key == "today" and target <= now:
            raise ValueError(
                f"{time_str} already passed today. If the user did not explicitly say "
                f"today, retry with when=soonest or when=tomorrow")
        return target

    # a weekday name: nearest occurrence, today counting only if the time is still ahead
    target_idx = DAYS.index(key)
    now_idx = (now.weekday() + 1) % 7          # python Mon=0 -> our sun=0 indexing
    delta = (target_idx - now_idx) % 7
    target = at(now.date() + timedelta(days=delta))
    if target <= now:
        target = at(now.date() + timedelta(days=delta + 7))
    return target
```

Keep the `_PY_WEEKDAY` map — Task 7 uses it to name the resolved date's weekday for the row contract.

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_schedule_tools.py -q -k resolve or tomorrow or soonest or weekday or dst`
Expected: PASS for the new tests. The old `_one_time_target` tests (`test_schedule_tools.py:2, 24, 30`) now fail on import — delete them in this step; they test a helper that no longer exists.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/test_schedule_tools.py
git commit -m "feat(schedules): resolve relative days in python via a when enum"
```

---

### Task 7: Split the tools — one-time vs recurring

**Files:**
- Modify: `src/home_agent/schedules.py` — `_SCHEDULE_SCHEMA`, `_schedule_impl`, `build_schedule_tools`
- Test: `tests/home_agent/test_schedule_tools.py`, `tests/home_agent/test_schedules_cloud.py`

**Interfaces:**
- Consumes: `_resolve_fire_at` (Task 6), `_expire_and_reprogram` (Task 4).
- Produces: two tools from `build_schedule_tools` — `schedule_device(device, action, time, when)` (one-time) and `schedule_recurring_device(device, action, time, days, repetition_phrase)` (recurring). Both are bound to one shared `_schedule_impl(args, *, recurring, registry, store, write_fn, now_fn, scheduler)`.

**Row contract** (write it exactly this way — leaving it implicit is how `days='fri', once=0` got written): one-time → `days=[weekday of fire_at]`, `once=1`, `fire_at` set (UTC). Recurring → `days=<user days>`, `once=0`, `fire_at=NULL`. They are distinguished by `once`, **never** by `days`.

- [ ] **Step 1: Write the failing test**

```python
def test_schedule_device_has_no_days_property(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    props = _tool(tools, "schedule_device").schema["function"]["parameters"]["properties"]
    assert "days" not in props
    assert props["when"]["enum"][0] == "soonest"
    assert "when" in _tool(tools, "schedule_device").schema["function"]["parameters"]["required"]


def test_one_time_row_contract(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    row = store.list("dining")[0]
    assert row["once"] is True
    assert row["days"] == ["sat"]                     # weekday OF the resolved date
    assert row["fire_at"].endswith("+00:00")          # stored UTC


def test_recurring_row_contract_and_required_phrase(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    out = _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "days": ["fri"],
         "repetition_phrase": "כל שישי"})
    assert "RECURRING" in out
    row = store.list("dining")[0]
    assert row["once"] is False and row["fire_at"] is None

    store2_out = _tool(tools, "schedule_recurring_device").impl(
        {"device": "מטבח", "action": "on", "time": "18:30", "days": ["fri"],
         "repetition_phrase": "  "})
    assert "repetition" in store2_out.lower() or "schedule_device" in store2_out
    assert store.list("kitchen") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_schedule_tools.py -q -k contract or no_days`
Expected: FAIL — `KeyError: 'schedule_recurring_device'`, and `days` is still a property.

- [ ] **Step 3: Write minimal implementation**

Replace the schema and impl in `src/home_agent/schedules.py`. Keep every clause of the existing description that the spec preserves — only the "Omit `days` … give `days`" sentence is removed:

```python
_SCHEDULE_SCHEMA = {"type": "function", "function": {
    "name": "schedule_device",
    "description": (
        "Schedule a SwitchBot device to turn on/off (or press) ONCE, at a clock time on a "
        "specific day. Bluetooth devices are programmed into the device's own timer (they fire "
        "even if this computer is off); cloud devices (e.g. the garden) are fired by the "
        "home-agent, so it must be running. `time` is 24-hour \"HH:MM\". `when` says which day "
        "and is required — use \"soonest\" when the user named no day at all. Never guess a "
        "weekday: if the user's wording does not map cleanly onto a `when` value, or asks for "
        "more than a week ahead, ask them which day instead of choosing one. For relative "
        "requests like 'in 5 minutes', first call get_current_time and compute the HH:MM. Each "
        "device holds at most 5 timers. Report what you scheduled, including the date, in the "
        "user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "Room/device name or alias, Hebrew or English."},
        "action": {"type": "string", "enum": ["on", "off", "press"],
                   "description": "on, off, or press; the AC only honors press."},
        "time": {"type": "string", "description": "24-hour clock time, \"HH:MM\"."},
        "when": {"type": "string", "enum": _WHEN_TOKENS,
                 "description": "Which day: soonest (no day named), today, tomorrow, "
                                "day_after_tomorrow, in_a_week, or a weekday sun..sat."},
    }, "required": ["device", "action", "time", "when"], "additionalProperties": False},
}}

_RECURRING_SCHEMA = {"type": "function", "function": {
    "name": "schedule_recurring_device",
    "description": (
        "Schedule a SwitchBot device to repeat WEEKLY on the given days. Use ONLY when the user "
        "explicitly asked for repetition (כל יום / כל שישי / every week) — this is rare. For a "
        "single day, even a named weekday like 'ביום שישי', use schedule_device instead. Pass "
        "the user's own repetition words in `repetition_phrase`. Each device holds at most 5 "
        "timers. Report what you scheduled, in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "Room/device name or alias, Hebrew or English."},
        "action": {"type": "string", "enum": ["on", "off", "press"],
                   "description": "on, off, or press; the AC only honors press."},
        "time": {"type": "string", "description": "24-hour clock time, \"HH:MM\"."},
        "days": {"type": "array", "items": {"type": "string"},
                 "description": "Any of sun mon tue wed thu fri sat, or daily/weekdays/weekends."},
        "repetition_phrase": {"type": "string",
                              "description": "The user's own words that mean repetition."},
    }, "required": ["device", "action", "time", "days", "repetition_phrase"],
       "additionalProperties": False},
}}
```

Rewrite `_schedule_impl` as one shared impl:

```python
def _schedule_impl(args, *, recurring, registry, store, write_fn, now_fn, scheduler=None):
    spoken = (args.get("device") or "").strip()
    action = (args.get("action") or "").strip().lower()
    time_str = (args.get("time") or "").strip()
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    if action not in ("on", "off", "press"):
        return f"unknown action '{action}'. Use on, off, or press."

    _expire_and_reprogram(store, registry, write_fn, now_fn)
    if len(store.list(name)) >= MAX_ALARMS:
        return f"{name} already has {MAX_ALARMS} timers, which is the maximum. Cancel one first."

    try:
        if recurring:
            if not (args.get("repetition_phrase") or "").strip():
                return ("repetition_phrase is required — if the user did not actually ask for a "
                        "repeating timer, use schedule_device instead.")
            days, once, fire_at, target = _normalize_days(args.get("days") or []), False, None, None
            if not days:
                return "give at least one day for a recurring timer."
        else:
            target = _resolve_fire_at(args.get("when"), time_str, now_fn())
            days, once, fire_at = [_PY_WEEKDAY[target.weekday()]], True, _utc_iso(target)
    except (ValueError, AttributeError) as e:
        return f"couldn't set the timer: {e}"

    row_id = store.add(name, action, time_str, days, once, fire_at)
    if registry.is_cloud(name):
        try:
            scheduler.schedule_row({"id": row_id, "device": name, "action": action,
                                    "time": time_str, "days": days, "once": once,
                                    "fire_at": fire_at})
        except Exception as e:
            store.remove_id(row_id)
            return f"couldn't schedule {name} ({e}) — timer not set"
    else:
        try:
            _program_device(name, store, registry, write_fn)
        except ScheduleError as e:
            store.remove_id(row_id); return f"can't schedule that: {e}"
        except Exception as e:
            store.remove_id(row_id); return f"couldn't reach {name} — timer not set ({e})"
    return f"{name}: {_describe_row(action, time_str, days, once, fire_at, now_fn())} ✅"
```

Bind both tools in `build_schedule_tools`:

```python
        Tool(name="schedule_device", schema=_SCHEDULE_SCHEMA,
             impl=lambda args: _schedule_impl(
                 args, recurring=False, registry=registry, store=store,
                 write_fn=write_fn, now_fn=now_fn, scheduler=scheduler)),
        Tool(name="schedule_recurring_device", schema=_RECURRING_SCHEMA,
             impl=lambda args: _schedule_impl(
                 args, recurring=True, registry=registry, store=store,
                 write_fn=write_fn, now_fn=now_fn, scheduler=scheduler)),
```

`_describe_row` is written in Task 8 — for this task, stub it as
`f"{action} at {time_str}"` and replace it there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_schedule_tools.py tests/home_agent/test_schedules_cloud.py -q`
Expected: the new tests PASS. The ten existing tests that pass `days` to `schedule_device` now fail — migrate them in this step to `schedule_recurring_device` (adding `repetition_phrase`), namely `test_schedule_tools.py:78, 96-97, 106-107, 135, 161, 172-173, 196, 207` and `test_schedules_cloud.py:29, 36, 43`. Tests that assert one-time behaviour instead gain `"when": "soonest"`.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/
git commit -m "feat(schedules): split one-time and recurring into separate tools"
```

---

### Task 8: Dates in every confirmation and listing

**Files:**
- Modify: `src/home_agent/schedules.py` — add `_describe_row`, rewrite `_get_schedule_impl`
- Test: `tests/home_agent/test_schedule_tools.py`

**Interfaces:**
- Consumes: the row contract from Task 7.
- Produces: `_describe_row(action, time_str, days, once, fire_at, now) -> str`. `_get_schedule_impl` no longer calls `switchbot_scheduler.readback`.

- [ ] **Step 1: Write the failing test**

```python
def test_one_time_confirmation_states_the_date(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    out = _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    assert "2026-08-15" in out and "Sat" in out and "ONE-TIME" in out


def test_a_week_out_is_flagged(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    out = _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "in_a_week"})
    assert "2026-08-21" in out and "NEXT WEEK" in out


def test_get_schedule_lists_dates_and_marks_recurring(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "מטבח", "action": "on", "time": "07:00", "days": ["mon"],
         "repetition_phrase": "כל יום שני"})
    out = _tool(tools, "get_schedule").impl({})
    assert "2026-08-15" in out and "ONE-TIME" in out
    assert "RECURRING" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_schedule_tools.py -q -k date or week or lists`
Expected: FAIL — the stub `_describe_row` prints no date.

- [ ] **Step 3: Write minimal implementation**

```python
def _describe_row(action, time_str, days, once, fire_at, now):
    """One line per timer, always carrying the resolved calendar date for one-time rows —
    a weekday name alone is what made the original off-by-one invisible."""
    if not once or not fire_at:
        return f"{action} at {time_str} — RECURRING, every {describe_days(days)}"
    local = datetime.fromisoformat(fire_at).astimezone(now.tzinfo)
    line = f"{action} at {time_str} on {local.date().isoformat()} ({local.strftime('%a')}) — ONE-TIME"
    if (local.date() - now.date()).days > 6:
        line += " — NEXT WEEK"
    return line
```

Rewrite `_get_schedule_impl`'s tail (keeping its read-only `remove_expired` sweep):

```python
    rows = store.list(device)
    if not rows:
        return "nothing scheduled" if device is None else f"{device}: nothing scheduled"
    now = now_fn()
    return "\n".join(
        f"{r['device']}: {_describe_row(r['action'], r['time'], r['days'], r['once'], r['fire_at'], now)}"
        for r in rows)
```

Drop the `readback`/`Event`/`DeviceSchedule`/`Schedule` imports if they become unused in this module (`_program_device` still needs `Event`, `Schedule`, `DeviceSchedule`, so only `readback` goes; keep `describe_days`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS, full suite.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/test_schedule_tools.py
git commit -m "feat(schedules): show the resolved date in confirmations and listings"
```

---

### Task 9: Date-aware cancel

**Files:**
- Modify: `src/home_agent/schedules.py` — `_CANCEL_SCHEMA`, `_cancel_impl`
- Test: `tests/home_agent/test_schedule_tools.py`

**Interfaces:**
- Consumes: `_resolve_fire_at` (Task 6), `_describe_row` (Task 8), `ScheduleStore.remove_id`.
- Produces: `cancel_schedule(device, time=?, when=?)`. Selection happens in Python and deletion is per-row via `remove_id`; `store.remove`'s rowcount is no longer the "nothing matched" signal.

- [ ] **Step 1: Write the failing test**

```python
def test_cancel_can_disambiguate_two_timers_at_the_same_clock_time(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "in_a_week"})
    out = _tool(tools, "cancel_schedule").impl(
        {"device": "פינת אוכל", "time": "18:30", "when": "tomorrow"})
    assert "2026-08-15" in out
    remaining = store.list("dining")
    assert len(remaining) == 1 and remaining[0]["fire_at"].startswith("2026-08-21")


def test_cancel_with_a_date_leaves_recurring_rows_alone(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "days": ["mon"],
         "repetition_phrase": "כל יום שני"})
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    _tool(tools, "cancel_schedule").impl(
        {"device": "פינת אוכל", "time": "18:30", "when": "tomorrow"})
    remaining = store.list("dining")
    assert len(remaining) == 1 and remaining[0]["once"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_schedule_tools.py -q -k cancel`
Expected: FAIL — both timers are deleted, since matching is device+time only.

- [ ] **Step 3: Write minimal implementation**

Add `when` to `_CANCEL_SCHEMA["function"]["parameters"]["properties"]` (same enum, optional) and update its description to: *"Cancel timers. Device alone clears all of them; add `time`, and `when` if two timers share a clock time on different dates. Reports the date of everything cancelled."*

```python
def _cancel_impl(args, *, registry, store, write_fn, now_fn, scheduler=None):
    spoken = (args.get("device") or "").strip()
    time_str = (args.get("time") or "").strip() or None
    when = (args.get("when") or "").strip() or None
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"

    _expire_and_reprogram(store, registry, write_fn, now_fn)
    now = now_fn()
    target_date = None
    if when:
        if not time_str:
            return "give the time too when you name a day."
        try:
            target_date = _resolve_fire_at(when, time_str, now).date()
        except ValueError as e:
            return f"couldn't cancel: {e}"

    removed_rows = []
    for r in store.list(name):
        if time_str is not None and r["time"] != time_str:
            continue
        if target_date is not None:
            # Recurring rows have no date and must never be swept up by a dated cancel.
            if not r["once"] or not r["fire_at"]:
                continue
            # fire_at is UTC; compare in HOME_TZ or a 01:00-local timer lands a day early.
            if datetime.fromisoformat(r["fire_at"]).astimezone(now.tzinfo).date() != target_date:
                continue
        removed_rows.append(r)

    if not removed_rows:
        return f"nothing scheduled matched for {name}."
    for r in removed_rows:
        store.remove_id(r["id"])
    if registry.is_cloud(name):
        if scheduler is not None:
            for r in removed_rows:
                scheduler.unschedule(r["id"])
    else:
        try:
            _program_device(name, store, registry, write_fn)
        except Exception as e:
            for r in removed_rows:   # roll back so the record matches the Bot
                store.add(r["device"], r["action"], r["time"], r["days"], r["once"], r["fire_at"])
            return f"couldn't reprogram {name} ({e}) — timer(s) not cancelled, try again."
    what = "; ".join(
        _describe_row(r["action"], r["time"], r["days"], r["once"], r["fire_at"], now)
        for r in removed_rows)
    return f"{name}: cancelled {len(removed_rows)} timer(s) — {what} ✅"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS, full suite.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/test_schedule_tools.py
git commit -m "feat(schedules): cancel by date so same-time timers are distinguishable"
```

---

### Task 10: The three prompt rules

**Files:**
- Modify: `src/home_agent/prompts.py:9-11`
- Test: `tests/home_agent/test_system_prompt.py`

**Interfaces:**
- Consumes: the tool names from Task 7.
- Produces: no code interface; three added sentences.

- [ ] **Step 1: Write the failing test**

```python
def test_prompt_carries_the_scheduling_rules(tmp_path):
    p = FAMILY_SYSTEM_PROMPT
    assert "one-time" in p.lower()
    assert "schedule_recurring_device" in p
    assert "date" in p.lower()
    assert not any(ch.isdigit() for ch in p)      # still digit-free
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_system_prompt.py -q`
Expected: FAIL — `schedule_recurring_device` is not in the prompt.

- [ ] **Step 3: Write minimal implementation**

Replace the scheduling sentence at `prompts.py:9-10` with (note: no digits anywhere):

```python
    "You can control the home devices immediately, and you can schedule device on/off/press timers "
    "that run on the devices themselves. Device timers are one-time by default: use "
    "schedule_recurring_device only when the user explicitly asked for repetition, such as every "
    "day or every week — this is rare. When you confirm or list a device timer, always state the "
    "calendar date the tool returned, never only a relative word like tomorrow. Never say that a "
    "timer was set, changed, or cancelled unless the tool call actually returned success. "
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_system_prompt.py -q`
Expected: PASS — including the existing digit-free and byte-stability tests.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/prompts.py tests/home_agent/test_system_prompt.py
git commit -m "feat(prompts): one-time by default, always state the date, never claim untested success"
```

---

### Task 11: Model-facing regression tests

**Files:**
- Test: `tests/home_agent/test_schedule_tools.py` (or a new `tests/home_agent/test_schedule_model_behavior.py`)

**Interfaces:**
- Consumes: `make_fake_client` (fixture in `tests/home_agent/conftest.py`), `run_turn`.
- Produces: nothing.

These are the tests that would have caught the original incident end-to-end.

- [ ] **Step 1: Write the tests**

```python
def test_model_reaches_for_the_one_time_tool_on_a_named_weekday(tmp_path, make_fake_client):
    """The incident's exact wrong turn: 'ביום שישי' must not become a weekly timer."""
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "schedule_device",
                         "arguments": {"device": "סלון", "action": "on", "time": "18:30",
                                       "when": "sat"}}]},
        {"content": "קבעתי להדלקה בשבת בשעה 18:30, פעם אחת."},
    ])
    writes = []
    tools, _store = _tools(tmp_path, writes, now=_fri_1721)
    run_turn("תדליק את האור בשבת ב-18:30", [], client=client, model="gpt-4o",
             system=FAMILY_SYSTEM_PROMPT, tools=tools)
    sent_tools = {t["function"]["name"] for t in client._calls[0]["tools"]}
    assert "schedule_recurring_device" in sent_tools               # it WAS available
    tool_msgs = [m for m in client._calls[-1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs, "the model made no tool call"
    assert all("RECURRING" not in m["content"] for m in tool_msgs)


def test_model_states_the_date_in_its_reply(tmp_path, make_fake_client):
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "schedule_device",
                         "arguments": {"device": "פינת אוכל", "action": "on",
                                       "time": "18:30", "when": "tomorrow"}}]},
        {"content": "תזמנתי למחר, 2026-08-15, בשעה 18:30."},
    ])
    writes = []
    tools, _store = _tools(tmp_path, writes, now=_fri_1721)
    reply = run_turn("תזמן את האור למחר ב-18:30", [], client=client, model="gpt-4o",
                     system=FAMILY_SYSTEM_PROMPT, tools=tools)
    assert "2026-08-15" in reply
    # and the tool result the model was given carried that date in the first place
    tool_msg = [m for m in client._calls[-1]["messages"] if m.get("role") == "tool"][0]
    assert "2026-08-15" in tool_msg["content"]
```

The fake client records each `chat.completions.create(**kwargs)` call in `client._calls`, so
`client._calls[0]["tools"]` is the schema list the model saw and `client._calls[-1]["messages"]`
contains the tool-result messages fed back. There is no dedicated accessor — use `client._calls`.

- [ ] **Step 2: Run them**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/ -q -k model`
Expected: PASS.

- [ ] **Step 3: Run the whole suite**

Run: `/Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/home_agent/
git commit -m "test(schedules): model-facing regression tests for the tomorrow incident"
```

**PHASE 2 GATE:** full suite green, the reported bug covered by a test. Review before continuing.

---

# Phase 3 — BLE spike and rollout

### Task 12: Verify the Bot firmware's once+weekday semantic (manual, hardware)

**Files:**
- Create: `docs/superpowers/specs/2026-08-15-ble-once-weekday-spike.md` (findings)

**Interfaces:**
- Consumes: nothing.
- Produces: a documented yes/no on whether bit 7 + a day mask means "fire once on that weekday".

**This gates BLE only.** The box currently routes all five devices via `cloud_id`, so phases 1–2 ship without it. Do it before any device returns to BLE.

- [ ] **Step 1: Program a Bot with a non-adjacent weekday**

On the box, set a one-time timer for a weekday two or more days out and confirm it does **not** fire before that day.

- [ ] **Step 2: Test the `in_a_week` edge specifically**

`in_a_week` resolves to **today's own weekday**. If the firmware reads bit 7 as "next occurrence of that weekday", a BLE `in_a_week` set for a time still ahead today would fire **today** instead of in seven days. Verify explicitly.

- [ ] **Step 3: Record the result and, if the semantic fails, implement the fallback**

Fallback per the spec: program BLE one-timers as recurring-on-that-weekday plus a store-side auto-cancel after firing.

- [ ] **Step 4: Commit findings**

```bash
git add docs/superpowers/specs/2026-08-15-ble-once-weekday-spike.md
git commit -m "docs(schedules): BLE once+weekday firmware spike findings"
```

---

### Task 13: Deploy and verify against the real bug

- [ ] **Step 1: Check the migration precondition**

The new UTC `fire_at` format must not mix with old local-tz rows:

```bash
ssh -i ~/.ssh/smarthome_box nathaniel@100.111.96.97 \
  "cd ~/smart-home && PYTHONPATH=src ./.venv/bin/python -c \"
from home_agent.schedule_store import ScheduleStore
print(ScheduleStore('/home/nathaniel/smart-home/home_agent.db').list())\""
```

Expected: `[]`. If not empty, normalize the surviving `fire_at` values to UTC before starting the new code.

- [ ] **Step 2: Deploy**

Use the `deploy-box` skill (rsync over Tailscale + systemd restart + health check). Do not hand-roll it.

- [ ] **Step 3: Verify against the actual reported bug**

In the Telegram group, ask for a light **tomorrow** at a near time. Confirm:
1. The Hebrew reply states tomorrow's calendar date, not just "מחר".
2. `get_schedule` agrees.
3. **The device actually fires tomorrow and not today.** The store being right is not sufficient evidence — that was true in the incident too.
