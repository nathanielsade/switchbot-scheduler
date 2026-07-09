# Scheduling via on-device Bot timers (agent tools) — design

- **Date:** 2026-07-09
- **Roadmap:** the scheduling capability (relates to Epic D "schedule_task" in `docs/ROADMAP.md`),
  built now on the **on-device-timer** path so it works without the always-on box.
- **Status:** approved design, pre-implementation.
- **Predecessors:** Epic 1 agent core; home-mcp control tools
  (`docs/superpowers/specs/2026-07-09-home-mcp-control-tools-design.md`) — same in-process `Tool`
  pattern and `switchbot_scheduler` reuse.

## Goal

Let the family agent set, list, and cancel **timed** actions on the SwitchBot Bots from natural
language ("turn on the dining light in 5 minutes", "living room every day at 18:00", "what's the
kitchen set to do?", "cancel that"). Timers are programmed into each **Bot's own built-in clock**, so
they fire even when the Mac / phone / internet are all off — no always-on computer required.

## Decision: on-device Bot timers (not a cron/always-on scheduler)

The user has no always-on home box yet, and wants scheduling working today. SwitchBot Bots hold their
own alarm table (max 5), which `switchbot_scheduler` already programs over BLE (`ble_writer.write_alarms`,
`encode_alarm`). We expose that as agent tools. Consequence & limitations accepted:
- Timers can only **flip a switch** (on/off/press) at a clock time by weekday. No reminder *messages*,
  no conditional logic — those need the always-on box (roadmap Epic D / cron) and are **deferred**.
- Bots **cannot be read back** — so the agent keeps its own record of what it programmed (see below).

## Scope

**In scope — 3 tools, covering all 4 requested abilities:**
1. `schedule_device(device, action, time, days?)` — one-time (days omitted) and recurring (days given).
2. `get_schedule(device?)` — report what is scheduled.
3. `cancel_schedule(device, time?)` — cancel one timer or all of a device's timers.

**Out of scope (deferred):** reminder/message scheduling, "every day at *sunset*" (needs sun-time
computation + the box), conditional/if-this-then-that, and reading timers set outside the agent.

## The core architectural constraint (why there's a store)

Two hard facts about SwitchBot Bots drive the whole design:
1. **No readback** — there is no reliable way to ask a Bot "what alarms do you hold?"
2. **Whole-set writes** — `write_alarms(ble_id, alarms)` **replaces** the Bot's entire alarm table in
   one shot (clock + count + all alarm frames).

Therefore the agent keeps its **own SQLite record**, which is the **source of truth** for every timer it
set. Each mutating operation is: update the record → rebuild that device's **complete** alarm list from
the record → validate (≤5) → write the whole list to the Bot → persist only on write success.
`get_schedule` reads the record. Clearing a device writes an **empty** list to the Bot.

## Architecture & components

**New `home_agent` modules (mirroring existing patterns):**
- `src/home_agent/schedule_store.py` — `ScheduleStore`, SQLite, **connection-per-operation** (thread-safe,
  exactly like `memory.Conversation`; PTB runs handlers in a worker thread). Table `schedules`:
  `id INTEGER PK, device TEXT, action TEXT, time TEXT, days TEXT (csv), once INTEGER, set_at TEXT DEFAULT CURRENT_TIMESTAMP`.
  Methods: `add(device, action, time, days, once)`, `list(device=None) -> list[row]`,
  `remove(device, time=None) -> int` (count removed), `days` stored as a comma-joined subset of `DAYS`.
- `src/home_agent/schedules.py` — `build_schedule_tools(registry, store, *, write_fn=None) -> list[Tool]`
  returning the three `Tool` objects, plus the impls and a real `_program_bot(ble_id, alarms)` that wraps
  `asyncio.run(write_alarms(ble_id, alarms))` (BLE, production only; `write_fn` seam injected in tests).

**Reuse from `switchbot_scheduler` (no changes to it):**
- `model.Event(time, action, days, once)`, `DeviceSchedule`, `Schedule`, `DAYS`.
- `encoder.encode_alarm(event, inverted)` — weekday bits, one-time bit 7, and the inversion swap.
- `validator.validate(schedule, registry)` (raises `ScheduleError`; `MAX_ALARMS = 5` per device).
- `ble_writer.write_alarms(ble_id, alarms)` — programs a Bot's full alarm set (empty list clears it).
- `readback.readback(schedule)` / `describe_days` — human-readable schedule text for `get_schedule`.

**Wiring:** `build_application` constructs the `ScheduleStore(config.db_path)` and composes
`tools = list(DEFAULT_TOOLS) + build_home_tools(registry) + build_schedule_tools(registry, store)`.
(Schedule store shares the existing `home_agent.db`.)

## Rebuild-and-write helper (the heart of every op)

For a device, after the store is updated:
1. Rows → `events = [Event(time, action, days.split(","), bool(once)) for row]` (empty days list allowed
   only for a fully-cleared device, i.e. no events).
2. `validator.validate(Schedule([DeviceSchedule(device, events)]), registry)` — friendly `ScheduleError`
   message on the 5-cap / bad time / bad day.
3. `alarms = [encode_alarm(e, inverted=registry.is_inverted(device)) for e in events]`.
4. `write_fn(registry.ble_id(device), alarms)` — writes the whole set (empty list clears the Bot).
5. On success the store row(s) are already persisted; on write failure, roll back the store change and
   return a readable error.

## The three tools (schemas = per-tool instructions)

### `schedule_device(device, action, time, days?)`
- **description:** "Schedule a SwitchBot device to turn on/off (or press) at a clock time, programmed into
  the device's own timer so it fires even if this computer is off. `time` is 24-hour `\"HH:MM\"`. Omit
  `days` for a ONE-TIME timer (fires at the next occurrence of that time); give `days` for a RECURRING
  timer. For relative requests like 'in 5 minutes', first call `get_current_time` and compute the `HH:MM`.
  Each device can hold at most 5 timers. Report back what you scheduled, in the user's language."
- **params:** `device` (string, required — name/alias, Hebrew/English), `action` (enum `on|off|press`,
  required — "the AC only honors press"), `time` (string, required — `\"HH:MM\"` 24-hour),
  `days` (array of strings, optional — any of `sun mon tue wed thu fri sat`, or the words
  `daily`/`weekdays`/`weekends`; omit for one-time). `additionalProperties: false`.
- **impl:** resolve device (unknown → friendly list). Normalize `days`: `daily`→all 7, `weekdays`→mon-fri,
  `weekends`→sat,sun, else the given DAYS subset. **One-time** (no days): `once=True`, and `days` = the
  single weekday of the next occurrence — computed from `datetime.now().astimezone()`: today if `time`
  is still ahead, else tomorrow. **Recurring:** `once=False`, `days` = the normalized set. Add to store →
  rebuild-and-write (above). Success → e.g. `"מטבח: on at 18:05 (one-time) ✅"` style summary; the model
  relays in Hebrew.

### `get_schedule(device?)`
- **description:** "List the timers currently programmed (from what I have set). Use when the user asks
  what's scheduled, for one device or all. This reflects what I programmed; timers set outside me (e.g.
  the SwitchBot app) won't appear."
