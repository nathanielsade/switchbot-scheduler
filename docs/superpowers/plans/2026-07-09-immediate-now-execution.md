# Immediate ("now") Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user say "turn on X now" (עכשיו) and have the bot actuate immediately over live BLE, while still supporting scheduled timers — in the same message if needed.

**Architecture:** The parser gains an `immediate` output array (actions with no time). A new `actuator.py` sends a live `57 01 xx` BLE control command per device. `core.preview_conversation` returns a `PreviewResult` carrying clarification / schedule / immediate. The web layer fires immediate actions on send with no confirmation (new `/execute` endpoint) and keeps the existing Approve-&-write flow for schedules.

**Tech Stack:** Python 3.11+, FastAPI, bleak (BLE), OpenAI (parser), pytest.

## Global Constraints

- Python 3.11+; no new third-party dependencies (bleak and openai already vendored).
- Never fabricate a schedule time. Immediate intent → `immediate`; genuinely ambiguous → `clarification`.
- Immediate actions fire with **no confirmation** and must **report** each action taken (success or per-device failure).
- Live-action semantics must match the scheduled path: `inverted` swaps on/off; press-mode (`ac`) forces `press`. Reuse `encoder.ACTION_CODE` (`press=0, on=1, off=2`); command byte is `0x57 0x01 <code>`.
- Success is NOT gated on the BLE reply byte (new firmware returns `0x05` yet actuates); success = write completed without a BLE exception.
- Run tests with the project venv: `.venv/bin/python -m pytest`.
- Follow existing patterns: dataclasses for models, `completion_fn` test seam for the parser, `monkeypatch` + `TestClient` for web tests.

---

### Task 1: `ImmediateAction` model + parser emits immediate actions

**Files:**
- Modify: `src/switchbot_scheduler/model.py` (add `ImmediateAction`)
- Modify: `src/switchbot_scheduler/parser.py` (`ParseResult.immediate`, prompt rules, `parse_conversation`)
- Test: `tests/test_parser_conversation.py`

**Interfaces:**
- Produces: `ImmediateAction(device: str, action: str)` in `model.py`.
- Produces: `ParseResult(schedule, clarification, immediate: list[ImmediateAction])` — `immediate` defaults to `[]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_parser_conversation.py`:

```python
def test_conversation_emits_immediate_now():
    canned = lambda s, u: json.dumps({"immediate": [{"device": "living_room", "action": "on"}]})
    res = parse_conversation(["turn on the salon now"], _reg(), NOW, completion_fn=canned)
    assert res.schedule is None
    assert res.clarification is None
    assert len(res.immediate) == 1
    assert res.immediate[0].device == "living_room" and res.immediate[0].action == "on"


def test_conversation_resolves_immediate_alias():
    canned = lambda s, u: json.dumps({"immediate": [{"device": "סלון", "action": "off"}]})
    res = parse_conversation(["כבה את הסלון עכשיו"], _reg(), NOW, completion_fn=canned)
    assert res.immediate[0].device == "living_room"  # alias resolved to canonical


def test_conversation_mixed_immediate_and_schedule():
    canned = lambda s, u: json.dumps({
        "immediate": [{"device": "living_room", "action": "on"}],
        "schedules": [{"device": "living_room",
            "events": [{"time": "22:00", "action": "off", "days": ["mon"], "once": False}]}],
    })
    res = parse_conversation(["salon on now and off at 22:00 mondays"], _reg(), NOW, completion_fn=canned)
    assert len(res.immediate) == 1 and res.immediate[0].action == "on"
    assert res.schedule is not None and res.schedule.schedules[0].events[0].time == "22:00"


def test_immediate_prompt_forbids_fabricated_time():
    seen = {}
    def cap(system, user):
        seen["system"] = system
        return json.dumps({"immediate": [{"device": "living_room", "action": "on"}]})
    parse_conversation(["now"], _reg(), NOW, completion_fn=cap)
    assert "immediate" in seen["system"]
    assert "never" in seen["system"].lower()  # the "never invent a time" rule is present
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_parser_conversation.py -k immediate -v`
Expected: FAIL (`ImmediateAction` / `res.immediate` do not exist yet).

