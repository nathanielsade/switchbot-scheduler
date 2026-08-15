# Schedule date resolution — design

**Date:** 2026-08-15 · **Branch:** `fix/schedule-dates` · **Status:** v4, ready for planning
(three adversarial review rounds)

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
| `schedule_device(device, action, time, when)` | **one-time** timer |
| `schedule_recurring_device(device, action, time, days, repetition_phrase)` | **recurring weekly** timer |

Rationale: with both on one tool, "תדליק ביום שישי" has two equally natural encodings —
`when="fri"` (correct) and `days=["fri"]` (recurring forever) — and nothing structural
distinguishes them. Splitting them means a one-time weekday request has exactly one
representation.

The split alone is not enough, though: `schedule_recurring_device(days=["fri"])` still encodes
"one-time Friday" wrongly, and that is the exact shape the model reached for during the incident
(rows 40–43). So the recurring tool takes a **required `repetition_phrase`** — the user's own
words that mean repetition ("כל יום", "כל שישי", "every week"). The impl rejects an empty or
whitespace-only value. A model that has to quote non-existent words is far likelier to fall back
to the one-time tool than one that merely reads a discouraging description.

This is deliberately a **nudge, not enforcement** — a model can invent a phrase at no cost. It
cannot be strengthened into verification: `Tool.impl(args: dict) -> str` has no access to the
user's message, so substring-matching against what was actually said is not implementable without
plumbing user text into every tool. **The plan must not attempt that.** The value is that the
failure mode is biased safely: a model unsure whether repetition was asked for falls back to the
one-time tool.

Description clauses (the model-facing surface, which CLAUDE.md says *is* the instruction). These
are the **added or changed** clauses — the existing `schedule_device` text
(`schedules.py:45-52`) is otherwise preserved verbatim, including the BLE-vs-cloud "fires even if
this computer is off" caveat, `time` being 24-hour "HH:MM", the five-timer cap, and the
"for 'in 5 minutes', call get_current_time and compute the HH:MM" guidance that the non-goals
above explicitly keep. The clause being *removed* is "Omit `days` for a ONE-TIME timer … give
`days` for a RECURRING timer", which no longer describes the tool.

- `schedule_device` — *"Schedule a device to turn on/off/press ONCE, at a clock time on a specific
  day. `when` says which day and is required; use `soonest` when the user named no day at all.
  Never guess a weekday — if the user's wording does not map cleanly onto a `when` value, or asks
  for more than a week ahead, ask them which day instead of choosing one."*
- `schedule_recurring_device` — *"Schedule a device to repeat WEEKLY on given days. Use ONLY when
  the user explicitly asked for repetition (כל יום / כל שישי / every week) — this is rare. For a
  single day, even a named weekday like 'ביום שישי', use schedule_device instead. Pass the user's
  own repetition words in `repetition_phrase`."*
- `cancel_schedule` — *"Cancel timers. Device alone clears all of them; add `time`, and `when` if
  two timers share a clock time on different dates. Reports the date of everything cancelled."*

### One-time: when to fire

`when` is a **required**, total enum — no optional companion field, and never inferred from
omission (models over-fill optional enums, and a spurious `today` would hard-error a request that
named no day at all).

| `when` | Meaning | Max horizon |
|---|---|---|
| `soonest` | today at `time` if still ahead, else tomorrow. **The value to use when the user named no day.** | +1 |
| `today` | today at `time`; error if already passed | 0 |
| `tomorrow` | the next calendar day at `time`, unconditionally | +1 |
| `day_after_tomorrow` | two calendar days ahead ("מחרתיים", "בעוד יומיים") | +2 |
| `in_a_week` | seven calendar days ahead ("בעוד שבוע") | +7 |
| `sun`…`sat` | nearest occurrence of that weekday, today counting only if `time` is still ahead | +7 |

An earlier draft used an integer `in_days` (0–30) instead of the last three rows. It was dropped
for two reasons, both disqualifying:

- **Unreachable.** With `when` required *and* mutually exclusive with `in_days`, no legal call can
  pass `in_days`. The contract contradicted itself.
- **Not representable on BLE.** `encode_alarm` emits only `{weekday mask | 0x80, hour, minute}` —
  there is no date on the wire. A target 10 days out stores only its weekday, so a Bot fires it
  **~3 days early**. Any horizon beyond 7 days is silently wrong on BLE.

Capping the enum at +7 makes every value round-trip through a weekday, so BLE and cloud agree.
"מחרתיים" is still representable without the model ever naming a weekday — which is the property
that matters, since naming a weekday requires knowing today's, and that guess caused this bug.

Anything unrepresentable (">7 days out", "ביום שישי הבא" — ambiguous even between people) **must
produce a clarifying question, never a guess.** Stated in the tool description and system prompt.

Boundary rule: at write time a target exactly equal to `now` counts as passed (`target <= now`),
matching the existing `_one_time_target`, so such a timer is never created. `remove_expired` uses
strict `<`, so a stored row whose moment later becomes exactly `now` survives that one comparison.
Harmless — the two are noted only so an implementer doesn't "fix" one into disagreeing with the
other.

### Timezone and `fire_at` format

Load-bearing, and currently wrong. `build_schedule_tools` is called at `telegram_app.py:127`
with **no `now_fn`**, so it falls back to `schedules._now()` = `datetime.now().astimezone()` —
the *host system* tz, as a fixed-offset `timezone`, while `CloudScheduler` is built with
`ZoneInfo(config.home_tz)` (`telegram_app.py:118-119`). Two clocks that disagree whenever the box
tz is UTC (a common Ubuntu default).

The design pins all four:

- `build_schedule_tools` is wired with `now_fn=lambda: datetime.now(ZoneInfo(config.home_tz))`.
- `_resolve_fire_at` constructs the target from `date` + `time` + `ZoneInfo(home_tz)` — never
  `replace()`/`timedelta` arithmetic on a fixed-offset value, which would stamp tomorrow's
  wall-clock with today's offset and fire an hour early across the October DST boundary.
- **`get_current_time` too.** `tools.py:15` also uses `datetime.now().astimezone()`, and it is the
  clock the *model* reads — it drives the model's own choice between `when="fri"` and `"sat"`. Its
  own comment records an earlier weekday-guessing bug. Pinning the scheduler's clock while leaving
  the model's on host tz is half a fix. `DEFAULT_TOOLS` is a module-level constant, so this needs
  a `build_time_tools(tz)` factory wired from `build_application`.
- `fire_at` is stored as **UTC** ISO with a `+00:00` suffix, and rendered in HOME_TZ for display
  only. This is forced by `remove_expired`, which compares ISO strings *lexicographically*
  (`schedule_store.py:66`): local-time strings sort by wall clock before the offset suffix is ever
  reached, so across Israel's October fall-back `…T02:30:00+03:00` sorts after the later instant
  `…T02:00:00+02:00` and an already-passed row fails to expire. UTC is the only format where
  lexicographic order equals instant order.

**Both sides of that comparison must be UTC.** Storage alone is not enough: `remove_expired` is
called today as `now_fn().isoformat()` from `schedules.py:155` and `cloud_scheduler.py:21`, which
under the pinned `now_fn` yields `+03:00`. Comparing a `+03:00` now against `+00:00` rows
lexicographically deletes live timers — e.g. `fire_at = 2026-08-15T22:00:00+00:00` (01:00 local on
the 16th) against `now = 2026-08-16T00:30:00+03:00` (21:30 UTC, half an hour *before* it fires)
compares as `"2026-08-15…" < "2026-08-16…"` and drops it. That is the same silent-wrong-date family
as the bug being fixed. So: **`remove_expired(now_iso)` takes a UTC ISO string**, both call sites
convert, and this is stated on the method's contract.

Anywhere a stored `fire_at` is matched against a *calendar date* — notably the cancel path below —
it must first be converted back to HOME_TZ. String-prefixing the UTC value would put a 01:00-local
timer on the previous day.

