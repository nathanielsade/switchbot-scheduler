# Home Family Agent — Roadmap

> A personal AI agent for the family, running 24/7 on an always-on home Linux machine,
> driven from a shared **Telegram group** in natural-language Hebrew. Controls the house,
> manages shared family life, and acts as household financial analyst.
> Full vision: see the Project Brief (this doc is the epics/tasks breakdown of it).

## Where we are today (becomes Module 1)
We already built a **natural-language SwitchBot scheduler** (repo `nathanielsade/switchbot-scheduler`):
- Prompt (He/En) → schedule → validate → encode → **Bluetooth** control of the Bots.
- CLI + web chat UI; conversational; one-time & recurring; per-device inversion (סלון) + press-mode (מזגן).
- **Brain today = one OpenAI call (a parser). NOT agentic.** Deterministic Python does the rest.
- Verified on real hardware; 62 tests.

The brief changes two foundational things:
1. **Brain: OpenAI single-call → Claude API agentic tool-use** (Claude decides which tools to call; no hardcoded routing).
2. **Interface: local web page → shared Telegram group.**
Everything we built (registry, validator, encoder, ble_writer, devices.yaml) is **reused** — rewrapped as `home-mcp` tools under the new agent.

## Target architecture
```
Telegram group (me + wife + bot)
      │  python-telegram-bot (~150 lines), allowlisted to our chat IDs
      ▼
Claude API — tool-use loop (agentic), model claude-opus-4-8
      │  standing system prompt = family context (prompt-cached)
      ├── home-mcp     (AC, lights, scenes)      ← wraps our existing code
      ├── family-mcp   (calendar, reminders, shopping list, shared memory)
      └── finance-mcp  (wrapper over Firefly III)
  + SQLite (conversation/shared memory, shopping history, payslips)
  + cron  (schedule_task tool → future runs of the agent)
```
Single agent, ~20 tools, multiple small MCP servers (fault isolation + separate credentials + reuse). Code organized so any domain can later become a sub-agent.

---

## Key decisions & reconciliations (resolve before/while building)

**D1 — Claude tool-use, local MCP servers.** Use the Anthropic Python SDK's local-MCP path: run each MCP server as a local process, convert its tools with `anthropic.lib.tools.mcp` (`mcp_tool`/`async_mcp_tool`) and drive the loop with `client.beta.messages.tool_runner(...)`. (The hosted `mcp_servers` connector is for *remote* servers; ours are local, so use the SDK helpers.)

