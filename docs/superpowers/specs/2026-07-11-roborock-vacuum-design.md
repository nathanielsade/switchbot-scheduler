# Roborock Q Revo vacuum control (`roborock-mcp`) — design

- **Date:** 2026-07-11.
- **Roadmap:** Epic H. In-process agent tools (no separate service/MCP server), on SQLite-less cloud client.
- **Status:** approved design, pre-implementation.
- **Predecessors / patterns reused:** agent core, home-mcp, scheduling, shopping, calendar — same in-process
  `Tool` pattern, deterministic registry with the **model doing fuzzy Hebrew mapping**, **injectable client
  seam** (fake in tests), and **graceful-if-unconfigured** loading.

## Goal

Full natural-language control of the Roborock **Q Revo** (vac + mop, auto-empty / mop-wash dock) from the
Telegram family bot, in Hebrew:
*"תשאב את הסלון"*, *"תנקה את חדר השינה — שאיבה ואז שטיפה"*, *"תחזור לתחנה"*, *"תרוקן את המיכל"*,
*"כמה סוללה נשאר לשואב?"*, *"תנקה כל יום בשמונה"*.

## Scope

**In scope (full epic in one v1):** cloud auth + room/segment discovery; immediate control (whole-home &
per-room clean, pause/resume/stop, return-to-dock, locate); **per-run** cleaning plan (suction, mop
water-flow, vac / mop / vac-then-mop order); dock actions (empty bin, wash mop, dry mop); status;
consumables; recurring/one-off scheduling via the **robot's own native timers**.

**Out of scope / deferred:**
- **Per-room** cleaning plan (different suction/mop per room in one command) — the API and natural Hebrew
  both make this awkward; v1 applies **one plan per run**. Add later only if wanted.
- **Local (LAN) transport** — v1 talks to the robot over **cloud (MQTT)**; local is a future speed-up behind
  the same tool interface (auth flow is identical).
- Zoned (coordinate) cleaning, `app_goto_target`, DND-timer and valley-electricity-timer management,
  ground-material / segment-naming — not needed for the stated goals.

## Transport & auth — cloud (MQTT)

Log in once at startup with the Roborock account, pick the Q Revo, build a **cloud MQTT** client shared by
all tools. Cloud ⇒ immediate control works **without** the always-on box; works even if the bot machine and
robot aren't on the same LAN. `python-roborock` is imported **lazily inside the bootstrap** (like
`bleak`/`openai`) so importing `roborock.py` never touches the network and tests don't need the lib.

## Config (new `.env` keys, read in `config.py`)

- `ROBOROCK_USERNAME` — Roborock account email.
- `ROBOROCK_PASSWORD` — Roborock account password. **Either username or password unset → vacuum tools don't
  load** (bot still runs), same graceful pattern as calendar's `GOOGLE_SA_KEYFILE`. Warning logged.
- `ROBOROCK_ROOMS` — path to the rooms YAML (default `roborock_rooms.yaml`).

## Dependencies

Add `python-roborock` (current `4.22.0`, 2026-03; supports Q Revo, segment clean, `get_room_mapping`,
`server_timer`) to `pyproject.toml`. Lazy-imported inside `load_roborock_client` and the real
`RoborockClient` methods; **confirm the exact version + method signatures at build time.**

## Room registry — hand-authored YAML + discovery script

The robot cleans by numeric **segment ids**, not names. We need a deterministic segment-id ↔ Hebrew-room map.

- **`roborock_rooms.yaml`** (checked in, mirrors `devices.yaml`):
  ```yaml
  rooms:
    living_room:
      segment_id: 16
      aliases: ["סלון", "salon", "living room"]
    kitchen:
      segment_id: 17
      aliases: ["מטבח", "kitchen"]
  ```
- **`scripts/roborock_discover.py`** — one-time helper: logs in, calls `get_room_mapping`, prints current
  segment ids + names so the user seeds the YAML (adding Hebrew aliases). Re-run only when the home is
  re-mapped in the Roborock app (rare).
- **`RoomRegistry`** (`src/home_agent/roborock_rooms.py`) mirrors the SwitchBot `Registry`: `resolve(spoken)
  -> segment_id | None`, `known_names()`, `name_for_segment(id) -> str` (for status). Deterministic; the
  **model** does the fuzzy wording→room mapping (as with shopping `known_items`). If the YAML is absent,
  room-scoped cleans are refused with a helpful message but whole-home clean still works.

## The tools

Seven tools (schedule is one logical group of three). Each `impl(args) -> str`; every schema `description`
tells the model when/how to use it and to report back in the user's language. The model calls `list_rooms`
when it needs the exact room name before a room clean (like `list_devices`).

- **`list_rooms()`** — list room names + Hebrew/English aliases (from the YAML). Does not report clean state.

