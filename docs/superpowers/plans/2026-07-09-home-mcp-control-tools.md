# Home control tools (home-mcp, in-process) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the family agent three in-process tools — `control_device`, `list_devices`, `battery_status` — that drive the real SwitchBot Bots from natural-language Telegram chat.

**Architecture:** A single new module `src/home_agent/home.py` wraps the existing `switchbot_scheduler` immediate-action path (`run_immediate`, `Registry`) as `home_agent.tools.Tool` objects, composed into the agent's tool list at startup. No separate process, no MCP protocol. All BLE/OpenAI seams are injectable so the automated suite touches no hardware and no network.

**Tech Stack:** Python 3.11+, `python-telegram-bot`, `openai`, `bleak` (BLE, production only), `pytest`. Reuses `switchbot_scheduler` unchanged.

## Global Constraints

- Python **3.11+** (uses `set[int]`, `str | None`).
- **No BLE and no network in the automated test suite** — inject `actuate_fn` / `battery_fn` and use the existing `make_fake_client` OpenAI fake.
- **Reuse `switchbot_scheduler`; make no changes to it.** Home tools call into it.
- Tools are the existing `home_agent.tools.Tool` dataclass: `Tool(name: str, schema: dict, impl: Callable[[dict], str])`.
- Env policy stays `override=False` (shell exports win) via `switchbot_scheduler.config.load_env`, already used by `home_agent.config`.
- `devices.yaml` currently holds **macOS CoreBluetooth UUIDs** — real fires work on the Mac now; the Linux box (Epic 2) will need MAC addresses. Out of scope here.
- Control command wire format (from `switchbot_scheduler.actuator`): `0x57 0x01 <code>` where `ACTION_CODE` = `press=0, on=1, off=2`. `resolve_action` maps press-mode → press and swaps on/off for inverted devices.
- `.venv/bin/pytest` is the test runner. Commit after every task.

---

### Task 1: Config — add `devices_path`

**Files:**
- Modify: `src/home_agent/config.py`
- Test: `tests/home_agent/test_home_agent_config.py`

**Interfaces:**
- Produces: `Config.devices_path: str` (default `DEFAULT_DEVICES_PATH = "devices.yaml"`, env `SWITCHBOT_DEVICES`); module constant `DEFAULT_DEVICES_PATH`.

- [ ] **Step 1: Extend `_clean_env` and write the failing test**

In `tests/home_agent/test_home_agent_config.py`, add `"SWITCHBOT_DEVICES"` to the `_clean_env` tuple, then add:

```python
def test_load_config_devices_path_default_and_override(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    from home_agent.config import load_config, DEFAULT_DEVICES_PATH
    env = tmp_path / ".env"
    env.write_text('OPENAI_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN=tok\n')
    assert load_config(str(env)).devices_path == DEFAULT_DEVICES_PATH
    monkeypatch.setenv("SWITCHBOT_DEVICES", "/tmp/d.yaml")
    assert load_config(str(env)).devices_path == "/tmp/d.yaml"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_home_agent_config.py::test_load_config_devices_path_default_and_override -v`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_DEVICES_PATH'`.

- [ ] **Step 3: Implement in `config.py`**

Add the constant next to the other defaults, the dataclass field, and the loader line:

```python
DEFAULT_DEVICES_PATH = "devices.yaml"
```
```python
@dataclass
class Config:
    openai_api_key: str
    telegram_bot_token: str
    allowed_chat_ids: set[int]
    model: str = DEFAULT_MODEL
    db_path: str = DEFAULT_DB_PATH
    openai_timeout: float = DEFAULT_OPENAI_TIMEOUT
    devices_path: str = DEFAULT_DEVICES_PATH
```
And inside `load_config(...)`'s `return Config(...)`, add:
```python
        devices_path=os.environ.get("SWITCHBOT_DEVICES", DEFAULT_DEVICES_PATH),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/home_agent/test_home_agent_config.py -v`
