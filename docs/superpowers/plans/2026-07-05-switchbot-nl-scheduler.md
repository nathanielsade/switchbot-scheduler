# SwitchBot Natural-Language Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tool that turns a plain-language prompt (Hebrew/English) into on-device Bluetooth alarms on SwitchBot Bots, so schedules run on the Bots themselves with nothing left powered on.

**Architecture:** A fuzzy stage (one OpenAI call: prompt → structured `Schedule` JSON) feeds a chain of deterministic stages (validate → read-back → encode → write-over-Bluetooth). A single `apply_schedule()` core function ties them together; the CLI is a thin front door over it. A YAML device registry is the single source of truth for which Bots exist, keeping the tool generic as Bots are added.

**Tech Stack:** Python 3.11+, `openai` (parser), `bleak` (Bluetooth on macOS), `PyYAML` (registry), `pytest` (tests).

## Global Constraints

- **Python 3.11+** — uses `list[str]` / `X | None` syntax and `dataclasses`.
- **Secrets from environment only** — read `OPENAI_API_KEY` via the `openai` client's default env lookup; never hardcode. Any secrets file is gitignored.
- **Platform: macOS** — `bleak` addresses devices by a **CoreBluetooth UUID string**, not a `XX:XX` MAC. The registry's `ble_id` holds that UUID.
- **Firmware limit: max 5 alarms per Bot.** Enforced in the Validator, never by the LLM.
- **Action codes:** `press=0, on=1, off=2`.
- **Day bit order:** `sun=bit0, mon=bit1, tue=bit2, wed=bit3, thu=bit4, fri=bit5, sat=bit6`; recurring-weekly uses repeat-byte bit 7 = 0.
- **Package import root:** `src/switchbot_scheduler/`. Run tests with `PYTHONPATH=src pytest` (configured in `pyproject.toml`).
- **The LLM does only prompt → JSON.** All validation, encoding, and device I/O is deterministic, testable code.

---

## File Structure

```
smart-home/
  pyproject.toml                       # deps + pytest config
  .gitignore
  devices.yaml                         # the registry (user edits to add Bots)
  src/switchbot_scheduler/
    __init__.py
    model.py                           # Event, DeviceSchedule, Schedule
    registry.py                        # Device, Registry (load YAML, alias resolution)
    validator.py                       # validate(schedule, registry); ScheduleError
    readback.py                        # readback(schedule) -> human string
    encoder.py                         # encode_alarm(event) -> alarm dict
    parser.py                          # parse_schedule(prompt, registry) via OpenAI
    ble_writer.py                      # write_alarms(ble_id, alarms) via bleak
    core.py                            # apply_schedule(...) ties stages together
    cli.py                             # argparse entry point
  spikes/
    ble_spike.py                       # Task 2 manual de-risk (not shipped logic)
  tests/
    test_model.py
    test_registry.py
    test_validator.py
    test_readback.py
    test_encoder.py
    test_parser.py
    test_core.py
    fixtures/parser_living_room.json
```

---

## Task 1: Project scaffold + toolchain

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/switchbot_scheduler/__init__.py`, `tests/test_sanity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: an installable package layout and a working `pytest` run.

- [ ] **Step 1: Create `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
.env
*.egg-info/
.pytest_cache/
secrets.yaml
```

- [ ] **Step 2: Create `pyproject.toml`**

```toml
[project]
name = "switchbot-scheduler"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["openai>=1.0", "bleak>=0.21", "PyYAML>=6.0"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

- [ ] **Step 3: Create the package marker**

`src/switchbot_scheduler/__init__.py`:
```python
"""SwitchBot natural-language scheduler."""
```

- [ ] **Step 4: Write a sanity test**

`tests/test_sanity.py`:
```python
import switchbot_scheduler


def test_package_imports():
    assert switchbot_scheduler is not None
```

- [ ] **Step 5: Set up the environment and run the sanity test**

Run:
```bash
cd /Users/netanelsade/smart-home
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/test_sanity.py -v
```
Expected: `test_package_imports PASSED`.

- [ ] **Step 6: Initialize git and commit**

```bash
git init
git add .gitignore pyproject.toml src tests
git commit -m "chore: scaffold switchbot-scheduler package"
```

---

## Task 2: Bluetooth spike (manual de-risk) — GATE

Do this task while physically home, near the Bots. It is exploratory, not TDD; its
deliverable is **confirmed facts** written into a notes file, which later tasks rely on.

**Files:**
- Create: `spikes/ble_spike.py`, `spikes/FINDINGS.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the CoreBluetooth UUIDs of the Bots (for `devices.yaml`), and confirmation
  that a single alarm written over BLE actually fires — informs Task 7 (encoder byte
  format) and Task 10 (writer command framing).

