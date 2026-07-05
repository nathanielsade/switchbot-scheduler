# Conversational Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the scheduler conversational (remembers the thread so corrections refine), support one-time scheduling within 7 days ("tomorrow"/"this Friday"), and never silently default — return a clarification instead.

**Architecture:** Add `once` to the model + encoder (fire-once bit). Replace the parser's single-prompt entry with a date-aware `parse_conversation(messages, …)` that returns a schedule OR a clarification. Add `preview_conversation` to core. The `/preview` endpoint takes a message thread and returns a `kind`; the frontend keeps thread memory and renders schedule cards vs clarification bubbles.

**Tech Stack:** Existing Python core + FastAPI web layer + vanilla JS frontend. No new deps.

## Global Constraints

- Python 3.11+. No new dependencies.
- One-time uses the SwitchBot repeat byte **bit 7** (`0x80`) = execute once; day bits (Mon=0..Sun=6) unchanged.
- Parser output is EITHER `{"schedules":[{"device","events":[{"time","action","days","once"}]}]}` OR `{"clarification":"<text>"}` — never both, never a silent "every day" guess for an implied specific/one-time day.
- One-time supported only within the next 7 days (relative weekday). Specific dates further out → clarification.
- Read-back wording: recurring `— every day` / etc.; one-time `— once (<describe_days>)`.
- Do NOT change the CLI path (`cli.py`, `apply_schedule`, `build_schedule`) behavior; the web path uses the new conversational functions.
- `completion_fn(system: str, user: str) -> str` seam stays; tests inject canned JSON. Server computes `now = datetime.now()` and passes it to the parser.
- Errors returned as `{"ok":false,"error":...}`; clarifications as `{"ok":true,"kind":"clarification","message":...}`; schedules as `{"ok":true,"kind":"schedule","readback","schedule"}`.

---

## File Structure
```
src/switchbot_scheduler/model.py        # Event gains once: bool = False
src/switchbot_scheduler/encoder.py      # once -> repeat bit 7
src/switchbot_scheduler/readback.py     # one-time wording
src/switchbot_scheduler/parser.py       # ParseResult + parse_conversation(messages, registry, now, completion_fn)
src/switchbot_scheduler/core.py         # preview_conversation(messages, registry, now, completion_fn)
src/switchbot_scheduler/web/app.py      # /preview takes {messages}, returns kind; schedule_from_json reads once
src/switchbot_scheduler/web/static/index.html  # thread memory, clarification bubbles, once display, reset-on-approve
tests/test_model.py, test_encoder.py, test_readback.py, test_parser_conversation.py, test_core.py, test_web.py
```

---

## Task 1: Model + encoder + read-back for one-time

**Files:**
- Modify: `src/switchbot_scheduler/model.py`, `src/switchbot_scheduler/encoder.py`, `src/switchbot_scheduler/readback.py`
- Test: `tests/test_encoder.py`, `tests/test_readback.py`, `tests/test_model.py`

**Interfaces:**
- Consumes: `Event`, `DAY_BIT`, `ACTION_CODE`, `describe_days`.
- Produces: `Event(..., once: bool = False)`; `encode_alarm` sets repeat bit 7 when `once`; `readback` renders `— once (<days>)`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_encoder.py`:
```python
def test_encode_once_sets_bit7():
    a = encode_alarm(Event("09:00", "on", ["mon"], once=True))
    assert a["repeat_byte"] & 0x80 == 0x80
    assert a["repeat_byte"] & 0x7f == 0b0000001  # mon still bit0


def test_encode_recurring_leaves_bit7_clear():
    a = encode_alarm(Event("09:00", "on", ["mon"], once=False))
    assert a["repeat_byte"] & 0x80 == 0
```
Append to `tests/test_readback.py`:
```python
from switchbot_scheduler.model import Event, DeviceSchedule, Schedule
from switchbot_scheduler.readback import readback

