# Epic 1 — Agent Core Implementation Plan (tasks 1.1–1.5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the offline-testable spine of the family agent — an OpenAI function-calling loop with a tool registry, SQLite conversation memory, and a stable family system prompt — as a new `home_agent` package. No Telegram, no live API, no hardware.

**Architecture:** New package `src/home_agent/`. A pure `run_turn(...)` runs the OpenAI Chat Completions **function-calling loop** with an **injectable client** (real `openai.OpenAI()` in prod; a scripted fake in tests) so the whole loop is verified network-free. One throwaway `get_current_time` tool proves tool execution. SQLite stores per-chat conversation. Config + secrets come from `.env`.

**Tech Stack:** Python 3.11+, `openai` (already a dep), `python-dotenv` (already a dep), stdlib `sqlite3`, `pytest`.

## Global Constraints
- **Portability:** pure Python, runs on Linux unchanged. No hardcoded absolute paths — DB/config paths from `.env` or relative. Machine-specific values only in `.env` (git-ignored).
- **Provider:** OpenAI. Model default `gpt-4o` (via `HOME_AGENT_MODEL`, swappable). No Anthropic.
- **Test seam:** the OpenAI client is injected into `run_turn`; tests pass a scripted fake — **no network in tests**.
- **Scope:** tasks 1.1–1.5 only. Telegram adapter (1.6) + live e2e (1.7) are a separate later phase (need the bot token).
- **Package import root:** `src/`; tests run with the repo's configured `pythonpath=["src"]`. New package is `home_agent` (sibling of `switchbot_scheduler`), self-contained (does not import `switchbot_scheduler`).

---

## File structure
```
src/home_agent/__init__.py
src/home_agent/config.py     # load_config() -> Config from .env
src/home_agent/prompts.py    # FAMILY_SYSTEM_PROMPT (stable placeholder)
src/home_agent/tools.py      # Tool dataclass, get_current_time, DEFAULT_TOOLS
src/home_agent/agent.py      # run_turn(...) — the function-calling loop
src/home_agent/memory.py     # Conversation (SQLite)
tests/home_agent/fakes.py    # make_fake_client(script)
tests/home_agent/test_config.py
tests/home_agent/test_loop.py
tests/home_agent/test_time_tool.py
tests/home_agent/test_memory.py
tests/home_agent/test_system_prompt.py
```

---

## Task 1.1 — Package scaffold + config
**Files:** Create `src/home_agent/__init__.py`, `src/home_agent/config.py`, `tests/home_agent/__init__.py`, `tests/home_agent/test_config.py`
**Interfaces:**
- Produces: `Config` dataclass `(openai_api_key:str, telegram_bot_token:str, allowed_chat_ids:set[int], model:str="gpt-4o", db_path:str="home_agent.db")`; `load_config(path: str | None = None) -> Config`.

- [ ] **Step 1: Write the failing test** — `tests/home_agent/test_config.py`:
```python
def test_load_config_parses_allowlist(tmp_path, monkeypatch):
    for k in ("ALLOWED_CHAT_IDS", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "HOME_AGENT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text('OPENAI_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN=tok\nALLOWED_CHAT_IDS=111, 222\n')
    from home_agent.config import load_config
    cfg = load_config(str(env))
    assert cfg.openai_api_key == "sk-x"
    assert cfg.telegram_bot_token == "tok"
    assert cfg.allowed_chat_ids == {111, 222}
    assert cfg.model == "gpt-4o"  # default
```
- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=src pytest tests/home_agent/test_config.py -v` → FAIL (ModuleNotFoundError: home_agent).
- [ ] **Step 3: Implement** — `src/home_agent/__init__.py`:
```python
"""Family home agent."""
```
`src/home_agent/config.py`:
```python
import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Config:
    openai_api_key: str
    telegram_bot_token: str
    allowed_chat_ids: set[int]
    model: str = "gpt-4o"
    db_path: str = "home_agent.db"


def load_config(path: str | None = None) -> Config:
    load_dotenv(dotenv_path=path, override=False)
    raw = os.environ.get("ALLOWED_CHAT_IDS", "")
    allowed = {int(x) for x in raw.replace(",", " ").split() if x.strip()}
    return Config(
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        allowed_chat_ids=allowed,
        model=os.environ.get("HOME_AGENT_MODEL", "gpt-4o"),
        db_path=os.environ.get("HOME_AGENT_DB", "home_agent.db"),
    )
