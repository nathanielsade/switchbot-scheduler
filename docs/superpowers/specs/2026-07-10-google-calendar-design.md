# Google Calendar (family-mcp) — design

- **Date:** 2026-07-10 (**revised twice same day** after design reviews — see "Revision notes" at end).
- **Roadmap:** Epic 4 "family-mcp", the calendar piece. In-process agent tools.
- **Status:** approved design, pre-implementation.
- **Predecessors:** agent core, home-mcp, scheduling, shopping — same in-process `Tool` pattern, injectable
  seams, graceful-if-unconfigured wiring, and the chat-scoped-tool + pending-confirmation patterns.

## Goal

Let the family agent read and manage the couple's calendar from Telegram in Hebrew: *"what do we have this
week?"*, *"are we free Saturday?"*, *"when's the dentist?"*, *"add a dentist appointment Tuesday at 3"*,
*"move it to 4"*, *"cancel it"*. Reads span **both spouses' personal calendars plus a shared Family
calendar**; new events are created on the **shared Family calendar** (both subscribe → both see them, no
attendee invites — see Auth).

## Scope

**In scope:** read across all configured calendars; create/update/delete events on the Family calendar,
gated by a **deterministic, cross-turn confirm step**. Tools: `find_events`, `prepare_calendar_change`,
`commit_calendar_change`, `cancel_calendar_change`.

**Out of scope (deferred):** creating *recurring* events (reads still expand recurrences); FreeBusy API
("are we free?" is answered by listing that day and letting the model reason); attendee invites; editing
events on the **personal** calendars (v1 edits/deletes only the Family calendar); per-event time zones
(fixed `Asia/Jerusalem`); reminders/notifications scheduling (needs the box).

## Auth — service account + shared Family calendar

A **service account** (robot Google identity with its own key file) — no interactive OAuth, no token
expiry. **Scope: `https://www.googleapis.com/auth/calendar.events`** (event read/write only — narrowest that
works; calendar ids are configured, so we never list calendars).