### Fired timers must not resurrect

`_program_device` rebuilds a Bot's entire alarm set from `store.list(device)`, and `remove_expired`
is called *only* from `_get_schedule_impl` and `CloudScheduler.reconcile`. So a fired one-time row
survives (BLE has no fire callback at all), and the next unrelated write to that device re-arms it
for the same weekday next week. It also silently consumes the `MAX_ALARMS = 5` budget.

Fix: `_schedule_impl` and `_cancel_impl` both expire stale rows before touching the store. This is
a pre-existing bug that the new design would have promoted from rare to routine.

Clearing the *store* is not sufficient on its own, though: `_program_device` rewrites one device's
alarms, so expiring device A's fired row while scheduling device B leaves A's **Bot** still holding
the alarm — untracked by `get_schedule`, unreachable by `cancel_schedule`, and still consuming one
of `MAX_ALARMS = 5`. So `remove_expired` must **return the rows it deleted**, and every affected
BLE device gets reprogrammed, not just the device being written. (Cloud rows need no equivalent:
their one-time jobs self-remove in `_make_cb`, and orphaned past-due JobQueue entries are inert.)

Two behaviours this raises, decided here so the plan doesn't invent them:

- **`_get_schedule_impl` does *not* reprogram.** It keeps calling `remove_expired` to keep its
  listing honest, but stays read-only — a "what's scheduled?" question must never trigger BLE
  writes. Reprogramming happens only in the two tools that already write (`_schedule_impl`,
  `_cancel_impl`).
- **A failed reprogram of an unrelated device does not fail the call.** If device A's stale row is
  expired while the user is scheduling device B, and A's Bot is unreachable, the tool logs a
  warning and completes B. Failing B because of A would make an unrelated Bot's dead battery block
  all scheduling. A's row is already gone from the store, so `get_schedule` stays truthful.

**Row contract**, stated explicitly because leaving it implicit is how `days='fri', once=0` got
written last time. One-time: `days = [weekday of fire_at]`, `once=1`, `fire_at` set. Recurring:
`days = <user's days>`, `once=0`, `fire_at = NULL`. The two are distinguished by **`once`, never by
`days`** — both shapes can carry a single weekday.

### Cancel must be unambiguous

`_cancel_impl` matches on `device` + `time` only (`schedules.py:172`), so with one-time timers now
the norm, "Saturday 18:30" and "next Friday 18:30" are indistinguishable and one cancel kills both.

`cancel_schedule` gains an optional `when` (same resolution rule) to disambiguate. This is not
implementable through `store.remove(device, time)`, whose SQL has no date predicate
(`schedule_store.py:48-56`), so `_cancel_impl` changes shape: select candidate rows in Python
(device, optional `time`, optional resolved `fire_at` date), delete each by `store.remove_id`,
unschedule each cloud row by id, and re-add exactly those rows on the rollback path. `store.remove`'s
rowcount is no longer the "nothing matched" signal — the selected-row list is. When a date is
given, **recurring rows are excluded** (they have `fire_at IS NULL` and would otherwise match on
device+time and be deleted silently).

### Output format

Tools return English; the model renders Hebrew, as elsewhere.

- One-time: `kitchen: on at 18:30 on 2026-08-15 (Sat) — ONE-TIME`
- One-time more than 6 days out: same, plus ` — NEXT WEEK`, so a weekday token that quietly rolled
  +7 is visible. The date is always printed regardless of the flag.
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
| `when` missing | Error listing valid tokens (it is required). |
| Unknown `when` token | Error listing valid tokens. Strict lowercase; `"friday"`/`"שישי"` rejected. |
| `when="today"`, time already passed | Error that also instructs the retry: *if the user did not explicitly say היום, retry with `soonest` or `tomorrow`*. Self-healing, so a late-evening "18:30" isn't a dead end. |
| `repetition_phrase` empty on the recurring tool | Error: use `schedule_device` unless the user actually asked for repetition. |
| More than 5 timers on a device | `validate` currently runs only inside `_program_device`, i.e. **BLE only**, so cloud devices can accumulate unbounded rows. `_schedule_impl` gains a per-device count check before `store.add`, covering both routes. |
| Malformed `time`, cloud failure | Existing paths unchanged (`validate`, `store.remove_id` rollback). |
| Resolved moment already past at scheduling time | `CloudScheduler.schedule_row` currently `return`s silently (`cloud_scheduler.py:32-33`) while the tool reports ✅ — a timer that never fires. It must raise so `_schedule_impl`'s existing rollback runs. `reconcile` relies on the silent skip, so it gets its own guard. |

