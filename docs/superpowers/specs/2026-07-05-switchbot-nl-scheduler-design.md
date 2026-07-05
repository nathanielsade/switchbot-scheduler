# SwitchBot Natural-Language Scheduler — Design

**Date:** 2026-07-05
**Status:** Design approved, pending spec review → implementation plan

## 1. Goal

A tool where the user writes a schedule in plain language (Hebrew or English) —
e.g. *"turn the living room on 6am–5pm every day, and the A/C on at 8pm off at 10pm"* —
and the tool programs each SwitchBot Bot accordingly. Motivation: the SwitchBot
app's built-in scheduling UI is painful. The tool must be **generic** so that
adding more Bots (3 today → 7 planned) requires no code changes.

## 2. Key decisions (and why)

### 2.1 Execution model: on-device Bluetooth alarms ("Option B")

The system needs *something awake* to fire commands at the scheduled time. Three
candidates were considered:

- **SwitchBot cloud holds the schedule** — ❌ impossible. Verified against the
  official Cloud API v1.1 docs: the public API can only send *immediate* commands
  (`turnOn`/`turnOff`/`press`) and *execute* pre-made scenes. It has **no**
  endpoint to create timers, schedules, or scenes. (The SwitchBot *app* can
  schedule, but via private channels not exposed to third-party programs.)
- **An always-on machine (PC or cloud server) fires commands** ("Option A") — works,
  and is the only way to schedule/change *from anywhere*, but requires a machine
  running 24/7.
- **On-device Bluetooth alarms** ("Option B") — ✅ **chosen.** The Bot has its own
  clock and memory; we write alarms into it over Bluetooth, then it fires them on
  its own forever, with nothing else powered on.

**Trade-off accepted:** Bluetooth is short-range, so schedules can only be *set/changed*
while physically at home near the Bots. This fits the usage pattern (mostly
recurring schedules, changed occasionally). Max 5 alarms per Bot (firmware limit).

Confirmed via the official BLE protocol docs: command `0x09` "Set Device Time
Management Info" sets alarms. Byte format includes a repeat mode (`1=once`,
`0=recurring`), day-of-week bits (Sun–Sat), hour, minute, and action
(`0=press, 1=on, 2=off`). One-off alarms are therefore possible at the firmware level.

### 2.2 Two-stage split: fuzzy LLM, then deterministic code

Per the project's founding principle: *LLMs are great at fuzzy language, unreliable
at exact protocols.* The LLM does **only** natural-language → structured JSON.
Everything after (validate, encode, write, verify) is plain, tested, deterministic
code. **No agent / no LLM-driven tool orchestration** — the physical-device steps
must be predictable and testable without the model.

### 2.3 LLM provider: OpenAI GPT

The parser uses the OpenAI API (user has credit; cost per parse is a fraction of a
cent). Uses structured JSON output to force a valid response shape. The parser is
isolated behind a clean interface, so swapping providers later is a one-file change.
Reads `OPENAI_API_KEY` from the environment. Exact (small, cheap) model name to be
confirmed at implementation.

### 2.4 Interface phasing

- **Phase 1 (now):** command-line tool on the Mac, calling the core.
- **Phase 2 (later, easy):** a small web page served by the Mac on the home WiFi,
  so the user can type the prompt from their phone *while home*; the Mac does the
  Bluetooth write. Optionally, an MCP server front-door so the user can drive the
  same core by chatting with Claude. Both are thin wrappers over the same core —
  no rewrite. Truly-remote ("from anywhere") is out of scope for Option B by design.

## 3. Architecture

```
prompt ──▶ [1. Parser (GPT)] ──▶ Schedule JSON ──▶ [2. Validator] ──▶ [read-back → user confirms]
                                                                              │ approved
                                                        [4. Bluetooth writer] ◀── [3. Encoder]
                                                                │
                                                        Bot stores alarms, runs them itself
```

Two supporting pieces:
- **Device registry** — the single source of truth for "what Bots exist."
- **Core function** `apply_schedule(prompt)` — ties stages 1→4 together; every
  interface (CLI now, web/MCP later) calls this same function.

### 3.1 Component responsibilities

| # | Component | Input → Output | Depends on |
|---|-----------|----------------|------------|
| 1 | **Parser** | prompt text (+ registry names injected) → `Schedule` JSON | OpenAI API, registry |
| 2 | **Validator** | `Schedule` → ok / loud error | registry |
| — | **Read-back** | `Schedule` → human sentence (deterministic, no LLM) | — |
| 3 | **Encoder** | `Event` → Bot alarm bytes | — |
| 4 | **Bluetooth writer** | per-Bot alarm set → written+verified on device | bleak, registry |

## 4. Data model

