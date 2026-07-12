# Shared Memory (remember / recall / forget) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Menashe a durable, curated family fact store — `remember` a fact when explicitly asked, `recall` to answer questions about stored facts, `forget` on request.

**Architecture:** One new module `src/home_agent/facts.py` with an append-only `FactStore` (SQLite, connection-per-operation, thread-safe — mirrors `memory.Conversation`/`shopping_store`) and a `build_memory_tools(store, *, sender, now_fn=None)` factory returning three in-process `Tool`s. Tools are built **per-turn** in `handle_message` so `remember` records the current speaker (`sender`) and timestamp; the `FactStore` is created once at startup. `recall` returns the (small) fact set newest-first and the model answers from it.

**Tech Stack:** Python 3.11+ (venv 3.14), SQLite (stdlib `sqlite3`), `pytest`. OpenAI function-calling loop already exists.

## Global Constraints

- **Python 3.11+** (code targets 3.11; venv runs 3.14).
- **Tests are the only CI gate** — `.venv/bin/pytest -q --ignore=integration_tests`. No `ruff`/`mypy`.
- **No network in the automated suite.** The OpenAI loop is faked (`make_fake_client`); the clock is injected (`now_fn`); the speaker is injected (`sender`).
- **Tools are `home_agent.tools.Tool(name, schema, impl)`.** `schema` is the OpenAI function schema; its `description` IS the model's instruction. `impl(args: dict) -> str` (returns a string).
- **SQLite stores are thread-safe by connection-per-operation** (`with closing(sqlite3.connect(self.db_path)) as conn:`, commit inside) — mirror `memory.Conversation`.
- **Append-only.** Never `DELETE`; `forget` flips a `status` column to `'forgotten'`.
- **`prompts.FAMILY_SYSTEM_PROMPT` must stay digit-free and byte-stable** (tests enforce both).
- **`author` and `created_at` are injected (closure), never model arguments** — they must not appear in any tool schema.
- **Explicit-only capture:** `remember` fires only when the user clearly asks; the prompt enforces this.
- **This is distinct from `memory.Conversation`** (the rolling chat log). New module is `facts.py`.
- **Spec:** `docs/superpowers/specs/2026-07-12-shared-memory-design.md`.

---

### Task 1: `FactStore` (append-only SQLite store)

**Files:**
- Create: `src/home_agent/facts.py`
- Test: `tests/home_agent/test_facts_store.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces `class FactStore(db_path: str)` with:
  - `add(subject: str, fact: str, author, created_at: str) -> int` — insert an `active` row, return its id.
  - `active() -> list[dict]` — all `active` rows **newest-first** (`ORDER BY id DESC`); each `{id, subject, fact, author, created_at}`.
  - `find_active(query: str) -> list[dict]` — `active` rows whose `subject` OR `fact` contains `query` (case-insensitive), newest-first; same dict shape.
  - `forget(fact_id: int) -> None` — set `status='forgotten'` for that id (idempotent).

- [ ] **Step 1: Write the failing test**

Create `tests/home_agent/test_facts_store.py`:

```python
from home_agent.facts import FactStore


def _store(tmp_path):
    return FactStore(str(tmp_path / "facts.db"))


def test_add_and_active_newest_first(tmp_path):
    s = _store(tmp_path)
    s.add("gate code", "1234", "נתנאל", "2026-07-12T10:00:00")
    s.add("passports", "in the safe", "שרי", "2026-07-12T11:00:00")
    rows = s.active()
    assert [r["subject"] for r in rows] == ["passports", "gate code"]  # newest first
    assert rows[0] == {"id": 2, "subject": "passports", "fact": "in the safe",
                       "author": "שרי", "created_at": "2026-07-12T11:00:00"}


def test_find_active_matches_subject_and_fact_case_insensitive(tmp_path):
    s = _store(tmp_path)
    s.add("gate code", "1234", "נתנאל", "t1")
    s.add("wifi", "the PassWord is abc", "נתנאל", "t2")
    assert [r["subject"] for r in s.find_active("GATE")] == ["gate code"]   # subject, case-insensitive
    assert [r["subject"] for r in s.find_active("password")] == ["wifi"]    # fact text, case-insensitive
    assert s.find_active("nonexistent") == []


def test_forget_flips_status_and_is_idempotent(tmp_path):
    s = _store(tmp_path)
    fid = s.add("gate code", "1234", "נתנאל", "t1")
    s.forget(fid)
    assert s.active() == []              # gone from active
    assert s.find_active("gate") == []   # and from matches
    s.forget(fid)                        # idempotent — no error