Expected: PASS (all config tests).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/config.py tests/home_agent/test_home_agent_config.py
git commit -m "feat(home): Config.devices_path (SWITCHBOT_DEVICES)"
```

---

### Task 2: `home.py` + `list_devices` tool

**Files:**
- Create: `src/home_agent/home.py`
- Test: `tests/home_agent/test_home_tools.py`

**Interfaces:**
- Consumes: `home_agent.tools.Tool`; `switchbot_scheduler.registry.{Registry, Device}`.
- Produces: `build_home_tools(registry, *, actuate_fn=None, battery_fn=None) -> list[Tool]` (this task returns just the `list_devices` tool; Tasks 3–4 append `control_device` and `battery_status`). Helper `_device_type(device) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/home_agent/test_home_tools.py`:

```python
from switchbot_scheduler.registry import Registry, Device
from home_agent.home import build_home_tools


def _registry():
    return Registry([
        Device(name="living_room", aliases=["סלון", "living room"], ble_id="ID1", inverted=True),
        Device(name="ac", aliases=["מזגן", "ac"], ble_id="ID2", mode="press"),
        Device(name="kitchen", aliases=["מטבח"], ble_id="ID3"),
    ])


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_list_devices_lists_names_aliases_and_type():
    tools = build_home_tools(_registry())
    out = _tool(tools, "list_devices").impl({})
    assert "living_room" in out and "סלון" in out
    assert "inverted" in out            # living_room type note
    assert "ac" in out and "toggle" in out
    assert "kitchen" in out and "מטבח" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_home_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'home_agent.home'`.

- [ ] **Step 3: Create `src/home_agent/home.py`**

```python
import logging
import os

from switchbot_scheduler.actuator import run_immediate
from switchbot_scheduler.model import ImmediateAction
from switchbot_scheduler.registry import Registry

from .tools import Tool

log = logging.getLogger("home_agent")