```
Event          = { time: "HH:MM", action: "on"|"off"|"press", days: [sun..sat] }
DeviceSchedule = { device: "<canonical name>", events: [ Event, ... ] }   # one Bot
Schedule       = { schedules: [ DeviceSchedule, ... ] }                   # many Bots
```

A single prompt can cover several Bots; each Bot gets its own independent alarm list.

### 4.1 Device registry (the only file edited to add a Bot)

```yaml
devices:
  living_room:
    aliases: ["living room", "סלון", "salon"]
    ble_id: "<CoreBluetooth UUID on macOS — discovered by scanning>"
  dining:
    aliases: ["dining", "פינת אוכל", "dining nook"]
    ble_id: "..."
  ac:
    aliases: ["ac", "air conditioner", "מזגן"]
    ble_id: "..."
```

Adding a Bot = one block here. Parser (knows aliases), Validator (valid names),
and Writer (name → BLE id) all read from this.

> **macOS note:** macOS does not expose a Bot's `XX:XX` Bluetooth address; it uses
> an internal CoreBluetooth UUID. The registry stores that UUID, discovered by scanning.

Known devices (from live connectivity test on 2026-07-05, via cloud — BLE ids TBD by scan):
`סלון` `F2B200463779`, `פינת אוכל` `F2B201C66779`, `מזגן` `F2B206464C6C`,
Hub Mini `FAEE46B6877F`.

## 5. The Parser (LLM stage)

- One OpenAI call with a fixed system prompt; structured JSON output enforced.
- System prompt rules: output only JSON matching the schema; understand Hebrew &
  English; map spoken names → canonical device names using the injected registry
  list; every on and every off is its own event; "every day"/unstated → all 7 days;
  expand day ranges; 24-hour time.
- **The LLM is told the limits (max 5 alarms per Bot) so it can give helpful
  messages, but it must never silently drop events to fit.** Enforcement is the
  Validator's job, not the LLM's.
- **Read-back** is generated from the JSON by plain code (not a second LLM call),
  so it is 100% faithful to what will be written. It is the user's confirmation gate.

## 6. Error handling

| Where | Failure | Behavior |
|-------|---------|----------|
| Setup | missing `OPENAI_API_KEY` | stop at startup, clear message |
| Parser | invalid JSON | caught (rare with structured output) → "couldn't understand, reword" |
| Parser | unknown device name | stop: name not in registry, list known names |
| Validator | >5 alarms for a Bot | stop loudly, name the Bot and counts |
| Validator | bad time / day | stop, name the offending value |
| User | read-back wrong | user declines at confirm gate → nothing written |
| Bluetooth | Bot not found / out of range | clear message ("are you home / powered?") + retry |
| Bluetooth | connection drops mid-write | atomic full-set write (below) prevents half-state |

**Atomic write + verify:** always write a Bot's *complete* alarm set in one `0x09`
operation (never incremental), then read the alarms back and compare to intended.
"Success" means verified, not hoped.

## 7. Testing

- **Pure unit tests (no hardware, no LLM, instant), written test-first:** Validator
  (5-limit, times, days), Encoder (event → exact bytes), Read-back formatter, alias
  resolution. This is the bulk of the logic.
- **Parser tests:** recorded prompt → expected-JSON fixtures; a couple of live smoke tests.
- **Dry-run mode (default):** runs parse → validate → read-back and prints exactly
  what *would* be written. Zero risk; day-one usable.
- **Bluetooth:** verified by the spike (below) + manual physical confirmation
  (the physical world can't be unit-tested).

**Defense in depth:** dry-run default → read-back approval → hard validation gate →
atomic full-set write → read-back-and-verify.

## 8. First implementation step — the Bluetooth spike

Before building the full writer, de-risk with a spike:
**`bleak` → scan for and connect to one Bot → send `0x09` with a single test alarm
(e.g. "press 2 minutes from now") → confirm the Bot physically fires it.**

This single test resolves the residual unknowns at once: which library works on
macOS, the exact alarm byte format on the real device, and the CoreBluetooth UUID
discovery. Stages 1–3 do not depend on this and can be built and tested in parallel
via dry-run.

## 9. Security

- Credentials (`OPENAI_API_KEY`, and any SwitchBot cloud token if used for testing)
  loaded from environment variables only; never hardcoded; secrets file gitignored.
- The SwitchBot cloud token/secret used during connectivity testing on 2026-07-05
  were pasted into a chat and **must be rotated** in the app's Developer Options.

## 10. Out of scope (YAGNI)

- Changing schedules while away from home (a physical limit of Option B).
- An always-on server / cloud dispatch (Option A) — revisit only if daily-changing
  one-off schedules become a real need.
- Controlling the Hub Mini's IR remotes (none configured).
```