- [ ] **Step 1: Write a scan script**

`spikes/ble_spike.py`:
```python
"""Manual Bluetooth spike. Run near the Bots. NOT shipped logic."""
import asyncio
from bleak import BleakScanner

# SwitchBot BLE GATT characteristics (from OpenWonderLabs/SwitchBotAPI-BLE)
WRITE_CHAR = "cba20002-224d-11e6-9fb9-0002a5d5c51b"
NOTIFY_CHAR = "cba20003-224d-11e6-9fb9-0002a5d5c51b"


async def scan():
    devices = await BleakScanner.discover(timeout=10.0)
    for d in devices:
        print(f"{d.address}  name={d.name!r}  rssi={getattr(d, 'rssi', '?')}")


if __name__ == "__main__":
    asyncio.run(scan())
```

- [ ] **Step 2: Run the scan and identify the Bots**

Run: `python spikes/ble_spike.py`
Expected: a list of BLE devices. SwitchBot Bots usually advertise as `WoHand`/`Bot`
or with no name. Note each Bot's `address` (a UUID on macOS). Record which UUID is
which physical Bot (toggle-test from the app if unsure).

- [ ] **Step 3: Confirm a press works (baseline)**

Add to `ble_spike.py` and run against one Bot's UUID:
```python
async def press(ble_id: str):
    from bleak import BleakClient
    async with BleakClient(ble_id) as client:
        await client.write_gatt_char(WRITE_CHAR, b"\x57\x01\x00", response=False)
        print("press sent")
```
Expected: the Bot arm physically moves. This proves `bleak` + the write characteristic
work on this Mac.

- [ ] **Step 4: Attempt a single test alarm write and observe**

Using the documented `0x09` "Set Device Time Management Info" command, write one
recurring alarm a couple of minutes in the future (e.g. `press`). Watch whether the
Bot fires at that time. Iterate on the exact byte frame until it fires. **Record the
exact working byte sequence.**

- [ ] **Step 5: Record findings**

`spikes/FINDINGS.md` — write down, in plain text:
- Each Bot's CoreBluetooth UUID ↔ physical location.
- The exact working alarm-write byte frame (header, command, per-alarm bytes).
- Whether `bleak` connected reliably; any pairing quirks.
- GATT characteristic UUIDs actually used.

- [ ] **Step 6: Commit**

```bash
git add spikes/
git commit -m "chore: BLE spike — confirm alarm write + record device UUIDs"
```

> **GATE:** If Step 4 cannot be made to fire after reasonable effort, STOP and
> revisit the design (the fallback is Option A / cloud dispatch). Do not build the
> full writer on an unproven protocol.

---

## Task 3: Data model

**Files:**
- Create: `src/switchbot_scheduler/model.py`, `tests/test_model.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `DAYS: list[str]`; `Event(time: str, action: str, days: list[str])`;
  `DeviceSchedule(device: str, events: list[Event])`;
  `Schedule(schedules: list[DeviceSchedule])`. Used by every later task.

- [ ] **Step 1: Write the failing test**

`tests/test_model.py`:
```python
from switchbot_scheduler.model import Event, DeviceSchedule, Schedule, DAYS


def test_days_are_seven_lowercase_codes():
    assert DAYS == ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def test_schedule_nests_device_schedules_and_events():
    e = Event(time="06:00", action="on", days=["sun", "mon"])
    ds = DeviceSchedule(device="living_room", events=[e])
    sched = Schedule(schedules=[ds])
    assert sched.schedules[0].events[0].time == "06:00"
    assert sched.schedules[0].device == "living_room"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_model.py -v`
Expected: FAIL — `ModuleNotFoundError: switchbot_scheduler.model`.

- [ ] **Step 3: Write minimal implementation**

`src/switchbot_scheduler/model.py`:
```python
from dataclasses import dataclass
from typing import Literal

DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
Action = Literal["on", "off", "press"]


@dataclass
class Event:
    time: str          # "HH:MM"
    action: Action     # "on" | "off" | "press"
    days: list[str]    # subset of DAYS


@dataclass
class DeviceSchedule:
    device: str            # canonical device name
    events: list[Event]


@dataclass
class Schedule:
    schedules: list[DeviceSchedule]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_model.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/switchbot_scheduler/model.py tests/test_model.py
git commit -m "feat: add schedule data model"
```

---

## Task 4: Device registry + alias resolution

**Files:**
- Create: `src/switchbot_scheduler/registry.py`, `devices.yaml`, `tests/test_registry.py`

**Interfaces:**
- Consumes: nothing (pure config loader).
- Produces: `Device(name: str, aliases: list[str], ble_id: str)`; `Registry` with
  `Registry.load(path) -> Registry`, `resolve(spoken: str) -> str | None`,
  `known_names() -> list[str]`, `ble_id(name: str) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/test_registry.py`:
```python
from switchbot_scheduler.registry import Registry, Device


