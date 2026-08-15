# Schedule date resolution — design

**Date:** 2026-08-15 · **Branch:** `fix/schedule-dates` · **Status:** revised after review (v2)

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

**1. The tool cannot express "tomorrow."** `schedule_device` accepts only `time` and an optional
`days`. `_schedule_impl` (`src/home_agent/schedules.py:95-100`) has exactly two branches:

```python
raw_days = args.get("days") or []
if raw_days:
    days, once, fire_at = _normalize_days(raw_days), False, None   # any day ⇒ recurring, always
else:
    day, fire_at = _one_time_target(time_str, now_fn())
```

`_one_time_target` (`schedules.py:33`) resolves to *"today if the time is still ahead, else
tomorrow"*. At 17:21, 18:30 was still ahead, so "tomorrow 18:30" silently became **today**. And
because passing any day forces `once=False`, **"one-time on a named day" is unrepresentable** —
once the model needed to name a day, recurring was the only reachable state.

This is not a model failure. Every path the API offered was wrong. **The central design
requirement that follows: any request the user can plausibly make must have exactly one correct
representation, or the tool must force a clarifying question.** A model handed an unrepresentable
request will invent something.

**2. The readback carries no date.** `readback()` / `describe_days()`
(`src/switchbot_scheduler/readback.py:6,17`) print weekday names and the word "once" only;
`fire_at` is stored but never displayed. Menashe saw `fri` with no date and captioned it "מחר" —
the off-by-one was invisible across four exchanges.

**3. No grounding, and an unverified success claim.** Nothing required resolving a relative day
against the real clock, so the model guessed "tomorrow is Friday" while today *was* Friday.
Separately, it reported a change it never made.

The hardware was never the constraint at the code level: `encode_alarm`
(`src/switchbot_scheduler/encoder.py:9-24`) sets bit 7 for `once` on top of the weekday mask.
See "Open hardware question" below for the caveat.

## Goals

1. "מחר" can never resolve to today. Relative-day words resolve deterministically in Python.
2. Every request the user can plausibly phrase is representable, or triggers a clarifying question.
3. Recurring timers are hard to create by accident — a separate deliberate act.
4. The resolved **date** reaches the user in Hebrew, not just the tool output.
5. Fired one-time timers never resurrect.
6. Menashe never claims a timer was set, changed, or cancelled without a successful tool call.

## Non-goals

- Changing how timers *fire* (`CloudScheduler` job mechanics, BLE write path).
- Reminders, notifications, or non-device scheduling.
- Absolute calendar dates in user speech ("ב-3 בספטמבר"). Deferred until asked for.
- Relative *times* ("בעוד חמש דקות"). The existing tool description tells the model to compute
  the HH:MM itself, which contradicts CLAUDE.md's "no clock math in the model". Pre-existing and
  out of scope here; tracked as a follow-up.

## Design

### Module boundaries

`src/switchbot_scheduler/` stays **unmodified**, per CLAUDE.md ("reused, not modified"). Its
`Event` has no date field and should not gain one: a date is a `home_agent` store concept
(`fire_at`), not part of the Bot wire protocol. `get_schedule` therefore stops calling
`switchbot_scheduler.readback` and uses a date-aware formatter local to `home_agent/schedules.py`.
Verified safe: every other `readback`/`describe_days` consumer lives inside `switchbot_scheduler`
(`core.py`, `web/app.py`, and that package's own tests).

### Two tools, not one

The decisive change. `days` is **removed from `schedule_device` entirely** and recurring moves to
its own tool:

| Tool | Produces |
|---|---|
| `schedule_device(device, action, time, when=?, in_days=?)` | **one-time** timer |
| `schedule_recurring_device(device, action, time, days)` | **recurring weekly** timer |

Rationale: with both on one tool, "תדליק ביום שישי" has two equally natural encodings —
`when="fri"` (correct) and `days=["fri"]` (recurring forever) — and nothing structural
distinguishes them. Splitting the tools means a one-time weekday request has exactly one
representation, and recurring requires deliberately reaching for a differently-named tool whose
description opens with *"Use ONLY when the user explicitly asked for repetition (כל יום / כל
שישי / every week). This is rare."*

### One-time: when to fire

`when` is a **required** enum — never inferred from omission, because models over-fill optional
enums and a spurious `today` would hard-error a request that named no day at all.

