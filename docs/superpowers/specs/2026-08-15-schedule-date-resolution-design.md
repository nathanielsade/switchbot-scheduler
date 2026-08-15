# Schedule date resolution — design

**Date:** 2026-08-15 · **Branch:** `fix/schedule-dates` · **Status:** approved design, pending plan

## Problem

On Friday 2026-08-14 at 17:21, Netanel asked Menashe to turn on three lights and the AC
**tomorrow** at 18:30. The lights came on **that same evening**, and the timer that ended up
stored was a **weekly recurring Friday** timer — so on Saturday (the day actually requested)
nothing fired at all.

Evidence from the box (`journalctl -u home-agent`, `home_agent.db`):

- 17:21 — four jobs added with trigger `date[2026-08-14 18:30:00 +03:00]` — **today**, not tomorrow.
- 18:30:00 — all four fired. Lights on, AC pressed.
- 18:32:51 — after a correction attempt, rows 40–43 were written as
  `days='fri', once=0` — a **recurring weekly** timer.
- 18:33 — Menashe replied that he had rescheduled the timer to Saturday. **No tool call was made**;
  the logs show zero scheduler activity after 18:32:51. The claim was fabricated.

### Root cause

Three defects, in order of importance.

**1. The tool cannot express "tomorrow."** `schedule_device` accepts only `time` and an optional
`days`. `_schedule_impl` (`src/home_agent/schedules.py:88-93`) has exactly two branches:

```python
if raw_days:
    days, once = _normalize_days(raw_days), False   # any day given ⇒ recurring, always
else:
    day, fire_at = _one_time_target(time_str, now_fn())
    days, once = [day], True
```

`_one_time_target` resolves to *"today if the time is still ahead, else tomorrow"*. At 17:21,
18:30 was still ahead, so "tomorrow 18:30" silently became **today** 18:30. And because passing
any day forces `once=False`, **"one-time on a named day" is unrepresentable** — once the model
needed to name a day, recurring was the only reachable state.

This is not a model failure. Every path the API offered was wrong.

**2. The readback carries no date.** `readback()` / `describe_days()`
(`src/switchbot_scheduler/readback.py:17-24`) print weekday names and the word "once" only;
`fire_at` is stored but never displayed. When Menashe listed the timers he saw `fri` with no
date and captioned it "מחר" — the off-by-one was invisible to him and to the user across four
exchanges.

**3. No grounding, and an unverified success claim.** Nothing in the tool description or system
prompt requires resolving a relative day against the real clock, so the model guessed "tomorrow
is Friday" while today *was* Friday. Separately, it reported a schedule change it never made.

Note the hardware was never the constraint: `encode_alarm`
(`src/switchbot_scheduler/encoder.py:9-24`) sets bit 7 for `once` **on top of** the weekday mask,
so "one-time on a specific weekday" is natively supported by the Bot protocol.

## Goals

1. "מחר" can never resolve to today. Relative-day words resolve deterministically in Python.
2. One-time and recurring become an explicit, independent choice.
3. Every confirmation and listing states the resolved **date**, so a wrong day is visible at once.
4. Recurring timers are opt-in and unmistakably labelled.
5. Menashe never claims a timer was set, changed, or cancelled without a successful tool call.

## Non-goals

- Changing how timers *fire* (`CloudScheduler`, BLE programming) — untouched.
- Reminders, notifications, or non-device scheduling.
- Multi-day or date-range schedules ("every day next week").
- Absolute calendar dates in user speech ("ב-3 בספטמבר"). Deferred until asked for.

## Design

### Module boundaries

`src/switchbot_scheduler/` stays **unmodified**, per CLAUDE.md ("reused, not modified"). Its
`Event` has no date field and should not gain one: a date is a `home_agent` store concept
(`fire_at`), not part of the Bot wire protocol.

Consequence: `get_schedule` stops calling `switchbot_scheduler.readback` and uses a new
date-aware formatter local to `home_agent/schedules.py`. `readback` itself is left in place for
its existing `switchbot_scheduler` consumers.

### Tool schema

```
schedule_device(device, action, time, when=?, days=?)
```

| Field | Values | Meaning |
|---|---|---|
| `when` | `soonest` \| `today` \| `tomorrow` \| `sun`…`sat` | **one-time** timer |
| `days` | weekday list, or `daily` / `weekdays` / `weekends` | **recurring weekly** timer |

- `when` and `days` are mutually exclusive; supplying both is an error.
- Supplying neither defaults to `when="soonest"` — used when the user names a time but no day
  at all ("תדליק את האור ב-18:30").