def test_readback_marks_one_time():
    sched = Schedule(schedules=[DeviceSchedule(device="living_room",
        events=[Event("09:00", "on", ["mon"], once=True)])])
    assert "living_room: on 09:00 — once (mon)" in readback(sched)
```
Append to `tests/test_model.py`:
```python
def test_event_once_defaults_false():
    assert Event("06:00", "on", ["mon"]).once is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_encoder.py tests/test_readback.py tests/test_model.py -v`
Expected: FAIL — `Event() got an unexpected keyword argument 'once'`.

- [ ] **Step 3: Implement**

In `model.py`, add the field to `Event` (keep existing fields/order):
```python
@dataclass
class Event:
    time: str
    action: Action
    days: list[str]
    once: bool = False
```
In `encoder.py`, set bit 7 in `encode_alarm`:
```python
def encode_alarm(event: Event, inverted: bool = False) -> dict:
    day_mask = 0
    for d in event.days:
        day_mask |= (1 << DAY_BIT[d])
    if event.once:
        day_mask |= 0x80   # bit 7 = execute once
    hour, minute = (int(x) for x in event.time.split(":"))
    action = event.action
    if inverted and action in ("on", "off"):
        action = "off" if action == "on" else "on"
    return {"repeat_byte": day_mask, "hour": hour, "minute": minute, "action": ACTION_CODE[action]}