- [ ] **Step 3: Add the `ImmediateAction` dataclass**

In `src/switchbot_scheduler/model.py`, after the `Event` dataclass, add:

```python
@dataclass
class ImmediateAction:
    device: str        # canonical device name
    action: Action     # "on" | "off" | "press"
```

- [ ] **Step 4: Extend `ParseResult` and `parse_conversation`**

In `src/switchbot_scheduler/parser.py`:

Change the import at the top of the file:

```python
from dataclasses import dataclass, field
```

Add `ImmediateAction` to the model import:

```python
from .model import Schedule, DeviceSchedule, Event, ImmediateAction
```

Replace the `ParseResult` dataclass with:

```python
@dataclass
class ParseResult:
    schedule: Schedule | None
    clarification: str | None
    immediate: list[ImmediateAction] = field(default_factory=list)
```

In `build_conversation_system_prompt`, replace the "Output EXACTLY ONE of" block and add an immediate rule. The output-shape block becomes:

```python
    return f"""You convert a conversation about device schedules (Hebrew or English) into strict JSON.
Today is {today}.

Output JSON with any of these keys (omit a key when it does not apply):
  "immediate": [{{"device": <name>, "action": "on"|"off"|"press"}}]        # act right now, NO time
  "schedules": [{{"device": <name>, "events": [
     {{"time": "HH:MM", "action": "on"|"off"|"press", "days": [<weekdays>], "once": <bool>}} ]}} ]
  "clarification": "<a short question or explanation>"                       # use alone, when unsure

Known device names (map spoken names/aliases to exactly one of these):
{names}

Rules:
- "now"/"עכשיו"/"right now"/"straight away", or an act-now verb with no future time,
  means IMMEDIATE: put it in "immediate" (no time). NEVER invent a time and NEVER emit a
  00:00 schedule for a "now" request.
- weekdays are lowercase 3-letter codes: sun mon tue wed thu fri sat. Use 24-hour zero-padded time.
- Every turn-ON and every turn-OFF is its OWN event. Each device supports at most {MAX_ALARMS} events.
- Recurring: "every day"/no day -> all 7 days, once=false. Named weekdays repeating -> those days, once=false.
- One-time: "today"/"tomorrow"/"this <weekday>" -> set days to that single weekday and once=true.
  Resolve relative days using today's date above.
- NEVER output "every day" when the user implied a specific or one-time day.
- A single message may contain BOTH immediate actions and schedules — output both keys.
- If the request is ambiguous, unparseable, or asks for a specific calendar date more than 7 days away
  (which the device cannot do), return ONLY "clarification" — do NOT guess a schedule.
- The conversation is a list of user turns, newest reflecting corrections. Output the CURRENT complete
  intended schedule reflecting the WHOLE conversation.
"""
```

Replace `parse_conversation` with:

```python
def parse_conversation(messages, registry, now, completion_fn=_default_completion) -> ParseResult:
    system = build_conversation_system_prompt(registry, now)
    convo = "\n".join(f"[{i+1}] {m}" for i, m in enumerate(messages))
    raw = completion_fn(system, convo)
    data = json.loads(raw)

    immediate = []
    for a in data.get("immediate", []):
        canonical = registry.resolve(a["device"])
        immediate.append(ImmediateAction(device=canonical or a["device"], action=a["action"]))

    if "clarification" in data and "schedules" not in data and "immediate" not in data:
        return ParseResult(schedule=None, clarification=str(data["clarification"]), immediate=[])

    schedules = []
    for s in data.get("schedules", []):
        raw_device = s["device"]
        canonical = registry.resolve(raw_device)
        device = canonical if canonical is not None else raw_device
        events = [Event(time=e["time"], action=e["action"], days=e["days"], once=bool(e.get("once", False)))
                  for e in s["events"]]
        schedules.append(DeviceSchedule(device=device, events=events))
    schedule = Schedule(schedules=schedules) if schedules else None
    return ParseResult(schedule=schedule, clarification=None, immediate=immediate)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_parser_conversation.py -v`
