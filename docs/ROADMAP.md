# Home Family Agent — Roadmap

> A personal AI agent for the family, running 24/7 on an always-on home Linux machine,
> driven from a shared **Telegram group** in natural-language Hebrew. Controls the house,
> manages shared family life, and acts as household financial analyst.
> Full vision: see the Project Brief (this doc is the epics/tasks breakdown of it).

## Working method (so we never lose the thread)
Hierarchy: **Project → Epic → Task → Step.**
- **Epic** — a big capability that ships something usable on its own.
- **Task** — smallest reviewable unit; every task carries **Goal / Context / Definition of Done / How to test**.
- **Step** — bite-sized TDD action (write test → run → implement → commit).
Each epic, when reached, gets its own breakdown doc (`docs/epics/`) with its tasks fully specified,
then a TDD plan at build time. This file is the map; the session todo list tracks the *current* epic.

## Epic sequence & status  (order = dependency + value, NOT existing code)
1. ✅ **Agent core** — Telegram ↔ OpenAI function-calling loop (+ family system prompt + memory). DONE & proven live 2026-07-09; merged + pushed to main (incl. review-hardening 46e4f93). (`docs/epics/epic-1-agent-core.md`)
2. ⬜ **Infra / home box** — Linux + BlueZ + systemd + re-scan Bots (overlaps #1) ← **NEXT**
3. ✅ **home-mcp** — wrap the existing scheduler code. DONE & FULLY VERIFIED LIVE 2026-07-09: in-process tools `control_device`/`list_devices`/`battery_status`, merged to main, 123 tests. Real Telegram message flipped the kitchen+living-room Bots; battery_status returned real values (battery byte index 1 confirmed on hardware). Built ahead of #2 (Infra) since it's pure software. (`docs/superpowers/plans/2026-07-09-home-mcp-control-tools.md`)
4. ⬜ **family-mcp** — reminders + shopping + calendar + `schedule_task`/memory
5. ⬜ **finance-mcp** — Discount scraper → local SQLite → in-agent Q&A + weekly summary (read-only)
6. ⬜ **payslips** (vision)
7. ⬜ **Israeli live-price layer**
8. ⬜ **Roborock vacuum control** (`roborock-mcp`) — full control of the Q Revo: rooms, vac/mop plan, scheduling. Pure software (cloud API); 24/7 schedules want the box.
9. ⬜ **Sensibo Sky AC control** (`sensibo-mcp`) — cloud-API stateful AC control (mode/temp/fan) + room climate sensing. Pure software.

*(The old "what did I set last Friday" history idea is folded into the agent's SQLite memory — Epics 1 & 4 — not a separate feature.)*
*(Epics 8–9 are independent device integrations that extend the home-control surface; both are pure software behind an injectable client seam, buildable anytime — no strict box dependency for immediate control.)*

## Where we are today (becomes Module 1)
We already built a **natural-language SwitchBot scheduler** (repo `nathanielsade/switchbot-scheduler`):
- Prompt (He/En) → schedule → validate → encode → **Bluetooth** control of the Bots.
- CLI + web chat UI; conversational; one-time & recurring; per-device inversion (סלון) + press-mode (מזגן).
- **Brain today = one OpenAI call (a parser). NOT agentic.** Deterministic Python does the rest.
- Verified on real hardware; 62 tests.

The brief changes two foundational things:
1. **Brain: OpenAI single-call (a parser) → OpenAI agentic function-calling loop** (the model decides which tools to call; no hardcoded routing). **Same provider — no migration**; reuses the existing OpenAI setup + credit.
2. **Interface: local web page → shared Telegram group.**
Everything we built (registry, validator, encoder, ble_writer, devices.yaml) is **reused** — rewrapped as `home-mcp` tools under the new agent.

## Target architecture
```
Telegram group (me + wife + bot)
      │  python-telegram-bot (~150 lines), allowlisted to our chat IDs
      ▼
OpenAI API — function-calling loop (agentic), model gpt-4o
      │  standing system prompt = family context (auto prompt-cached)
      ├── home-mcp     (AC, lights, scenes)      ← wraps our existing code
      ├── family-mcp   (calendar, reminders, shopping list, shared memory)
      └── finance-mcp  (Discount scraper → local SQLite store, read-only)
  + SQLite (conversation/shared memory, shopping history, payslips)
  + cron  (schedule_task tool → future runs of the agent)
```
Single agent, ~20 tools, multiple small MCP servers (fault isolation + separate credentials + reuse). Code organized so any domain can later become a sub-agent.

---

## Key decisions & reconciliations (resolve before/while building)

**D1 — OpenAI function-calling loop; tools now, MCP later.** Build the agent as an OpenAI **function-calling loop** (send message + tool/function defs → model returns tool calls → execute → feed results back → repeat until it stops calling tools). Epic 1 uses in-process function tools. For the MCP servers (Epic 3+), bridge each MCP server's tools into OpenAI function definitions — either manually, or via the **OpenAI Agents SDK** (which speaks MCP + tool loops natively). Decide the manual-loop-vs-Agents-SDK question at Epic 3; Epic 1 doesn't need it.

**D2 — Model.** Default **`gpt-4o`** for the agent (strong tool orchestration); **`gpt-4o-mini`** for cheap/simple turns (what the old parser used). A reasoning model is an option for hard planning turns. Confirm the current best OpenAI model at build time. OpenAI **auto-caches** long stable prompt prefixes, so keeping the family-context system prompt byte-stable gives the caching win with no extra config.

**D3 — Home scheduling model: retire BLE on-device timers in favor of cron.** The whole premise is now an always-on box. So `home-mcp` exposes **immediate** actions (`set_ac`, `set_light`, `press`) over the BLE control we already proved, and *scheduling* is done by the general **`schedule_task`** tool (writes a cron line + prompt that re-runs the agent at time T). This unifies "schedule anything" for free and drops the fragile on-device-timer reverse-engineering. *(Alternative: keep on-device timers for offline robustness. Recommend cron; confirm.)*

**D4 — Repo structure.** Grow into a small **monorepo** (`home-agent/`) with the Telegram bot, the three MCP servers, and the existing scheduler code as the `home-mcp` package. The current `switchbot-scheduler` repo becomes that package. *(Confirm: rename/restructure vs new repo.)*

**D5 — Security (non-negotiable, cross-cutting).** Telegram bot restricted to our two chat IDs / the family group ID. Bank + API credentials in encrypted env vars, never in prompts/messages/SQLite. **Finance tools are read-only** — the agent can never execute a bank transaction. Everything runs locally on the home box.

---

## Infra (Epic 0) — the always-on home box
Prereq for everything (from earlier sessions: no Mac dependency, wife-friendly).
- [ ] Choose the box: old laptop (free) **or** Raspberry Pi Zero 2 W (~₪85). Needs Bluetooth + Wi-Fi.
- [ ] Install Linux; Python 3.11+; **BlueZ** (Linux BLE stack for `bleak`).
- [ ] **Re-scan the Bots on Linux** → device addresses are **MACs**, not the macOS CoreBluetooth UUIDs; update `devices.yaml`. Confirm a real BLE write fires.
- [ ] Run services (bot + MCP servers) under **systemd** (auto-start on boot, restart on crash); laptop lid-close = no-sleep (`HandleLidSwitch=ignore`).
- [ ] Secrets: encrypted env file, `chmod 600`, loaded by systemd; rotate the OpenAI/SwitchBot creds that leaked into chat.
  - ⚠️ **DEFERRED 2026-07-09 (user's explicit call):** credential rotation (OpenAI key + Telegram bot token) and the private family-group lockdown are intentionally postponed for now. Note: the leaked keys stay live until rotated, and a private group does **not** mitigate a leaked bot token / API key (someone with the token can drive the bot without being in the group). Revisit before the box goes 24/7. Related: switch `ALLOWED_CHAT_IDS` from the direct chat (5494778294) to the family-group id via discovery mode.

## Epic A — Agent core (Telegram ↔ OpenAI function-calling loop)
The brain transplant + interface. **Build order step 1.**
- [ ] Telegram bot skeleton (`python-telegram-bot`), **allowlist** our chat IDs / group ID; ignore everyone else.
- [ ] OpenAI function-calling loop: send message + tool defs → run tools → feed results → repeat until the model stops calling tools; `gpt-4o`.
- [ ] Standing **system prompt** = family context (who we are, tone, Hebrew responses); **prompt-cached**.
- [ ] SQLite conversation history / shared memory (stateless API → resend context each turn).
- [ ] **Vertical slice:** wire ONE simple tool (`set_ac`) end-to-end — Telegram → OpenAI → tool → Bot → reply. Proves the whole spine before adding modules.

## Epic B — home-mcp (wrap the existing smart-home code)
**Build order step 2.** Reuse registry/validator/encoder/ble_writer.
- [ ] `home-mcp` server exposing tools: `set_ac(room,state)`, `set_light(room,state)`, `press(device)`, `home_status()`.
- [ ] Scenes: `sleep_mode`, `leaving_home` (compose multiple device actions).
- [ ] Natural-language scheduling routed through `schedule_task` (per D3), e.g. "turn on the living-room AC at 17:30".
- [ ] Carry over device quirks: inversion (סלון), press-mode (מזגן).

## Epic C — family-mcp: reminders + shopping list + calendar
**Build order step 3.**
- 🟡 Shared **calendar** (Google Calendar API): "what do we have this week?" — SOFTWARE SHIPPED 2026-07-10 (merged to main, 189 tests): `find_events` (spans calendars, dedup) + deterministic cross-turn confirmed `prepare_`/`commit_`/`cancel_calendar_change`; service account + shared Family calendar. PENDING: the user's Google Cloud setup (service account + share calendars) + live smoke. Spec/plan: `docs/superpowers/{specs,plans}/2026-07-10-google-calendar*`.
- [ ] **Reminders**: "remind us tomorrow evening to pay arnona" → `schedule_task` → Telegram message at time T.
- 🟡 **Smart shopping list** (SQLite, shared) — designed as one spec, built in 3 phases. Spec: `docs/superpowers/specs/2026-07-09-smart-shopping-list-design.md`.
  - ✅ **Phase 1 SHIPPED 2026-07-09** (merged to main, 163 tests): shared add/remove/show/mark-bought via in-process tools over an append-only `items`/`list`/`purchases` SQLite store; canonicalization done by the agent (`known_items`). Plan: `docs/superpowers/plans/2026-07-09-shopping-list-phase-1.md`. (Live Telegram smoke test still pending.)
  - ✅ **Phase 2 SHIPPED 2026-07-10** (merged to main, 170 tests): learns purchase cycles via `suggest_restock` (median-gap over DISTINCT dates — same-day dedup done, excludes items already listed, ≥2 purchases) + `purchase_history` (deterministic math, agent reasons); canonicalization policy lifted into FAMILY_SYSTEM_PROMPT. On-demand only (proactive nudge needs the box). Plan: `docs/superpowers/plans/2026-07-10-shopping-list-phase-2.md`.
  - ⏸️ **Phase 3 — DEFERRED (future task, user's call 2026-07-10)** — **receipt photos** → OpenAI **vision** → confirm → log full basket + cost. Fully designed (spec + `docs/architecture/shopping-list.md`), incl. review fixes: receipt tools bound per-turn with chat_id in a Python closure (not a model arg); two-turn flow (deterministic `parse_receipt`+`stage_receipt` on the photo, canonicalize+`commit_receipt` on the "yes"); pending-receipt expiry (~15 min); price = per-unit. Pick up whenever.

## Epic D — schedule_task + shared memory
**Build order step 4.** (Reminders/scheduling in C depend on `schedule_task`; formalize it here.)
- ✅ **Device scheduling DONE & VERIFIED LIVE 2026-07-09 via ON-DEVICE Bot timers** (no box needed): `schedule_device`/`get_schedule`/`cancel_schedule`, in-process, backed by a SQLite record (source of truth). One-time + recurring, 5-timers/Bot cap, 148 tests. Merged to main. Proven on hardware: a living-room Bot fired from its own timer at the scheduled minute; list + cancel verified against the DB. Spec/plan: `docs/superpowers/{specs,plans}/2026-07-09-scheduling-on-device-timers*`.
- [ ] `schedule_task(cron, prompt)` tool → writes a cron entry that re-invokes the agent with that prompt (the FULL/flexible path: reminders, messages, "every day at sunset", conditional). Needs the always-on box (Epic 2). Complements the on-device-timer path above.
- ✅ **Shared memory SHIPPED 2026-07-12** (merged to main, 248 tests): `remember`/`recall`/`forget` over an append-only `FactStore` (SQLite, connection-per-op) in `src/home_agent/facts.py`. Explicit-only capture; recall returns all active facts newest-first and the model reasons over them (Approach A); forget flips a status (reversible), retiring one match / listing several / friendly on none; each fact records the author (per-turn `sender`) + timestamp. Tools built per-turn in `handle_message`; store created once at startup. Spec/plan: `docs/superpowers/{specs,plans}/2026-07-12-shared-memory*`. Fully offline-tested; ready for live Telegram smoke (no box needed).

## Epic E — finance-mcp (Discount scraper → local SQLite)
**Build order step 5.** Read-only. OpenAI is the insight layer. **Scope revised 2026-07-12** (see
`docs/superpowers/specs/2026-07-12-finance-discount-design.md`): dropped the self-hosted **Firefly III**
(Docker service + `firefly-iii-mcp`) in favour of a **lightweight local SQLite store** owned by the agent —
simpler, consistent with our other stores, and fully offline-testable. **Discount-only** to start (the one
audited scraper adapter); more banks/cards are a later add.
- [ ] **Node collector** pinning `israeli-bank-scrapers` (Discount only, read-only) → prints transactions as
  JSON → a Python importer upserts into a local SQLite `finance_store` (money as **integer agorot**; dedup by
  `(source, account, identifier)` with a normalized-fields hash fallback). Nightly on the box; run under an
  **egress allow-list to `start.telebank.co.il`** + pinned version.
- [ ] **In-agent tools** (no external MCP): `sync_finances`, `financial_summary`, `find_transactions`,
  `spending_by_category`, `set_category_rule`, `cash_flow_forecast` — categories **derived at read time** from
  `category_rules` (model classifies new merchants + persists a rule, like shopping `known_items`).
- [ ] Family financial context in the system prompt (income, savings goals, "unusual for us", trailing-3-month comparisons).
- [ ] Free-text Q&A ("eating out this month vs average?"), anomaly detection (duplicate charges, subscription creep).
- [ ] **Weekly summary**: Sunday 08:00 cron → agent "analyze the week" (pulls via MCP) → posts to Telegram.
- [ ] **Security gate:** verify finance tools are read-only; no transaction-executing capability exists.

## Epic F — payslip ingestion (הפרשות)
**Build order step 6.**
- [ ] Send payslip PDF to the bot → OpenAI **vision/PDF** extracts gross, net, employee/employer pension, keren hishtalmut, tax → `save_payslip` → SQLite.
- [ ] Enables: payslip error checking, real savings picture, gross-vs-net trends.
- [ ] Pension-projection calculator tool using real contribution data; quarterly fund statements the same way.

## Epic G — family-mcp Phase 2: live Israeli price layer
**Build order step 7.** (חוק שקיפות מחירים — chains publish price XML per branch.)
- [ ] Nightly cron pulls prices for our 2–3 branches (`israeli-supermarket-scrapers` or SuperGET API) into a local DB.
- [ ] Tools: `price_check(item)`, `compare_basket(list)` → "Rami Levy 342₪ vs Shufersal 389₪, but Shufersal has 1+1 on your coffee".
- [ ] Price-drop alerts on regularly-bought products.

## Epic H — Roborock Q Revo vacuum control (`roborock-mcp`)
Full natural-language control of the Roborock **Q Revo** robot vacuum (vac + mop, auto-empty/mop-wash dock).
Same in-process `Tool` pattern as `home-mcp`, behind an **injectable client seam** so tests stay offline.
**Goal:** *"תשאב את הסלון"*, *"תנקה את חדר השינה — שאיבה ואז שטיפה"*, *"תחזור לתחנה"*, *"תרוקן את המיכל"*,
*"תנקה כל יום בשמונה"*.
- **Library / auth:** `python-roborock` (cloud + local; powers the Home Assistant integration — confirm current
  version at build time). Log in with the Roborock account; discover devices; supports MQTT (cloud) + local.
- [x] **Auth + device/map discovery:** log in, list devices, pull the home **map + room segmentation**; cache a
  registry mapping **segment ids ↔ Hebrew room names** (סלון/מטבח/חדר שינה…), like `devices.yaml`. Deterministic
  registry; the model does the fuzzy room-name mapping (as with shopping `known_items`).
- [x] **Immediate control:** `start_clean` (whole home), `clean_rooms(rooms=[…])` (segment/room clean),
  `pause`/`resume`/`stop`, `return_to_dock`, `locate`.
- [x] **Cleaning plan:** suction/fan power (quiet/balanced/turbo/max), mop water-flow level, and clean **order** —
  vacuum-only / mop-only / **vac-then-mop** (שאיבה ואז שטיפה) — settable per run and per room.
- [x] **Dock actions:** empty dust bin, wash mop, dry mop.
- [x] **Status:** `vacuum_status` → state, battery %, area/time cleaned, current room, error state.
- [x] **Scheduling:** one-off + recurring cleans — prefer the robot's own on-device schedules where supported
  (offline-robust, like the SwitchBot on-device timers); otherwise route through `schedule_task`/cron (needs the box, per D3).
- [x] **Consumables (optional):** brush/filter/sensor life readouts → feed maintenance reminders.
- **Notes:** cloud API ⇒ immediate control works without the box; only 24/7 cron scheduling needs it. Follows the
  BLE/vision testing convention — inject a **fake roborock client** (no network) mirroring `actuate_fn`/`write_fn`.
- **Shipped:** cloud (MQTT) transport, python-roborock 5.x, token-file auth (`scripts/roborock_login.py`).
  **Live-verified 2026-07-12** on "Roborock Qrevo Edge Series" (a187): list_rooms, vacuum_status,
  consumables, locate, `clean(סלון)` segment clean, return_to_dock — all confirmed on the physical robot.
  Deferred: local (LAN) transport; per-room plans; recurring **server-timer scheduling** (`set_timer`
  raises a friendly error for now — route via cron/box later); consumable %s are estimates vs. standard
  lifetimes.

## Epic I — Sensibo Sky AC control (`sensibo-mcp`)
Stateful control of the **Sensibo Sky** (Wi-Fi IR controller for the AC) via its official cloud API — full
mode/temperature/fan control **plus** the Sky's built-in room temp/humidity sensor.
**Goal:** *"תדליק מזגן בחדר שינה על עשרים ושתיים, קור"*, *"כמה חם בסלון?"*, *"תכבה את המזגן"*.
- **Library / auth:** Sensibo **REST API v2** (`home.sensibo.com/api/v2`) via `pysensibo` (powers the Home
  Assistant integration — confirm at build time). Auth = a **Sensibo API key** (`SENSIBO_API_KEY` in `.env`).
- [ ] **Config + device discovery:** read `SENSIBO_API_KEY`; list pods; cache a registry mapping **pod ids ↔
    Hebrew room names**. Unset key → tools don't load (bot still runs), same graceful pattern as calendar.
- [ ] **Control:** `set_ac_power(on/off)`, `set_ac_mode(cool/heat/fan/dry/auto)`, `set_ac_temperature`,
    `set_ac_fan_level`, `set_ac_swing`.
- [ ] **Sense:** `sensibo_status` → current on/off + settings, and the Sky's **room temperature + humidity**.
- [ ] **Optional:** Climate React (threshold automation) enable/disable; simple presets ("לילה", "יציאה מהבית").
- [ ] **Scheduling** via `schedule_task`/cron (box) — "תדליק מזגן בחדר שינה בעשר בלילה על עשרים וארבע".
- **Notes / reconciliation:** this **supersedes/complements** the existing BLE SwitchBot מזגן *press-mode* control
  — the SwitchBot IR blast only toggles blindly, whereas Sensibo is **stateful** (knows mode/temp/setpoint and reads
  the room). Decide at build time whether Sensibo replaces the SwitchBot path for that AC or they coexist. Inject a
  **fake Sensibo client** for offline tests.

---

## Cross-cutting
- **Observability/cost:** log per-turn token usage (`response.usage`); watch the 24/7 spend; use effort/model tier + prompt caching to control it.
- **Testing:** keep the deterministic tools unit-tested (as today); mock the OpenAI loop with canned tool-call sequences; MCP servers testable in isolation.
- **Docs:** each MCP server + the agent get their own spec (brainstorm → plan) as we reach that epic.

## Suggested sequencing
Epic 0 (box) → A (agent spine + 1 tool) → B (home) → C+D (family + scheduling) → E (finance) → F (payslips) → G (prices). Each epic ships something usable on its own.
Epics H (Roborock) and I (Sensibo) are independent device integrations — slot them in whenever, no hard dependency (immediate control needs no box; only their recurring schedules do).