```
In `readback.py`, change the per-event line to mark one-time:
```python
def readback(schedule: Schedule) -> str:
    lines = []
    for ds in schedule.schedules:
        for e in ds.events:
            when = f"once ({describe_days(e.days)})" if e.once else describe_days(e.days)
            lines.append(f"{ds.device}: {e.action} {e.time} — {when}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_encoder.py tests/test_readback.py tests/test_model.py -v`
Expected: all PASS. Then full suite `PYTHONPATH=src pytest -q` — all pass (existing recurring read-back tests still show `— every day`, unaffected).

- [ ] **Step 5: Commit**
```bash
git add src/switchbot_scheduler/model.py src/switchbot_scheduler/encoder.py src/switchbot_scheduler/readback.py tests/
git commit -m "feat: one-time alarms — Event.once, encoder bit 7, read-back wording"
```

---

## Task 2: Parser — conversation in, schedule-or-clarification out, date-aware

**Files:**
- Modify: `src/switchbot_scheduler/parser.py`
- Test: `tests/test_parser_conversation.py` (create)

**Interfaces:**
- Consumes: `Registry`, `Schedule/DeviceSchedule/Event`.
- Produces: `@dataclass ParseResult(schedule: Schedule | None, clarification: str | None)`;
  `parse_conversation(messages: list[str], registry, now: datetime, completion_fn=<default>) -> ParseResult`.
  `completion_fn(system: str, user: str) -> str` unchanged.

- [ ] **Step 1: Write failing tests**

`tests/test_parser_conversation.py`:
```python
import json
from datetime import datetime
from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.parser import parse_conversation, ParseResult

NOW = datetime(2026, 7, 5, 12, 0)  # a Sunday


def _reg():
    return Registry([Device(name="living_room", aliases=["סלון"], ble_id="U1")])


def test_conversation_passes_all_turns_to_model():
    seen = {}
    def cap(system, user):
        seen["user"] = user
        return json.dumps({"schedules": [{"device": "living_room",
            "events": [{"time": "09:00", "action": "on", "days": ["mon"], "once": True}]}]})
    parse_conversation(["turn on living room tomorrow 9am", "make it one-time"], _reg(), NOW, completion_fn=cap)
    assert "turn on living room tomorrow 9am" in seen["user"]
    assert "make it one-time" in seen["user"]


def test_conversation_builds_schedule_with_once():
    canned = lambda s, u: json.dumps({"schedules": [{"device": "living_room",
        "events": [{"time": "09:00", "action": "on", "days": ["mon"], "once": True}]}]})
    res = parse_conversation(["living room on tomorrow 9am, once"], _reg(), NOW, completion_fn=canned)
    assert res.clarification is None
    ev = res.schedule.schedules[0].events[0]
    assert ev.once is True and ev.time == "09:00"


def test_clarification_path():
    canned = lambda s, u: json.dumps({"clarification": "Which device did you mean?"})
    res = parse_conversation(["do the thing"], _reg(), NOW, completion_fn=canned)
    assert res.schedule is None
    assert res.clarification == "Which device did you mean?"


def test_system_prompt_includes_today():
    seen = {}
    def cap(system, user):
        seen["system"] = system
        return json.dumps({"clarification": "?"})
    parse_conversation(["hi"], _reg(), NOW, completion_fn=cap)
    assert "2026-07-05" in seen["system"] and "Sunday" in seen["system"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_parser_conversation.py -v`
Expected: FAIL — `cannot import name 'parse_conversation'`.

- [ ] **Step 3: Implement in `parser.py`**

Add (keep existing `MODEL`, `_default_completion`, `parse_schedule`, `build_system_prompt` for the CLI path):
```python
from dataclasses import dataclass
from datetime import datetime
from .validator import MAX_ALARMS


@dataclass
class ParseResult:
    schedule: Schedule | None
    clarification: str | None


def build_conversation_system_prompt(registry: Registry, now: datetime) -> str:
    device_lines = []
    for d in registry.devices:
        if d.aliases:
            device_lines.append(f"{d.name} (aliases: {', '.join(d.aliases)})")
        else:
            device_lines.append(d.name)
    names = "\n".join(device_lines)
    today = now.strftime("%A, %Y-%m-%d")
    return f"""You convert a conversation about device schedules (Hebrew or English) into strict JSON.
Today is {today}.

Output EXACTLY ONE of:
  {{"schedules": [{{"device": <name>, "events": [
     {{"time": "HH:MM", "action": "on"|"off"|"press", "days": [<weekdays>], "once": <bool>}} ]}} ]}}
  {{"clarification": "<a short question or explanation>"}}

Known device names (map spoken names/aliases to exactly one of these):
{names}

Rules:
- weekdays are lowercase 3-letter codes: sun mon tue wed thu fri sat. Use 24-hour zero-padded time.
- Every turn-ON and every turn-OFF is its OWN event. Each device supports at most {MAX_ALARMS} events.
- Recurring: "every day"/no day -> all 7 days, once=false. Named weekdays repeating -> those days, once=false.
- One-time: "today"/"tomorrow"/"this <weekday>" -> set days to that single weekday and once=true.
  Resolve relative days using today's date above.
- NEVER output "every day" when the user implied a specific or one-time day.
- If the request is ambiguous, unparseable, or asks for a specific calendar date more than 7 days away
  (which the device cannot do), return a "clarification" explaining/asking — do NOT guess a schedule.
- The conversation is a list of user turns, newest reflecting corrections. Output the CURRENT complete
  intended schedule reflecting the WHOLE conversation.
"""


def parse_conversation(messages, registry, now, completion_fn=_default_completion) -> ParseResult:
    system = build_conversation_system_prompt(registry, now)
    convo = "\n".join(f"[{i+1}] {m}" for i, m in enumerate(messages))
    raw = completion_fn(system, convo)
    data = json.loads(raw)
    if "clarification" in data and "schedules" not in data:
        return ParseResult(schedule=None, clarification=str(data["clarification"]))
    schedules = []
    for s in data["schedules"]:
        raw_device = s["device"]
        canonical = registry.resolve(raw_device)
        device = canonical if canonical is not None else raw_device
        events = [Event(time=e["time"], action=e["action"], days=e["days"], once=bool(e.get("once", False)))
                  for e in s["events"]]
        schedules.append(DeviceSchedule(device=device, events=events))
    return ParseResult(schedule=Schedule(schedules=schedules), clarification=None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_parser_conversation.py -v`
Expected: 4 PASS. Full suite still green.

- [ ] **Step 5: Commit**
```bash
git add src/switchbot_scheduler/parser.py tests/test_parser_conversation.py
git commit -m "feat: parse_conversation — thread + date aware, schedule-or-clarification"
```

---

## Task 3: Core + web endpoint (conversation preview)

**Files:**
- Modify: `src/switchbot_scheduler/core.py`, `src/switchbot_scheduler/web/app.py`
- Test: `tests/test_core.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `parse_conversation`/`ParseResult` (Task 2), `validate`, `_apply_press_mode`, `readback`.
- Produces: `preview_conversation(messages, registry, now, completion_fn=None) -> tuple[str, str, Schedule | None]`
  returning `("schedule", readback_text, schedule)` or `("clarification", message, None)`.
  `/preview` now accepts `{"messages": [...]}`; `schedule_from_json` reads `once`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_core.py`:
```python
import json as _json
from datetime import datetime as _dt
from switchbot_scheduler.core import preview_conversation

def test_preview_conversation_schedule(monkeypatch):
    canned = lambda s, u: _json.dumps({"schedules": [{"device": "living_room",
        "events": [{"time": "09:00", "action": "on", "days": ["mon"], "once": True}]}]})
    kind, text, sched = preview_conversation(["living room on tomorrow, once"], _reg(),
                                             _dt(2026, 7, 5, 12, 0), completion_fn=canned)
    assert kind == "schedule" and "once (mon)" in text and sched is not None

def test_preview_conversation_clarification():
    canned = lambda s, u: _json.dumps({"clarification": "Which device?"})
    kind, text, sched = preview_conversation(["do it"], _reg(), _dt(2026, 7, 5, 12, 0), completion_fn=canned)
    assert kind == "clarification" and text == "Which device?" and sched is None
```
(`_reg()` already exists in test_core.py: a Registry with `living_room`.)

Append to `tests/test_web.py`:
```python
def test_preview_thread_returns_schedule_kind(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)  # canned parser returns living_room on 06:00
    body = client.post("/preview", json={"messages": ["salon on 6am"]}).json()
    assert body["ok"] is True and body["kind"] == "schedule"
    assert "living_room: on 06:00" in body["readback"]

def test_preview_thread_returns_clarification_kind(tmp_path, monkeypatch):
    import json
    clar = lambda s, u: json.dumps({"clarification": "Which room?"})
    client = _client(tmp_path, monkeypatch, clar)
    body = client.post("/preview", json={"messages": ["do the thing"]}).json()
    assert body["ok"] is True and body["kind"] == "clarification" and body["message"] == "Which room?"

def test_apply_reads_once(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(webapp, "write_schedule", lambda s, r: calls.append(s))
    sched = {"schedules": [{"device": "living_room",
        "events": [{"time": "09:00", "action": "on", "days": ["mon"], "once": True}]}]}
    body = client.post("/apply", json={"schedule": sched}).json()
    assert body["ok"] is True and calls[0].schedules[0].events[0].once is True
```
Note: the existing `_client` helper's canned parser returns the old `{"schedules":[{"device":"living_room","events":[{"time":"06:00","action":"on","days":["mon"]}]}]}` (no `once`) — that's fine; `schedule_from_json`/parser default `once` to False.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_core.py tests/test_web.py -v`
Expected: FAIL — `cannot import name 'preview_conversation'` / `/preview` KeyError on `messages`.

- [ ] **Step 3: Implement**

In `core.py` add:
```python
from .parser import parse_conversation

def preview_conversation(messages, registry, now, completion_fn=None):
    kwargs = {"completion_fn": completion_fn} if completion_fn is not None else {}
    result = parse_conversation(messages, registry, now, **kwargs)
    if result.clarification is not None:
        return ("clarification", result.clarification, None)
    schedule = result.schedule
    validate(schedule, registry)
    _apply_press_mode(schedule, registry)
    return ("schedule", readback(schedule), schedule)
```
In `web/app.py`:
- Add `from datetime import datetime` and `from ..core import preview_conversation`.
- Change `schedule_from_json`'s Event build to include once:
```python
Event(time=e["time"], action=e["action"], days=e["days"], once=bool(e.get("once", False)))
```
- Replace the `PreviewReq`/`preview` handler:
```python
class PreviewReq(BaseModel):
    messages: list[str]

@app.post("/preview")
def preview(req: PreviewReq):
    try:
        registry = _registry()
        kind, payload, schedule = preview_conversation(
            req.messages, registry, datetime.now(), completion_fn=_completion_fn)
        if kind == "clarification":
            return {"ok": True, "kind": "clarification", "message": payload}
        return {"ok": True, "kind": "schedule", "readback": payload, "schedule": schedule_to_json(schedule)}
    except Exception as err:
        return {"ok": False, "error": str(err)}
```
(Leave `/apply`, `main`, `schedule_to_json` otherwise unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_core.py tests/test_web.py -v`
Expected: PASS. Full suite green. (The old single-prompt `/preview` test that posted `{"prompt": ...}` is replaced by the new `messages` tests — remove/replace it if present so the suite stays green.)

- [ ] **Step 5: Commit**
```bash
git add src/switchbot_scheduler/core.py src/switchbot_scheduler/web/app.py tests/
git commit -m "feat: preview_conversation + /preview thread endpoint (schedule|clarification)"
```

---

## Task 4: Frontend — thread memory, clarification bubbles, one-time display

Visual/manual task. Replace the chat page's script so it keeps a message thread, posts the whole thread, renders schedule cards vs clarification bubbles, and resets on approve.

**Files:**
- Modify: `src/switchbot_scheduler/web/static/index.html`

**Interfaces:**
- Consumes: `POST /preview {messages:[...]}` → `{ok,kind:"schedule",readback,schedule}` | `{ok,kind:"clarification",message}` | `{ok:false,error}`; `POST /apply {schedule}` → `{ok,written}` | `{ok:false,error}`.

- [ ] **Step 1: Update the page's markup + script**

Keep the existing `<head>`/CSS and the `<header>`, `#log`, and `<form>` with the auto-growing `<textarea id="p">`. Add a **New** button to the header:
```html
<header>SwitchBot Scheduler
  <small>Type a schedule in Hebrew or English — you'll approve before anything is written.</small>
  <button id="newbtn" type="button" style="float:right;font-size:13px;padding:6px 12px">New</button>
</header>
```
Replace the entire `<script>` body with:
```html
<script>
const log = document.getElementById('log'), form = document.getElementById('f'),
      input = document.getElementById('p'), newbtn = document.getElementById('newbtn');
let thread = [];   // user turns in the current conversation
let lastSchedule = null;

function toBottom(){ log.scrollTop = log.scrollHeight; }
function add(el){ log.appendChild(el); toBottom(); return el; }
function grow(){ input.style.height='auto'; input.style.height=Math.min(input.scrollHeight,180)+'px'; }
input.addEventListener('input', grow);
input.addEventListener('keydown', (e)=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); form.requestSubmit(); }});

function bubble(text, side, cls){
  const row=document.createElement('div'); row.className='row '+side;
  const b=document.createElement('div'); b.className='bubble'; b.dir='auto';
  if(cls) b.classList.add('status',cls); b.textContent=text; row.appendChild(b); return add(row);
}

function scheduleCard(readback, schedule){
  lastSchedule = schedule;
  const row=document.createElement('div'); row.className='row bot';
  const card=document.createElement('div'); card.className='card';
  const h=document.createElement('h4'); h.textContent="Here is what I understood"; card.appendChild(h);
  readback.split('\n').forEach((line)=>{ const d=document.createElement('div'); d.className='line'; d.dir='auto'; d.textContent=line; card.appendChild(d); });
  const actions=document.createElement('div'); actions.className='actions';
  const approve=document.createElement('button'); approve.className='approve'; approve.textContent='Approve & write';
  const hint=document.createElement('span'); hint.style.color='var(--muted)'; hint.textContent='…or just type a correction';
  actions.appendChild(approve); actions.appendChild(hint); card.appendChild(actions);
  row.appendChild(card); add(row);
  approve.onclick=async()=>{
    approve.disabled=true; approve.textContent='Writing…';
    try{
      const r=await fetch('/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({schedule})});
      const data=await r.json();
      if(data.ok){ bubble('✅ Written to '+data.written.join(', '),'bot','ok'); thread=[]; lastSchedule=null; }
      else { bubble('⚠️ '+data.error,'bot','err'); approve.disabled=false; approve.textContent='Approve & write'; }
    }catch(err){ bubble('⚠️ '+err,'bot','err'); approve.disabled=false; approve.textContent='Approve & write'; }
  };
}

newbtn.onclick=()=>{ thread=[]; lastSchedule=null; log.innerHTML=''; input.focus(); };

form.onsubmit=async(e)=>{
  e.preventDefault();
  const text=input.value.trim(); if(!text) return;
  input.value=''; grow(); bubble(text,'user'); thread.push(text);
  const thinking=bubble('…','bot');
  try{
    const r=await fetch('/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:thread})});
    const data=await r.json(); thinking.remove();
    if(!data.ok){ bubble('⚠️ '+data.error,'bot','err'); }
    else if(data.kind==='clarification'){ bubble(data.message,'bot'); }
    else { scheduleCard(data.readback, data.schedule); }
  }catch(err){ thinking.remove(); bubble('⚠️ '+err,'bot','err'); }
};
</script>
```

- [ ] **Step 2: Verify by eye**

Run `OPENAI_API_KEY=... switchbot-ui` (or `PYTHONPATH=src python -c "from switchbot_scheduler.web.app import main; main()"`), open http://localhost:8000, hard-refresh. Confirm: a normal prompt shows a card (one-time prompts show `— once (…)`); a correction like "make it one-time" updates the card (thread memory); gibberish shows a plain clarification bubble (no dead card); Approve writes then the thread resets; New clears the log.

- [ ] **Step 3: Commit**
```bash
git add src/switchbot_scheduler/web/static/index.html
git commit -m "feat: conversational chat UI — thread memory, clarifications, one-time display, reset-on-approve"
```

---

## Self-Review

**Spec coverage:** conversation memory = Task 2 (`parse_conversation` over `messages`) + Task 3 (`/preview {messages}`) + Task 4 (thread array) ✅; one-time within 7 days = Task 1 (`once`/bit7/readback) + Task 2 (date-aware prompt + `once` extraction) ✅; never silently default = Task 2 clarification contract + prompt rules ✅; UI kinds (card vs clarification bubble) + reset-on-approve + New = Task 4 ✅; date awareness = Task 2 (`build_conversation_system_prompt` injects today) + Task 3 (`datetime.now()`) ✅; read-back wording = Task 1 ✅; `/apply` reads `once` = Task 3 ✅; CLI path untouched ✅ (parse_schedule/build_schedule/apply_schedule left in place). Error table covered (validation → endpoint except; clarification → bubble). Testing section mapped to Tasks 1-4 tests.

**Placeholder scan:** No TBDs; complete code in every code step. Task 4 is intentionally manual (UI).

**Type consistency:** `ParseResult(schedule, clarification)` produced in Task 2, consumed in Task 3. `preview_conversation(messages, registry, now, completion_fn) -> (kind, payload, schedule)` consistent between Task 3 core + endpoint + Task 4's expected JSON. `Event(..., once=False)` field consistent across model/encoder/readback/parser/schedule_from_json. `completion_fn(system, user)` seam unchanged. `/preview` input `{messages:[...]}` matches frontend post and web test.
