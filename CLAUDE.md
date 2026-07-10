# CLAUDE.md — smart-home

Project context for AI coding sessions. Durable facts & conventions only — **status lives in
`docs/ROADMAP.md` and the auto-loaded memory, not here.**

## What this is

A personal **Home Family Agent**: a 24/7 assistant driven from a shared **Telegram** group in
natural-language **Hebrew** (user + wife), running on a home machine. It grew out of a natural-language
**SwitchBot scheduler** (`src/switchbot_scheduler/`), which is now a reused library. The new agent lives
in `src/home_agent/`.

Two Python packages under `src/` (repo uses `pythonpath = ["src"]`):
- **`home_agent/`** — the agent: Telegram ↔ OpenAI function-calling loop + in-process tools + SQLite.
- **`switchbot_scheduler/`** — the existing BLE SwitchBot control/scheduling code. **Reused, not modified**
  by `home_agent` (registry, actuator, encoder, validator, ble_writer, model, readback).

## Architecture in one breath

Telegram message → `telegram_app.handle_message` → `agent.run_turn` (OpenAI **function-calling loop**:
model picks tools → we run them → feed results back → repeat until it answers) → tools act on SQLite /
BLE → Hebrew reply. A "capability" = a bundle of **in-process `Tool`s** + guidance in the system prompt.
**No separate services, no MCP servers** (yet); everything is in-process on SQLite.

## Run & test

```bash
# Run the bot (needs .env — see .env.example). ONE instance only (see gotchas).
PYTHONPATH=src .venv/bin/python -m home_agent

# Tests — this is the whole CI gate.
.venv/bin/pytest -q --ignore=integration_tests
```

- **No `ruff`/`mypy` in this project** — `pytest` is the only check. Don't assume they exist.
- **No network / BLE / OpenAI-vision in the automated suite.** Every side effect is behind an **injectable
  seam** filled with a fake in tests: `make_fake_client` (OpenAI, a pytest fixture in
  `tests/home_agent/conftest.py`), `now_fn` (clock), `actuate_fn` / `write_fn` (BLE), `vision_fn` (receipts).
- `.env` keys: `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS` (empty = discovery mode), plus
  optional `HOME_AGENT_MODEL`, `HOME_AGENT_DB`, `HOME_AGENT_OPENAI_TIMEOUT`, `SWITCHBOT_DEVICES`.

## Conventions (follow these)

- **Tools are `home_agent.tools.Tool(name, schema, impl)`.** `schema` is the OpenAI function schema — its
  `description` IS the model's instruction for when/how to use the tool. `impl(args: dict) -> str`.
- **Deterministic tools vs. model reasoning:** if it must be exactly right (storage, math, parsing) it's a
  **tool/Python**; if it needs language or judgment (interpreting Hebrew, mapping wording to a canonical
  item, composing the reply) it's the **model**, steered by the system prompt. Never put cost/date math in
  the model; never hard-code Hebrew mapping in Python.
- **SQLite stores are thread-safe by connection-per-operation** (a fresh `sqlite3.connect` per method via
  `contextlib.closing`) — because python-telegram-bot runs handlers in a worker thread (`asyncio.to_thread`).
  Pattern lives in `memory.Conversation`; mirror it (`ScheduleStore`, `ShoppingStore`).
- **Append-only data** where it's history: never `DELETE`; flip a `status` column / stamp a timestamp.
- **System prompt (`prompts.FAMILY_SYSTEM_PROMPT`)** must stay **digit-free** and **byte-stable** across
  turns (tests enforce both; byte-stability lets OpenAI prompt-cache the prefix). Respond in Hebrew.
- **Real actions are injectable**: BLE writes / vision calls import `bleak`/`openai` **lazily inside the
  function** so importing a module never touches hardware/network.

## Adding a capability (the recipe)

1. If it needs storage: a `*_store.py` (SQLite, connection-per-op, append-only) — see `shopping_store.py`.
2. A `build_*_tools(...)` factory returning `Tool`s (see `home.py`, `schedules.py`, `shopping.py`).
   Inject seams (`now_fn`, `write_fn`, …) so tests stay offline.
3. Compose it into the agent in `telegram_app.build_application` (chat-agnostic tools are built once at
   startup; anything needing per-chat state must be bound per turn in `handle_message`).
4. TDD: tests under `tests/home_agent/`, no network, frozen clock where time matters.

## How work is done here

Features go **brainstorm → spec (`docs/superpowers/specs/`) → plan (`docs/superpowers/plans/`) → build**,
usually via **subagent-driven development** (fresh subagent per task + review gates + a final whole-branch
review), each on a `feat/*` branch merged to `main`. The SDD build ledger is `.superpowers/sdd/progress.md`.

## Doc map (read these for detail — don't duplicate them here)

- `docs/ROADMAP.md` — the plan + **current status** of every epic.
- `docs/architecture/` — holistic architecture explainers (e.g. `shopping-list.md`).
- `docs/superpowers/specs/` — approved designs; `docs/superpowers/plans/` — TDD implementation plans.
- `docs/epics/`, `docs/superpowers/plans/` — per-epic breakdowns.
- Memory (auto-loaded): `~/.claude/projects/-Users-netanelsade-smart-home/memory/` — decisions, gotchas.
- `src/home_agent/CLAUDE.md` — the agent package's module map + conventions.

## Gotchas (these have bitten us)

- **Run exactly ONE bot instance** — two → `telegram.error.Conflict` (getUpdates collision). To kill
  stragglers, match **case-insensitively**: the process binary is `.../Python -m home_agent` (capital P),
  so `pkill -f "python -m home_agent"` (lowercase) SILENTLY MISSES them. Use
  `ps aux | grep -iE "[Pp]ython -m home_agent" | grep -v grep | awk '{print $2}' | xargs kill -9`.
- **`devices.yaml` holds macOS CoreBluetooth UUIDs** — a Linux host needs a re-scan to MAC addresses.
- **`.env`/creds:** the OpenAI + Telegram creds were once exposed in a chat; rotation is a pending TODO.
  Never echo secrets; `.env`, `*.db`, `secrets.yaml` are git-ignored.
- The `.venv` runs **Python 3.14**; code targets **3.11+**.
