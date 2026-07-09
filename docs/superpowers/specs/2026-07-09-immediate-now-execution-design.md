# Immediate ("now") Execution — Design

**Date:** 2026-07-09
**Status:** Approved (design), pending implementation plan

## Problem

The scheduler only converts requests into *future* on-device timers (the `0x09`
alarm protocol). Its LLM prompt has no concept of "now". When a user says
`תדליק לי עכשיו את האור במטבח ובגינה ובסלון` ("turn on the light **now** in
kitchen, garden and living room"), the model is forced to emit a schedule and
fabricates `00:00 once (thu)` — a garbage time — and (in the first turn) even
wrote it to the bots. See the failure captured 2026-07-09.

Two defects:
1. **Missing capability:** no way to actuate a bot immediately.
2. **Silent fabrication:** unschedulable intent becomes a bogus `00:00` schedule
   instead of being recognized.

Immediate action is technically trivial — it is the live BLE press already used
to identify bots during setup (`spikes/ble_spike.py press`). The scheduler simply
does not expose it.

## Decisions (from brainstorming)

- **Execute immediately** — "now" sends a live BLE command and actuates the bot
  right then. Requires the bot in Bluetooth range of the machine at that moment
  (unlike scheduled timers, which fire autonomously on the bot).
- **No confirmation** for immediate actions — they fire on send.
- **Report what was done** — the app states each immediate action it took
  (e.g. `⚡ Turned on kitchen`), including per-device failures.
- **Support mixed messages** — one message can trigger immediate actions *and*
  set schedules together.
- **Never fabricate a time** — if intent is immediate, it goes to `immediate`;
  the parser must not invent `00:00`.

## Architecture

Three focused changes; no rework of the existing scheduled-timer path.

### 1. Parser (`parser.py`)

`build_conversation_system_prompt` gains rules distinguishing immediate intent
("now" / "עכשיו" / "right now" / no future time and an act-now verb) from
scheduled intent. Output schema (any field may be absent/empty):

```json
{
  "immediate":  [{"device": "kitchen", "action": "on" | "off" | "press"}],
  "schedules":  [{"device": "living_room", "events": [ { "time": "HH:MM", "action": ..., "days": [...], "once": <bool> } ]}],
  "clarification": "<short question or explanation>"
}
```

- `immediate` items have **no time**.
- Prompt rule (explicit): if the user wants something to happen now, emit an
  `immediate` entry — **never** invent a time or a `00:00` schedule. If intent is
  genuinely ambiguous, return `clarification`.
- Device names in `immediate` are resolved through `registry.resolve` exactly
  like `schedules`.

`ParseResult` gains `immediate: list[ImmediateAction]` (default empty).
`ImmediateAction` = `{device: str, action: str}`.

### 2. Actuator (`actuator.py`, new)

Live BLE command primitive, mirroring the scheduled-path semantics.

```python
WRITE_CHAR  = "cba20002-224d-11e6-9fb8-0002a5d5c51b"   # same as ble_writer
NOTIFY_CHAR = "cba20003-224d-11e6-9fb8-0002a5d5c51b"

async def actuate(ble_id, action_code) -> bytes          # writes 57 01 {code}; returns reply
def run_immediate(actions, registry) -> list[ActionResult]  # sync wrapper
```

- Command byte: `0x57 0x01 <code>`, `code` from `encoder.ACTION_CODE`
  (`press=0, on=1, off=2`) — reused, not duplicated.
- Apply the **same mapping** the encoder uses: swap on/off when the device is
  `inverted`; force `press` when the device is press-mode (`ac`).
- One BLE connection per device (bots allow a single central connection).
- Returns `ActionResult(device, action, ok, error)` per device; a failure on one
  device does not abort the others.

### 3. Web (`web/app.py` + `static/index.html`)

Real endpoints today are `POST /preview` (parse + read-back) and `POST /apply`
(write schedule). This feature extends `/preview` and adds `/execute`:

- `/preview` response gains an always-present `immediate` array alongside the
  existing `kind` (`clarification` | `schedule` | `none`), `readback`, `schedule`.
- New `POST /execute` — body is the list of immediate actions; runs
  `run_immediate` and returns the per-device `ActionResult`s.
- Frontend: on send, if `immediate` is non-empty, POST it to `/execute`
  **without any approval** and render one result line per device
  (`⚡ Turned on kitchen`, `⚠️ garden — out of range`). If `kind=="schedule"`,
  render the existing **Approve & write** card unchanged. Both can appear for
  one message. Executing immediate actions resets the conversation thread so a
  "now" action cannot re-fire on the next turn.

## Error handling

- Out-of-range / device-not-found (the exact failure the kitchen bot showed during
  setup) is caught per-device and surfaced as a `⚠️` line naming the device and the
  reason. It never blocks other devices or the schedule card.
- New firmware returns status `0x05` (not `0x01`) to `57 01 xx` yet still actuates,
  so success is **not** gated on the reply byte — success = the write completed
  without a BLE exception. (Reply bytes are logged for diagnostics only.)

## Testing

- **Parser (fake completion_fn):**
  - `תדליק עכשיו את האור במטבח` → `immediate:[{kitchen, on}]`, no schedules, no
    fabricated time.
  - Mixed: `תדליק עכשיו את המטבח וכבה את הסלון ב-22:00` → one immediate + one
    scheduled event.
  - Ambiguous immediate intent → `clarification`, never a `00:00` schedule.
- **Actuator (fake BLE client):** action→byte mapping; `inverted` swaps on/off;
  press-mode forces press; per-device `ActionResult`, one failure doesn't abort
  the rest.
- **Web (TestClient):** `/execute` returns per-device results; `/parse` carries all
  three fields.

## Residuals / follow-ups

- The `57 01 01` (on) and `57 01 02` (off) bytes are only spike-verified for
  `press` (`57 01 00`). Needs a quick hardware check on a real switch-mode bot,
  especially on the new firmware. Low risk (documented SwitchBot BLE bytes).
- Immediate execution requires BLE range at request time — this is inherent to
  "now" and is surfaced via the ⚠️ per-device error, not hidden.

## Out of scope

- Immediate control from anywhere but BLE range (no cloud fallback).
- Querying/telling current on/off state ("is the kitchen on?").