```
`tests/home_agent/__init__.py`: empty file.
- [ ] **Step 4: Run to verify it passes** — `PYTHONPATH=src pytest tests/home_agent/test_config.py -v` → PASS. Then `PYTHONPATH=src pytest -q` → all pass.
- [ ] **Step 5: Commit** — `git add src/home_agent tests/home_agent && git commit -m "feat(agent): package scaffold + .env config"`

---

## Task 1.2 — Function-calling loop (`run_turn`) + fake client
**Files:** Create `src/home_agent/agent.py`, `tests/home_agent/fakes.py`, `tests/home_agent/test_loop.py`
**Interfaces:**
- Consumes: nothing from other tasks (tools passed in as a list of objects with `.name`, `.schema`, `.impl`).
- Produces: `run_turn(user_text: str, history: list[dict], *, client, model: str, system: str, tools: list) -> str` (final assistant text). `history` is a list of `{"role","content"}` (user/assistant only); tool plumbing stays internal to the turn.
- `make_fake_client(script)` test helper: `script` is a list; each item is `{"tool_calls": [{"id","name","arguments":dict}]}` or `{"content": "text"}`. Exposes `client._calls` (the kwargs of each `create`).

- [ ] **Step 1: Write the fake + failing tests** — `tests/home_agent/fakes.py`:
```python
import json
from types import SimpleNamespace


def make_fake_client(script):
    calls = []
    state = {"i": 0}

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            r = script[state["i"]]
            state["i"] += 1
            if "tool_calls" in r:
                tcs = [SimpleNamespace(
                    id=tc["id"], type="function",
                    function=SimpleNamespace(name=tc["name"], arguments=json.dumps(tc["arguments"])),
                ) for tc in r["tool_calls"]]
                msg = SimpleNamespace(role="assistant", content=None, tool_calls=tcs)
            else:
                msg = SimpleNamespace(role="assistant", content=r["content"], tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()), _calls=calls)
```
`tests/home_agent/test_loop.py`:
```python
from types import SimpleNamespace
from home_agent.agent import run_turn
from tests.home_agent.fakes import make_fake_client


def _tool(name, impl):
    schema = {"type": "function", "function": {"name": name, "description": name,
              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    return SimpleNamespace(name=name, schema=schema, impl=impl)


def test_loop_executes_tool_then_returns_final_text():
    ran = []
    tool = _tool("do_it", lambda args: ran.append(args) or "tool-output")
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "do_it", "arguments": {}}]},
        {"content": "final answer"},
    ])
    reply = run_turn("hi", [], client=client, model="gpt-4o", system="S", tools=[tool])
    assert reply == "final answer"
    assert ran == [{}]                     # tool impl ran once
    # second request carried the tool result back to the model
    second_msgs = client._calls[1]["messages"]
    assert any(m["role"] == "tool" and m["content"] == "tool-output" for m in second_msgs)


def test_loop_returns_tool_error_as_result():
    def boom(args):
        raise RuntimeError("kaboom")
    tool = _tool("do_it", boom)
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "do_it", "arguments": {}}]},
        {"content": "handled"},
    ])
    reply = run_turn("hi", [], client=client, model="gpt-4o", system="S", tools=[tool])
    assert reply == "handled"
    tool_msg = next(m for m in client._calls[1]["messages"] if m["role"] == "tool")
    assert "kaboom" in tool_msg["content"]
```
- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=src pytest tests/home_agent/test_loop.py -v` → FAIL (no module `home_agent.agent`).
- [ ] **Step 3: Implement** — `src/home_agent/agent.py`:
```python
import json


def run_turn(user_text, history, *, client, model, system, tools):
    """Run one agentic turn: OpenAI function-calling loop until the model stops calling tools.
    Returns the final assistant text. Tool plumbing stays internal to this turn."""
    tool_by_name = {t.name: t for t in tools}
    schemas = [t.schema for t in tools]
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_text}]
    while True:
        kwargs = {"model": model, "messages": messages}
        if schemas:
            kwargs["tools"] = schemas
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        assistant = {"role": "assistant", "content": msg.content or ""}
        if tool_calls:
            assistant["tool_calls"] = [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            } for tc in tool_calls]
        messages.append(assistant)
        if not tool_calls:
            return msg.content or ""
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                tool = tool_by_name.get(tc.function.name)
                result = tool.impl(args) if tool else f"error: unknown tool {tc.function.name}"
            except Exception as e:
                result = f"error: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
```
- [ ] **Step 4: Run to verify it passes** — `PYTHONPATH=src pytest tests/home_agent/test_loop.py -v` → PASS; full suite green.
- [ ] **Step 5: Commit** — `git add src/home_agent/agent.py tests/home_agent/fakes.py tests/home_agent/test_loop.py && git commit -m "feat(agent): OpenAI function-calling loop (injectable client)"`

