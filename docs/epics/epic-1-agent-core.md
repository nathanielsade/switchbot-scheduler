# Epic 1 — Agent Core (Telegram ↔ Claude tool-use loop)

**Epic goal:** A minimal, runnable agent spine — a Telegram bot (allowlisted to the family)
that sends each message to Claude in an agentic **tool-use loop**, lets Claude decide which
tool to call, executes it, and replies. Proven end-to-end on the Mac with ONE throwaway tool.
Everything later (home/family/finance) plugs into this spine as more tools.

**Epic Definition of Done:** From the family Telegram group on your phone, you send a message;
the Mac-run agent replies. Asking "what time is it?" makes Claude call the `get_current_time`
tool and answer with the real time. A second message shows it remembered the first. A message
from a non-allowlisted chat is ignored.

**Out of scope for Epic 1:** MCP servers, the home box, real hardware, any real capability.
One in-process fake tool only. (MCP + real tools start in Epic 3.)

---

## Prerequisites & decisions (confirm before building)

- **P1 — Anthropic API key.** The brain is now Claude, not OpenAI. You need an `ANTHROPIC_API_KEY`
  (console.anthropic.com). *Do you have one?* This blocks everything in Epic 1.
- **P2 — Telegram bot token + chat IDs.** Create a bot via **@BotFather** → token. Add it to your
  family group. We capture the **group chat ID** (and your two user IDs) for the allowlist.
- **D-client — Claude loop style:** use the Anthropic SDK **manual tool-use loop** (not the
  auto `tool_runner`). Rationale: we'll soon have side-effecting home tools and read-only finance
  tools, so we want explicit control over execution/logging/approval. *Recommend manual loop; ok?*
- **D-repo — where the code lives now:** add an `agent/` package **in this repo for now**; defer
  the `home-agent` monorepo rename until Epic 3 (when the scheduler becomes `home-mcp`). *Ok?*
- **Model:** `claude-opus-4-8`, adaptive thinking. (Cost tier is a later call; caching the system prompt is in T1.5.)

---

## Tasks

### Task 1.0 — Prerequisites in place
- **Goal:** Have the two credentials and the allowlist values available locally.
- **Context:** User action, not code. Anthropic key + Telegram bot token + the family group chat ID and both user IDs. Stored in a git-ignored `.env`.
- **Definition of Done:** `.env` contains `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS`; `.env` is git-ignored.
- **How to test:** `curl` a 1-token Claude message → HTTP 200; Telegram `getMe` on the token returns the bot; the group chat ID resolves (a test message to the group is seen by `getUpdates`).