def _reg():
    return Registry([
        Device(name="living_room", aliases=["living room", "סלון"], ble_id="UUID-1"),
        Device(name="ac", aliases=["air conditioner", "מזגן"], ble_id="UUID-2"),
    ])


def test_resolve_by_alias_case_insensitive():
    assert _reg().resolve("Living Room") == "living_room"
    assert _reg().resolve("סלון") == "living_room"


def test_resolve_by_canonical_name():
    assert _reg().resolve("ac") == "ac"


def test_resolve_unknown_returns_none():
    assert _reg().resolve("bedroom") is None


def test_known_names_and_ble_id():
    r = _reg()
    assert r.known_names() == ["living_room", "ac"]
    assert r.ble_id("ac") == "UUID-2"


def test_load_from_yaml(tmp_path):
    p = tmp_path / "devices.yaml"
    p.write_text(
        "devices:\n"
        "  living_room:\n"
        "    aliases: [\"salon\"]\n"
        "    ble_id: \"UUID-X\"\n"
    )
    r = Registry.load(str(p))
    assert r.resolve("salon") == "living_room"
    assert r.ble_id("living_room") == "UUID-X"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_registry.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

`src/switchbot_scheduler/registry.py`:
```python
from dataclasses import dataclass
import yaml


@dataclass
class Device:
    name: str
    aliases: list[str]
    ble_id: str


class Registry:
    def __init__(self, devices: list[Device]):
        self.devices = devices
        self._by_name = {d.name: d for d in devices}
        self._alias_map: dict[str, str] = {}
        for d in devices:
            self._alias_map[d.name.lower()] = d.name
            for a in d.aliases:
                self._alias_map[a.strip().lower()] = d.name

    @classmethod
    def load(cls, path: str) -> "Registry":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        devices = [
            Device(name=name, aliases=cfg.get("aliases", []), ble_id=cfg.get("ble_id", ""))
            for name, cfg in data["devices"].items()
        ]
        return cls(devices)

    def resolve(self, spoken: str) -> str | None:
        return self._alias_map.get(spoken.strip().lower())

    def known_names(self) -> list[str]:
        return [d.name for d in self.devices]

    def ble_id(self, name: str) -> str:
        return self._by_name[name].ble_id
```

- [ ] **Step 4: Create the starter `devices.yaml`**