- The model's only job is language → token (`"מחר"` → `"tomorrow"`). It performs **no date
  arithmetic**, per CLAUDE.md ("never put cost/date math in the model").

### Resolution rule

One rule everywhere: **the next occurrence of the named day, counting today if the time is still
ahead.** Implemented as `_resolve_fire_at(when, time_str, now) -> datetime`:

- `soonest` (the default when no day is named) — today at `time` if still ahead, otherwise
  tomorrow. This is the pre-existing `_one_time_target` behaviour, kept because it matches what a
  person usually means. It is safe now only because the resolved date is always stated back, so a
  wrong assumption is visible immediately.
- `today` — today at `time`; if that moment has already passed, return an error
  ("that time already passed today") rather than rolling to another day. Distinct from `soonest`:
  here the user *named* today, so silently moving to another day would contradict them.
- `tomorrow` — `now.date() + 1 day` at `time`, unconditionally. No branch on the current time, so
  "מחר" can never collapse into today.
- `sun`…`sat` — the nearest occurrence of that weekday at `time` that is strictly in the future;
  0–7 days out, where today counts when the time is still ahead.

The resolved `datetime` is stored in `fire_at`; `days` records that date's weekday name and
`once=True`. `CloudScheduler.schedule_row` already branches on `row["once"]` and uses `fire_at`
for one-time jobs, so it needs no change. BLE devices encode as `once` + weekday mask, which
`encode_alarm` already supports.

### Output format

Both the create confirmation and `get_schedule` render the same way. Tools return English; the
model renders Hebrew, as elsewhere in this codebase.

- One-time: `kitchen: on 2026-08-15 (Sat) 18:30 — ONE-TIME`
- Recurring: `kitchen: on 18:30 — RECURRING WEEKLY (fri) — repeats every week`

Recurring is labelled loudly enough that a mistaken recurring timer is obvious in the reply
without requiring a separate confirmation turn.

### System prompt

Two additions to `prompts.FAMILY_SYSTEM_PROMPT`, kept **digit-free** so
`test_system_prompt.py::test_prompt_is_nonempty_and_stable` continues to pass, and appended as
fixed text so the prompt stays byte-stable across turns:

1. Device timers are **one-time by default**. Use a recurring timer only when the user explicitly
   asks for repetition ("כל יום", "כל שישי", "every week").
2. Never state that a timer was set, changed, or cancelled unless the corresponding tool call
   returned success.

### Error handling

| Condition | Behaviour |
|---|---|
| `when` and `days` both given | Error: pick one-time or recurring, not both. |
| Unknown `when` token | Error listing valid tokens. |
| `when="today"` but the time already passed | Error naming the passed time; no silent roll-forward. |
| Malformed `time` | Existing validator error path, unchanged. |
| >5 timers on one device | Existing `ScheduleError` from `validate`, unchanged. |
| Cloud scheduling raises | Existing rollback (`store.remove_id`) path, unchanged. |

## Testing

Frozen clock (`now_fn`) throughout; no network, no BLE — consistent with the existing suite.

**Regression (the reported bug):**
- "tomorrow 18:30" asked Fri 2026-08-14 17:21 → `fire_at` is **2026-08-15**, `once=True`.
- The resulting timer does not fire on the 14th.

**Resolution rule:**
- `when="fri"` on a Friday at 17:00 → today; at 19:00 → +7 days.
- `when="tomorrow"` at 23:00 and at 01:00 → both resolve to the following calendar day.
- `when="today"` with a passed time → error, nothing written to the store.
- `when="soonest"` (and `when` omitted entirely) at 17:21 for 18:30 → today; at 19:00 → tomorrow.

**One-time vs recurring:**
- `when` and `days` together → error, nothing written.
- `days=["fri"]` → `once=False`, `fire_at` is None.
- `when="fri"` → `once=True` with a real `fire_at`; round-trips through `ScheduleStore` and
  produces `repeat_byte` with both bit 7 and the Friday bit set via `encode_alarm`.

**Formatting:**
- One-time listing contains the ISO date; recurring listing contains "RECURRING WEEKLY".

**Prompt:**
- `FAMILY_SYSTEM_PROMPT` stays digit-free and byte-stable (existing tests).

**Baseline:** all 346 existing tests stay green.

## Migration

None required. The `schedules` table was emptied on 2026-08-15 when the stale recurring Friday
timers from this incident were cancelled, and it is currently empty on the box.

## Rollout

Deploy to the box via the `deploy-box` skill, then verify against the real bug: schedule
something for "מחר" and confirm both the reply and `get_schedule` show tomorrow's date.
