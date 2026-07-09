# Home control tools (home-mcp, in-process) — design

- **Date:** 2026-07-09
- **Roadmap epic:** #3 "home-mcp" (Epic B) — built now, ahead of Infra (#2), because it is pure
  software and testable on macOS today.
- **Status:** approved design, pre-implementation.
- **Predecessor:** Epic 1 agent core (`docs/epics/epic-1-agent-core.md`) — the Telegram ↔ OpenAI
  function-calling loop, `Tool` dataclass, and `DEFAULT_TOOLS`, all live on `main`.

## Goal

Give the family agent real control of the house. The agent gains tools to switch the SwitchBot
Bots on/off (and toggle the AC), to list what it can control, and to report device battery levels —
all driven from natural-language Hebrew/English in the Telegram chat.

## Decision: in-process tools, not a separate MCP server

Home control is exposed as **in-process function tools** wired into the existing agent loop, exactly
like `get_current_time` — not as a separate MCP server process. Rationale (roadmap decision D1 defers
the manual-loop-vs-Agents-SDK question to when it is actually needed): home control has **no separate
credentials** (local Bluetooth) and **no fault-isolation need** (a failed AC command should surface
inline, not be swallowed by a sidecar). The first real MCP server belongs in finance (Epic 5), which
genuinely has separate bank credentials. The home module is structured behind a clean interface so it
can graduate to an MCP server later with no redesign.

## Scope

**In scope (this epic):**
1. `control_device(device, action)` — on/off/press a Bot.
2. `list_devices()` — enumerate controllable devices + aliases + type.
3. `battery_status(device?)` — report battery level(s).

**Explicitly out of scope (deferred):**
- Scenes (`sleep_mode`, `leaving_home`) — compose later once single-device control is proven.
- Scheduling / `schedule_task` and "tell me the schedule" — a separate (scheduling) epic. Rationale:
  a SwitchBot Bot **cannot be read for its programmed alarms** (writing alarms is one-way and
  reverse-engineered; there is no reliable readback), and nothing persists a schedule after it is set.
  So a schedule-query tool can only read from a store that a schedule-*set* tool writes — the two must
  be built together, and they need the always-on box (cron per D3) to actually fire. Not this epic.
- Live power-state in `list_devices` — Bots do not reliably expose on/off state; omitted deliberately.
- Linux BLE-id re-scan — `devices.yaml` currently holds macOS CoreBluetooth UUIDs; the Linux box
  (Epic 2) will need MAC addresses. Out of scope here.

## Architecture & components

**One new module:** `src/home_agent/home.py`. It:
- loads a `switchbot_scheduler.registry.Registry` from a devices YAML path (from `Config`),
- defines a factory `build_home_tools(registry, *, actuate_fn=None, battery_fn=None) -> list[Tool]`
  returning the three `Tool` objects (same `home_agent.tools.Tool` dataclass: `name`, `schema`,
  `impl`). The `*_fn` seams are injected in tests; production uses the real BLE functions.

**Reuse (no changes expected to `switchbot_scheduler`):**
- `Registry` — alias resolution (incl. Hebrew), `resolve`, `known_names`, `ble_id`, `is_inverted`,
  `is_press_mode`.
- `actuator.run_immediate(actions, registry, actuate_fn=None)` — fires each `ImmediateAction`, one BLE
  connection per device, applies `resolve_action` (press-mode forces press; inverted swaps on/off),
  and **never raises** (per-device failure → `ActionResult(ok=False, error=…)`).
- `actuator.ACTION_CODE` (`press=0, on=1, off=2`), `model.ImmediateAction(device, action)`.

**Wiring (small changes to existing files):**
- `config.py`: add `devices_path: str` (env `SWITCHBOT_DEVICES`, default `"devices.yaml"`) so the home
  tools know where the registry lives. Add module constant `DEFAULT_DEVICES_PATH = "devices.yaml"`.
- `telegram_app.build_application`: at startup, load the registry and compose
  `tools = list(DEFAULT_TOOLS) + build_home_tools(registry)`, and pass `tools=` through to
  `handle_message` (which already forwards `tools` to `run_turn`). `handle_message` keeps its
  `tools=DEFAULT_TOOLS` default for tests that don't need home control.
- `__main__.py`: if the devices file is missing, log a clear warning and continue with the
  time-only tool set (the bot still runs; it just cannot control the house yet).

## The three tools (schemas = the model's per-tool instructions)

### 1. `control_device(device, action)`
- **description:** "Control a SwitchBot device by sending an on/off/press command. Use whenever the
  user asks to turn something on or off, or to toggle it (lights, AC). `device` = the room/device name
  or any Hebrew/English alias (e.g. `סלון`, `living room`, `מזגן`, `kitchen`). `action` = `on`, `off`,
  or `press`. The air conditioner supports only `press` (a momentary toggle — its resulting on/off
  state is unknown). If unsure of the exact device name, call `list_devices` first. Report back what
  happened, in the user's language."
- **params:** `device` (string, required — "room/device name or alias, Hebrew or English"),
  `action` (string enum `["on","off","press"]`, required — "on, off, or press; the AC only honors press").