**Why a shared Family calendar, not attendee invites:** a service account on **personal Gmail** cannot
populate event `attendees` without Workspace **domain-wide delegation** (personal accounts don't have it).
So "both see it" is structural: create on a **Family calendar both spouses subscribe to**. Reading each
spouse's *personal* calendar still works — that only needs normal calendar **sharing** with the SA.

**One-time setup (user's part; exact clicks go in the plan):**
1. Google Cloud console: project → enable **Google Calendar API**.
2. Create a **service account**; download its **JSON key**; note its email (`…@….iam.gserviceaccount.com`).
3. Create a **"Family" calendar**; share it with **your wife** (See/Edit) and the **SA email** ("Make
   changes to events"); both **subscribe**. This id is `CALENDAR_WRITE_ID`.
4. Optionally share each **personal** calendar with the SA email (read) so `find_events` spans them.
5. Put the key in the repo (git-ignored); set the env vars.

## Config (new `.env` keys, read in `config.py`)

- `GOOGLE_SA_KEYFILE` — path to the SA JSON key. **Unset → calendar tools don't load** (bot still runs).
- `CALENDAR_IDS` — comma/space-separated calendar ids to **read** (Family + both personal).
- `CALENDAR_WRITE_ID` — the **Family** calendar id; all create/update/delete happen here. Defaults to the
  first of `CALENDAR_IDS`.
- Times are `Asia/Jerusalem`.

## Dependencies

Add `google-api-python-client` + `google-auth` to `pyproject.toml`. Imported **lazily** inside the
bootstrap (like `bleak`/`openai`) so importing `gcal.py` needs no network and tests (fake service) don't
need the libs at import time.

## Module & wiring

**New `src/home_agent/gcal.py`** (named `gcal`, not `calendar`, to avoid shadowing Python's stdlib):
- `build_calendar_tools(service, pending_store, chat_id, committable_id, *, calendar_ids, write_id, now_fn=None) -> list[Tool]`
  — Google `service` + pending store are **injectable seams**. `chat_id` and `committable_id` make the write
  tools **chat-scoped and same-turn-safe** (below); this factory is called **per turn** in `handle_message`
  with those values **captured in a Python closure and omitted from the tool schemas** (the model never
  passes a Telegram id or a token).
- `load_calendar_service(config)` — builds the real Google client from `GOOGLE_SA_KEYFILE` (lazy import),
  or `None` if unset/missing (→ no calendar tools, warning).
- **Pending store:** `CalendarPending` (SQLite in `config.db_path`, connection-per-op). Table
  `pending_calendar_changes(id INTEGER PK AUTOINCREMENT, chat_id, payload_json, created_at)`. `stage(chat_id,
  payload)` deletes any prior row for the chat then inserts a new one → **fresh `id` per staging**.
  `current(chat_id) -> {id, payload, created_at} | None`. `clear(chat_id)`. Expires ~15 min via `created_at`.

**Wiring (`telegram_app`):** build the Google `service` + `CalendarPending` once at startup (if configured).
In `handle_message`, **before** `run_turn`, snapshot `committable_id = pending.current(chat_id)?.id` (the id
of a change staged in a *previous* turn, or `None`), then compose
`build_calendar_tools(service, pending, chat_id, committable_id, …)` for this turn.

## The tools

All datetimes ISO 8601 in `Asia/Jerusalem`; the model converts relative phrasing via `get_current_time`.

- **`find_events(time_min?, time_max?, query?)`** — read across **all** `CALENDAR_IDS`.
  `events().list(calendarId=…, timeMin, timeMax, singleEvents=True, orderBy="startTime", q=…)` per calendar;
  merge; **dedup by `(iCalUID, start)`** — so the *same instance* appearing on two calendars collapses (keep
  the copy on `CALENDAR_WRITE_ID`, else the first), but distinct instances of a recurring series stay
  separate. **Default range:** if `query` is given → now −30d … now +180d (open-ended "when's the dentist?");
  otherwise → now … +7d. Returns each event as a line with an **opaque `ref`** (encodes the chosen copy's
  calendar id + event id), title, start, end.

- **`prepare_calendar_change(action, title?, start?, end?, all_day?, notes?, ref?)`** — **stages** a change;
  **touches nothing in Google.** `action` ∈ `create | update | delete`.
  - `create`: requires `title` + `start`; target = `CALENDAR_WRITE_ID`. Timed events use
    `start/end.dateTime` (default `end` = +1h). **All-day** events use `start.date`/`end.date` with an
    **exclusive** `end.date` (a single all-day event → `end.date = start.date + 1 day`).
  - `update` / `delete`: require `ref`, and **`ref` must be on `CALENDAR_WRITE_ID`** (the Family calendar).
    A ref on a personal calendar → refuse: "I can only change events on the Family calendar."
  Validates, writes the payload via `pending.stage(chat_id, …)`, returns a **human summary + confirm ask**.

- **`commit_calendar_change()`** — executes the chat's pending change **iff** it exists, is **not expired**,
  **and its `id == committable_id`** (i.e. it was staged in a *prior* turn — the same-turn guard). Runs
  `events().insert` / `patch` / `delete` on the right calendar, then `pending.clear`. Cases:
  - pending id != `committable_id` (staged during *this* turn) → **refuse**: "I've noted the change — reply
    כן to apply it." (No Google call. Forces the user to actually see + confirm on a later turn.)
  - no pending / expired → friendly "nothing staged / it expired — try again."
  - (No `sendUpdates` — no attendees; if attendees are ever added, set `sendUpdates="all"` here + on update/delete.)

- **`cancel_calendar_change()`** — `pending.clear(chat_id)`; discards the staged change.

Unknown/expired `ref` or pending, personal-calendar write ref, or an API error → readable message; the
loop's try/except is the backstop.

## Safety — deterministic AND cross-turn

Two code-level gates, not prompt hope:
1. **No executing write verb exists.** create/update/delete are only *staged* by `prepare_calendar_change`;
   the sole executor is `commit_calendar_change`, acting only on a staged payload.
2. **Commit can't fire in the same turn as prepare.** `commit_calendar_change` executes only a pending whose
   `id` matches the `committable_id` snapshotted at the **start of this `handle_message`** — i.e. one staged
   in a *previous* turn. A pending staged mid-turn has a new `id` → commit refuses. So the model **cannot**
   stage-and-apply behind the user's back; the user must see the summary and send a fresh message ("כן")
   whose turn then finds the pending "committable."

Pending changes also **expire (~15 min)** so a stale one isn't committed by an unrelated later "כן".
The system prompt still nudges "find first, summarize, confirm" — polish on top of the gates, not the gate.

## Data flow (create)

Turn 1: *"תוסיף רופא שיניים יום שלישי ב-3"* → model computes ISO start → `prepare_calendar_change(action=
"create", title="רופא שיניים", start="2026-07-14T15:00:00+03:00")` → staged (id=42), bot replies "לקבוע … ?"
(commit in this same turn would be refused). Turn 2: user "כן" → turn-start snapshot sees committable id=42
→ model calls `commit_calendar_change()` → inserts on the Family calendar → both see it.

## Testing

No network in CI — inject a **fake `service`** (mimics `service.events().list/insert/patch/delete().execute()`)
+ a real `CalendarPending` (tmp SQLite):
- `find_events`: two calendars share an event with the same `(iCalUID, start)` → **deduped to one**, prefers
  `CALENDAR_WRITE_ID` copy; two recurring **instances** (same `iCalUID`, different `start`) → **both kept**;
  `query` present → wide range (−30d/+180d) vs. absent → now/+7d, under a **frozen `now_fn`**; `ref` decodes
  to the right (calendar, event).
- `prepare_calendar_change`: create/update/delete each **stage** (assert `CalendarPending` row + **no** Google
  mutate call); validation errors (create missing title/start; update/delete missing ref; **update/delete
  ref not on `CALENDAR_WRITE_ID`** → refused) → friendly message, nothing staged; **all-day** create stages
  `start.date`/`end.date` with exclusive end (one-day → +1).
- `commit_calendar_change`: **same-turn guard** — stage with `committable_id=None` (fresh id) → commit refuses,
  no Google call; stage in a prior "turn" then commit with matching `committable_id` → executes the right
  call/calendar/body + clears pending; **expired** pending → refused; nothing staged → friendly.
- `cancel_calendar_change`: clears pending, no Google call. Chat-scoping: two chats don't collide.
- Config: `load_calendar_service` → `None` (warning) when `GOOGLE_SA_KEYFILE` unset/missing.

**Manual (real Google, outside CI):** after SA setup — reads return real events; a full add/edit/delete
round-trip via prepare→(next message)→commit appears on both calendars. Like the BLE/vision manual checks.

## Build order (this spec → one plan, ~6 tasks)

1. Config keys + deps in pyproject.
2. `CalendarPending` store (autoincrement id, `stage`/`current`/`clear`, expiry).
3. `gcal.py` service bootstrap + `find_events` (ref encoding, `(iCalUID,start)` dedup, query-vs-default range).
4. `prepare_calendar_change` (stage create/update/delete; validation incl. write-calendar-only ref; all-day).
5. `commit_calendar_change` (same-turn + expiry guards) + `cancel_calendar_change`.
6. Prompt policy + per-turn chat-scoped wiring (incl. the turn-start `committable_id` snapshot) + live smoke.

## Revision notes

- **v1 → v2:** service-account **attendee invites** need Workspace domain-wide delegation (fails on personal
  Gmail) → switched to a **shared Family calendar, no invites**. **Prompt-only** confirm isn't enforced →
  **deterministic `prepare_`/`commit_`** with a pending store + expiry. Added `iCalUID` dedup; scope →
  `calendar.events`.
- **v2 → v3:** `prepare→commit` could still fire **in one turn** → added the **same-turn guard** (commit only
  applies a pending staged in a *prior* turn, via a turn-start `committable_id` snapshot). Dedup key →
  **`(iCalUID, start)`** so recurring instances aren't collapsed. **Update/delete refs restricted to
  `CALENDAR_WRITE_ID`.** `query` searches use a **wide default range**. **All-day** event `date`/exclusive-end
  semantics made explicit.