**D2 — Model.** Default **`claude-opus-4-8`** (adaptive thinking for anything non-trivial; `output_config.effort` to tune cost). For a 24/7 personal agent, cost is real — **`claude-sonnet-5`** (cheaper, near-Opus) or **`claude-haiku-4-5`** (cheapest, for simple turns) are valid if you choose them. **Prompt-cache the family-context system prompt** (it's sent every turn) to cut cost ~90% on that prefix.

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

## Epic A — Agent core (Telegram ↔ Claude tool-use loop)
The brain transplant + interface. **Build order step 1.**
- [ ] Telegram bot skeleton (`python-telegram-bot`), **allowlist** our chat IDs / group ID; ignore everyone else.
- [ ] Claude tool-use loop: send message + tool defs → run tools → feed results → repeat until `end_turn` (SDK `tool_runner`); `claude-opus-4-8`, adaptive thinking.
- [ ] Standing **system prompt** = family context (who we are, tone, Hebrew responses); **prompt-cached**.
- [ ] SQLite conversation history / shared memory (stateless API → resend context each turn).
- [ ] **Vertical slice:** wire ONE simple tool (`set_ac`) end-to-end — Telegram → Claude → tool → Bot → reply. Proves the whole spine before adding modules.

## Epic B — home-mcp (wrap the existing smart-home code)
**Build order step 2.** Reuse registry/validator/encoder/ble_writer.
- [ ] `home-mcp` server exposing tools: `set_ac(room,state)`, `set_light(room,state)`, `press(device)`, `home_status()`.
- [ ] Scenes: `sleep_mode`, `leaving_home` (compose multiple device actions).
- [ ] Natural-language scheduling routed through `schedule_task` (per D3), e.g. "turn on the living-room AC at 17:30".
- [ ] Carry over device quirks: inversion (סלון), press-mode (מזגן).

## Epic C — family-mcp: reminders + shopping list + calendar
**Build order step 3.**
- [ ] Shared **calendar** (Google Calendar API): "what do we have this week?"
- [ ] **Reminders**: "remind us tomorrow evening to pay arnona" → `schedule_task` → Telegram message at time T.
- [ ] **Smart shopping list** (SQLite, shared): add/remove in chat; both see the same list.
  - [ ] Every purchased item stored **with a date** → learn purchase cycles ("milk every ~5 days").
  - [ ] On shopping day, propose a list from history ("probably out of: milk, eggs, bread").
  - [ ] **Receipt photos** → Claude **vision** reads them, updates what was bought + cost.

## Epic D — schedule_task + shared memory
**Build order step 4.** (Reminders/scheduling in C depend on `schedule_task`; formalize it here.)
- [ ] `schedule_task(cron, prompt)` tool → writes a cron entry that re-invokes the agent with that prompt. Every capability becomes schedulable for free.
- [ ] `remember(fact)` / `recall(question)` over SQLite ("where did we put the passports?").

## Epic E — finance-mcp (Firefly III + bank importer)
**Build order step 5.** Read-only. Claude is the insight layer.
- [ ] **Firefly III** self-hosted (Docker) on the home box; category rule engine ("שופרסל" → groceries).
- [ ] **`israeli-bank-scrapers`** nightly cron (all major IL banks/cards incl. 2FA) → importer → Firefly.
- [ ] **`firefly-iii-mcp`** (existing OSS, via `npx`) → agent queries transactions/budgets/insights.
- [ ] Family financial context in the system prompt (income, savings goals, "unusual for us", trailing-3-month comparisons).
- [ ] Free-text Q&A ("eating out this month vs average?"), anomaly detection (duplicate charges, subscription creep).
- [ ] **Weekly summary**: Sunday 08:00 cron → agent "analyze the week" (pulls via MCP) → posts to Telegram.
- [ ] **Security gate:** verify finance tools are read-only; no transaction-executing capability exists.

## Epic F — payslip ingestion (הפרשות)
**Build order step 6.**
- [ ] Send payslip PDF to the bot → Claude **vision/PDF** extracts gross, net, employee/employer pension, keren hishtalmut, tax → `save_payslip` → SQLite.
- [ ] Enables: payslip error checking, real savings picture, gross-vs-net trends.
- [ ] Pension-projection calculator tool using real contribution data; quarterly fund statements the same way.

## Epic G — family-mcp Phase 2: live Israeli price layer
**Build order step 7.** (חוק שקיפות מחירים — chains publish price XML per branch.)
- [ ] Nightly cron pulls prices for our 2–3 branches (`israeli-supermarket-scrapers` or SuperGET API) into a local DB.
- [ ] Tools: `price_check(item)`, `compare_basket(list)` → "Rami Levy 342₪ vs Shufersal 389₪, but Shufersal has 1+1 on your coffee".
- [ ] Price-drop alerts on regularly-bought products.

---

## Cross-cutting
- **Observability/cost:** log per-turn token usage (`response.usage`); watch the 24/7 spend; use effort/model tier + prompt caching to control it.
- **Testing:** keep the deterministic tools unit-tested (as today); mock the Claude loop with canned tool-call sequences; MCP servers testable in isolation.
- **Docs:** each MCP server + the agent get their own spec (brainstorm → plan) as we reach that epic.

## Suggested sequencing
Epic 0 (box) → A (agent spine + 1 tool) → B (home) → C+D (family + scheduling) → E (finance) → F (payslips) → G (prices). Each epic ships something usable on its own.