_LIST_SCHEMA = {"type": "function", "function": {
    "name": "list_devices",
    "description": (
        "List the home devices you can control — names, Hebrew/English aliases, and type. "
        "Use when the user asks what you can control, or when you need the exact device name "
        "before control_device. Does NOT report whether a device is currently on or off."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}


def _device_type(device) -> str:
    if device.mode == "press":
        return "AC / momentary toggle"
    if device.inverted:
        return "light (mounted inverted)"
    return "light"


def _list_impl(args, *, registry) -> str:
    lines = []
    for d in registry.devices:
        aliases = ", ".join(d.aliases) if d.aliases else "(no aliases)"
        lines.append(f"{d.name} [{_device_type(d)}] — aliases: {aliases}")
    return "\n".join(lines)


def build_home_tools(registry, *, actuate_fn=None, battery_fn=None) -> list[Tool]:
    return [
        Tool(name="list_devices", schema=_LIST_SCHEMA,
             impl=lambda args: _list_impl(args, registry=registry)),
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/home_agent/test_home_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/home.py tests/home_agent/test_home_tools.py
git commit -m "feat(home): list_devices tool + build_home_tools"
```

---

### Task 3: `control_device` tool

**Files:**
- Modify: `src/home_agent/home.py`
- Test: `tests/home_agent/test_home_tools.py`

**Interfaces:**
- Consumes: `build_home_tools`, `_registry`, `_tool` from Task 2.
- Produces: a `control_device` tool in the list; `_control_impl(args, *, registry, actuate_fn) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_home_tools.py`:

```python
def test_control_device_resolves_alias_and_fires():
    calls = []
    tools = build_home_tools(_registry(), actuate_fn=lambda ble_id, code: calls.append((ble_id, code)))
    out = _tool(tools, "control_device").impl({"device": "מטבח", "action": "on"})
    assert calls == [("ID3", 1)]        # kitchen on → code 1
    assert "kitchen" in out and "✅" in out


def test_control_device_applies_inversion():
    calls = []
    tools = build_home_tools(_registry(), actuate_fn=lambda b, c: calls.append((b, c)))
    _tool(tools, "control_device").impl({"device": "סלון", "action": "on"})
    assert calls == [("ID1", 2)]        # inverted: on → off → code 2


def test_control_device_ac_is_press_mode():
    calls = []
    tools = build_home_tools(_registry(), actuate_fn=lambda b, c: calls.append((b, c)))
    _tool(tools, "control_device").impl({"device": "מזגן", "action": "on"})
    assert calls == [("ID2", 0)]        # press-mode: on → press → code 0


def test_control_device_unknown_device_is_friendly():
    out = _tool(build_home_tools(_registry()), "control_device").impl({"device": "garage", "action": "on"})
    assert "unknown device" in out.lower()
    assert "kitchen" in out             # lists known devices
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_home_tools.py -k control -v`
Expected: FAIL — `StopIteration` (no tool named `control_device`).

- [ ] **Step 3: Implement in `home.py`**

Add the schema constant near `_LIST_SCHEMA`:

```python
_CONTROL_SCHEMA = {"type": "function", "function": {
    "name": "control_device",
    "description": (
        "Control a SwitchBot device by sending an on/off/press command. Use whenever the user asks "
        "to turn something on or off, or to toggle it (lights, AC). The air conditioner supports only "
        "'press' (a momentary toggle whose resulting on/off state is unknown). If unsure of the exact "
        "device name, call list_devices first. Report back what happened in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string",
                   "description": "Room/device name or alias, Hebrew or English (e.g. 'סלון', 'living room', 'מזגן', 'kitchen')."},
        "action": {"type": "string", "enum": ["on", "off", "press"],
                   "description": "on, off, or press; the AC only honors press."},
    }, "required": ["device", "action"], "additionalProperties": False},
}}
```

Add the impl:

```python
def _control_impl(args, *, registry, actuate_fn) -> str:
    spoken = (args.get("device") or "").strip()
    action = (args.get("action") or "").strip().lower()
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    if action not in ("on", "off", "press"):
        return f"unknown action '{action}'. Use on, off, or press."
    result = run_immediate([ImmediateAction(name, action)], registry, actuate_fn=actuate_fn)[0]
    if result.ok:
        return f"{result.device}: {result.action} ✅"
    return f"{result.device}: failed — {result.error}"
```

Replace `build_home_tools` with:

```python
def build_home_tools(registry, *, actuate_fn=None, battery_fn=None) -> list[Tool]:
    return [
        Tool(name="control_device", schema=_CONTROL_SCHEMA,
             impl=lambda args: _control_impl(args, registry=registry, actuate_fn=actuate_fn)),
        Tool(name="list_devices", schema=_LIST_SCHEMA,
             impl=lambda args: _list_impl(args, registry=registry)),
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_home_tools.py -v`
Expected: PASS (list + control tests).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/home.py tests/home_agent/test_home_tools.py
git commit -m "feat(home): control_device tool (alias/inversion/press-mode via run_immediate)"
```

---

### Task 4: `battery_status` tool + real BLE battery reader

**Files:**
- Modify: `src/home_agent/home.py`
- Test: `tests/home_agent/test_home_tools.py`

**Interfaces:**
- Produces: a `battery_status` tool; `_battery_impl(args, *, registry, battery_fn) -> str`; `_run_battery(ble_id: str) -> int` (real BLE reader, lazy `bleak` import — its exact battery byte offset is CONFIRMED in Task 6). `build_home_tools` now defaults `battery_fn` to `_run_battery`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_home_tools.py`:

```python
def test_battery_status_all_devices():
    tools = build_home_tools(_registry(), battery_fn=lambda b: {"ID1": 88, "ID2": 40, "ID3": 15}[b])
    out = _tool(tools, "battery_status").impl({})
    assert "living_room: 88%" in out
    assert "ac: 40%" in out
    assert "kitchen: 15%" in out


def test_battery_status_single_device():
    tools = build_home_tools(_registry(), battery_fn=lambda b: 55)
    out = _tool(tools, "battery_status").impl({"device": "מטבח"})
    assert out.strip() == "kitchen: 55%"


def test_battery_status_isolates_a_failure():
    def bf(ble_id):
        if ble_id == "ID2":
            raise RuntimeError("timeout")
        return 90
    tools = build_home_tools(_registry(), battery_fn=bf)
    out = _tool(tools, "battery_status").impl({})
    assert "ac: unavailable — timeout" in out
    assert "living_room: 90%" in out    # other devices still reported
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_home_tools.py -k battery -v`
Expected: FAIL — `StopIteration` (no `battery_status` tool).

- [ ] **Step 3: Implement in `home.py`**

Add the schema and the BLE constants/reader near the top imports:

```python
_BATTERY_SCHEMA = {"type": "function", "function": {
    "name": "battery_status",
    "description": (
        "Report SwitchBot device battery levels. Use when the user asks about battery or whether a "
        "device is running low. Pass one device name/alias to check just that one; omit to check all. "
        "Returns each device's battery percent, or an error if a Bot couldn't be reached."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "One device name/alias; omit to check all."},
    }, "additionalProperties": False},
}}

