# Epic 1 — Telegram phase Implementation Plan (tasks 1.6–1.7)

> Stacked on `feat/epic-1-agent-core`. Builds the Telegram front-end for the OpenAI agent core.
> REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Wire the existing `run_turn` agent to a Telegram bot for a private family group, gated by an allow-list, with auto-discovery of chat IDs.

**Architecture:** `python -m home_agent` loads `Config`, builds a `python-telegram-bot` `Application` (long-poll), and routes each text message through a sync `handle_message()` that enforces the allow-list, loads per-chat memory, calls `run_turn`, replies, and persists. Blocking agent work runs in a thread so the async event loop stays responsive.

**Tech Stack:** python-telegram-bot >=21 (installed: 22.8), OpenAI, SQLite (existing `Conversation`), the existing `home_agent` package.

## Global Constraints
- Provider is **OpenAI** (reuse the agent core; do NOT introduce Anthropic).
- `home_agent` stays self-contained — no `switchbot_scheduler` import.
- Portable: no hardcoded absolute paths; machine values only via `.env`; requires-python >=3.11.
- Secrets only from env/.env (already gitignored). Never log message text at INFO beyond its length; never log the token.
- Tests must not hit the network (no real Telegram, no real OpenAI). Use the `make_fake_client` fixture + `tmp_path`.
- Allow-list semantics: empty `allowed_chat_ids` ⇒ **discovery mode** (reply the chat's own ID, do NOT run the agent). Non-empty ⇒ ignore any chat not listed (return None, no reply).

---

## Task 1.6-core — `handle_message` (allow-list + memory + agent)
**Files:** Create `src/home_agent/telegram_app.py` (handler portion), `tests/home_agent/test_telegram_handler.py`
**Interfaces:**
- Consumes: `run_turn` (agent.py), `Conversation` (memory.py), `DEFAULT_TOOLS` (tools.py), `FAMILY_SYSTEM_PROMPT` (prompts.py), `Config` (config.py).
- Produces: `handle_message(chat_id:int, text:str, *, config, conversation, client, tools=DEFAULT_TOOLS, system=FAMILY_SYSTEM_PROMPT, model=None) -> str | None`.

- [ ] **Step 1: Write the failing test** — `tests/home_agent/test_telegram_handler.py`:
```python
import pytest
from home_agent.config import Config
from home_agent.memory import Conversation
from home_agent.telegram_app import handle_message


def _cfg(tmp_path, allowed):
    return Config(openai_api_key="x", telegram_bot_token="t:t", allowed_chat_ids=set(allowed),
                  model="gpt-4o", db_path=str(tmp_path / "m.db"))


def test_allowed_chat_runs_agent_persists_and_replies(tmp_path, make_fake_client):
    client = make_fake_client([{"content": "שלום"}])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "היי", config=_cfg(tmp_path, {1}), conversation=conv, client=client)
    assert reply == "שלום"
    assert conv.load(1) == [{"role": "user", "content": "היי"},
                            {"role": "assistant", "content": "שלום"}]


def test_unauthorized_chat_ignored_no_side_effects(tmp_path, make_fake_client):
    client = make_fake_client([{"content": "should not happen"}])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(999, "hi", config=_cfg(tmp_path, {1}), conversation=conv, client=client)
    assert reply is None
    assert conv.load(999) == []
    assert client._calls == []  # agent never invoked


def test_discovery_mode_reveals_chat_id_without_running_agent(tmp_path, make_fake_client):
    client = make_fake_client([{"content": "nope"}])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(-100123, "anything", config=_cfg(tmp_path, set()), conversation=conv, client=client)
    assert "-100123" in reply
    assert client._calls == []
    assert conv.load(-100123) == []


def test_history_loaded_before_appending_current_message(tmp_path, make_fake_client):
    client = make_fake_client([{"content": "ok"}])
    conv = Conversation(str(tmp_path / "m.db"))
    conv.append(1, "user", "old")
    conv.append(1, "assistant", "prev")
    handle_message(1, "new", config=_cfg(tmp_path, {1}), conversation=conv, client=client)
    sent = client._calls[0]["messages"]
    assert [m["content"] for m in sent if m["content"] == "new"] == ["new"]  # current appears once
    assert any(m["content"] == "old" for m in sent)  # prior history present


def test_agent_error_returns_friendly_message_and_does_not_persist(tmp_path):
    class Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("openai down")
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "hi", config=_cfg(tmp_path, {1}), conversation=conv, client=Boom())
    assert isinstance(reply, str) and reply.strip()
    assert reply != "hi"
    assert conv.load(1) == []  # nothing persisted on failure
```
- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=src pytest tests/home_agent/test_telegram_handler.py -v` → FAIL (no `home_agent.telegram_app`).
- [ ] **Step 3: Implement** — create `src/home_agent/telegram_app.py` with (handler portion only for this task):
```python
import logging

from .agent import run_turn
from .prompts import FAMILY_SYSTEM_PROMPT
from .tools import DEFAULT_TOOLS

log = logging.getLogger("home_agent")

_ERROR_REPLY = "מצטער, נתקלתי בתקלה רגעית. נסו שוב עוד רגע."  # "sorry, momentary glitch, try again"


def handle_message(chat_id, text, *, config, conversation, client,
                   tools=DEFAULT_TOOLS, system=FAMILY_SYSTEM_PROMPT, model=None):
    """Process one inbound Telegram text message. Returns the reply text, or None to stay silent.
    Sequencing note: history is loaded BEFORE the current message is persisted, so the current
    turn is not duplicated in the model context."""
    model = model or config.model
    log.info("incoming chat_id=%s text_len=%d", chat_id, len(text or ""))
    if not config.allowed_chat_ids:  # discovery mode: allow-list not configured yet
        log.warning("discovery mode (empty allow-list): revealing chat_id=%s", chat_id)
        return (f"\U0001F44B chat_id = {chat_id}\n"
                "Add it to ALLOWED_CHAT_IDS in .env and restart me to activate.")
    if chat_id not in config.allowed_chat_ids:
        log.warning("ignoring message from unauthorized chat_id=%s", chat_id)
        return None
    history = conversation.load(chat_id)
    try:
        reply = run_turn(text, history, client=client, model=model, system=system, tools=tools)
    except Exception:
        log.exception("agent error for chat_id=%s", chat_id)
        return _ERROR_REPLY
    conversation.append(chat_id, "user", text)
    conversation.append(chat_id, "assistant", reply)
    return reply
```
- [ ] **Step 4: Run to verify it passes** — `PYTHONPATH=src pytest tests/home_agent/test_telegram_handler.py -v` → PASS; full suite green.
- [ ] **Step 5: Commit** — `git add src/home_agent/telegram_app.py tests/home_agent/test_telegram_handler.py && git commit -m "feat(agent): Telegram message handler (allow-list, memory, discovery mode)"`

---

## Task 1.6-wire + 1.7-entry — `build_application`, entry point, `.env.example`
**Files:** Modify `src/home_agent/telegram_app.py` (add `build_application`), create `src/home_agent/__main__.py`, modify `pyproject.toml` (console script), modify `.env.example`, create `tests/home_agent/test_telegram_app.py`
**Interfaces:**
- Consumes: `handle_message` (this module), `Config`/`load_config` (config.py), `Conversation` (memory.py), OpenAI client.
- Produces: `build_application(config, *, client=None, conversation=None) -> telegram.ext.Application`; `main()` in `home_agent.__main__`.

- [ ] **Step 1: Write the failing test** — `tests/home_agent/test_telegram_app.py`:
```python
from telegram.ext import Application
from home_agent.config import Config
from home_agent.memory import Conversation
from home_agent.telegram_app import build_application


def _cfg(tmp_path):
    # token must be BotFather-shaped ("<digits>:<rest>") for python-telegram-bot to accept it
    return Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                  allowed_chat_ids={1}, model="gpt-4o", db_path=str(tmp_path / "m.db"))