## Open hardware question (gates BLE only)

`encode_alarm` sets bit 7 alongside the day mask, but that the Bot firmware reads this as "fire
once **on that weekday**" is **unverified**. Every alarm this agent has ever written had
`days == [weekday of the next occurrence]`, so bits and intent never disagreed. Setting Friday for
Saturday is new. If bit 7 instead means "fire at the next HH:MM, then delete", a BLE one-time fires
**tonight** — the original bug, unfixed.

**Routing, verified 2026-08-15:** the **box's** `devices.yaml` routes all five devices via
`cloud_id` (living_room, dining, kitchen, ac, garden — each commented "cloud via Hub Mini"). The
**repo's** copy still lists `ble_id` for all five, and memory `box-deployment.md` still says three
lights stay on BLE — both are stale, because `devices.yaml` is excluded from the deploy rsync and
diverges by design. Cloud timers fire from `CloudScheduler` on an exact datetime, so the firmware
semantic is not exercised on the live box today.

It remains a real requirement, since the code fully supports BLE and a fresh deploy from the repo
config would be BLE. The plan must (a) include the hardware spike before any device returns to BLE
— program a Bot with `once` + a *non-adjacent* weekday and confirm it does not fire early — and
(b) note the fallback if the semantic fails: program BLE one-timers as recurring-on-that-weekday
plus a store-side auto-cancel after firing. The +7 horizon cap above is what keeps every value
BLE-encodable at all.

## Implementation shape and phasing

The two scheduling tools share **one `_schedule_impl(args, *, recurring: bool, …)`**, bound twice
in `build_schedule_tools`. The device/action/time validation, the `remove_expired` sweep, the
`MAX_ALARMS` pre-check, the row-contract write, and the cloud-vs-BLE routing are identical; only
the when-resolution branch and the output line differ. Two separate impls would duplicate the cap
check and the row contract — precisely the code whose divergence caused this bug.

The plan should run in three gated phases, because phase 1 is invisible to the model and the
existing suite is a real regression net for it:

1. **Grounding and safety, no model-facing change** — tz pinning (`build_time_tools`, `now_fn=`
   wiring, hoisting the `ZoneInfo` import out of the cloud-creds block at `telegram_app.py:111`),
   UTC `fire_at` plus the both-sides comparison contract, `remove_expired` returning rows with
   anti-resurrection reprogramming, `CloudScheduler.schedule_row` raising on a past moment,
   `MAX_ALARMS` pre-check.
2. **The API change** — `when` enum and `_resolve_fire_at`, the two-tool split with
   `repetition_phrase`, the date-aware local formatter replacing `readback`, date-aware cancel,
   the three prompt rules, and the full test migration.
3. **BLE spike, fallback, rollout** — gated; required only before any device returns to BLE.

## Testing

Frozen clock (`now_fn`) throughout; no network, no BLE.

**Regression (the reported bug):** "tomorrow 18:30" asked Fri 2026-08-14 17:21 → `fire_at` is
**2026-08-15**, `once=True`, and nothing fires on the 14th. Multi-device variant: all four devices
in one turn, since that was the real request.

**Resolution:** `when="fri"` on a Friday at 17:00 → today; at 19:00 → +7 days and the output says
NEXT WEEK. `tomorrow` at 23:00 and 01:00 → both the following calendar day. `soonest` at 17:21 →
today; at 19:00 → tomorrow. `today` with a passed time → error, nothing stored. `day_after_tomorrow`
→ +2. `in_a_week` → +7. Missing or unknown `when` → error, nothing stored.