- **impl:** resolve the alias via `Registry.resolve`; if unknown → return a readable message naming the
  known devices (no exception). Otherwise build one `ImmediateAction(name, action)` and call
  `run_immediate([action], registry, actuate_fn)`. Convert the single `ActionResult` to a short string:
  success → `"{device}: {resolved_action} ✅"` (resolved action reflects inversion/press mapping);
  failure → `"{device}: failed — {error}"`. The model relays it in Hebrew.

### 2. `list_devices()`
- **description:** "List the home devices you can control — names, Hebrew/English aliases, and type.
  Use when the user asks what you can control, or when you need the exact device name before
  `control_device`. Does NOT report whether a device is currently on or off (SwitchBot Bots don't
  expose power state)."
- **params:** none (`{"type":"object","properties":{},"additionalProperties":false}`).
- **impl:** iterate `registry.devices`; for each emit name, aliases, and a human type derived from its
  flags: `press` → "AC / momentary toggle", `inverted` → "light (mounted inverted)", else "light".
  Return a compact multi-line string.

### 3. `battery_status(device?)`
- **description:** "Report SwitchBot device battery levels. Use when the user asks about battery or
  whether a device is running low / needs new batteries. Pass one device name/alias to check just that
  one; omit to check all. Returns each device's battery % (or an error if a Bot couldn't be reached)."
- **params:** `device` (string, optional — "one device name/alias; omit to check all").
- **impl:** resolve target set (one resolved device, or all `registry.devices`). For each, call
  `battery_fn(ble_id)` → int percentage; collect into `"{device}: {pct}%"` lines; a per-device failure
  becomes `"{device}: unavailable — {error}"` and does not abort the rest (same isolation contract as
  `run_immediate`).
- **BLE detail (needs a small spike, see Risks):** SwitchBot Bots return battery in the notify response
  to a "get device basic info" command over the same `WRITE_CHAR`/`NOTIFY_CHAR` used by `actuate`. The
  real `battery_fn` sends that command and parses the battery byte from the reply. The exact command
  bytes and the battery byte offset are confirmed by a hardware spike before wiring the real function.

## Data flow (one turn)

`"תדליק את הסלון"` → `on_message` → `handle_message(..., tools=<time+home>)` → `run_turn` → model calls
`control_device("סלון","on")` → resolve → `living_room`; `run_immediate` → `resolve_action` swaps to
`off` (inverted) → BLE control `0x57 0x01 0x02` fires → `ActionResult(ok=True)` → tool returns
`"living_room: off ✅"` → model replies `"הדלקתי את הסלון"` → chat.

## Error handling

- **Unknown device:** friendly message listing known devices; no exception; model asks the user to pick
  a known device.
- **BLE failure** (out of range, Bot asleep, wrong id on Linux): `ActionResult(ok=False, error)` /
  `battery_fn` raises → caught per-device → readable "failed/unavailable" line; the model apologizes.
- **Missing devices file at startup:** warn + run with time-only tools (bot stays up).
- All of the above sit inside `handle_message`'s existing `try/except` backstop and the bounded
  `run_turn` loop.

## Testing

Consistent with the existing style (`make_fake_client`, injectable seams) — **no BLE, no network in
the automated suite**:
- `home.py` unit tests with an **injected fake `actuate_fn`** that records `(ble_id, action_code)`:
  assert alias resolution (Hebrew + English), the `living_room` inversion swap (on → code 2/off),
  the `ac` press-mode mapping (on/off → press/code 0), the unknown-device path, and the success/failure
  string formatting.
- `battery_status` unit tests with an injected fake `battery_fn`: single device, all devices, and a
  per-device failure that still reports the others.
- `list_devices` test: asserts every device + alias + type appears.
- One loop test: a fake OpenAI client scripts a `control_device` tool call; assert the tool ran (fake
  actuator recorded the call) and the result was fed back into the second model turn.
- A `build_application` test: with a temp `devices.yaml`, assert the composed tool set includes
  `control_device`, `list_devices`, `battery_status` alongside `get_current_time`.

**Hardware verification (manual, outside the automated suite):** a BLE spike script confirms the
battery command/byte on a real Bot; then, on the Mac near the Bots, a real Telegram message
(`"תדליק את המטבח"`) is observed to fire the physical switch, and `battery_status` returns a real %.

## Risks / open items

- **Battery byte layout unknown until the spike.** The `battery_fn` real implementation is blocked on
  confirming the command bytes + battery offset on a real Bot. Mitigation: the spike is the first build
  step; the tool + tests (fake `battery_fn`) can be built in parallel and are unaffected by the byte
  layout. If battery readback proves unreliable on these Bots, `battery_status` degrades to
  "unavailable" per device and we revisit — control tools are independent and still ship.
- **macOS-only ble_ids.** Real fires work on the Mac now; the Linux box (Epic 2) will require
  re-scanning to MAC addresses in `devices.yaml`. Noted, not handled here.

## Definition of Done

- `home.py` + `build_home_tools` implemented; the three tools wired into the running bot.
- `config.devices_path` added and read from `SWITCHBOT_DEVICES`.
- Full automated suite green (existing + new home tests), no BLE/network in tests.
- Battery BLE spike completed and the real `battery_fn` implemented (or, if readback proves infeasible,
  `battery_status` documented as degraded and the control tools shipped regardless).
- Manual real-hardware smoke test on the Mac: one control command fires a physical Bot; battery returns
  a real percentage.