def test_build_application_registers_one_text_handler(tmp_path, make_fake_client):
    app = build_application(_cfg(tmp_path), client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    assert sum(len(hs) for hs in app.handlers.values()) == 1  # exactly one message handler, no network
```
- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=src pytest tests/home_agent/test_telegram_app.py -v` → FAIL (no `build_application`).
- [ ] **Step 3: Implement**
  Append to `src/home_agent/telegram_app.py`:
```python
import asyncio

from telegram.ext import Application, MessageHandler, filters

from .memory import Conversation


def build_application(config, *, client=None, conversation=None):
    """Build the long-poll Telegram Application. Injectable client/conversation for tests
    (no network is touched until .run_polling())."""
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=config.openai_api_key)
    if conversation is None:
        conversation = Conversation(config.db_path)
    app = Application.builder().token(config.telegram_bot_token).build()

    async def on_message(update, context):
        message = update.effective_message
        if message is None or update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        reply = await asyncio.to_thread(
            handle_message, chat_id, message.text or "",
            config=config, conversation=conversation, client=client)
        if reply:
            await message.reply_text(reply)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app
```
  Create `src/home_agent/__main__.py`:
```python
import logging

from .config import load_config
from .telegram_app import build_application


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("home_agent")
    config = load_config()
    if not config.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (put it in .env).")
    if not config.allowed_chat_ids:
        log.warning("ALLOWED_CHAT_IDS is empty — DISCOVERY MODE: I will reply with each "
                    "chat's ID and will NOT run the agent until the allow-list is set.")
    log.info("home-agent starting (model=%s, db=%s)", config.model, config.db_path)
    app = build_application(config)
    app.run_polling()


if __name__ == "__main__":
    main()
```
  Add to `pyproject.toml` under `[project.scripts]`:
```
home-agent = "home_agent.__main__:main"
```
  Update `.env.example` — append (values illustrative, not real):
```
# --- Home Family Agent (Telegram) ---
TELEGRAM_BOT_TOKEN=123456:your-botfather-token
# Space/comma-separated Telegram chat IDs allowed to command the bot (your family group).
# Leave EMPTY to run in discovery mode: the bot replies with each chat's ID so you can fill this in.
ALLOWED_CHAT_IDS=
# Optional overrides:
HOME_AGENT_MODEL=gpt-4o
HOME_AGENT_DB=home_agent.db
```
- [ ] **Step 4: Run to verify it passes** — `PYTHONPATH=src pytest tests/home_agent/test_telegram_app.py -v` → PASS; full suite green.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(agent): Telegram Application wiring + home-agent entry point + .env.example"`

---

## Manual live e2e (with the user — needs the phone; not automated)
1. In `@BotFather`: `/setprivacy` → select the bot → **Disable** (so it can read all messages in a group, not just @mentions).
2. Create a Telegram group, add `@Netanel_saray_bot`.
3. Run (discovery mode, allow-list still empty): `PYTHONPATH=src .venv/bin/python -m home_agent`
4. Send any message in the group → the bot replies with `chat_id = -100…`. Copy that number.
5. Stop the bot, set `ALLOWED_CHAT_IDS=<that id>` in `.env`, restart.
6. Send "מה השעה?" → the agent should call `get_current_time` and reply in Hebrew with the time; a follow-up should show memory carrying.

## Self-Review
- Spec coverage: allow-list gate ✔, discovery mode ✔, memory load-before-append ✔, agent-error resilience ✔, offline tests for handler + wiring ✔, entry point ✔, portability (`.env`, `python -m`) ✔.
- Deferred/none: real-network paths are exercised only in the manual live e2e by design.
