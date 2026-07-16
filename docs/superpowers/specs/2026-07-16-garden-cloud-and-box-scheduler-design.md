# Garden bot via SwitchBot Cloud API + box-side scheduler

- **Date:** 2026-07-16
- **Status:** Approved (design)
- **Epic:** Home control — reach the out-of-BLE-range garden bot

## Problem

The garden SwitchBot Bot (`EE:CE:11:1B:5B:1C`, cloud id `EECE111B5B1C`, name "גינה") is **out of
reliable Bluetooth range** from the 24/7 box's home location (every direct-BLE connect times out).
The box must live where Wi-Fi + the other 4 bots are; one BLE radio can't cover bots spread across the
house. The other 4 bots (kitchen/living_room/dining/ac) work over direct BLE and are unchanged.

**Proven foundation (spike, 2026-07-16):** the SwitchBot Cloud API (`api.switch-bot.com`, HMAC auth via
the user's token/secret) successfully controls the garden bot through the user's **Hub Mini 7F**
(`FAEE46B6877F`) — `turnOn`/`turnOff`/`press` all returned `statusCode 100` and physically actuated the bot.

The Cloud API has **no scheduling** feature, so scheduled garden actions must be fired **by the box**.

## Goals

1. Menashe controls the garden bot (immediate on/off/press) via the SwitchBot Cloud API.
2. Menashe can **schedule** garden actions — the box holds the schedule and fires the cloud command at
   the right time, **with no LLM call at fire time**.
3. Same Hebrew UX as the other bots ("תדליק את הגינה", "תכבה את הגינה כל יום ב-18:00").

## Non-goals (YAGNI)

- No cloud **fallback** for the 4 BLE bots — they stay BLE-only.
- No rewrite of the existing **on-device-timer** scheduling for BLE bots (hybrid model, per decision).
- No IR / Hub / other SwitchBot device types — only the garden Bot.

## Architecture — hybrid routing

A device is routed by its config:

| Device | Config key | Immediate control | Scheduling |
|--------|-----------|-------------------|------------|
| kitchen, living_room, dining, ac | `ble_id` | direct BLE (`run_immediate`) — unchanged | on-device Bot timers — unchanged |
| **garden** | `cloud_id` | SwitchBot Cloud API | **box-side scheduler** (PTB JobQueue) |

`Registry` is the single source of routing truth.

## Components

### 1. `switchbot_cloud.py` (new) — cloud client
- `send_command(device_id, command, *, token, secret, http_fn=None) -> None`
  where `command ∈ {"turnOn","turnOff","press"}`.
- Auth per SwitchBot v1.1: `t = ms epoch`, `nonce = uuid4`,
  `sign = base64(HMAC_SHA256(secret, token + t + nonce))`; headers `Authorization, sign, t, nonce,
  Content-Type: application/json`. POST body `{"command":..., "parameter":"default", "commandType":"command"}`.
- **Validation:** require HTTP 2xx **and** response body `statusCode == 100`; otherwise raise
  `SwitchBotCloudError` with the API `message`.
- **Timeout:** ~10s. **Retry:** transient failures (timeouts, 5xx, connection errors) up to 2 retries
  with short backoff; do not retry on auth/validation errors.
- **Security:** never log the token, secret, `sign`, or full headers.
- **Injectable seam:** `http_fn` (default: real `urllib`/`httpx` call) so tests run offline.

### 2. `Registry` (switchbot_scheduler/registry.py) — routing helpers
- Add `cloud_id: str = ""` to `Device` and load it from `devices.yaml`.
- `is_cloud(name) -> bool` (True iff `cloud_id` set), `cloud_id(name) -> str`.
- Keep `ble_id(name)` as-is. A device has **either** `ble_id` **or** `cloud_id`.

### 3. `ScheduleStore.list()` — expose row id
- Add `id` to the SELECT and to each returned dict. Needed so box-side jobs are named by row id
  (`switchbot-cloud:{id}`) for reliable cancel + reload-on-restart. (`add()` already returns the id.)

### 4. `home.py` `control_device` — route immediate commands
- Resolve the device name, then apply the **existing** `actuator.resolve_action` (press-mode → press,
  inverted → swap on/off) to get the effective action.
- If `registry.is_cloud(name)`: map action → cloud command (`on→turnOn`, `off→turnOff`, `press→press`)
  and call `switchbot_cloud.send_command(...)`. `control_device` runs **synchronously in a worker thread**
  (`handle_message` is already dispatched via `asyncio.to_thread`), so a blocking `send_command` here does
  not touch the event loop — call it directly. (The event-loop concern applies only to the scheduler
  callback in §5.)
- Else: existing BLE `run_immediate` path — unchanged.
- Report the user's *requested* action in Hebrew (inverted-safe), same as today; on failure return a clear
  "couldn't reach the garden" message.

### 5. Box-side scheduler — PTB JobQueue
- **Dependency:** `pyproject.toml` → `python-telegram-bot[job-queue]>=21.0` (adds APScheduler). Deploy
  reinstalls deps.
- Built in `telegram_app.build_application` after the Application is created (so `app.job_queue` exists).
- **Timezone:** all jobs scheduled in the **home timezone** (from `now_fn().tzinfo`, i.e. Asia/Jerusalem),
  never UTC.
- **Startup reconciliation:** call `ScheduleStore.remove_expired(now)` first (drop past one-time rows so
  they are **not** fired late after a reboot). Then for each **cloud-device** schedule in the store:
  - recurring (`once=False`): `job_queue.run_daily(cb, time=HH:MM@tz, days=weekday-set, name="switchbot-cloud:{id}")`
  - one-time (`once=True`): `job_queue.run_once(cb, when=fire_at, name="switchbot-cloud:{id}")` (only if `fire_at` is future)
- **Job callback:** async; calls `await asyncio.to_thread(send_command, cloud_id, mapped_command)` so the
  event loop is never blocked. Logs success/failure; a one-time job removes its store row after firing.
- BLE-device schedules are ignored here (they use on-device timers).

### 6. `schedule_device` / `cancel_schedule` — branch by device type (schedules.py)
- **Cloud device:** store the row in `ScheduleStore` (as today) and register/cancel the corresponding
  JobQueue job by name `switchbot-cloud:{id}`. No BLE write.
- **BLE device:** unchanged (write on-device timer).
- `get_schedule` already reads the store → works for both.
- **Wiring:** the schedule tools need the JobQueue to register/cancel cloud jobs. `build_schedule_tools`
  gains an injected `scheduler` handle (a thin wrapper over `app.job_queue` exposing
  `add_cloud_job(row_id, ...)` / `remove_cloud_job(row_id)`), constructed in `build_application` after the
  Application exists. Tests inject a fake scheduler.

### 7. Tool description update (schedules.py `_SCHEDULE_SCHEMA`)
- Current text ("programmed into the device's own timer so it fires even if this computer is off") is false
  for cloud devices. Reword: *BLE devices fire from their own on-device timer (even if the box is off);
  cloud devices (e.g. garden) are fired by the home-agent, so they require it to be running.* Update the
  matching assertion in `tests/home_agent/test_schedules*.py`.

## Config

- `.env`: `SWITCHBOT_TOKEN`, `SWITCHBOT_SECRET` (already added on the box).
- `devices.yaml` garden entry: replace empty `ble_id` with `cloud_id: "EECE111B5B1C"`.
- `config.py`: read `SWITCHBOT_TOKEN`/`SWITCHBOT_SECRET` into `Config`; expose to the cloud client + tools.

## Error handling

- Immediate cloud failure → user-facing Hebrew error; nothing silently swallowed.
- Scheduled cloud failure → logged (with device + command, never secrets); scheduler keeps running; the
  schedule stays (recurring will try again next occurrence).
- Missing token/secret → cloud routing disabled with a startup warning (like the Roborock gate); garden
  commands return a clear "cloud not configured" message.

## Testing (offline, no network/hub)

- **Cloud client:** inject `http_fn`; assert correct signing inputs, `statusCode != 100` raises, retry on
  transient, no secrets in logs.
- **Routing:** `control_device` on the garden calls the cloud seam (not BLE); on a BLE bot calls BLE.
  `resolve_action` still applies (press/inversion) before mapping.
- **Scheduler:** frozen clock; assert `run_daily`/`run_once` registered with home tz + correct name;
  startup drops expired one-time rows; callback maps action → command and calls the cloud seam.
- Full suite stays green; no network in the automated suite.

## Deployment

Via the `deploy-box` skill: rsync code → `pip install -e .` (pulls the job-queue extra) → restart
`home-agent` → verify. `devices.yaml` is excluded from the rsync (box-local), so the garden `cloud_id`
edit is applied **directly on the box**. `.env` already has the token/secret on the box.
