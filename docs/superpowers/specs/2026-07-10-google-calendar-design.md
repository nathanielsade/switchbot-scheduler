# Google Calendar (family-mcp) — design

- **Date:** 2026-07-10 (**revised same day** after a design review — see "Revision notes" at end).
- **Roadmap:** Epic 4 "family-mcp", the calendar piece. In-process agent tools.
- **Status:** approved design, pre-implementation.
- **Predecessors:** agent core, home-mcp, scheduling, shopping — same in-process `Tool` pattern, injectable
  seams, graceful-if-unconfigured wiring, and the chat-scoped-tool + pending-confirmation patterns.

## Goal

Let the family agent read and manage the couple's calendar from Telegram in Hebrew: *"what do we have this
week?"*, *"are we free Saturday?"*, *"when's the dentist?"*, *"add a dentist appointment Tuesday at 3"*,
*"move it to 4"*, *"cancel it"*. Reads span **both spouses' personal calendars plus a shared Family
calendar**; new events are created on the **shared Family calendar**, which both subscribe to, so **both
see them** — no attendee invites (see Auth).

## Scope

**In scope:** read events across all configured calendars; create/update/delete events on the shared
Family calendar, gated by a **deterministic confirm step**. Tools: `find_events`, `prepare_calendar_change`,
`commit_calendar_change`, `cancel_calendar_change`.

**Out of scope (deferred):** recurring-event *creation* (reads still expand recurrences); FreeBusy API
("are we free?" is answered by listing that day and letting the model reason); attendee invites / external
guests; per-event time zones (fixed `Asia/Jerusalem` v1); reminders/notifications scheduling (needs the box).

## Auth — service account + shared Family calendar

A **service account** (robot Google identity with its own key file) — no interactive OAuth, no token
expiry. **Scope: `https://www.googleapis.com/auth/calendar.events`** (event read/write only — narrowest that
works; calendar ids are configured, so we never list calendars).

**Why a shared Family calendar and not attendee invites:** a service account on **personal Gmail** cannot
populate event `attendees` without Workspace **domain-wide delegation** (which personal accounts don't
have). So "both see it" is achieved structurally: create events on a **Family calendar that both spouses
subscribe to**. Reading each spouse's *personal* calendar still works — that only needs normal calendar
**sharing** with the SA, not delegation.