Expected: PASS (new immediate tests + the 4 pre-existing tests). Note `test_clarification_path` still passes: its canned response is clarification-only, so `schedule is None` and `immediate == []`.

- [ ] **Step 6: Commit**

```bash
git add src/switchbot_scheduler/model.py src/switchbot_scheduler/parser.py tests/test_parser_conversation.py
git commit -m "feat(parser): emit immediate actions for 'now' intent

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `actuator.py` — live BLE actuation

**Files:**
- Create: `src/switchbot_scheduler/actuator.py`
- Test: `tests/test_actuator.py`

**Interfaces:**
- Consumes: `ImmediateAction` (Task 1), `encoder.ACTION_CODE`, `Registry` (`known_names`, `is_press_mode`, `is_inverted`, `ble_id`).
- Produces:
  - `ActionResult(device: str, action: str, ok: bool, error: str | None = None)`
  - `resolve_action(device: str, action: str, registry: Registry) -> str`
  - `run_immediate(actions: list[ImmediateAction], registry: Registry, actuate_fn=None) -> list[ActionResult]`
  - `actuate(ble_id: str, action_code: int) -> bytes` (async; real BLE)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_actuator.py`:

```python
from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.model import ImmediateAction
from switchbot_scheduler.actuator import run_immediate, resolve_action


def _reg():
    return Registry([
        Device(name="living_room", aliases=[], ble_id="U1", inverted=True),
        Device(name="kitchen", aliases=[], ble_id="U2"),
        Device(name="ac", aliases=[], ble_id="U3", mode="press"),
        Device(name="no_ble", aliases=[], ble_id=""),
    ])


def test_resolve_action_plain():
    assert resolve_action("kitchen", "on", _reg()) == "on"


def test_resolve_action_inverted_swaps_on_off():
    assert resolve_action("living_room", "on", _reg()) == "off"
    assert resolve_action("living_room", "off", _reg()) == "on"


def test_resolve_action_press_mode_forces_press():
    assert resolve_action("ac", "on", _reg()) == "press"


def test_run_immediate_sends_correct_bytes():
    calls = []
    fake = lambda ble_id, code: calls.append((ble_id, code)) or b"\x01"
    results = run_immediate([ImmediateAction("kitchen", "on")], _reg(), actuate_fn=fake)
    assert calls == [("U2", 1)]                       # ACTION_CODE["on"] == 1
    assert results[0].ok is True and results[0].action == "on"


def test_run_immediate_inverted_and_press():
    calls = []
    fake = lambda ble_id, code: calls.append((ble_id, code)) or b""
    run_immediate([ImmediateAction("living_room", "on"), ImmediateAction("ac", "off")], _reg(), actuate_fn=fake)
    assert calls == [("U1", 2), ("U3", 0)]            # inverted on->off (2); press-mode ->press (0)


def test_run_immediate_unknown_device_is_reported_not_raised():
    fake = lambda ble_id, code: b""
    results = run_immediate([ImmediateAction("bedroom", "on")], _reg(), actuate_fn=fake)
    assert results[0].ok is False and "unknown" in results[0].error.lower()


def test_run_immediate_missing_ble_id_reported():
    fake = lambda ble_id, code: b""
    results = run_immediate([ImmediateAction("no_ble", "on")], _reg(), actuate_fn=fake)
    assert results[0].ok is False and "ble_id" in results[0].error


def test_run_immediate_ble_error_does_not_abort_others():
    def fake(ble_id, code):
        if ble_id == "U1":
            raise RuntimeError("out of range")
        return b""
    results = run_immediate([ImmediateAction("living_room", "on"), ImmediateAction("kitchen", "on")], _reg(), actuate_fn=fake)
    assert results[0].ok is False and "out of range" in results[0].error
    assert results[1].ok is True                       # second device still ran
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_actuator.py -v`
Expected: FAIL (`switchbot_scheduler.actuator` does not exist).

- [ ] **Step 3: Write `actuator.py`**

Create `src/switchbot_scheduler/actuator.py`:

```python
import asyncio
from dataclasses import dataclass
from .encoder import ACTION_CODE
from .registry import Registry
from .model import ImmediateAction

# Same GATT characteristics as the scheduled writer.
WRITE_CHAR = "cba20002-224d-11e6-9fb8-0002a5d5c51b"
NOTIFY_CHAR = "cba20003-224d-11e6-9fb8-0002a5d5c51b"
MAGIC = 0x57
CMD_CONTROL = 0x01   # 0x57 0x01 <code>: press=0, on=1, off=2


@dataclass
class ActionResult:
    device: str
    action: str          # the RESOLVED action actually sent (after inverted/press mapping)
    ok: bool
    error: str | None = None


def resolve_action(device: str, action: str, registry: Registry) -> str:
    """Mirror the scheduled path: press-mode forces press; inverted swaps on/off."""
    if registry.is_press_mode(device):
        return "press"
    if registry.is_inverted(device) and action in ("on", "off"):
        return "off" if action == "on" else "on"
    return action


async def actuate(ble_id: str, action_code: int) -> bytes:
    """Send one live control command and return the Bot's reply (empty if none)."""
    from bleak import BleakClient
    responses: list[bytes] = []
    async with BleakClient(ble_id) as client:
        await client.start_notify(NOTIFY_CHAR, lambda _, d: responses.append(bytes(d)))
        await client.write_gatt_char(WRITE_CHAR, bytes([MAGIC, CMD_CONTROL, action_code]), response=True)
        await asyncio.sleep(1.0)
        await client.stop_notify(NOTIFY_CHAR)
    return responses[-1] if responses else b""


def _run_actuate(ble_id: str, action_code: int) -> bytes:
    return asyncio.run(actuate(ble_id, action_code))


def run_immediate(actions, registry: Registry, actuate_fn=None) -> list[ActionResult]:
    """Fire each immediate action live, one BLE connection per device. Never raises:
    a per-device failure becomes an ActionResult(ok=False) and does not abort the rest."""
    do = actuate_fn or _run_actuate
    known = registry.known_names()
    results: list[ActionResult] = []
    for a in actions:
        if a.device not in known:
            results.append(ActionResult(a.device, a.action, False, f"unknown device '{a.device}'"))
            continue
        action = resolve_action(a.device, a.action, registry)
        ble_id = registry.ble_id(a.device)
        if not ble_id:
            results.append(ActionResult(a.device, action, False, "no ble_id in devices.yaml"))
            continue
        try:
            do(ble_id, ACTION_CODE[action])
            results.append(ActionResult(a.device, action, True, None))
        except Exception as err:
            results.append(ActionResult(a.device, action, False, str(err)))
    return results
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_actuator.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/switchbot_scheduler/actuator.py tests/test_actuator.py
git commit -m "feat(actuator): live BLE actuation with inverted/press mapping

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `core.preview_conversation` returns `PreviewResult` with immediate

**Files:**
- Modify: `src/switchbot_scheduler/core.py`
- Test: `tests/test_core.py` (update the 2 existing `preview_conversation` tests + add immediate coverage)

**Interfaces:**
- Consumes: `ParseResult.immediate` (Task 1).
- Produces: `PreviewResult(clarification: str | None, schedule: Schedule | None, readback: str | None, immediate: list[ImmediateAction])`. Returned by `preview_conversation` (its return type CHANGES from a 3-tuple to `PreviewResult`).

- [ ] **Step 1: Update the two existing tests and add new ones**

In `tests/test_core.py`, replace `test_preview_conversation_schedule` and `test_preview_conversation_clarification` with:

```python
def test_preview_conversation_schedule():
    canned = lambda s, u: _json.dumps({"schedules": [{"device": "living_room",
        "events": [{"time": "09:00", "action": "on", "days": ["mon"], "once": True}]}]})
    res = preview_conversation(["living room on tomorrow, once"], _reg(),
                               _dt(2026, 7, 5, 12, 0), completion_fn=canned)
    assert res.clarification is None
    assert res.schedule is not None and "once (mon)" in res.readback
    assert res.immediate == []