`devices.yaml` (fill `ble_id` from Task 2's FINDINGS.md):
```yaml
devices:
  living_room:
    aliases: ["living room", "סלון", "salon"]
    ble_id: ""   # CoreBluetooth UUID from spikes/FINDINGS.md
  dining:
    aliases: ["dining", "פינת אוכל", "dining nook"]
    ble_id: ""
  ac:
    aliases: ["ac", "air conditioner", "מזגן"]
    ble_id: ""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_registry.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/switchbot_scheduler/registry.py devices.yaml tests/test_registry.py
git commit -m "feat: add device registry with alias resolution"
```

---

## Task 5: Validator

**Files:**
- Create: `src/switchbot_scheduler/validator.py`, `tests/test_validator.py`

**Interfaces:**
- Consumes: `Schedule` (Task 3), `Registry` (Task 4).
- Produces: `MAX_ALARMS = 5`; `ScheduleError(ValueError)`;
  `validate(schedule: Schedule, registry: Registry) -> None` (raises `ScheduleError`).

- [ ] **Step 1: Write the failing test**

`tests/test_validator.py`:
```python
import pytest
from switchbot_scheduler.model import Event, DeviceSchedule, Schedule
from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.validator import validate, ScheduleError


def _reg():
    return Registry([Device(name="living_room", aliases=[], ble_id="U1")])


def _sched(events, device="living_room"):
    return Schedule(schedules=[DeviceSchedule(device=device, events=events)])


def test_valid_schedule_passes():
    validate(_sched([Event("06:00", "on", ["sun"])]), _reg())  # no raise


def test_unknown_device_raises():
    with pytest.raises(ScheduleError, match="Unknown device"):
        validate(_sched([Event("06:00", "on", ["sun"])], device="bedroom"), _reg())


def test_more_than_five_alarms_raises():
    events = [Event(f"0{h}:00", "on", ["sun"]) for h in range(6)]  # 6 alarms
    with pytest.raises(ScheduleError, match="max is 5"):
        validate(_sched(events), _reg())


def test_bad_time_raises():
    with pytest.raises(ScheduleError, match="time"):
        validate(_sched([Event("25:00", "on", ["sun"])]), _reg())


def test_bad_day_raises():
    with pytest.raises(ScheduleError, match="day"):
        validate(_sched([Event("06:00", "on", ["funday"])]), _reg())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_validator.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

`src/switchbot_scheduler/validator.py`:
```python
from .model import Schedule, DAYS
from .registry import Registry

MAX_ALARMS = 5


class ScheduleError(ValueError):
    pass


def validate(schedule: Schedule, registry: Registry) -> None:
    known = registry.known_names()
    for ds in schedule.schedules:
        if ds.device not in known:
            raise ScheduleError(
                f"Unknown device '{ds.device}'. Known devices: {known}"
            )
        if len(ds.events) > MAX_ALARMS:
            raise ScheduleError(
                f"{ds.device} needs {len(ds.events)} alarms, but max is {MAX_ALARMS}. "
                f"Simplify the schedule."
            )
        for e in ds.events:
            _check_time(e.time, ds.device)
            if e.action not in ("on", "off", "press"):
                raise ScheduleError(f"Bad action '{e.action}' for {ds.device}")
            for d in e.days:
                if d not in DAYS:
                    raise ScheduleError(f"Bad day '{d}' for {ds.device}")


def _check_time(t: str, device: str) -> None:
    try:
        hh, mm = (int(x) for x in t.split(":"))
    except (ValueError, AttributeError):
        raise ScheduleError(f"Bad time '{t}' for {device}")
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ScheduleError(f"Bad time '{t}' for {device}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_validator.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/switchbot_scheduler/validator.py tests/test_validator.py
git commit -m "feat: add schedule validator (per-Bot 5-alarm limit, times, days, known devices)"
```

---

## Task 6: Read-back formatter

**Files:**
- Create: `src/switchbot_scheduler/readback.py`, `tests/test_readback.py`

**Interfaces:**
- Consumes: `Schedule` (Task 3), `DAYS` (Task 3).
- Produces: `readback(schedule: Schedule) -> str`; `describe_days(days: list[str]) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/test_readback.py`:
```python
from switchbot_scheduler.model import Event, DeviceSchedule, Schedule
from switchbot_scheduler.readback import readback, describe_days


def test_describe_days_every_day():
    assert describe_days(["sun", "mon", "tue", "wed", "thu", "fri", "sat"]) == "every day"


def test_describe_days_partial_in_week_order():
    assert describe_days(["mon", "sun", "wed"]) == "sun, mon, wed"


def test_readback_lists_each_event():
    sched = Schedule(schedules=[
        DeviceSchedule(device="living_room", events=[
            Event("06:00", "on", ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]),
            Event("17:00", "off", ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]),
        ]),
    ])
    text = readback(sched)
    assert "living_room: on 06:00 — every day" in text
    assert "living_room: off 17:00 — every day" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_readback.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

`src/switchbot_scheduler/readback.py`:
```python
from .model import Schedule, DAYS

_ORDER = {d: i for i, d in enumerate(DAYS)}


def describe_days(days: list[str]) -> str:
    s = set(days)
    if s == set(DAYS):
        return "every day"
    if s == {"mon", "tue", "wed", "thu", "fri"}:
        return "weekdays"
    if s == {"sat", "sun"}:
        return "weekends"
    return ", ".join(sorted(s, key=lambda d: _ORDER[d]))


def readback(schedule: Schedule) -> str:
    lines = []
    for ds in schedule.schedules:
        for e in ds.events:
            lines.append(f"{ds.device}: {e.action} {e.time} — {describe_days(e.days)}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_readback.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/switchbot_scheduler/readback.py tests/test_readback.py
git commit -m "feat: add deterministic read-back formatter"
```

---

## Task 7: Encoder

Uses the byte facts confirmed in Task 2. Encodes the recurring-weekly case
(repeat-byte bit 7 = 0). One-off alarms are out of scope for v1.

**Files:**
- Create: `src/switchbot_scheduler/encoder.py`, `tests/test_encoder.py`

**Interfaces:**
- Consumes: `Event` (Task 3).
- Produces: `DAY_BIT: dict[str,int]`; `ACTION_CODE: dict[str,int]`;
  `encode_alarm(event: Event) -> dict` returning
  `{"repeat_byte": int, "hour": int, "minute": int, "action": int}`.

- [ ] **Step 1: Write the failing test**