def test_connection_per_op_persists_across_instances(tmp_path):
    path = str(tmp_path / "facts.db")
    FactStore(path).add("gate code", "1234", "נתנאל", "t1")
    assert FactStore(path).active()[0]["fact"] == "1234"   # a fresh instance sees prior rows
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_facts_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'home_agent.facts'`.

- [ ] **Step 3: Implement `FactStore`**

Create `src/home_agent/facts.py`:

```python
import sqlite3
from contextlib import closing

from .tools import Tool


class FactStore:
    """Durable, append-only family fact store (SQLite). Thread-safe: a fresh connection per
    operation (PTB runs handlers off-thread), mirroring memory.Conversation. Append-only:
    facts are never deleted — forget() flips status to 'forgotten'."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS facts ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, fact TEXT NOT NULL,"
                " author TEXT, created_at TEXT, status TEXT NOT NULL DEFAULT 'active')"
            )
            conn.commit()

    def add(self, subject, fact, author, created_at) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO facts (subject, fact, author, created_at) VALUES (?, ?, ?, ?)",
                (subject, fact, author, created_at),
            )
            conn.commit()
            return cur.lastrowid

    def _rows(self, conn, where, params):
        sql = ("SELECT id, subject, fact, author, created_at FROM facts "
               "WHERE status='active'" + where + " ORDER BY id DESC")
        return [
            {"id": i, "subject": s, "fact": f, "author": a, "created_at": c}
            for i, s, f, a, c in conn.execute(sql, params).fetchall()
        ]

    def active(self) -> list[dict]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return self._rows(conn, "", ())

    def find_active(self, query) -> list[dict]:
        like = f"%{query}%"
        with closing(sqlite3.connect(self.db_path)) as conn:
            return self._rows(
                conn,
                " AND (subject LIKE ? COLLATE NOCASE OR fact LIKE ? COLLATE NOCASE)",
                (like, like),
            )

    def forget(self, fact_id) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE facts SET status='forgotten' WHERE id=?", (fact_id,))
            conn.commit()
```

(The `from .tools import Tool` import is used by the tool factory added in Task 2 — keep it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_facts_store.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/facts.py tests/home_agent/test_facts_store.py
git commit -m "feat(memory): FactStore append-only SQLite store (add/active/find_active/forget)"
```

---

### Task 2: `remember` tool + `build_memory_tools` skeleton

**Files:**
- Modify: `src/home_agent/facts.py`
- Test: `tests/home_agent/test_memory_tools.py`

**Interfaces:**
- Consumes: `FactStore.add(subject, fact, author, created_at)` (Task 1).
- Produces:
  - `build_memory_tools(store, *, sender, now_fn=None) -> list[Tool]` (this task returns just `remember`; Tasks 3–4 add `recall`/`forget`).
  - `remember(subject, fact)` tool — records `author=sender`, `created_at=now_fn().isoformat()`. `author`/`created_at` are NOT schema params.
  - Module helper `_now() -> datetime` (`datetime.now().astimezone()`), mirroring `shopping._now`.

- [ ] **Step 1: Write the failing test**

Create `tests/home_agent/test_memory_tools.py`:

```python
from datetime import datetime
from home_agent.facts import FactStore, build_memory_tools


def _frozen(iso="2026-07-12T10:00:00+03:00"):
    dt = datetime.fromisoformat(iso)
    return lambda: dt


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_remember_stores_subject_fact_author_timestamp(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    out = _tool(tools, "remember").impl({"subject": "דרכונים", "fact": "בכספת"})
    assert out  # non-empty confirmation
    rows = store.active()
    assert len(rows) == 1
    assert rows[0]["subject"] == "דרכונים"
    assert rows[0]["fact"] == "בכספת"
    assert rows[0]["author"] == "נתנאל"
    assert rows[0]["created_at"] == "2026-07-12T10:00:00+03:00"


def test_remember_schema_hides_author_and_timestamp(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    props = _tool(tools, "remember").schema["function"]["parameters"]["properties"]
    assert set(props) == {"subject", "fact"}   # author/created_at injected, never model args
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_memory_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_memory_tools' from 'home_agent.facts'`.

- [ ] **Step 3: Implement `remember` + the factory skeleton**

Add to `src/home_agent/facts.py` — a `datetime` import at the top (join the existing imports), the schema near the top after the imports, and the impl + factory at the bottom:

```python
from datetime import datetime
```

```python
def _now():
    return datetime.now().astimezone()


_REMEMBER_SCHEMA = {"type": "function", "function": {
    "name": "remember",
    "description": (
        "Store a durable family fact, ONLY when the user explicitly asks you to remember something "
        "(e.g. 'תזכור ש…', 'remember that…'). Never store facts on your own initiative. Give a short "
        "'subject' label (e.g. 'gate code', 'passports') and the 'fact' detail. Report back briefly."
    ),
    "parameters": {"type": "object", "properties": {
        "subject": {"type": "string", "description": "A short label for the fact, e.g. 'gate code', 'passports'."},
        "fact": {"type": "string", "description": "The detail to remember, e.g. 'in the safe', 'the code is five six seven eight'."},
    }, "required": ["subject", "fact"], "additionalProperties": False}}}


def _remember_impl(args, *, store, sender, now_fn) -> str:
    subject = (args.get("subject") or "").strip()
    fact = (args.get("fact") or "").strip()
    if not fact:
        return "there was nothing to remember — tell me the detail."
    store.add(subject, fact, sender, now_fn().isoformat())
    label = f"{subject}: {fact}" if subject else fact
    return f"remembered — {label}"


def build_memory_tools(store, *, sender, now_fn=None) -> list[Tool]:
    now_fn = now_fn or _now
    return [
        Tool(name="remember", schema=_REMEMBER_SCHEMA,
             impl=lambda a: _remember_impl(a, store=store, sender=sender, now_fn=now_fn)),
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_memory_tools.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/facts.py tests/home_agent/test_memory_tools.py
git commit -m "feat(memory): remember tool + build_memory_tools (author/clock injected)"
```

---

### Task 3: `recall` tool

**Files:**
- Modify: `src/home_agent/facts.py`
- Test: `tests/home_agent/test_memory_tools.py`

**Interfaces:**
- Consumes: `FactStore.active()` (Task 1); `build_memory_tools` (Task 2).
- Produces: `recall()` tool (no arguments) added to `build_memory_tools`. Returns active facts newest-first, one line each as `subject — fact (author, YYYY-MM-DD)`; empty store → a friendly "nothing remembered yet" line.

- [ ] **Step 1: Write the failing test**

Append to `tests/home_agent/test_memory_tools.py`:

```python
def test_recall_empty_store_is_friendly(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    out = _tool(tools, "recall").impl({})
    assert out.strip()                       # a real message, not empty
    assert "remember" in out.lower()         # the friendly "nothing remembered yet" wording


def test_recall_returns_facts_newest_first_with_author_and_date(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    store.add("gate code", "1234", "נתנאל", "2026-07-10T09:00:00+03:00")
    store.add("passports", "in the safe", "שרי", "2026-07-12T09:00:00+03:00")
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    out = _tool(tools, "recall").impl({})
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0].startswith("passports")        # newest first
    assert "in the safe" in lines[0] and "שרי" in lines[0] and "2026-07-12" in lines[0]
    assert any("gate code" in ln and "1234" in ln and "נתנאל" in ln for ln in lines)


def test_recall_takes_no_arguments(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    props = _tool(tools, "recall").schema["function"]["parameters"]["properties"]
    assert props == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_memory_tools.py -k recall -v`
Expected: FAIL — `StopIteration` (no tool named `recall`).

- [ ] **Step 3: Implement `recall`**

Add to `src/home_agent/facts.py` — schema near the other schema, impl + a formatting helper near the other impl, and register the tool in `build_memory_tools`:

```python
_RECALL_SCHEMA = {"type": "function", "function": {
    "name": "recall",
    "description": (
        "List everything you have been told to remember (family facts), newest first. Call this whenever "
        "the user asks about something that might have been saved — where something is kept, a code, a "
        "password, a date. When values conflict, prefer the most recent. Answer the user from what you find."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def _format_fact(row) -> str:
    label = f"{row['subject']} — {row['fact']}" if row.get("subject") else row["fact"]
    author = row.get("author") or "unknown"
    date = (row.get("created_at") or "")[:10]
    return f"{label} ({author}, {date})"


def _recall_impl(args, *, store) -> str:
    rows = store.active()
    if not rows:
        return "I have not been told to remember anything yet."
    return "\n".join(_format_fact(r) for r in rows)
```

Register in `build_memory_tools` (add before the closing `]`):

```python
        Tool(name="recall", schema=_RECALL_SCHEMA, impl=lambda a: _recall_impl(a, store=store)),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_memory_tools.py -v`
Expected: PASS (5 tests total).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/facts.py tests/home_agent/test_memory_tools.py
git commit -m "feat(memory): recall tool (newest-first, author+date, empty-store message)"
```

---

### Task 4: `forget` tool

**Files:**
- Modify: `src/home_agent/facts.py`
- Test: `tests/home_agent/test_memory_tools.py`

**Interfaces:**
- Consumes: `FactStore.find_active(query)` + `FactStore.forget(id)` (Task 1); `build_memory_tools` (Task 2); `_format_fact` (Task 3).
- Produces: `forget(query)` tool added to `build_memory_tools`. One match → retire + confirm; several → retire nothing, list them; none → friendly message.

- [ ] **Step 1: Write the failing test**

Append to `tests/home_agent/test_memory_tools.py`:

```python
def test_forget_single_match_retires_it(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    store.add("gate code", "1234", "נתנאל", "t1")
    store.add("passports", "in the safe", "שרי", "t2")
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    out = _tool(tools, "forget").impl({"query": "gate"})
    assert "gate code" in out
    assert [r["subject"] for r in store.active()] == ["passports"]  # only the match was retired


def test_forget_several_matches_retires_nothing_and_lists(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    store.add("gate code", "1234", "נתנאל", "t1")
    store.add("alarm code", "9999", "נתנאל", "t2")
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    out = _tool(tools, "forget").impl({"query": "code"})
    assert "gate code" in out and "alarm code" in out    # lists both
    assert len(store.active()) == 2                       # nothing retired — ambiguous


def test_forget_no_match_is_friendly(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    store.add("gate code", "1234", "נתנאל", "t1")
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    out = _tool(tools, "forget").impl({"query": "nonexistent"})
    assert "nothing" in out.lower()
    assert len(store.active()) == 1   # untouched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_memory_tools.py -k forget -v`
Expected: FAIL — `StopIteration` (no tool named `forget`).

- [ ] **Step 3: Implement `forget`**

Add to `src/home_agent/facts.py` — schema + impl, and register in `build_memory_tools`:

```python
_FORGET_SCHEMA = {"type": "function", "function": {
    "name": "forget",
    "description": (
        "Retire a stored fact when the user explicitly asks to forget something (e.g. 'תשכח את קוד השער'). "
        "Pass a 'query' describing what to forget. If exactly one stored fact matches, it is retired; if "
        "several match, none are retired and they are listed so you can ask the user which one; if none "
        "match, you are told so. Retired facts stop appearing but are recoverable."
    ),
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "What to forget, e.g. 'gate code', 'the passports'."},
    }, "required": ["query"], "additionalProperties": False}}}


def _forget_impl(args, *, store) -> str:
    query = (args.get("query") or "").strip()
    matches = store.find_active(query) if query else []
    if not matches:
        return f"nothing matching '{query}' to forget."
    if len(matches) > 1:
        listed = "\n".join(_format_fact(m) for m in matches)
        return f"several facts match '{query}' — which one should I forget?\n{listed}"
    store.forget(matches[0]["id"])
    return f"forgot — {_format_fact(matches[0])}"
```

Register in `build_memory_tools` (add before the closing `]`):

```python
        Tool(name="forget", schema=_FORGET_SCHEMA, impl=lambda a: _forget_impl(a, store=store)),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_memory_tools.py -v`
Expected: PASS (8 tests total).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/facts.py tests/home_agent/test_memory_tools.py
git commit -m "feat(memory): forget tool (one retires, several lists, none friendly)"
```

---

### Task 5: Prompt additions + per-turn wiring + end-to-end test

**Files:**
- Modify: `src/home_agent/prompts.py`
- Modify: `src/home_agent/telegram_app.py`
- Test: `tests/home_agent/test_system_prompt.py`, `tests/home_agent/test_telegram_handler.py`

**Interfaces:**
- Consumes: `build_memory_tools(store, *, sender, now_fn=None)` (Tasks 2–4); `FactStore` (Task 1).
- Produces: `handle_message(..., fact_store=None)` builds the memory tools per-turn (with `sender`) when `fact_store` is provided; `build_application` creates the `FactStore` once and threads it through.

- [ ] **Step 1: Write the failing tests**

Add to `tests/home_agent/test_system_prompt.py` (inside `test_prompt_is_nonempty_and_stable`, after the existing asserts):

```python
    assert "remember" in FAMILY_SYSTEM_PROMPT.lower()   # shared-memory guidance present
```

Add to `tests/home_agent/test_telegram_handler.py`:

```python
def test_handle_message_remember_then_recall_through_composed_tools(tmp_path, make_fake_client):
    from home_agent.facts import FactStore
    store = FactStore(str(tmp_path / "m.db"))
    conv = Conversation(str(tmp_path / "m.db"))
    cfg = _cfg(tmp_path, {1})

    # Turn 1: user asks to remember; model calls remember, then replies.
    client1 = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "remember",
                         "arguments": {"subject": "דרכונים", "fact": "בכספת"}}]},
        {"content": "אזכור שהדרכונים בכספת"},
    ])
    handle_message(1, "תזכור שהדרכונים בכספת", config=cfg, conversation=conv,
                   client=client1, fact_store=store, sender="נתנאל")
    assert store.active()[0]["fact"] == "בכספת"
    assert store.active()[0]["author"] == "נתנאל"

    # Turn 2: user asks where; model calls recall, sees the fact, answers.
    client2 = make_fake_client([
        {"tool_calls": [{"id": "c2", "name": "recall", "arguments": {}}]},
        {"content": "הדרכונים בכספת"},
    ])
    reply = handle_message(1, "איפה הדרכונים?", config=cfg, conversation=conv,
                           client=client2, fact_store=store, sender="שרי")
    # The recall tool result fed to the model contained the stored fact:
    recall_msgs = [m for m in client2._calls[1]["messages"] if m.get("role") == "tool"]
    assert any("בכספת" in m["content"] for m in recall_msgs)
    assert reply == "הדרכונים בכספת"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_system_prompt.py tests/home_agent/test_telegram_handler.py::test_handle_message_remember_then_recall_through_composed_tools -v`
Expected: FAIL — the prompt assert fails (`remember` not yet in the prompt); the handler test fails with `TypeError` (`handle_message` has no `fact_store` argument).

- [ ] **Step 3: Add the prompt guidance (digit-free, byte-stable)**

In `src/home_agent/prompts.py`, insert these sentences into `FAMILY_SYSTEM_PROMPT` immediately after the calendar-policy sentence (the one ending "…calling commit_calendar_change. ") and before the final "If a request is ambiguous…" sentence:

```python
    "You keep a durable family memory. When the user explicitly asks you to remember something "
    "(for example 'תזכור ש…'), store it with remember, giving a short subject label and the detail. "
    "Never store facts on your own initiative. When the user asks about something that may have been "
    "saved — where an item is kept, a code, a password, a date — call recall and answer from what you "
    "find, preferring the most recent when values conflict. When asked to forget something, use forget. "
```

Confirm the added text contains no digits (the digit-free + byte-stable prompt tests enforce this).

- [ ] **Step 4: Wire per-turn into `handle_message` and `build_application`**

In `src/home_agent/telegram_app.py`:

Add the import near the other `from .` imports:

```python
from .facts import FactStore, build_memory_tools
```

Add `fact_store=None` to the `handle_message` signature (alongside `calendar_service=None, calendar_pending=None, sender=None`):

```python
def handle_message(chat_id, text, *, config, conversation, client,
                   tools=DEFAULT_TOOLS, system=FAMILY_SYSTEM_PROMPT, model=None,
                   calendar_service=None, calendar_pending=None, sender=None, fact_store=None):
```

In `handle_message`, after the calendar-tools block (right after the `turn_tools = turn_tools + build_calendar_tools(...)` block) and before `message_text = ...`, add:

```python
    if fact_store is not None:
        turn_tools = turn_tools + build_memory_tools(fact_store, sender=sender)
```

In `build_application`, create the store once (near the other startup construction, e.g. after `registry = load_registry(config)`):

```python
    fact_store = FactStore(config.db_path)
```

And pass it into the `handle_message` call inside `on_message` (add `fact_store=fact_store` to the existing kwargs):

```python
            reply = await asyncio.to_thread(
                handle_message, chat_id, message.text or "",
                config=config, conversation=conversation, client=client, tools=tools,
                calendar_service=cal_service, calendar_pending=cal_pending, sender=sender,
                fact_store=fact_store)
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (all existing tests + the new memory tests; count increases by ~15).

- [ ] **Step 6: Commit**

```bash
git add src/home_agent/prompts.py src/home_agent/telegram_app.py \
        tests/home_agent/test_system_prompt.py tests/home_agent/test_telegram_handler.py
git commit -m "feat(memory): prompt guidance + per-turn wiring (remember/recall/forget) + e2e test"
```

---

## Post-implementation (for the human)

Live smoke in Telegram: tell Menashe "תזכור ש…" a fact, ask about it in a later message (verify `recall` answers), then ask it to forget it (verify it's gone from a subsequent recall). No config or setup needed — the store is always on the agent's SQLite db.