### Task 1.1 — Agent package scaffold
- **Goal:** A runnable `agent/` Python package with deps and config loading.
- **Context:** New package in this repo (per D-repo). Deps: `anthropic`, `python-telegram-bot`; stdlib `sqlite3`. Config module loads `.env` (reuse our `python-dotenv` pattern): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS` (parsed to a set of ints).
- **Definition of Done:** `pip install -e ".[dev]"` succeeds; `config.load()` returns the parsed allowlist; a sanity test imports the package.
- **How to test:** `pytest agent/tests/test_config.py` — asserts a sample `.env` parses `ALLOWED_CHAT_IDS` into `{111,222}`.

### Task 1.2 — Claude tool-use loop (pure core, injectable client)
- **Goal:** `run_turn(history, user_text, tools) -> (reply_text, new_history)` implementing the manual Anthropic tool-use loop.
- **Context:** The spine. `client` is injectable (real `anthropic.Anthropic()` by default; a fake in tests) so it's network-free to test — same seam idea as our old `completion_fn`. Uses `claude-opus-4-8`, `thinking={"type":"adaptive"}`, a `system` param, and a tool registry of `{schema, impl}`. Loops on `stop_reason == "tool_use"`: execute each `tool_use` block, append `tool_result`s (one user message), repeat until `end_turn`. Tool errors → `tool_result` with `is_error: true`.
- **Definition of Done:** Given a scripted fake client that returns a `tool_use` then an `end_turn`, `run_turn` calls the tool impl, feeds the result back, and returns the final text. Handles multiple/parallel tool_use blocks in one turn and a tool that raises.
- **How to test:** `pytest agent/tests/test_loop.py` — a fake client scripted with a tool_use→end_turn sequence; assert the tool ran once and the returned text is the model's final text; a second test with a raising tool asserts `is_error` was sent.

### Task 1.3 — First real tool: `get_current_time`
- **Goal:** A trivial, read-only tool that proves the loop drives a real function.
- **Context:** No side effects, no hardware, no MCP — just a Python function returning the local time, plus its JSON schema. Registered in the tool registry passed to `run_turn`.
- **Definition of Done:** Asking a time question makes the loop invoke `get_current_time` and produce a reply containing the returned time string.
- **How to test:** `pytest agent/tests/test_time_tool.py` — fake client scripted to call `get_current_time`; assert the reply contains the value the tool returned. (Real e2e happens in T1.7.)

### Task 1.4 — SQLite conversation memory
- **Goal:** Persist and load per-chat conversation so context carries across messages and process restarts.
- **Context:** SQLite file (git-ignored). Table `messages(id, chat_id, role, content, ts)`. `load(chat_id, limit)` returns recent turns for `run_turn`; `append(chat_id, role, content)` after each. (This is the durable "memory" the old history idea folds into.)
- **Definition of Done:** After turn 1 is saved, loading for the same chat returns it; a fresh process (new DB connection) still sees it; different chat_ids are isolated.
- **How to test:** `pytest agent/tests/test_memory.py` against a `tmp_path` DB — append → reload in a new connection → assert content + ordering + per-chat isolation.

### Task 1.5 — Family system prompt + prompt caching
- **Goal:** A standing family-context system prompt, sent every turn, prompt-cached to cut cost.
- **Context:** A `system` text block with `cache_control: {"type":"ephemeral"}`. Content is a placeholder family context (who we are, tone, "respond in Hebrew") we flesh out later — the mechanism is what matters now. Must be byte-stable across turns (no timestamps in it) so the cache actually hits.
- **Definition of Done:** Every Claude request includes the system block with `cache_control`; the block is identical across turns.
- **How to test:** `pytest agent/tests/test_system_prompt.py` — the injectable client captures the request args; assert `system` carries a `cache_control` breakpoint and is identical on two successive `run_turn` calls.

### Task 1.6 — Telegram adapter + allowlist
- **Goal:** Receive messages from the family group/users only, route to `run_turn`, reply; ignore everyone else.
- **Context:** `python-telegram-bot`, **long-polling** (outbound to Telegram — no public URL, no firewall, works from behind NAT; this is why Telegram beats the web page for remote). A message handler gates on `chat_id ∈ ALLOWED_CHAT_IDS`, loads memory, calls `run_turn`, appends, replies.
- **Definition of Done:** A handler invoked with an allowed chat calls `run_turn` and sends the reply; a non-allowlisted chat is ignored (logged, no Claude call).
- **How to test:** `pytest agent/tests/test_telegram.py` — call the handler with a faked allowed Update (assert reply sent, `run_turn` called via a stub) and a non-allowed Update (assert no reply, no `run_turn`).

### Task 1.7 — Assemble + run (entry point) & e2e
- **Goal:** An `agent` console command that starts the bot (long-poll) wiring loop + memory + tools + Telegram; the working vertical slice.
- **Context:** Reads `.env`, registers `get_current_time`, runs until Ctrl+C. Run on the Mac for now.
- **Definition of Done:** Running locally, texting the bot in the family group gets a reply; "what time is it?" triggers the tool and returns the real time; a follow-up shows memory carried; a non-member chat is ignored.
- **How to test:** **Manual e2e** — start `agent`, from your phone in the group: (1) "hi" → reply; (2) "what time is it?" → correct time via the tool; (3) a follow-up referencing (1) → shows memory. Confirm the full suite still passes (`pytest -q`).

---

## Notes
- Keeps the "brain proposes, code/human dispose" discipline: the loop executes tools; when we add side-effecting/finance tools later, T1.2's manual loop is where approval/read-only gating lives.
- After Epic 1's slice works, Epic 3 (home-mcp) is "expose the scheduler as tools and register them" — no spine changes.
