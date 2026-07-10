# Google Calendar (family-mcp) — design

- **Date:** 2026-07-10
- **Roadmap:** Epic 4 "family-mcp", the calendar piece. In-process agent tools.
- **Status:** approved design, pre-implementation.
- **Predecessors:** agent core, home-mcp, scheduling, shopping — same in-process `Tool` pattern,
  injectable seams, graceful-if-unconfigured wiring.

## Goal

Let the family agent read and manage the couple's Google Calendars from Telegram in Hebrew:
*"what do we have this week?"*, *"are we free Saturday?"*, *"when's the dentist?"*, *"add a dentist
appointment Tuesday at 3"*, *"move it to 4"*, *"cancel it"*. Reads span **both spouses' existing
calendars**; a created event lands on a default calendar and invites the other spouse so **both see it**.

## Scope

**In scope — full read + write, four tools:** `find_events`, `create_event`, `update_event`,
`delete_event`. Writes are guarded by confirm-before-acting (see Safety). Reads span all configured
calendars; writes target one default calendar and auto-invite the other spouse.

**Out of scope (deferred):** creating *recurring* events (reads still expand recurrences for the range);
free/busy via the FreeBusy API ("are we free?" is answered by listing that day's events and letting the
model reason); more than one auto-invitee; changing time zone per-event (fixed to `Asia/Jerusalem` for v1);
event reminders/notifications scheduling (that's the box + `schedule_task`, a separate epic).

## Auth — service account (set-and-forget)

A **service account** (a robot Google identity with its own key file). No interactive OAuth, no token
expiry — ideal for a 24/7 bot. The user shares their calendars *with* the robot's email.

**One-time setup (the user's part; exact steps go in the plan):**
1. In Google Cloud console: create a project → enable the **Google Calendar API**.
2. Create a **service account**; create + download a **JSON key** for it. Note the SA's email
   (`…@….iam.gserviceaccount.com`).
3. In Google Calendar settings, **share** each calendar (yours + your wife's) with that SA email, giving
   **"Make changes to events."**
4. Put the JSON key file in the repo (git-ignored) and set the env vars below.

The bot authenticates with `google.oauth2.service_account.Credentials.from_service_account_file(keyfile,
scopes=["https://www.googleapis.com/auth/calendar"])` and `googleapiclient.discovery.build("calendar",
"v3", credentials=…)`.

## Config (new `.env` keys, read in `config.py`)

- `GOOGLE_SA_KEYFILE` — path to the service-account JSON key. **If unset, the calendar tools don't load**
  (bot still runs) — same graceful pattern as home/scheduling without `devices.yaml`.
- `CALENDAR_IDS` — comma/space-separated calendar ids to **read** (the ones shared with the SA; typically
  both spouses'). A calendar id is usually the owner's email or a `…@group.calendar.google.com`.
- `CALENDAR_WRITE_ID` — the calendar new events are created on (defaults to the first of `CALENDAR_IDS`).
- `CALENDAR_INVITE` — the other spouse's email to auto-invite on new events, so the event appears on both
  existing calendars (optional; if unset, no auto-invite).
- All times are `Asia/Jerusalem`.

## Dependencies

Add to `pyproject.toml`: `google-api-python-client`, `google-auth`. Imported **lazily** inside the
bootstrap (like `bleak`/`openai`) so importing `gcal.py` needs no network and the automated tests (which
inject a fake service) don't require the libs at import time.

## Module & wiring

**New `src/home_agent/gcal.py`** (named `gcal`, **not** `calendar`, to avoid shadowing Python's stdlib
`calendar`):
- `build_calendar_tools(service, *, calendar_ids, write_id, invite_email=None, now_fn=None) -> list[Tool]`
  — the Google API `service` is an **injectable seam**; tests pass a fake, production passes the real client.
- `load_calendar_tools(config) -> list[Tool]` — builds the real service from `GOOGLE_SA_KEYFILE` (lazy
  import) and returns the tools; returns `[]` with a warning if the key file is unset/missing.

**Wiring:** `telegram_app.build_application` composes `load_calendar_tools(config)` into the tool list
**unconditionally-if-configured** (chat-agnostic → built once at startup, like the shopping tools).

## The four tools

Each is a `home_agent.tools.Tool`. All datetimes are ISO 8601 in `Asia/Jerusalem`; the model converts
relative phrasing ("יום שלישי ב-3") using `get_current_time`.

- **`find_events(when?, query?)`** — list events across **all** `CALENDAR_IDS`.
  - `when`: an optional range hint the model fills — but the tool is deterministic: it accepts explicit
    `time_min`/`time_max` ISO datetimes; if omitted, defaults to **now → +7 days**.
  - `query`: optional free-text (Google `q=`).
  - Uses `service.events().list(calendarId=…, timeMin, timeMax, singleEvents=True, orderBy="startTime",
    q=…)` per calendar, merges, sorts by start. Returns each event as a line with an **opaque `ref`**
    (encodes which calendar + event id), title, start, end. The `ref` is what `update`/`delete` take, so
    the model never handles raw Google ids or calendar routing.
- **`create_event(title, start, end?, all_day?, notes?)`** — `events().insert(calendarId=CALENDAR_WRITE_ID,
  body={summary, start, end, description, attendees:[{email: CALENDAR_INVITE}] if set}, sendUpdates="all")`.
  If `end` omitted, default +1h (or all-day if `all_day`). Returns a summary of what was created.
- **`update_event(ref, title?, start?, end?, notes?)`** — `events().patch(calendarId, eventId, body=…)`
  on the calendar the `ref` points to. Only provided fields change.
- **`delete_event(ref)`** — `events().delete(calendarId, eventId)`.

Unknown/expired `ref`, or an API error → a readable message (no crash); the loop's try/except is the backstop.

## Safety — confirm before writing (no state machine)

Two layers, no fragile pending-state:
1. **Prompt policy** (added to `FAMILY_SYSTEM_PROMPT`, cross-tool like the shopping canonicalization rule):
   *"For calendar changes, always summarize the exact change and get the user's confirmation before calling
   create_event/update_event/delete_event. For editing or deleting, first call find_events to locate the
   specific event and show it; act only on that event's `ref`."*
2. **Tool shape**: `update_event`/`delete_event` require a `ref`, which only `find_events` produces — so a
   destructive action is inherently a deliberate find → show → confirm → act sequence, not a blind guess.
   Delete gets the firmest wording.

## Data flow examples

- *"מה יש לנו השבוע?"* → `find_events()` (now→+7d, all calendars) → model formats the merged list in Hebrew.
- *"תוסיף רופא שיניים יום שלישי ב-3"* → model computes the ISO datetime (via `get_current_time`), **summarizes
  and asks to confirm**, then on "כן" → `create_event(title="רופא שיניים", start="2026-07-14T15:00:00+03:00")`
  → created on `CALENDAR_WRITE_ID`, wife invited → shows on both. 
- *"תבטל את זה"* → `find_events(query="רופא שיניים")` → show the match → confirm → `delete_event(ref)`.

## Testing

No network in the automated suite — inject a **fake `service`** mimicking the googleapiclient chain
(`service.events().list(...).execute()` / `insert` / `patch` / `delete`), returning canned data / recording
calls:
- `find_events`: fake returns events from two calendars → assert merge + sort by start, the `q`/range params
  passed, and that the returned `ref` round-trips to the right (calendar, event) for update/delete. Default
  range (now→+7d) computed under a **frozen `now_fn`**.
- `create_event`: assert `insert` called on `CALENDAR_WRITE_ID` with the right body, attendee added when
  `CALENDAR_INVITE` set / omitted when not, default end (+1h) and all-day handling.
- `update_event`/`delete_event`: assert `patch`/`delete` hit the calendar+eventId decoded from the `ref`;
  unknown ref → friendly message, no exception.
- Config: `load_calendar_tools` returns `[]` (warning) when `GOOGLE_SA_KEYFILE` is unset/missing.
- Wiring: `build_application` composes the calendar tools when configured (spy, like the shopping test).

**Manual (real Google, outside CI):** after the SA setup, "מה יש לנו השבוע?" returns real events; add/edit/
delete round-trips and shows on both calendars. Like the BLE/vision manual verifications.

## Build order (this spec → one plan, ~5 tasks)

1. Config keys (`GOOGLE_SA_KEYFILE`, `CALENDAR_IDS`, `CALENDAR_WRITE_ID`, `CALENDAR_INVITE`) + deps in pyproject.
2. `gcal.py` bootstrap + `find_events` (the `ref` encoding, range default, merge/sort) — inject fake service.
3. `create_event` (+ auto-invite, default end/all-day).
4. `update_event` + `delete_event` (by `ref`).
5. Prompt safety policy + wiring into `build_application` + a live-smoke manual step.