_CMD_INFO = 0x02  # 0x57 0x02: "get device basic info"; response carries battery %


def _run_battery(ble_id: str) -> int:
    """Real BLE battery read (production). Battery byte offset confirmed by the Task 6 spike."""
    import asyncio
    from switchbot_scheduler.actuator import WRITE_CHAR, NOTIFY_CHAR, MAGIC

    async def _read() -> int:
        from bleak import BleakClient
        responses: list[bytes] = []
        async with BleakClient(ble_id) as client:
            await client.start_notify(NOTIFY_CHAR, lambda _, d: responses.append(bytes(d)))
            await client.write_gatt_char(WRITE_CHAR, bytes([MAGIC, _CMD_INFO]), response=True)
            await asyncio.sleep(1.0)
            await client.stop_notify(NOTIFY_CHAR)
        if not responses:
            raise RuntimeError("no response from device")
        return responses[-1][1]  # battery percent (byte index 1; confirm/adjust in Task 6)

    return asyncio.run(_read())
```

Add the impl:

```python
def _battery_impl(args, *, registry, battery_fn) -> str:
    spoken = (args.get("device") or "").strip()
    if spoken:
        name = registry.resolve(spoken)
        if name is None:
            return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
        targets = [name]
    else:
        targets = registry.known_names()
    lines = []
    for name in targets:
        ble_id = registry.ble_id(name)
        if not ble_id:
            lines.append(f"{name}: unavailable — no ble_id")
            continue
        try:
            lines.append(f"{name}: {battery_fn(ble_id)}%")
        except Exception as e:
            lines.append(f"{name}: unavailable — {e}")
    return "\n".join(lines)