**Timezone:** resolution uses HOME_TZ regardless of host tz; a "tomorrow" spanning Israel's
October DST end lands at the right *wall-clock* time; `remove_expired` correctly drops a row
written on the other side of that transition.

**No resurrection:** a one-time row with a past `fire_at` plus a new schedule on the same device →
old row gone from the store and absent from the alarms written.

**Recurring:** `schedule_recurring_device` sets `once=False`, `fire_at=None`, and the output
contains "RECURRING". `schedule_device` has no `days` property in its schema. Empty
`repetition_phrase` → error, nothing stored. A `make_fake_client` test asserting the model reaches
for `schedule_device`, **not** the recurring tool, on "תדליק את האור בשבת ב-18:30" — the incident's
exact wrong turn.

**Cancel:** two one-time timers at the same HH:MM on different dates are individually cancellable;
the confirmation names dates and a count.

**Model-facing:** a `make_fake_client` test asserting the schema the model actually sees (`when`
required, valid enum values, `days` absent from `schedule_device`), and that a tool error string
is fed back so the model can recover.

**Prompt:** stays digit-free and byte-stable (existing tests). Plus a `make_fake_client` turn
asserting the assistant's reply actually contains the returned date — prompt rule 2 is otherwise
unverified, and "model receives the date, still says מחר" is the original failure.

**Existing tests must be migrated, not merely kept green.** Removing `days` from `schedule_device`
breaks them by design; "make it green" must not mean restoring `days`. Explicitly:

- Move to `schedule_recurring_device` (they assert genuine recurring behaviour):
  `test_schedule_tools.py:78, 96-97, 106-107, 135, 161, 172-173, 196, 207` and
  `test_schedules_cloud.py:29, 36, 43`.
- Rewrite against `_resolve_fire_at`, since the helper is replaced:
  `test_schedule_tools.py:24, 30` (`_one_time_target`), and the import at line 2.
- `test_schedule_tools.py:71` asserts `once is True and days == ["thu"]` — keep, as it pins the
  one-time row contract, but extend it to assert `fire_at` too.
- `test_schedule_store.py:39` asserts `remove_expired(...) == 1`; the return type becomes the
  deleted rows, so this becomes a length assertion. Its fixtures are naive ISO and move to UTC.
- `test_cloud_scheduler.py:43, 50, 69, 87` build `fire_at` fixtures as `+03:00` strings compared
  against a `+03:00` now; they move to the UTC contract.
- `test_time_tool.py:4, 24` and `test_loop.py:41, 53` import `DEFAULT_TOOLS` / `get_current_time`
  directly, as do `test_telegram_handler.py:98, 118, 140`. **`DEFAULT_TOOLS` is kept** as a
  host-tz fallback so `handle_message`'s default argument (`telegram_app.py:48`) and these tests
  keep working; `build_time_tools(tz)` is added alongside it and is what
  `build_application` uses (`telegram_app.py:123`). Only production wiring becomes tz-pinned.
- `test_schedule_tools.py:145-154` (the `get_schedule` expiry test) is affected by whichever
  reprogram decision is taken below.

With those, every other test in the suite stays green unchanged.

## Migration

Expected to be none: the `schedules` table was emptied on 2026-08-15 when the stale recurring
Friday timers from this incident were cancelled. But this must be **re-checked at deploy time**,
not assumed — any row written between then and rollout carries a host-tz, fixed-offset `fire_at`
and would mix formats with the new UTC canonical form, tripping exactly the lexicographic-compare
bug described above. Rollout step: confirm `select count(*) from schedules` is zero, or normalize
surviving `fire_at` values to UTC before starting the new code.

## Rollout

Deploy via the `deploy-box` skill, then verify against the real bug: schedule something for "מחר",
confirm the Hebrew reply states tomorrow's date, `get_schedule` agrees, and **the device actually
fires tomorrow and not today** — the store being right is not sufficient evidence.