def test_preview_conversation_clarification():
    canned = lambda s, u: _json.dumps({"clarification": "Which device?"})
    res = preview_conversation(["do it"], _reg(), _dt(2026, 7, 5, 12, 0), completion_fn=canned)
    assert res.clarification == "Which device?" and res.schedule is None and res.immediate == []


def test_preview_conversation_immediate_only():
    canned = lambda s, u: _json.dumps({"immediate": [{"device": "living_room", "action": "on"}]})
    res = preview_conversation(["salon on now"], _reg(), _dt(2026, 7, 5, 12, 0), completion_fn=canned)
    assert res.schedule is None and res.readback is None
    assert len(res.immediate) == 1 and res.immediate[0].action == "on"


def test_preview_conversation_mixed():
    canned = lambda s, u: _json.dumps({
        "immediate": [{"device": "living_room", "action": "on"}],
        "schedules": [{"device": "living_room",
            "events": [{"time": "22:00", "action": "off", "days": ["mon"], "once": False}]}]})
    res = preview_conversation(["salon on now, off 22:00 mon"], _reg(), _dt(2026, 7, 5, 12, 0), completion_fn=canned)
    assert len(res.immediate) == 1
    assert res.schedule is not None and "living_room: off 22:00" in res.readback
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_core.py -k preview -v`
Expected: FAIL (`preview_conversation` still returns a tuple; `res.clarification` attribute access fails).

- [ ] **Step 3: Add `PreviewResult` and rewrite `preview_conversation`**

In `src/switchbot_scheduler/core.py`:

Update imports at the top:

```python
from dataclasses import dataclass
from .parser import parse_schedule, parse_conversation
from .validator import validate
from .readback import readback
from .model import Schedule, ImmediateAction
from .registry import Registry
```

Add the dataclass (after the imports, before `build_schedule`):

```python
@dataclass
class PreviewResult:
    clarification: str | None
    schedule: Schedule | None
    readback: str | None
    immediate: list[ImmediateAction]
```

Replace `preview_conversation` with:

```python
def preview_conversation(messages, registry: Registry, now, completion_fn=None) -> PreviewResult:
    kwargs = {"completion_fn": completion_fn} if completion_fn is not None else {}
    result = parse_conversation(messages, registry, now, **kwargs)
    if result.clarification is not None:
        return PreviewResult(clarification=result.clarification, schedule=None,
                             readback=None, immediate=[])
    schedule = result.schedule
    if schedule is not None and schedule.schedules:
        validate(schedule, registry)
        _apply_press_mode(schedule, registry)
        return PreviewResult(clarification=None, schedule=schedule,
                             readback=readback(schedule), immediate=result.immediate)
    return PreviewResult(clarification=None, schedule=None, readback=None,
                         immediate=result.immediate)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_core.py -v`
Expected: PASS (preview tests updated; the `apply_schedule` tests are untouched and still pass).

- [ ] **Step 5: Commit**

```bash
git add src/switchbot_scheduler/core.py tests/test_core.py
git commit -m "feat(core): preview_conversation returns PreviewResult with immediate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: web `/preview` extension + new `/execute` endpoint

**Files:**
- Modify: `src/switchbot_scheduler/web/app.py`
- Test: `tests/test_web.py` (add `immediate` field to existing assertions where needed + new `/execute` tests)

**Interfaces:**
- Consumes: `PreviewResult` (Task 3), `run_immediate` + `ActionResult` (Task 2), `ImmediateAction` (Task 1).
- Produces HTTP contract:
  - `POST /preview` → `{"ok": true, "kind": "clarification"|"schedule"|"none", "immediate": [{device, action}], ...}`
    - `kind=="schedule"` also has `readback`, `schedule`; `kind=="clarification"` also has `message`. `immediate` is always present (possibly `[]`).
  - `POST /execute` body `{"actions": [{device, action}]}` → `{"ok": true, "results": [{device, action, ok, error}]}`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web.py`, add:

```python
def test_preview_immediate_only_kind_none(tmp_path, monkeypatch):
    imm = lambda s, u: json.dumps({"immediate": [{"device": "living_room", "action": "on"}]})
    body = _client(tmp_path, monkeypatch, imm).post("/preview", json={"messages": ["salon on now"]}).json()
    assert body["ok"] is True and body["kind"] == "none"
    assert body["immediate"] == [{"device": "living_room", "action": "on"}]