---

## Task 1.3 — First tool: `get_current_time`
**Files:** Create `src/home_agent/tools.py`, `tests/home_agent/test_time_tool.py`
**Interfaces:**
- Consumes: `run_turn` (1.2).
- Produces: `Tool` dataclass `(name:str, schema:dict, impl:Callable[[dict],str])`; `get_current_time` (a `Tool`); `DEFAULT_TOOLS: list[Tool]`.

- [ ] **Step 1: Write the failing test** — `tests/home_agent/test_time_tool.py`:
```python
from home_agent.agent import run_turn
from home_agent.tools import DEFAULT_TOOLS, get_current_time
from tests.home_agent.fakes import make_fake_client


def test_get_current_time_returns_nonempty_string():
    out = get_current_time.impl({})
    assert isinstance(out, str) and out.strip()


def test_loop_can_call_get_current_time():
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "get_current_time", "arguments": {}}]},
        {"content": "it is that time"},
    ])
    reply = run_turn("what time is it?", [], client=client, model="gpt-4o", system="S", tools=DEFAULT_TOOLS)
    assert reply == "it is that time"
    tool_msg = next(m for m in client._calls[1]["messages"] if m["role"] == "tool")
    assert tool_msg["content"].strip()  # the real time string was fed back
```
- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=src pytest tests/home_agent/test_time_tool.py -v` → FAIL (no `home_agent.tools`).
- [ ] **Step 3: Implement** — `src/home_agent/tools.py`:
```python
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


@dataclass
class Tool:
    name: str
    schema: dict
    impl: Callable[[dict], str]