| `when` | Meaning |
|---|---|
| `soonest` | today at `time` if still ahead, else tomorrow. **The value to use when the user named no day.** |
| `today` | today at `time`; error if already passed |
| `tomorrow` | the next calendar day at `time`, unconditionally |
| `sun`…`sat` | nearest occurrence of that weekday, today counting only if `time` is still ahead |

`in_days` (integer, 0–30) is the escape hatch for everything the enum cannot say, and is mutually
exclusive with `when`: `מחרתיים` → 2, `בעוד שלושה ימים` → 3, `בעוד שבוע` → 7. Counting the days
the user *named* is language → number, not calendar arithmetic, so it stays within CLAUDE.md's
rule. Without it, "מחרתיים" would force the model to name a weekday — which requires knowing
today's weekday, which is exactly the guess that caused this bug.

Anything still unrepresentable ("ביום שישי הבא" — ambiguous even between people) **must produce a
clarifying question, never a guess.** Stated in the tool description and the system prompt.

Boundary rule: a target exactly equal to `now` counts as passed (`target <= now`), matching the
existing `_one_time_target`.

### Timezone and `fire_at` format

Load-bearing, and currently wrong. `build_schedule_tools` is called at `telegram_app.py:127`
with **no `now_fn`**, so it falls back to `schedules._now()` = `datetime.now().astimezone()` —
the *host system* tz, as a fixed-offset `timezone`, while `CloudScheduler` is built with
`ZoneInfo(config.home_tz)` (`telegram_app.py:118-119`). Two clocks that disagree whenever the box
tz is UTC (a common Ubuntu default).

The design pins all three:

- `build_schedule_tools` is wired with `now_fn=lambda: datetime.now(ZoneInfo(config.home_tz))`.
- `_resolve_fire_at` constructs the target from `date` + `time` + `ZoneInfo(home_tz)` — never
  `replace()`/`timedelta` arithmetic on a fixed-offset value, which would stamp tomorrow's
  wall-clock with today's offset and fire an hour early across the October DST boundary.
- `fire_at` is **always** stored tz-aware, in HOME_TZ, at second precision. `remove_expired`
  compares ISO strings lexicographically (`schedule_store.py:66`), which is only correct if every
  row shares one canonical format.

### Fired timers must not resurrect

`_program_device` rebuilds a Bot's entire alarm set from `store.list(device)`, and `remove_expired`
is called *only* from `_get_schedule_impl` and `CloudScheduler.reconcile`. So a fired one-time row
survives (BLE has no fire callback at all), and the next unrelated write to that device re-arms it
for the same weekday next week. It also silently consumes the `MAX_ALARMS = 5` budget.

Fix: `_schedule_impl` and `_cancel_impl` both call `store.remove_expired(now_fn().isoformat())`
before touching the store. This is a pre-existing bug that the new design would have promoted from
rare to routine.

### Cancel must be unambiguous

`_cancel_impl` matches on `device` + `time` only (`schedules.py:172`), so with one-time timers now
the norm, "Saturday 18:30" and "next Friday 18:30" are indistinguishable and one cancel kills both.
`cancel_schedule` gains an optional `when`/`in_days` (same resolution rule) to disambiguate, and
its confirmation states the date of every row removed and their count.

### Output format

Tools return English; the model renders Hebrew, as elsewhere.

- One-time: `kitchen: on at 18:30 on 2026-08-15 (Sat) — ONE-TIME`
- One-time landing a week out: same, plus ` — NEXT WEEK` (so a weekday token that quietly rolled
  +7 is visible).
- Recurring: `kitchen: on at 18:30 — RECURRING, every fri`
- Cancel: `kitchen: cancelled 1 timer — on at 18:30 on 2026-08-15 (Sat)`

### System prompt

Three additions to `prompts.FAMILY_SYSTEM_PROMPT`, kept **digit-free** (enforced by
`test_system_prompt.py::test_prompt_is_nonempty_and_stable`) and appended as fixed text so the
prompt stays byte-stable across turns (`test_run_turn_sends_identical_system_prompt_each_turn`):

1. **Device** timers are one-time by default; use `schedule_recurring_device` only when the user
   explicitly asks for repetition. (Scoped to device on/off/press timers — the prompt also governs
   `schedule_clean`, which is inherently recurring.)
2. When confirming or listing a timer, always state the calendar date the tool returned, never
   only a relative word. *(Without this, the model receives the date and still replies "מחר" —
   the original invisibility failure.)*