```

Replace `build_home_tools` with the final three-tool version:

```python
def build_home_tools(registry, *, actuate_fn=None, battery_fn=None) -> list[Tool]:
    battery_fn = battery_fn or _run_battery
    return [
        Tool(name="control_device", schema=_CONTROL_SCHEMA,
             impl=lambda args: _control_impl(args, registry=registry, actuate_fn=actuate_fn)),
        Tool(name="list_devices", schema=_LIST_SCHEMA,
             impl=lambda args: _list_impl(args, registry=registry)),
        Tool(name="battery_status", schema=_BATTERY_SCHEMA,
             impl=lambda args: _battery_impl(args, registry=registry, battery_fn=battery_fn)),
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_home_tools.py -v`
Expected: PASS (all home-tool tests).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/home.py tests/home_agent/test_home_tools.py
git commit -m "feat(home): battery_status tool + BLE battery reader (offset TBC by spike)"
```

---

### Task 5: Wire home tools into the running bot

**Files:**
- Modify: `src/home_agent/home.py` (add `load_home_tools`)
- Modify: `src/home_agent/telegram_app.py`
- Test: `tests/home_agent/test_home_tools.py`, `tests/home_agent/test_telegram_handler.py`, `tests/home_agent/test_telegram_app.py`

**Interfaces:**
- Consumes: `Config.devices_path` (Task 1), `build_home_tools` (Tasks 2–4), `handle_message`'s existing `tools=` parameter.
- Produces: `home.load_home_tools(config) -> list[Tool]` (returns `[]` with a warning if the devices file is absent); `build_application` composes `list(DEFAULT_TOOLS) + load_home_tools(config)` and passes it to `handle_message`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_home_tools.py`:

```python
def test_load_home_tools_missing_file_returns_empty(tmp_path):
    from home_agent.home import load_home_tools
    from home_agent.config import Config
    cfg = Config(openai_api_key="x", telegram_bot_token="t:t", allowed_chat_ids={1},
                 devices_path=str(tmp_path / "nope.yaml"))
    assert load_home_tools(cfg) == []


def test_load_home_tools_present_file_builds_three(tmp_path):
    from home_agent.home import load_home_tools
    from home_agent.config import Config
    dev = tmp_path / "devices.yaml"
    dev.write_text("devices:\n  kitchen:\n    aliases: [מטבח]\n    ble_id: ID3\n")
    cfg = Config(openai_api_key="x", telegram_bot_token="t:t", allowed_chat_ids={1},
                 devices_path=str(dev))
    assert {t.name for t in load_home_tools(cfg)} == {"control_device", "list_devices", "battery_status"}
```

Append to `tests/home_agent/test_telegram_handler.py`:

```python
def test_handle_message_runs_control_device_through_composed_tools(tmp_path, make_fake_client):
    from home_agent.home import build_home_tools
    from home_agent.tools import DEFAULT_TOOLS
    from switchbot_scheduler.registry import Registry, Device
    reg = Registry([Device(name="kitchen", aliases=["מטבח"], ble_id="ID3")])
    calls = []
    tools = list(DEFAULT_TOOLS) + build_home_tools(reg, actuate_fn=lambda b, c: calls.append((b, c)))
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "control_device",
                         "arguments": {"device": "מטבח", "action": "on"}}]},
        {"content": "הדלקתי את המטבח"},
    ])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "תדליק את המטבח", config=_cfg(tmp_path, {1}),
                           conversation=conv, client=client, tools=tools)
    assert calls == [("ID3", 1)]
    assert reply == "הדלקתי את המטבח"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_home_tools.py -k load_home -v`
Expected: FAIL — `ImportError: cannot import name 'load_home_tools'`.

- [ ] **Step 3: Add `load_home_tools` to `home.py`**

```python
def load_home_tools(config) -> list[Tool]:
    """Build the home tools from config.devices_path. If the file is absent, log a warning and
    return [] so the bot still runs (time-only) instead of crashing at startup."""
    path = config.devices_path
    if not os.path.exists(path):
        log.warning("devices file not found at %s — home control disabled", path)
        return []
    return build_home_tools(Registry.load(path))
```

- [ ] **Step 4: Wire into `telegram_app.build_application`**

Add the import at the top of `telegram_app.py`:
```python
from .home import load_home_tools
```
Inside `build_application`, after the `conversation` is set up and before `app = Application...`, add:
```python
    tools = list(DEFAULT_TOOLS) + load_home_tools(config)
```
Change the `on_message` `handle_message` call to pass the composed tools:
```python
        reply = await asyncio.to_thread(
            handle_message, chat_id, message.text or "",
            config=config, conversation=conversation, client=client, tools=tools)
```

- [ ] **Step 5: Keep the existing app test hermetic**

In `tests/home_agent/test_telegram_app.py`, update `_cfg` so it does not depend on the repo's real `devices.yaml`:
```python
def _cfg(tmp_path):
    # token must be BotFather-shaped ("<digits>:<rest>") for python-telegram-bot to accept it
    return Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                  allowed_chat_ids={1}, model="gpt-4o", db_path=str(tmp_path / "m.db"),
                  devices_path=str(tmp_path / "no-devices.yaml"))
