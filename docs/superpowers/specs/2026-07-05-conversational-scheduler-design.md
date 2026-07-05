# Conversational, One-Time-Capable Scheduler — Design

**Date:** 2026-07-05
**Status:** Approved, going to implementation plan

## Goal
Turn the one-shot converter into a genuine conversational scheduler: it remembers
the thread (so corrections refine the previous understanding), supports one-time
scheduling within the next 7 days ("today / tomorrow / this Friday"), and NEVER
silently defaults — when unsure or out of scope it asks/explains instead of guessing.

## Constraints & scope
- One-time = the Bot's fire-once bit (bit 7) on a weekday alarm → fires on the next
  occurrence of that weekday, then disables. Supports **today / tomorrow / this <weekday>**
  (within 7 days). A specific calendar date further out is NOT supported (would need an
  always-on server) → the parser returns a clarification, never a guess.
- Recurring (every day / weekdays / specific weekdays) works as today.

## Components changed
model (add `once`), encoder (once-bit), parser (conversation in, schedule-OR-clarification
out, date-aware), core (`preview_conversation`), web app (`/preview` takes a thread, returns
a kind), frontend (thread memory, clarification bubbles, once display, reset-on-approve).
BLE writer, registry, validator: unchanged except `once` flows through untouched.

## 1. Model
`Event` gains `once: bool = False`. Everything else unchanged. `DeviceSchedule`/`Schedule` unchanged.

## 2. Encoder
`encode_alarm(event, inverted=False)` sets the repeat byte's **bit 7** when `event.once`
is true: `repeat_byte = day_mask | (0x80 if event.once else 0)`. Bit 7 = "execute once".
Action/inversion/day-bits unchanged.

## 3. Parser — conversation in, schedule-or-clarification out, date-aware
- New `parse_conversation(messages: list[str], registry, now: datetime, completion_fn=<default>) -> ParseResult`
  where `ParseResult` is a dataclass `{schedule: Schedule | None, clarification: str | None}` (exactly one set).
- The user turns are rendered into one conversation string for the model (numbered turns);
  `completion_fn(system, user) -> str` seam is unchanged (tests inject canned JSON).
- System prompt additions:
  - Injected line: `Today is <Weekday>, <YYYY-MM-DD>.`
  - Output schema: EITHER `{"schedules": [{"device","events":[{"time","action","days","once"}]}]}`
    OR `{"clarification": "<question or explanation>"}`.
  - Rules: `once:true` for one-time (today/tomorrow/this <weekday>) with `days` set to the
    target weekday; `once:false` for recurring. Resolve relative days using the injected date.
    If the user implies a specific/one-time day, NEVER output "every day". If the request is
    ambiguous, unparseable, or a specific date more than 7 days out, return a `clarification`
    (do not guess). The conversation may contain corrections — always output the CURRENT
    complete intended schedule reflecting the whole thread.

## 4. Core
`preview_conversation(messages, registry, now, completion_fn=None) -> (kind, payload, schedule)`:
- `parse_conversation(...)`. If `clarification` → return `("clarification", clarification_text, None)`.
- Else validate + press-mode normalize the schedule, then → `("schedule", readback(schedule), schedule)`.
- Validation errors (`ScheduleError`) propagate to the endpoint, surfaced as a friendly error (⚠️) — never a dead card.
(`build_schedule`/`apply_schedule`/CLI stay as-is for the CLI path; the web path uses the new function.)

## 5. Read-back wording
Recurring: `living_room: on 06:00 — every day` (unchanged). One-time:
`living_room: on 09:00 — once (mon)` (uses `describe_days` for the day label).

## 6. Web endpoints
- `POST /preview` body `{"messages": [str, ...]}` (the whole current thread) →
  `{"ok":true,"kind":"schedule","readback":str,"schedule":<JSON>}`
  or `{"ok":true,"kind":"clarification","message":str}`
  or `{"ok":false,"error":str}`. Server computes `now = datetime.now()` and passes it in.
- `POST /apply` unchanged, but `schedule_from_json` reads `once` (default false) so once flows to the writer.

## 7. Frontend
- Keeps `messages` (array of user turn strings) for the current thread. Each send appends and
  POSTs the whole array to `/preview`.
- `kind:"schedule"` → read-back card + Approve (shows one-time lines faithfully).
  `kind:"clarification"` → plain assistant bubble (no card, no Approve); the user keeps talking, context carries.
  `ok:false` → ⚠️ error bubble.
- **Approve & write** → `/apply` with the last schedule; on success show `✅ Written…` and **reset the thread**
  (`messages=[]`). A **New** button clears the thread manually. No empty/dead cards ever.

## 8. Error handling
| Case | Behavior |
|------|----------|
| Ambiguous / unparseable / far-future date | parser `clarification` → plain bubble, keep chatting |
| Unknown device / >5 per Bot / bad time | validator message → ⚠️ bubble |
| Missing OPENAI_API_KEY | ⚠️ bubble ("set OPENAI_API_KEY and restart") |
| BLE unreachable on Approve | ⚠️ bubble, Approve re-enabled to retry |

## 9. Testing
- Parser (canned `completion_fn`): multi-turn refinement (turn1 "every day" → turn2 "make it one-time"
  yields `once=True`); relative-day resolution against a FIXED injected `now`; clarification path
  (gibberish → `clarification` set, `schedule` None); the rendered conversation includes all turns.
- Encoder: `once=True` sets bit 7 (`repeat_byte & 0x80`); `once=False` leaves it clear.
- Core: `preview_conversation` returns `("clarification", …)` vs `("schedule", …)`.
- Web: `/preview` with a `messages` thread returns the two kinds correctly (injected parser);
  `/apply` writes a schedule whose events carry `once`.
- Frontend: manual e2e by the user.

## Out of scope
Specific far-future calendar dates (needs always-on server); persistence of threads across reloads;
multi-user/auth; viewing/clearing existing on-device alarms.