**One-time setup (the user's part; exact clicks go in the plan):**
1. Google Cloud console: create a project → enable the **Google Calendar API**.
2. Create a **service account**; download its **JSON key**. Note its email (`…@….iam.gserviceaccount.com`).
3. Create a new **"Family" calendar**. Share it with **your wife** (See/Edit) and with the **SA email**
   ("Make changes to events"). Make sure you both **subscribe** to it. This is `CALENDAR_WRITE_ID`.
4. Optionally **share each personal calendar** with the SA email (read) so `find_events` can span them.
5. Put the JSON key in the repo (git-ignored) and set the env vars below.

## Config (new `.env` keys, read in `config.py`)

- `GOOGLE_SA_KEYFILE` — path to the SA JSON key. **Unset → calendar tools don't load** (bot still runs).
- `CALENDAR_IDS` — comma/space-separated calendar ids to **read** (the Family calendar + both personal
  ones, whatever you shared with the SA).
- `CALENDAR_WRITE_ID` — the **Family** calendar id; all create/update/delete happen here. Defaults to the
  first of `CALENDAR_IDS`.
- Times are `Asia/Jerusalem`. *(No `CALENDAR_INVITE` — attendees are not used, per Auth.)*

## Dependencies

Add `google-api-python-client` + `google-auth` to `pyproject.toml`. Imported **lazily** inside the
bootstrap (like `bleak`/`openai`) so importing `gcal.py` needs no network and tests (fake service) don't
need the libs at import time.

## Module & wiring

**New `src/home_agent/gcal.py`** (named `gcal`, **not** `calendar`, to avoid shadowing Python's stdlib):
- `build_calendar_tools(service, pending_store, chat_id, *, calendar_ids, write_id, now_fn=None) -> list[Tool]`
  — Google `service` and the pending store are **injectable seams**. Note `chat_id`: the write tools are
  **chat-scoped**, so this factory is called **per turn** in `handle_message` with the chat's id **captured
  in a Python closure and omitted from the tool schemas** (the model never passes a Telegram id). `find_events`
  is chat-agnostic and could be split out, but for simplicity the whole bundle is built per turn.
- `load_calendar_service(config)` — builds the real Google client from `GOOGLE_SA_KEYFILE` (lazy import),
  or `None` if unset/missing (→ no calendar tools, with a warning).
- **Pending store:** `CalendarPending` (SQLite, in `config.db_path`, connection-per-op) — table
  `pending_calendar_changes(chat_id PK, payload_json, created_at)`. One staged change per chat; **expires
  ~15 min** (via `created_at`); replaced by a newer prepare; cleared on commit/cancel. Mirrors
  `pending_receipts`.

**Wiring:** `telegram_app` builds the Google `service` + `CalendarPending` once at startup (if configured);
`handle_message` composes `build_calendar_tools(service, pending, chat_id, …)` **per turn** so the write
tools are bound to the right chat.

## The tools

All datetimes are ISO 8601 in `Asia/Jerusalem`; the model converts relative phrasing ("יום שלישי ב-3")
using `get_current_time`.

- **`find_events(time_min?, time_max?, query?)`** — read across **all** `CALENDAR_IDS`.
  `service.events().list(calendarId=…, timeMin, timeMax, singleEvents=True, orderBy="startTime", q=…)` per
  calendar; merge; **dedup by `iCalUID`** (when the same event appears on multiple calendars, keep one —
  prefer the copy on `CALENDAR_WRITE_ID`, else the first). Default range: now → +7 days. Returns each event
  as a line with an **opaque `ref`** (encodes calendar id + event id of the preferred copy), title, start,
  end. `ref` is what a change refers to; the model never handles raw Google ids or calendar routing.

- **`prepare_calendar_change(action, title?, start?, end?, all_day?, notes?, ref?)`** — **stages** a change;
  **touches nothing in Google.** `action` ∈ `create | update | delete`.
  - `create`: requires `title` + `start` (default `end` = +1h, or all-day); target = `CALENDAR_WRITE_ID`.
  - `update`: requires `ref` (from `find_events`) + the fields to change.
  - `delete`: requires `ref`.
  Validates inputs, writes the staged payload to `CalendarPending[chat_id]`, and returns a **human-readable
  summary + confirm ask** ("Create 'רופא שיניים' Tue 14 Jul 15:00–16:00 on Family — confirm?"). No Google call.

- **`commit_calendar_change()`** — executes the chat's pending change **iff** present and not expired; else
  a friendly "nothing staged / it expired — try again". Runs `events().insert` (create) /
  `events().patch` (update) / `events().delete` (delete) on the right calendar; clears the pending. (No
  `sendUpdates` — there are no attendees; if attendees are ever added, set `sendUpdates="all"` on all three.)

- **`cancel_calendar_change()`** — discards the chat's pending change.

Unknown/expired `ref` or pending, or an API error → readable message; the loop's try/except is the backstop.

## Safety — confirm is deterministic, not prompt-hope

A model slip **cannot** create/edit/delete directly: those verbs don't exist as executing tools. The only
executor is `commit_calendar_change`, which acts solely on a payload that a **prior `prepare_` call** staged.
So the flow is enforced in code: **prepare (stage) → the bot shows the summary → user "כן" → commit
(execute)**. The system prompt *also* instructs the model to run `find_events` first for update/delete and
to summarize before committing — but that's UX polish on top of the deterministic gate, not the gate itself.
Pending changes **expire (~15 min)** so a stale one can't be committed by an unrelated later "כן".

## Data flow examples

- *"מה יש לנו השבוע?"* → `find_events()` (now→+7d, all calendars, deduped) → model formats in Hebrew.
- *"תוסיף רופא שיניים יום שלישי ב-3"* → model computes ISO start (via `get_current_time`) →
  `prepare_calendar_change(action="create", title="רופא שיניים", start="2026-07-14T15:00:00+03:00")` →
  bot: "לקבוע 'רופא שיניים' יום ג' 15:00–16:00 בלוח המשפחה?" → user "כן" → `commit_calendar_change()` →
  event created on the Family calendar → both see it (both subscribed).
- *"תבטל את זה"* → `find_events(query="רופא שיניים")` → show match → `prepare_calendar_change(action="delete",
  ref=…)` → confirm → `commit_calendar_change()`.

## Testing

No network in CI — inject a **fake `service`** mimicking the googleapiclient chain
(`service.events().list(...).execute()` / `insert` / `patch` / `delete`) plus a real `CalendarPending`
(tmp SQLite):
- `find_events`: fake returns overlapping events across two calendars sharing an `iCalUID` → assert **dedup**
  keeps one and prefers the `CALENDAR_WRITE_ID` copy; assert range/`q` params; default now→+7d under a
  **frozen `now_fn`**; assert the returned `ref` decodes to the right (calendar, event).
- `prepare_calendar_change`: create/update/delete each **stage** a payload (assert `CalendarPending` row,
  assert **no** `insert`/`patch`/`delete` was called on the fake), and return a summary; validation errors
  (create without title/start; update/delete without ref) → friendly message, nothing staged.
- `commit_calendar_change`: executes the staged action against the fake (assert the right call + calendar +
  body); clears pending; **expired** pending (older than the window via `created_at`) → refused, no Google
  call; nothing staged → friendly message.
- `cancel_calendar_change`: clears pending, no Google call.
- Chat-scoping: two chats' pending changes don't collide.
- Config: `load_calendar_service` → `None` (warning) when `GOOGLE_SA_KEYFILE` unset/missing.

**Manual (real Google, outside CI):** after SA setup — "מה יש לנו השבוע?" returns real events; a full
add/edit/delete round-trip via prepare→confirm→commit appears on both calendars. Like the BLE/vision manual
verifications.

## Build order (this spec → one plan, ~6 tasks)

1. Config keys (`GOOGLE_SA_KEYFILE`, `CALENDAR_IDS`, `CALENDAR_WRITE_ID`) + deps in pyproject.
2. `CalendarPending` store (SQLite, expiry) — mirror `pending_receipts`/`ScheduleStore` tests.
3. `gcal.py` service bootstrap + `find_events` (ref encoding, iCalUID dedup, range default).
4. `prepare_calendar_change` (stage create/update/delete + validation; no Google call).
5. `commit_calendar_change` + `cancel_calendar_change` (execute/discard; expiry guard).
6. Prompt policy + per-turn chat-scoped wiring in `build_application`/`handle_message` + live-smoke manual.

## Revision notes (2026-07-10 review)

Original design used a service account that **created events with the spouse as an attendee** and relied on
**prompt-only** confirmation. Review found: (1) SA attendee-invite needs Workspace domain-wide delegation →
won't work on personal Gmail; (2) prompt-only confirm isn't enforced (the loop executes tool calls
immediately). Revised to: **shared Family calendar (no attendees)** + **deterministic prepare→commit**
confirmation + **iCalUID dedup** on reads + **`calendar.events`** scope.