- **params:** `device` (string, optional — omit for all). `additionalProperties: false`.
- **impl:** read store rows (all or one device); **expire** one-time rows whose next occurrence is in the
  past (delete them from the store, best-effort, since we can't confirm the Bot fired). Build a `Schedule`
  and format via `readback.readback` grouped by device. Empty → "nothing scheduled".

### `cancel_schedule(device, time?)`
- **description:** "Cancel scheduled timers. Give a device to clear all its timers, or a device + time to
  cancel just that one. Reprograms the device so the cancelled timer no longer fires."
- **params:** `device` (string, required), `time` (string, optional — `\"HH:MM\"`).
  `additionalProperties: false`.
- **impl:** resolve device. `store.remove(device, time)` → if 0 removed, "nothing matched". Else
  rebuild-and-write the device's remaining timers (empty list clears the Bot). Report how many cancelled.

## Data flow (one-time example)

`"תדליק את פינת האוכל בעוד חמש דקות"` → model calls `get_current_time` (→ 18:24) → computes 18:29 →
calls `schedule_device("פינת אוכל", "on", "18:29")` → resolve→`dining`; one-time, days=[thu]; store.add;
rebuild dining's set (this one timer); `validate` ok; `encode_alarm`; `write_fn(dining_ble_id, [alarm])`
programs the Bot → tool returns `"dining: on at 18:29 (one-time) ✅"` → model replies in Hebrew. At 18:29
the dining Bot fires **on its own**.

## Error handling

- Unknown device → friendly list of known devices (no exception).
- 5-timer cap / bad time / bad day → `ScheduleError` caught → readable message ("that Bot already has 5
  timers — cancel one first").
- BLE write failure (Bot out of range) → roll back the store change, return "couldn't reach {device} —
  timer not set/cancelled"; the record stays consistent with the Bot.
- All inside `handle_message`'s existing try/except backstop.

## Known limitations (documented, accepted)

- The store is the agent's **belief**; timers set via the SwitchBot app are invisible and would be wiped
  by a rewrite. The user schedules only through the bot.
- One-time timers that already fired can't be confirmed cleared — expired from the store by time on
  `get_schedule`; a Bot could in rare cases still hold a just-fired one-time alarm until the next rewrite
  (SwitchBot one-time alarms self-disable, so this is cosmetic).
- Weekly recurrence only (no "every 2 days", no sunset). Reminders/messages excluded (need the box).

## Testing

No BLE in the automated suite — inject `write_fn(ble_id, alarms)` capturing calls:
- `schedule_store.py`: CRUD + cross-thread use (mirror `test_memory.py`'s thread test).
- `schedules.py`: one-time (asserts `once=True`, correct weekday, alarm code) with a **frozen "now"**
  (inject a clock or pass current time) so the weekday/day-roll is deterministic; recurring (weekday
  expansion incl. `daily`/`weekdays`/`weekends`); inversion (living_room on→code 2) and press-mode (ac→
  press) via the captured alarms; the **5-cap** rejection; add-then-add rebuilds the FULL set written to
  the Bot (not just the new one); `cancel_schedule` all vs one and the empty-clear write; `get_schedule`
  formatting + past one-time expiry; unknown-device path.
- Loop test: fake OpenAI client scripts a `schedule_device` call end-to-end.
- `build_application` composes the schedule tools alongside home + time tools.

**Determinism note:** `schedule_device`/`get_schedule` depend on the current time. The impls take an
injectable "now" (default `datetime.now().astimezone()`) so tests freeze it; production uses the default.

**Hardware verify (manual):** from Telegram, "turn on the kitchen in 2 minutes", watch the kitchen Bot
fire ~2 min later with the Mac's bot process **stopped** (proves the on-device timer, not the app).

## Definition of Done

- `schedule_store.py` + `schedules.py` implemented; 3 tools wired into the running bot.
- Full automated suite green (existing + new), no BLE/network in tests.
- Manual hardware check: a one-time timer set from Telegram fires on the Bot on its own.
- `switchbot_scheduler` unchanged.