3. Never state that a timer was set, changed, or cancelled unless the tool call returned success.

Rule 3 is prompt-only and therefore best-effort; there is no clean structural enforcement, since
the Hebrew reply is model-generated. The stronger lever is removing the *incentive* to fabricate —
the model lied during a correction because no representable fix existed. Goals 2 and 3 address the
cause. Additionally, each turn logs which tools ran, so a recurrence is detectable in
`journalctl` rather than only when a light fails to come on.

### Error handling

| Condition | Behaviour |
|---|---|
| `when` and `in_days` both given | Error naming which to keep. |
| Unknown `when` token | Error listing valid tokens. Strict lowercase; `"friday"`/`"שישי"` rejected. |
| `when="today"`, time already passed | Error that also instructs the retry: *if the user did not explicitly say היום, retry with `soonest` or `tomorrow`*. Self-healing, so a late-evening "18:30" isn't a dead end. |
| `in_days` out of range | Error; ask the user for an explicit day. |
| Malformed `time`, >5 timers, cloud failure | Existing paths unchanged (`validate`, `store.remove_id` rollback). |
| Resolved moment already past at scheduling time | `CloudScheduler.schedule_row` currently `return`s silently (`cloud_scheduler.py:32-33`) while the tool reports ✅ — a timer that never fires. It must raise so `_schedule_impl`'s existing rollback runs. `reconcile` relies on the silent skip, so it gets its own guard. |

## Open hardware question (gates BLE only)

`encode_alarm` sets bit 7 alongside the day mask, but that the Bot firmware reads this as "fire
once **on that weekday**" is **unverified**. Every alarm this agent has ever written had
`days == [weekday of the next occurrence]`, so bits and intent never disagreed. Setting Friday for
Saturday is new. If bit 7 instead means "fire at the next HH:MM, then delete", a BLE one-time fires
**tonight** — the original bug, unfixed.

This does not block: all five devices in the box's `devices.yaml` are currently `cloud_id`-routed,
and cloud timers fire from `CloudScheduler` on an exact datetime. The plan must (a) include a
hardware spike before any device returns to BLE, and (b) note the fallback if the semantic fails —
program BLE one-timers as recurring-on-that-weekday plus a store-side auto-cancel after firing.

## Testing

Frozen clock (`now_fn`) throughout; no network, no BLE.

**Regression (the reported bug):** "tomorrow 18:30" asked Fri 2026-08-14 17:21 → `fire_at` is
**2026-08-15**, `once=True`, and nothing fires on the 14th. Multi-device variant: all four devices
in one turn, since that was the real request.

**Resolution:** `when="fri"` on a Friday at 17:00 → today; at 19:00 → +7 days and the output says
NEXT WEEK. `tomorrow` at 23:00 and 01:00 → both the following calendar day. `soonest` at 17:21 →
today; at 19:00 → tomorrow. `today` with a passed time → error, nothing stored. `in_days=2` →
+2 days. `when` + `in_days` → error, nothing stored.

**Timezone:** resolution uses HOME_TZ regardless of host tz; a "tomorrow" spanning Israel's
October DST end lands at the right *wall-clock* time; `remove_expired` correctly drops a row
written on the other side of that transition.

**No resurrection:** a one-time row with a past `fire_at` plus a new schedule on the same device →
old row gone from the store and absent from the alarms written.

**Recurring:** `schedule_recurring_device` sets `once=False`, `fire_at=None`, and the output
contains "RECURRING". `schedule_device` has no `days` property in its schema.

**Cancel:** two one-time timers at the same HH:MM on different dates are individually cancellable;
the confirmation names dates and a count.

**Model-facing:** a `make_fake_client` test asserting the schema the model actually sees (`when`
required, valid enum values, `days` absent from `schedule_device`), and that a tool error string
is fed back so the model can recover.

**Prompt:** stays digit-free and byte-stable (existing tests).

**Baseline:** the existing suite stays green.

## Migration

None. The `schedules` table was emptied on 2026-08-15 when the stale recurring Friday timers from
this incident were cancelled, and is currently empty on the box.

## Rollout

Deploy via the `deploy-box` skill, then verify against the real bug: schedule something for "מחר",
confirm the Hebrew reply states tomorrow's date, `get_schedule` agrees, and **the device actually
fires tomorrow and not today** — the store being right is not sufficient evidence.