def test_preview_mixed_has_schedule_and_immediate(tmp_path, monkeypatch):
    mixed = lambda s, u: json.dumps({
        "immediate": [{"device": "living_room", "action": "on"}],
        "schedules": [{"device": "living_room",
            "events": [{"time": "22:00", "action": "off", "days": ["mon"]}]}]})
    body = _client(tmp_path, monkeypatch, mixed).post("/preview", json={"messages": ["salon now + 22:00"]}).json()
    assert body["kind"] == "schedule"
    assert body["immediate"] == [{"device": "living_room", "action": "on"}]
    assert "living_room: off 22:00" in body["readback"]


def test_execute_runs_immediate_actions(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = []
    from switchbot_scheduler.web import app as wa
    monkeypatch.setattr(wa, "run_immediate", lambda actions, reg: [
        wa.ActionResult(a.device, a.action, True, None) for a in actions])
    body = client.post("/execute", json={"actions": [{"device": "living_room", "action": "on"}]}).json()
    assert body["ok"] is True
    assert body["results"] == [{"device": "living_room", "action": "on", "ok": True, "error": None}]


def test_execute_reports_per_device_failure(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from switchbot_scheduler.web import app as wa
    monkeypatch.setattr(wa, "run_immediate", lambda actions, reg: [
        wa.ActionResult("living_room", "on", False, "out of range")])
    body = client.post("/execute", json={"actions": [{"device": "living_room", "action": "on"}]}).json()
    assert body["ok"] is True
    assert body["results"][0]["ok"] is False and "out of range" in body["results"][0]["error"]
```

Also update `test_preview_thread_returns_schedule_kind` to assert the new field is present:

```python
def test_preview_thread_returns_schedule_kind(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)  # canned parser returns living_room on 06:00
    body = client.post("/preview", json={"messages": ["salon on 6am"]}).json()
    assert body["ok"] is True and body["kind"] == "schedule"
    assert "living_room: on 06:00" in body["readback"]
    assert body["immediate"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_web.py -k "immediate or execute or schedule_kind" -v`
Expected: FAIL (no `/execute` route; `immediate` key missing; `run_immediate`/`ActionResult` not imported into the web module).

- [ ] **Step 3: Update `web/app.py`**

Add imports (with the other `..` imports near the top):

```python
from ..actuator import run_immediate, ActionResult
from ..model import Schedule, DeviceSchedule, Event, ImmediateAction
```

(Replace the existing `from ..model import Schedule, DeviceSchedule, Event` line with the one above.)

Add the request model next to `ApplyReq`:

```python
class ExecuteReq(BaseModel):
    actions: list[dict]
```

Replace the `/preview` handler body with:

```python
@app.post("/preview")
def preview(req: PreviewReq):
    try:
        registry = _registry()
        pr = preview_conversation(
            req.messages, registry, datetime.now(), completion_fn=_completion_fn)
        immediate = [{"device": a.device, "action": a.action} for a in pr.immediate]
        if pr.clarification is not None:
            return {"ok": True, "kind": "clarification", "message": pr.clarification, "immediate": immediate}
        if pr.schedule is not None:
            return {"ok": True, "kind": "schedule", "readback": pr.readback,
                    "schedule": schedule_to_json(pr.schedule), "immediate": immediate}
        return {"ok": True, "kind": "none", "immediate": immediate}
    except Exception as err:
        return {"ok": False, "error": str(err)}
```

Add the `/execute` handler after `/apply`:

```python
@app.post("/execute")
def execute(req: ExecuteReq):
    try:
        registry = _registry()
        actions = [ImmediateAction(device=a["device"], action=a["action"]) for a in req.actions]
        results = run_immediate(actions, registry)
        return {"ok": True, "results": [
            {"device": r.device, "action": r.action, "ok": r.ok, "error": r.error} for r in results]}
    except Exception as err:
        return {"ok": False, "error": str(err)}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_web.py -v`
Expected: PASS (new `/execute` + immediate tests, and all pre-existing web tests).

- [ ] **Step 5: Commit**

```bash
git add src/switchbot_scheduler/web/app.py tests/test_web.py
git commit -m "feat(web): /preview carries immediate; new /execute fires live actions

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Frontend wiring — fire immediate on send, report each action

**Files:**
- Modify: `src/switchbot_scheduler/web/static/index.html`

**Interfaces:**
- Consumes: the `/preview` response (`kind`, `immediate`, `readback`, `schedule`) and `/execute` (`results`) from Task 4.

There is no JS test harness in this project, so this task is verified manually against the running app.

- [ ] **Step 1: Add a `runImmediate` helper**

In `src/switchbot_scheduler/web/static/index.html`, inside `<script>`, add after the `scheduleCard` function:

```javascript
async function runImmediate(actions){
  try{
    const r=await fetch('/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actions})});
    const data=await r.json();
    if(!data.ok){ bubble('⚠️ '+data.error,'bot','err'); return; }
    data.results.forEach(res=>{
      if(res.ok){
        const verb = res.action==='on' ? 'Turned on' : res.action==='off' ? 'Turned off' : 'Pressed';
        bubble('⚡ '+verb+' '+res.device,'bot','ok');
      } else {
        bubble('⚠️ '+res.device+' — '+res.error,'bot','err');
      }
    });
  }catch(err){ bubble('⚠️ '+err,'bot','err'); }
}
```

- [ ] **Step 2: Branch on the new `/preview` response shape**

Replace the result-handling block inside `form.onsubmit` (the `if(!data.ok)…else…` chain) with:

```javascript
    if(!data.ok){ bubble('⚠️ '+data.error,'bot','err'); }
    else if(data.kind==='clarification'){ bubble(data.message,'bot'); }
    else {
      if(data.immediate && data.immediate.length){
        await runImmediate(data.immediate);
        thread=[];   // immediate intent is one-shot — clear the thread so it can't re-fire next turn
      }
      if(data.kind==='schedule'){ scheduleCard(data.readback, data.schedule); }
    }
```

- [ ] **Step 3: Manual verification against the running app**

Start the app and drive it (see `start.sh` / `web.app:main`, port 8000):

```bash
.venv/bin/python -m switchbot_scheduler.web.app
```

Verify, near a bot in BLE range (needs `OPENAI_API_KEY` in `.env`):
1. `תדליק עכשיו את האור במטבח` → a `⚡ Turned on kitchen` line appears and the bot actuates. **No** "Approve & write" card, **no** `00:00` schedule.
2. A message that turns one bot on now and schedules another for later → immediate line AND an Approve card both appear.
3. A bot out of range → `⚠️ <device> — …` line; other devices in the same message still act.

Expected: all three behave as described. If `OPENAI_API_KEY` is not set, verify shape only via `curl` against `/execute` with a hand-written `{"actions":[...]}` body.

- [ ] **Step 4: Run the full suite once more**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all prior tests + the new parser/actuator/core/web tests).

- [ ] **Step 5: Commit**

```bash
git add src/switchbot_scheduler/web/static/index.html
git commit -m "feat(web-ui): fire immediate actions on send and report each

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes / residuals (carry into implementation)

- The header copy in `index.html` still says "you'll approve before anything is written" — that is now only true for schedules. Update it in Task 5 Step 2 if desired (e.g. "…schedules need approval; 'now' acts immediately").
- **Mixed-message correction tradeoff:** executing immediate actions resets the conversation thread so a "now" action can't re-fire on the next turn. For a mixed message, this means correcting the *scheduled* part afterward requires re-stating it. Acceptable for now; revisit if it bites.
- The `57 01 01`/`57 01 02` (on/off) bytes are spike-verified only for `press` (`57 01 00`). Do a quick hardware check on a real switch-mode bot on the new firmware before trusting on/off in production.