get_current_time = Tool(
    name="get_current_time",
    schema={"type": "function", "function": {
        "name": "get_current_time",
        "description": "Return the current local date and time.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    impl=lambda args: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
)

DEFAULT_TOOLS = [get_current_time]
```
- [ ] **Step 4: Run to verify it passes** — `PYTHONPATH=src pytest tests/home_agent/test_time_tool.py -v` → PASS; full suite green.
- [ ] **Step 5: Commit** — `git add src/home_agent/tools.py tests/home_agent/test_time_tool.py && git commit -m "feat(agent): get_current_time tool + DEFAULT_TOOLS"`

---

## Task 1.4 — SQLite conversation memory
**Files:** Create `src/home_agent/memory.py`, `tests/home_agent/test_memory.py`
**Interfaces:**
- Produces: `Conversation(db_path: str)` with `.append(chat_id:int, role:str, content:str) -> None` and `.load(chat_id:int, limit:int=20) -> list[dict]` (oldest-first `{"role","content"}`).

- [ ] **Step 1: Write the failing test** — `tests/home_agent/test_memory.py`:
```python
from home_agent.memory import Conversation


def test_append_and_load_oldest_first(tmp_path):
    c = Conversation(str(tmp_path / "m.db"))
    c.append(1, "user", "hi")
    c.append(1, "assistant", "hello")
    assert c.load(1) == [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "hello"}]


def test_persists_across_connections_and_isolates_chats(tmp_path):
    path = str(tmp_path / "m.db")
    Conversation(path).append(1, "user", "remember me")
    Conversation(path).append(2, "user", "other chat")
    assert Conversation(path).load(1) == [{"role": "user", "content": "remember me"}]
    assert Conversation(path).load(2) == [{"role": "user", "content": "other chat"}]


def test_load_limit_keeps_most_recent_in_order(tmp_path):
    c = Conversation(str(tmp_path / "m.db"))
    for i in range(5):
        c.append(1, "user", f"m{i}")
    assert [m["content"] for m in c.load(1, limit=2)] == ["m3", "m4"]
```
- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=src pytest tests/home_agent/test_memory.py -v` → FAIL (no `home_agent.memory`).
- [ ] **Step 3: Implement** — `src/home_agent/memory.py`:
```python
import sqlite3


class Conversation:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, "
            "ts TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        self.conn.commit()

    def append(self, chat_id: int, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content),
        )
        self.conn.commit()

    def load(self, chat_id: int, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]
```
- [ ] **Step 4: Run to verify it passes** — `PYTHONPATH=src pytest tests/home_agent/test_memory.py -v` → PASS; full suite green.
- [ ] **Step 5: Commit** — `git add src/home_agent/memory.py tests/home_agent/test_memory.py && git commit -m "feat(agent): SQLite conversation memory"`

---

## Task 1.5 — Family system prompt (byte-stable)
**Files:** Create `src/home_agent/prompts.py`, `tests/home_agent/test_system_prompt.py`
**Interfaces:**
- Produces: `FAMILY_SYSTEM_PROMPT: str` (a stable placeholder family context; no timestamps/volatile content).

- [ ] **Step 1: Write the failing test** — `tests/home_agent/test_system_prompt.py`:
```python
from home_agent.prompts import FAMILY_SYSTEM_PROMPT
from home_agent.agent import run_turn
from tests.home_agent.fakes import make_fake_client


def test_prompt_is_nonempty_and_stable():
    assert FAMILY_SYSTEM_PROMPT.strip()
    assert FAMILY_SYSTEM_PROMPT == FAMILY_SYSTEM_PROMPT  # constant, no per-call variation


def test_run_turn_sends_identical_system_prompt_each_turn():
    client = make_fake_client([{"content": "a"}, {"content": "b"}])
    run_turn("one", [], client=client, model="gpt-4o", system=FAMILY_SYSTEM_PROMPT, tools=[])
    run_turn("two", [], client=client, model="gpt-4o", system=FAMILY_SYSTEM_PROMPT, tools=[])
    sys1 = client._calls[0]["messages"][0]
    sys2 = client._calls[1]["messages"][0]
    assert sys1 == {"role": "system", "content": FAMILY_SYSTEM_PROMPT}
    assert sys1 == sys2  # byte-identical → OpenAI auto-cache can hit
```
- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=src pytest tests/home_agent/test_system_prompt.py -v` → FAIL (no `home_agent.prompts`).
- [ ] **Step 3: Implement** — `src/home_agent/prompts.py`:
```python
FAMILY_SYSTEM_PROMPT = (
    "You are the family's home assistant, shared by two adults in one household. "
    "You help manage the home, family logistics, and finances. "
    "Respond in Hebrew unless the user writes in English. Be concise, warm, and practical. "
    "When you need information or need to act, use the tools available to you. "
    "If a request is ambiguous, ask one short clarifying question rather than guessing."
)
```
- [ ] **Step 4: Run to verify it passes** — `PYTHONPATH=src pytest tests/home_agent/test_system_prompt.py -v` → PASS; full suite green.
- [ ] **Step 5: Commit** — `git add src/home_agent/prompts.py tests/home_agent/test_system_prompt.py && git commit -m "feat(agent): stable family system prompt"`

---

## Deferred to the Telegram phase (needs the bot token)
- **1.6 — Telegram adapter + allowlist** (`python-telegram-bot`, long-poll, gate on `Config.allowed_chat_ids`, wire memory+run_turn).
- **1.7 — `home-agent` entry point + live e2e** (console script; text the bot; "what time is it?" fires the tool; memory carries).
We build 1.1–1.5 now (offline, fully unit-tested), then pause for @BotFather.

---

## Self-Review
- **Coverage (Epic 1 doc tasks 1.1–1.5):** scaffold+config = T1.1 ✅; function-calling loop injectable+error-handling = T1.2 ✅; get_current_time = T1.3 ✅; SQLite memory persist/isolate/limit = T1.4 ✅; stable family system prompt = T1.5 ✅. 1.0 (prereqs) is user-side; 1.6/1.7 deferred (documented).
- **Placeholders:** none — every step has complete code + exact commands.
- **Type consistency:** `run_turn(user_text, history, *, client, model, system, tools)` identical across T1.2/1.3/1.5; `Tool(name,schema,impl)` from T1.3 matches the `_tool` shape used in T1.2's test and `DEFAULT_TOOLS`; `Conversation(db_path).append/.load` consistent; `make_fake_client` script shape + `_calls` consistent across all loop tests; `Config` fields match `load_config`.