```

- [ ] **Step 6: Run the full suite to verify it passes**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (all tests, including the new wiring + handler tests).

- [ ] **Step 7: Commit**

```bash
git add src/home_agent/home.py src/home_agent/telegram_app.py tests/home_agent/
git commit -m "feat(home): compose home tools into the bot at startup (graceful if devices.yaml absent)"
```

---

### Task 6: Battery BLE spike — confirm the battery byte

**Files:**
- Create: `spikes/battery_spike.py`
- Modify (only if the spike shows a different offset): `src/home_agent/home.py` (`_run_battery` return line)

**Interfaces:** none (hardware spike; not part of the automated suite).

> This task needs a real Bot in Bluetooth range of the Mac. It confirms the assumption baked into
> `_run_battery` (battery = response byte index 1). If the real device disagrees, adjust that one line.

- [ ] **Step 1: Write the spike script**

Create `spikes/battery_spike.py`:

```python
"""One-off: read a SwitchBot Bot's 'basic info' reply and print the raw bytes so we can
locate the battery percentage. Usage: python spikes/battery_spike.py <BLE_ID>"""
import asyncio
import sys
from switchbot_scheduler.actuator import WRITE_CHAR, NOTIFY_CHAR, MAGIC

CMD_INFO = 0x02


async def main(ble_id: str) -> None:
    from bleak import BleakClient
    responses: list[bytes] = []
    async with BleakClient(ble_id) as client:
        await client.start_notify(NOTIFY_CHAR, lambda _, d: responses.append(bytes(d)))
        await client.write_gatt_char(WRITE_CHAR, bytes([MAGIC, CMD_INFO]), response=True)
        await asyncio.sleep(1.0)
        await client.stop_notify(NOTIFY_CHAR)
    for i, r in enumerate(responses):
        print(f"response[{i}] = {list(r)}  (hex: {r.hex()})")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 2: Run it against a real Bot**

Pick a `ble_id` from `devices.yaml` (e.g. the kitchen id) and run:
Run: `.venv/bin/python spikes/battery_spike.py 0F4665AE-85F1-3777-2F06-BEA04F8008B7`
Expected: one or more `response[i] = [...]` lines. Identify the byte that reads as a plausible battery percentage (0–100). SwitchBot Bot convention puts it at index 1.

- [ ] **Step 3: Adjust `_run_battery` only if needed**

If the battery value is at a different index than 1, change the return line in `home.py`:
```python
        return responses[-1][<confirmed_index>]  # battery percent (confirmed via spike)
```
If index 1 is correct, leave it unchanged.

- [ ] **Step 4: Record the finding**

Append a short note to `spikes/FINDINGS.md` (e.g. "battery %: basic-info reply byte index 1, confirmed on kitchen Bot 2026-07-09").

- [ ] **Step 5: Commit**

```bash
git add spikes/battery_spike.py spikes/FINDINGS.md src/home_agent/home.py
git commit -m "spike(home): confirm SwitchBot battery byte; wire real battery reader"
```

---

### Task 7: Real-hardware smoke test + final suite

**Files:** none (verification + docs).

**Interfaces:** none.

- [ ] **Step 1: Run the whole automated suite**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (all tests).

- [ ] **Step 2: Live control smoke test (Mac near the Bots)**

Ensure `.env` has a working `TELEGRAM_BOT_TOKEN` and your chat id in `ALLOWED_CHAT_IDS`, then:
Run: `PYTHONPATH=src .venv/bin/python -m home_agent`
In your Telegram chat send: `תדליק את המטבח`
Expected: the kitchen Bot physically fires, and the bot replies in Hebrew that it turned the kitchen on.

- [ ] **Step 3: Live battery smoke test**

In the chat send: `מה מצב הסוללות?`
Expected: the bot replies with a battery percentage per device (or "unavailable" for any Bot out of range).

- [ ] **Step 4: Update the roadmap**

In `docs/ROADMAP.md`, mark Epic #3 (home-mcp) status ✅ with a one-line note (date, tools shipped: control_device / list_devices / battery_status, in-process).

- [ ] **Step 5: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "docs(home): mark home-mcp control tools shipped (roadmap #3)"
```