`tests/test_encoder.py`:
```python
from switchbot_scheduler.model import Event
from switchbot_scheduler.encoder import encode_alarm


def test_encode_action_and_time():
    a = encode_alarm(Event("06:30", "on", ["sun"]))
    assert a["hour"] == 6 and a["minute"] == 30
    assert a["action"] == 1  # on=1


def test_encode_day_mask_sunday_is_bit0():
    a = encode_alarm(Event("06:00", "off", ["sun"]))
    assert a["repeat_byte"] == 0b0000001
    assert a["action"] == 2  # off=2


def test_encode_all_days_mask():
    a = encode_alarm(Event("06:00", "press", ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]))
    assert a["repeat_byte"] == 0b1111111
    assert a["action"] == 0  # press=0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_encoder.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

`src/switchbot_scheduler/encoder.py`:
```python
from .model import Event

# sun=bit0 .. sat=bit6 (repeat-byte bit 7 stays 0 => recurring weekly)
DAY_BIT = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
ACTION_CODE = {"press": 0, "on": 1, "off": 2}


def encode_alarm(event: Event) -> dict:
    day_mask = 0
    for d in event.days:
        day_mask |= (1 << DAY_BIT[d])
    hour, minute = (int(x) for x in event.time.split(":"))
    return {
        "repeat_byte": day_mask,   # bit 7 = 0 => recurring weekly
        "hour": hour,
        "minute": minute,
        "action": ACTION_CODE[event.action],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_encoder.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/switchbot_scheduler/encoder.py tests/test_encoder.py
git commit -m "feat: add alarm encoder (event -> Bot alarm bytes)"
```

---

## Task 8: Parser (OpenAI)

**Files:**
- Create: `src/switchbot_scheduler/parser.py`, `tests/test_parser.py`, `tests/fixtures/parser_living_room.json`

**Interfaces:**
- Consumes: `Registry` (Task 4); `Schedule/DeviceSchedule/Event` (Task 3).
- Produces: `MODEL: str`; `build_system_prompt(registry: Registry) -> str`;
  `parse_schedule(prompt: str, registry: Registry, completion_fn=<default>) -> Schedule`.
  `completion_fn(system: str, user: str) -> str` returns the model's raw JSON string;
  injectable so tests never call the network.

- [ ] **Step 1: Create the fixture (canned model output)**

`tests/fixtures/parser_living_room.json`:
```json
{"schedules": [{"device": "living_room", "events": [
  {"time": "06:00", "action": "on", "days": ["sun","mon","tue","wed","thu","fri","sat"]},
  {"time": "17:00", "action": "off", "days": ["sun","mon","tue","wed","thu","fri","sat"]}
]}]}
```

- [ ] **Step 2: Write the failing test**

`tests/test_parser.py`:
```python
from pathlib import Path
from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.parser import parse_schedule, build_system_prompt

FIXTURE = Path(__file__).parent / "fixtures" / "parser_living_room.json"


def _reg():
    return Registry([Device(name="living_room", aliases=["salon"], ble_id="U1")])


def test_system_prompt_lists_known_devices():
    prompt = build_system_prompt(_reg())
    assert "living_room" in prompt


def test_parse_schedule_builds_objects_from_json():
    canned = FIXTURE.read_text()
    sched = parse_schedule(
        "turn the living room on 6 to 5 every day",
        _reg(),
        completion_fn=lambda system, user: canned,
    )
    assert sched.schedules[0].device == "living_room"
    assert [e.action for e in sched.schedules[0].events] == ["on", "off"]
    assert sched.schedules[0].events[0].time == "06:00"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_parser.py -v`
Expected: FAIL — module not found.

- [ ] **Step 4: Write minimal implementation**

`src/switchbot_scheduler/parser.py`:
```python
import json
from .model import Schedule, DeviceSchedule, Event
from .registry import Registry
from .validator import MAX_ALARMS

MODEL = "gpt-4o-mini"  # small, cheap, supports JSON output; change here to swap models


def build_system_prompt(registry: Registry) -> str:
    names = ", ".join(registry.known_names())
    return f"""You convert natural-language lighting/device schedules (Hebrew or English)
into strict JSON. Output ONLY JSON, no prose.

Schema:
{{"schedules": [{{"device": <name>, "events": [
  {{"time": "HH:MM", "action": "on"|"off"|"press", "days": [<weekdays>]}} ]}} ]}}

Known device names (map spoken names/aliases to exactly one of these):
{names}

Rules:
- weekdays are lowercase 3-letter codes: sun mon tue wed thu fri sat
- Every turn-ON and every turn-OFF is its OWN separate event.
- "every day" or no day mentioned -> all 7 days. Expand ranges (e.g. Sun-Thu).
- Use 24-hour time, zero-padded ("06:00").
- Each device supports at most {MAX_ALARMS} events. If the request needs more,
  still output every event faithfully — do NOT drop any to fit.
"""


def _default_completion(system: str, user: str) -> str:
    from openai import OpenAI
    client = OpenAI()  # reads OPENAI_API_KEY from the environment
    resp = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content


def parse_schedule(prompt: str, registry: Registry, completion_fn=_default_completion) -> Schedule:
    system = build_system_prompt(registry)
    raw = completion_fn(system, prompt)
    data = json.loads(raw)
    schedules = [
        DeviceSchedule(
            device=s["device"],
            events=[Event(time=e["time"], action=e["action"], days=e["days"]) for e in s["events"]],
        )
        for s in data["schedules"]
    ]
    return Schedule(schedules=schedules)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_parser.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/switchbot_scheduler/parser.py tests/test_parser.py tests/fixtures/
git commit -m "feat: add OpenAI parser (prompt -> Schedule) with injectable completion_fn"
```

---

## Task 9: Core (`apply_schedule`) + dry-run

**Files:**
- Create: `src/switchbot_scheduler/core.py`, `tests/test_core.py`

**Interfaces:**
- Consumes: `parse_schedule` (Task 8), `validate` (Task 5), `readback` (Task 6).
- Produces:
  `build_schedule(prompt, registry, completion_fn=None) -> Schedule`;
  `apply_schedule(prompt, registry, *, dry_run=True, confirm=<fn>, writer=None, completion_fn=None) -> tuple[str, str, Schedule]`.
  Returns `(outcome, readback_text, schedule)` where `outcome` ∈
  `{"dry_run", "cancelled", "written"}`. `confirm(text) -> bool`.
  `writer(schedule, registry) -> None` (Task 10 supplies the real one).

- [ ] **Step 1: Write the failing test**

`tests/test_core.py`:
```python
from pathlib import Path
from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.core import apply_schedule

CANNED = (Path(__file__).parent / "fixtures" / "parser_living_room.json").read_text()


def _reg():
    return Registry([Device(name="living_room", aliases=[], ble_id="U1")])


def _fn(system, user):
    return CANNED


def test_dry_run_returns_readback_and_does_not_write():
    wrote = []
    outcome, text, sched = apply_schedule(
        "living room 6 to 5", _reg(), dry_run=True,
        writer=lambda s, r: wrote.append(s), completion_fn=_fn,
    )
    assert outcome == "dry_run"
    assert "living_room: on 06:00 — every day" in text
    assert wrote == []


def test_decline_at_confirm_does_not_write():
    wrote = []
    outcome, _, _ = apply_schedule(
        "living room 6 to 5", _reg(), dry_run=False,
        confirm=lambda text: False,
        writer=lambda s, r: wrote.append(s), completion_fn=_fn,
    )
    assert outcome == "cancelled"
    assert wrote == []


def test_confirm_yes_writes():
    wrote = []
    outcome, _, _ = apply_schedule(
        "living room 6 to 5", _reg(), dry_run=False,
        confirm=lambda text: True,
        writer=lambda s, r: wrote.append(s), completion_fn=_fn,
    )
    assert outcome == "written"
    assert len(wrote) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_core.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

`src/switchbot_scheduler/core.py`:
```python
from .parser import parse_schedule
from .validator import validate
from .readback import readback
from .model import Schedule
from .registry import Registry


def build_schedule(prompt: str, registry: Registry, completion_fn=None) -> Schedule:
    kwargs = {"completion_fn": completion_fn} if completion_fn is not None else {}
    schedule = parse_schedule(prompt, registry, **kwargs)
    validate(schedule, registry)
    return schedule


def apply_schedule(prompt, registry, *, dry_run=True, confirm=lambda text: True,
                   writer=None, completion_fn=None):
    schedule = build_schedule(prompt, registry, completion_fn)
    text = readback(schedule)
    if dry_run:
        return ("dry_run", text, schedule)
    if not confirm(text):
        return ("cancelled", text, schedule)
    if writer is None:
        raise ValueError("writer is required when dry_run=False")
    writer(schedule, registry)
    return ("written", text, schedule)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_core.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/switchbot_scheduler/core.py tests/test_core.py
git commit -m "feat: add apply_schedule core with dry-run and confirm gate"
```

---

## Task 10: Bluetooth writer

Assembles the alarm command frame confirmed in Task 2 and writes the **complete**
alarm set for one Bot in one operation, then reads back to verify. Hardware I/O is
verified manually (Task 2 proved the frame); unit tests cover the frame assembly only.

**Files:**
- Create: `src/switchbot_scheduler/ble_writer.py`, add `test_frame_assembly` to `tests/test_encoder.py`

**Interfaces:**
- Consumes: `encode_alarm` (Task 7), `Registry` (Task 4), `Schedule` (Task 3).
- Produces: `build_alarm_frames(alarms: list[dict]) -> list[bytes]`;
  `async write_alarms(ble_id: str, alarms: list[dict]) -> None`;
  `write_schedule(schedule: Schedule, registry: Registry) -> None` (sync wrapper,
  usable directly as the `writer` argument to `apply_schedule`).

- [ ] **Step 1: Write the failing test for frame assembly**

Append to `tests/test_encoder.py`:
```python
from switchbot_scheduler.ble_writer import build_alarm_frames


def test_build_alarm_frames_one_per_alarm_with_index_and_count():
    alarms = [
        {"repeat_byte": 0b1111111, "hour": 6, "minute": 0, "action": 1},
        {"repeat_byte": 0b1111111, "hour": 17, "minute": 0, "action": 2},
    ]
    frames = build_alarm_frames(alarms)
    assert len(frames) == 2
    # each frame carries total count (2) and its own index (0, 1)
    assert frames[0][2] == 2 and frames[0][3] == 0
    assert frames[1][2] == 2 and frames[1][3] == 1
    # hour/minute/action land in the documented positions
    assert frames[0][5] == 6 and frames[0][6] == 0 and frames[0][7] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_encoder.py::test_build_alarm_frames_one_per_alarm_with_index_and_count -v`
Expected: FAIL — `ble_writer` not found.

- [ ] **Step 3: Write minimal implementation**

`src/switchbot_scheduler/ble_writer.py`:
```python
import asyncio
from .encoder import encode_alarm
from .model import Schedule
from .registry import Registry

# SwitchBot BLE GATT characteristics (confirm against spikes/FINDINGS.md).
WRITE_CHAR = "cba20002-224d-11e6-9fb9-0002a5d5c51b"

# Command frame layout (confirm bytes against spikes/FINDINGS.md):
#   [0] 0x57 magic  [1] 0x09 set-time-management  [2] total count  [3] index
#   [4] repeat_byte [5] hour  [6] minute  [7] action(job type)
MAGIC = 0x57
CMD_SET_TIME_MGMT = 0x09


def build_alarm_frames(alarms: list[dict]) -> list[bytes]:
    total = len(alarms)
    frames = []
    for index, a in enumerate(alarms):
        frames.append(bytes([
            MAGIC, CMD_SET_TIME_MGMT, total, index,
            a["repeat_byte"], a["hour"], a["minute"], a["action"],
        ]))
    return frames


async def write_alarms(ble_id: str, alarms: list[dict]) -> None:
    from bleak import BleakClient
    frames = build_alarm_frames(alarms)
    async with BleakClient(ble_id) as client:
        for frame in frames:
            await client.write_gatt_char(WRITE_CHAR, frame, response=True)


def write_schedule(schedule: Schedule, registry: Registry) -> None:
    for ds in schedule.schedules:
        ble_id = registry.ble_id(ds.device)
        if not ble_id:
            raise ValueError(f"No ble_id for '{ds.device}'. Fill devices.yaml from the spike.")
        alarms = [encode_alarm(e) for e in ds.events]
        asyncio.run(write_alarms(ble_id, alarms))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_encoder.py -v`
Expected: all PASS (including the new frame test).

- [ ] **Step 5: Manual hardware check (near the Bots)**

With `devices.yaml` filled from Task 2, run a short REPL:
```bash
python -c "from switchbot_scheduler.ble_writer import write_schedule; \
from switchbot_scheduler.registry import Registry; \
from switchbot_scheduler.model import *; \
r=Registry.load('devices.yaml'); \
s=Schedule([DeviceSchedule('living_room',[Event('%H:%M+2min','press',['sun','mon','tue','wed','thu','fri','sat'])])]); \
write_schedule(s,r)"
```
(Substitute a real time ~2 minutes ahead.) Expected: the Bot fires at that time.
If the frame differs from Task 2's finding, update `build_alarm_frames` to match and
re-run the unit test.

- [ ] **Step 6: Commit**

```bash
git add src/switchbot_scheduler/ble_writer.py tests/test_encoder.py
git commit -m "feat: add BLE alarm writer (full-set write per Bot)"
```

---

## Task 11: CLI + wire the real write path

**Files:**
- Create: `src/switchbot_scheduler/cli.py`; add `[project.scripts]` to `pyproject.toml`

**Interfaces:**
- Consumes: `Registry.load` (Task 4), `apply_schedule` (Task 9), `write_schedule` (Task 10).
- Produces: `main(argv=None) -> int` console entry point `switchbot-schedule`.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:
```python
from pathlib import Path
from switchbot_scheduler import cli

CANNED = (Path(__file__).parent / "fixtures" / "parser_living_room.json").read_text()


def test_cli_dry_run_prints_readback(monkeypatch, capsys, tmp_path):
    reg = tmp_path / "devices.yaml"
    reg.write_text("devices:\n  living_room:\n    aliases: []\n    ble_id: \"U1\"\n")
    monkeypatch.setattr(cli, "_completion_fn", lambda system, user: CANNED)
    code = cli.main(["--devices", str(reg), "--dry-run", "living room 6 to 5"])
    out = capsys.readouterr().out
    assert code == 0
    assert "living_room: on 06:00 — every day" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL — `cli.main` not found.

- [ ] **Step 3: Write minimal implementation**

`src/switchbot_scheduler/cli.py`:
```python
import argparse
import sys
from .registry import Registry
from .core import apply_schedule
from .ble_writer import write_schedule

_completion_fn = None  # tests override; None => parser uses its OpenAI default


def _confirm(text: str) -> bool:
    print("\nGoing to write:\n" + text)
    return input("\nProceed? [y/N] ").strip().lower() == "y"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="switchbot-schedule")
    ap.add_argument("prompt", help="schedule in plain Hebrew/English")
    ap.add_argument("--devices", default="devices.yaml")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--write", dest="dry_run", action="store_false")
    args = ap.parse_args(argv)

    registry = Registry.load(args.devices)
    try:
        outcome, text, _ = apply_schedule(
            args.prompt, registry,
            dry_run=args.dry_run, confirm=_confirm,
            writer=write_schedule, completion_fn=_completion_fn,
        )
    except Exception as err:  # ScheduleError and friends -> friendly message
        print(f"⚠️  {err}", file=sys.stderr)
        return 1

    if outcome == "dry_run":
        print("[DRY RUN] would write:\n" + text)
    elif outcome == "cancelled":
        print("Cancelled — nothing written.")
    else:
        print("✅ Written to the Bots:\n" + text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Register the console script**

Add to `pyproject.toml`:
```toml
[project.scripts]
switchbot-schedule = "switchbot_scheduler.cli:main"
```
Then `pip install -e ".[dev]"` again so the command registers.

- [ ] **Step 6: Full test run + manual dry-run smoke**

Run:
```bash
pytest -v
switchbot-schedule --dry-run "turn the living room on at 6am off at 5pm every day"
```
Expected: all tests PASS; the dry-run prints the read-back with no network write.
(A live run needs `OPENAI_API_KEY` set; `--write` needs you home near the Bots.)

- [ ] **Step 7: Commit**

```bash
git add src/switchbot_scheduler/cli.py tests/test_cli.py pyproject.toml
git commit -m "feat: add CLI with dry-run default and --write path"
```

---

## Self-Review

**Spec coverage:**
- Goal (prompt → per-Bot alarms, generic): Tasks 3,4,8,9,10,11. ✅
- Option B / on-device Bluetooth alarms: Tasks 2,7,10. ✅
- Two-stage LLM→deterministic split: parser isolated (8); everything else deterministic (5,6,7,9,10). ✅
- OpenAI GPT parser, `OPENAI_API_KEY` from env, swappable: Task 8. ✅
- Registry-driven generic model: Task 4 + `devices.yaml`. ✅
- LLM told limits but Validator enforces: prompt text (8) + `validate` (5). ✅
- Read-back without a second LLM call: Task 6. ✅
- Error handling table (unknown device, >5, bad time/day, BLE unreachable, atomic write): Tasks 5, 10, 11. ✅
- Testing (pure unit tests, parser fixtures, dry-run default, BLE spike + manual): Tasks 2–11. ✅
- Security (env-only secrets, gitignore): Tasks 1, 8. ✅
- macOS UUID note: Global Constraints + Tasks 2, 4. ✅
- One-off alarms / away-changes / cloud server: correctly out of scope (spec §10); not planned. ✅

**Placeholder scan:** No "TBD/handle edge cases" in steps. The empty `ble_id: ""` values in `devices.yaml` are intentional (filled from the Task 2 spike) and flagged as such. `MODEL = "gpt-4o-mini"` is a concrete default with a swap comment.

**Type consistency:** `Event/DeviceSchedule/Schedule` fields consistent across Tasks 3–11. `encode_alarm` dict keys (`repeat_byte/hour/minute/action`) match between Task 7 (producer) and Task 10 (consumer). `apply_schedule`'s `writer` signature `(schedule, registry)` matches `write_schedule` in Task 10. `completion_fn(system, user)` signature consistent across Tasks 8, 9, 11.

**Note on Task 10 frame bytes:** `build_alarm_frames` encodes the *documented* structure; if Task 2's spike reveals a different frame, Step 5 directs updating it and re-running the unit test. This is expected reconciliation, not a placeholder.