- **`clean(rooms?, mode?, suction?, water_flow?, repeat?)`** — the main verb.
  - `rooms` **omitted → whole home** (`app_start`); present → **segment clean** (`app_segment_clean` with the
    resolved segment ids). Unknown room name → refuse with `known_names()` list, clean nothing.
  - `mode` ∈ `vacuum | mop | vac_and_mop` (default: robot's current). Applied **per run** by setting mop
    mode / water box before start (`set_mop_mode`, `set_water_box_custom_mode`); exact value mapping
    **confirmed at build time** against the Q Revo.
  - `suction` ∈ `quiet | balanced | turbo | max` → `set_custom_mode`.
  - `water_flow` ∈ `low | medium | high` → `set_water_box_custom_mode`.
  - `repeat` (segment clean) → passes count to `app_segment_clean`.
  - Sets the plan, then starts; reports the **requested** plan + target back to the user.

- **`control_vacuum(action)`** — `action` ∈ `pause | resume | stop | return_to_dock | locate` →
  `app_pause` / `app_start` (resume) / `app_stop` / `app_charge` / `find_me`.

- **`dock_action(action)`** — `action` ∈ `empty_bin | wash_mop | dry_mop` → `app_start_collect_dust` /
  `app_start_wash` / `app_set_dryer_setting`. Requires the robot docked; a not-docked error → readable msg.

- **`vacuum_status()`** — `get_status` → state, battery %, area & time cleaned, current room (named via
  `RoomRegistry.name_for_segment`), error state. Human-readable Hebrew-friendly summary.

- **`consumables()`** — `get_consumable` → main-brush / side-brush / filter / sensor life remaining; the
  model turns low values into maintenance hints.

- **Scheduling (robot-native timers, no box):**
  - **`schedule_clean(time, rooms?, mode?, suction?, water_flow?, recurring?)`** — creates a timer via
    `set_server_timer` that runs a clean (whole-home or segments) at the given time; `recurring` ∈
    `daily | once | <weekdays>`. Model computes the time spec via `get_current_time`.
  - **`get_cleaning_schedule()`** — `get_server_timer`; lists existing timers with an opaque id each.
  - **`cancel_cleaning_schedule(id)`** — `del_server_timer(id)`.
  - **No SQLite mirror store** (unlike SwitchBot Bot-timers): server timers are **readable back**, so the
    robot is the source of truth. `get_cleaning_schedule` reads live.
  - **Build-time risk:** confirm the `set_server_timer` payload format (cron spec + embedded clean command)
    in `python-roborock`. If it proves impractical, fall back to deferring recurring cleans to the future
    `schedule_task`/cron path (Epic D + box); native timers are strongly preferred and expected to work.

Any API error, unknown room/action, or not-configured state → a readable message; the agent loop's
try/except is the backstop.

## Safety / confirmation

Vacuum actions are **low-stakes and reversible** (a clean can be stopped; a schedule cancelled), so — unlike
calendar writes — they need **no** `prepare_/commit_` cross-turn confirm. This matches immediate home control
(`control_device` acts directly). The system prompt nudges the model to confirm exact room before a
room-scoped clean when ambiguous, but that's polish, not a gate.

## Module layout & wiring

- **`src/home_agent/roborock_rooms.py`** — `RoomRegistry` (YAML loader + `resolve` / `known_names` /
  `name_for_segment`).
- **`src/home_agent/roborock.py`** —
  - `RoborockClient` — thin wrapper exposing exactly the methods the tools use (`get_room_mapping`,
    `clean_segments`, `start`, `pause`, `resume`, `stop`, `charge`, `locate`, `set_plan`, `collect_dust`,
    `wash`, `dry`, `status`, `consumables`, `get_timers`, `set_timer`, `del_timer`). Real impl lazily imports
    `python-roborock`; the **injectable seam** — tests pass a fake.
  - `load_roborock_client(config)` — builds the real cloud client from `ROBOROCK_USERNAME`/`PASSWORD`, or
    `None` (warning) if unset/login fails → no vacuum tools.
  - `build_roborock_tools(client, registry, *, now_fn=None) -> list[Tool]`.
- **Wiring (`telegram_app.build_application`):** build the client + registry **once at startup** (if
  configured) and compose the tools. All tools are **chat-agnostic** (no per-chat state) → simpler than
  calendar; no per-turn binding.

## Testing (offline — the hard rule)

Inject a **fake `RoborockClient`** (records calls, returns canned room-mapping / status / consumables /
timers) + a real `RoomRegistry` from a temp YAML. No network, mirroring the BLE/vision convention.

- `list_rooms`: names + aliases from the YAML.
- `clean`: whole-home (no rooms) → `start` (no segment call); `clean(rooms=["סלון"], mode="vac_and_mop",
  suction="turbo")` → sets plan (mop mode + water box + custom mode) **then** `clean_segments([16])`;
  unknown room → refused, **no** clean call; `repeat` forwarded.
- `control_vacuum` / `dock_action`: each action → the right client method; unknown action → friendly error.
- `vacuum_status`: canned status → summary; current segment named via the registry.
- `consumables`: canned wear values → summary.
- Scheduling: `schedule_clean` → `set_timer` with the expected spec under a **frozen `now_fn`**;
  `get_cleaning_schedule` → lists canned timers; `cancel_cleaning_schedule(id)` → `del_timer(id)`.
- Config: `load_roborock_client` → `None` (warning) when username/password unset. Registry absent →
  room clean refused, whole-home still works.

**Manual (real robot, outside CI):** after account setup + seeding the YAML — a whole-home clean, a room
clean, return-to-dock, a dock action, status, and a schedule round-trip on the real Q Revo. Like the BLE /
vision manual checks.

## Build order (this spec → one plan)

1. Config keys + `python-roborock` dep in pyproject; `load_roborock_client` (lazy import, graceful `None`).
2. `RoomRegistry` + `roborock_rooms.yaml` + `scripts/roborock_discover.py`.
3. `RoborockClient` wrapper + fake; `build_roborock_tools` scaffold + `list_rooms`.
4. `clean` (whole-home + segments + per-run plan) with tests.
5. `control_vacuum` + `dock_action` + `vacuum_status` + `consumables`.
6. Scheduling trio (native server timers) — confirm payload format at build time.
7. Prompt policy (canonical room mapping, confirm-room nudge) + startup wiring + live smoke.
