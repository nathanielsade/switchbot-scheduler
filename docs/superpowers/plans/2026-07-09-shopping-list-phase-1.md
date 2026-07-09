# Shopping list — Phase 1 (foundation + shared list) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A shared, persistent shopping list both spouses drive from Telegram — add / remove / show items and mark them bought — on an append-only SQLite foundation that Phases 2 (cycle prediction) and 3 (receipts) will build on.

**Architecture:** A new `ShoppingStore` (SQLite, thread-safe connection-per-op like `memory.Conversation`/`ScheduleStore`) with three tables — `items` (canonical products), `list` (append-only entries with a status), `purchases` (append-only history). A new `shopping.py` exposes five in-process `Tool`s over the store; they're composed into the agent at startup. Canonicalization is the agent's job (it passes canonical names, aided by a `known_items` tool); the store only does exact get-or-create.

**Tech Stack:** Python 3.11+, `sqlite3` (stdlib), `pytest`. No new dependencies.

## Global Constraints

- Python **3.11+**.
- **No network in the automated tests.** The one time-dependent tool (`mark_bought`) takes an injectable `now_fn()`; loop tests use the existing `make_fake_client` fixture.
- Tools are the existing `home_agent.tools.Tool(name, schema, impl)` dataclass; **in-process** (no MCP server).
- Store lives in `config.db_path` (the existing `home_agent.db`); thread-safe via a fresh connection per operation (PTB runs handlers off the main thread) — mirror `src/home_agent/memory.py`.
- **Append-only history:** never `DELETE` from `list` or `purchases`. Removing/buying a list entry flips its `status` and stamps `resolved_at`; the row stays.
- Canonical item names come from the agent; the store's only identity logic is `_get_or_create_item(name)` (exact match → id, else insert). A `known_items()` tool exposes existing names to the agent.
- `purchases.purchased_on` is an ISO date string `"YYYY-MM-DD"` (Phase 2 does date math on it).
- `.venv/bin/pytest` is the runner. Commit after every task.

---

### Task 1: `ShoppingStore` (items / list / purchases)

**Files:**
- Create: `src/home_agent/shopping_store.py`
- Test: `tests/home_agent/test_shopping_store.py`

**Interfaces:**
- Produces: `ShoppingStore(db_path)` with `known_items() -> list[str]`, `add(name, quantity=None, note=None)`, `pending() -> list[dict]` (keys: item, quantity, note), `remove(name) -> int` (rows flipped), `buy(name, purchased_on, quantity=None, unit_price=None)`, `purchases_for(name) -> list[dict]` (keys: purchased_on, quantity, unit_price, source). Thread-safe (connection-per-op).

- [ ] **Step 1: Write the failing tests**

Create `tests/home_agent/test_shopping_store.py`:

```python
from home_agent.shopping_store import ShoppingStore


def test_add_and_pending(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.add("חלב", quantity="2", note="3%")
    s.add("קפה")
    assert s.pending() == [
        {"item": "חלב", "quantity": "2", "note": "3%"},
        {"item": "קפה", "quantity": None, "note": None},
    ]


def test_same_name_is_one_canonical_item_two_list_rows(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.add("חלב")
    s.add("חלב")
    assert s.known_items() == ["חלב"]              # one canonical item
    assert len(s.pending()) == 2                    # but two list entries


def test_known_items_sorted(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.add("קפה")
    s.add("חלב")
    assert s.known_items() == sorted(["קפה", "חלב"])


def test_remove_flips_status_and_is_append_only(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.add("חלב")
    assert s.remove("חלב") == 1
    assert s.pending() == []                        # no longer pending
    assert s.remove("חלב") == 0                     # nothing pending to remove now
    assert s.remove("לא-קיים") == 0                 # unknown item
    # append-only: the row still exists (as 'removed'), not deleted
    import sqlite3
    n = sqlite3.connect(str(tmp_path / "sh.db")).execute("SELECT COUNT(*) FROM list").fetchone()[0]
    assert n == 1


def test_buy_logs_purchase_and_marks_pending_bought(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.add("חלב")
    s.buy("חלב", "2026-07-09", quantity=1, unit_price=6.9)
    assert s.pending() == []                        # left the list
    assert s.purchases_for("חלב") == [
        {"purchased_on": "2026-07-09", "quantity": 1.0, "unit_price": 6.9, "source": "chat"}
    ]


def test_buy_unlisted_item_still_logs_purchase(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.buy("במבה", "2026-07-09")                      # never on the list
    assert [p["purchased_on"] for p in s.purchases_for("במבה")] == ["2026-07-09"]


def test_usable_from_a_different_thread(tmp_path):
    import threading
    s = ShoppingStore(str(tmp_path / "sh.db"))
    errors = []

    def worker():
        try:
            s.add("חלב")
            assert len(s.pending()) == 1
        except Exception as e:
            errors.append(repr(e))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_shopping_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'home_agent.shopping_store'`.

- [ ] **Step 3: Create `src/home_agent/shopping_store.py`**

```python
import sqlite3
from contextlib import closing


class ShoppingStore:
    """Shared shopping list + append-only purchase history (SQLite). Thread-safe: a fresh
    connection per operation (PTB runs handlers off-thread), mirroring memory.Conversation.
    Append-only: list/purchases rows are never deleted — removing/buying flips a status."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS items ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,"
                " created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
                "CREATE TABLE IF NOT EXISTS list ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,"
                " quantity TEXT, note TEXT, status TEXT NOT NULL DEFAULT 'pending',"
                " added_at TEXT DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT);"
                "CREATE TABLE IF NOT EXISTS purchases ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,"
                " quantity REAL, unit_price REAL, purchased_on TEXT NOT NULL,"
                " source TEXT NOT NULL, receipt_id TEXT,"
                " created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
            )
            conn.commit()

    def _get_or_create_item(self, conn, name):
        row = conn.execute("SELECT id FROM items WHERE name = ?", (name,)).fetchone()
        if row:
            return row[0]
        return conn.execute("INSERT INTO items (name) VALUES (?)", (name,)).lastrowid

    def known_items(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return [r[0] for r in conn.execute("SELECT name FROM items ORDER BY name").fetchall()]

    def add(self, name, quantity=None, note=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            item_id = self._get_or_create_item(conn, name)
            conn.execute("INSERT INTO list (item_id, quantity, note) VALUES (?, ?, ?)",
                         (item_id, quantity, note))
            conn.commit()

    def pending(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT i.name, l.quantity, l.note FROM list l JOIN items i ON i.id = l.item_id "
                "WHERE l.status = 'pending' ORDER BY l.id").fetchall()
        return [{"item": n, "quantity": q, "note": nt} for n, q, nt in rows]

    def remove(self, name):
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT id FROM items WHERE name = ?", (name,)).fetchone()
            if not row:
                return 0
            cur = conn.execute(
                "UPDATE list SET status='removed', resolved_at=CURRENT_TIMESTAMP "
                "WHERE item_id=? AND status='pending'", (row[0],))
            conn.commit()
            return cur.rowcount

    def buy(self, name, purchased_on, quantity=None, unit_price=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            item_id = self._get_or_create_item(conn, name)
            conn.execute(
                "UPDATE list SET status='bought', resolved_at=CURRENT_TIMESTAMP "
                "WHERE item_id=? AND status='pending'", (item_id,))
            conn.execute(
                "INSERT INTO purchases (item_id, quantity, unit_price, purchased_on, source) "
                "VALUES (?, ?, ?, ?, 'chat')", (item_id, quantity, unit_price, purchased_on))
            conn.commit()

    def purchases_for(self, name):
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT p.purchased_on, p.quantity, p.unit_price, p.source FROM purchases p "
                "JOIN items i ON i.id = p.item_id WHERE i.name = ? ORDER BY p.purchased_on, p.id",
                (name,)).fetchall()
        return [{"purchased_on": d, "quantity": q, "unit_price": u, "source": s}
                for d, q, u, s in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_shopping_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/shopping_store.py tests/home_agent/test_shopping_store.py
git commit -m "feat(shopping): ShoppingStore (items/list/purchases, append-only, thread-safe)"
```

---

### Task 2: Phase 1 list tools

**Files:**
- Create: `src/home_agent/shopping.py`
- Test: `tests/home_agent/test_shopping_tools.py`

**Interfaces:**
- Consumes: `ShoppingStore` (Task 1); `home_agent.tools.Tool`.
- Produces: `build_shopping_tools(store, *, now_fn=None) -> list[Tool]` returning five tools —
  `show_list`, `add_to_list`, `remove_from_list`, `mark_bought`, `known_items`. `now_fn()` defaults to
  local aware now (injected in tests); `mark_bought` stamps `purchased_on = now_fn().date().isoformat()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/home_agent/test_shopping_tools.py`:

```python
from datetime import datetime, timezone
from home_agent.shopping_store import ShoppingStore
from home_agent.shopping import build_shopping_tools


def _fixed_now():
    return datetime(2026, 7, 9, 18, 0, tzinfo=timezone.utc)


def _tools(tmp_path):
    store = ShoppingStore(str(tmp_path / "sh.db"))
    return build_shopping_tools(store, now_fn=_fixed_now), store


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_show_list_empty(tmp_path):
    tools, _ = _tools(tmp_path)
    assert "empty" in _tool(tools, "show_list").impl({}).lower()


def test_add_then_show(tmp_path):
    tools, store = _tools(tmp_path)
    out = _tool(tools, "add_to_list").impl({"item": "חלב", "quantity": "2"})
    assert "חלב" in out and "✅" in out
    shown = _tool(tools, "show_list").impl({})
    assert "חלב" in shown
    assert store.pending()[0]["item"] == "חלב"


def test_remove_present_and_absent(tmp_path):
    tools, _ = _tools(tmp_path)
    _tool(tools, "add_to_list").impl({"item": "חלב"})
    assert "✅" in _tool(tools, "remove_from_list").impl({"item": "חלב"})
    assert "isn't on the list" in _tool(tools, "remove_from_list").impl({"item": "חלב"})


def test_mark_bought_logs_purchase_with_frozen_date(tmp_path):
    tools, store = _tools(tmp_path)
    _tool(tools, "add_to_list").impl({"item": "חלב"})
    out = _tool(tools, "mark_bought").impl({"item": "חלב", "price": 6.9})
    assert "✅" in out
    assert store.pending() == []
    assert store.purchases_for("חלב") == [
        {"purchased_on": "2026-07-09", "quantity": None, "unit_price": 6.9, "source": "chat"}
    ]


def test_known_items(tmp_path):
    tools, _ = _tools(tmp_path)
    _tool(tools, "add_to_list").impl({"item": "חלב"})
    _tool(tools, "add_to_list").impl({"item": "קפה"})
    out = _tool(tools, "known_items").impl({})
    assert "חלב" in out and "קפה" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_shopping_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'home_agent.shopping'`.

- [ ] **Step 3: Create `src/home_agent/shopping.py`**

```python
from datetime import datetime

from .tools import Tool

_SHOW_SCHEMA = {"type": "function", "function": {
    "name": "show_list",
    "description": "Show the current shared shopping list (what still needs to be bought). Use when "
                   "the user asks what's on the list.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_ADD_SCHEMA = {"type": "function", "function": {
    "name": "add_to_list",
    "description": "Add an item to the shared shopping list. Use the canonical item name; if the user's "
                   "wording is a variant of something already known, reuse the known name (call "
                   "known_items if unsure). Report back in the user's language.",
    "parameters": {"type": "object", "properties": {
        "item": {"type": "string", "description": "Canonical item name (Hebrew or English)."},
        "quantity": {"type": "string", "description": "Optional free-text amount, e.g. '2' or '2 ליטר'."},
        "note": {"type": "string", "description": "Optional note, e.g. a brand or '3%'."},
    }, "required": ["item"], "additionalProperties": False},
}}

_REMOVE_SCHEMA = {"type": "function", "function": {
    "name": "remove_from_list",
    "description": "Remove an item from the shared shopping list (it's no longer needed). Use the "
                   "canonical item name.",
    "parameters": {"type": "object", "properties": {
        "item": {"type": "string", "description": "Canonical item name to remove."},
    }, "required": ["item"], "additionalProperties": False},
}}

_BOUGHT_SCHEMA = {"type": "function", "function": {
    "name": "mark_bought",
    "description": "Record that an item was bought (removes it from the list if present and logs it to "
                   "purchase history with today's date). Use when the user says they bought something.",
    "parameters": {"type": "object", "properties": {
        "item": {"type": "string", "description": "Canonical item name that was bought."},
        "quantity": {"type": "number", "description": "Optional amount bought."},
        "price": {"type": "number", "description": "Optional price paid, in shekels."},
    }, "required": ["item"], "additionalProperties": False},
}}

_KNOWN_SCHEMA = {"type": "function", "function": {
    "name": "known_items",
    "description": "List the canonical item names already known, so you can map a user's wording to an "
                   "existing item instead of creating a near-duplicate.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}


def _now():
    return datetime.now().astimezone()


def _show_impl(args, *, store):
    rows = store.pending()
    if not rows:
        return "the shopping list is empty"
    lines = []
    for r in rows:
        q = f" ({r['quantity']})" if r["quantity"] else ""
        n = f" — {r['note']}" if r["note"] else ""
        lines.append(f"- {r['item']}{q}{n}")
    return "\n".join(lines)


def _add_impl(args, *, store):
    item = (args.get("item") or "").strip()
    if not item:
        return "no item given"
    store.add(item, args.get("quantity"), args.get("note"))
    return f"added {item} to the list ✅"


def _remove_impl(args, *, store):
    item = (args.get("item") or "").strip()
    if store.remove(item) == 0:
        return f"{item} isn't on the list"
    return f"removed {item} from the list ✅"


def _bought_impl(args, *, store, now_fn):
    item = (args.get("item") or "").strip()
    if not item:
        return "no item given"
    store.buy(item, now_fn().date().isoformat(), args.get("quantity"), args.get("price"))
    return f"logged {item} as bought ✅"


def _known_impl(args, *, store):
    names = store.known_items()
    return ", ".join(names) if names else "(no items known yet)"


def build_shopping_tools(store, *, now_fn=None) -> list[Tool]:
    now_fn = now_fn or _now
    return [
        Tool(name="show_list", schema=_SHOW_SCHEMA, impl=lambda a: _show_impl(a, store=store)),
        Tool(name="add_to_list", schema=_ADD_SCHEMA, impl=lambda a: _add_impl(a, store=store)),
        Tool(name="remove_from_list", schema=_REMOVE_SCHEMA, impl=lambda a: _remove_impl(a, store=store)),
        Tool(name="mark_bought", schema=_BOUGHT_SCHEMA,
             impl=lambda a: _bought_impl(a, store=store, now_fn=now_fn)),
        Tool(name="known_items", schema=_KNOWN_SCHEMA, impl=lambda a: _known_impl(a, store=store)),
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_shopping_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/shopping.py tests/home_agent/test_shopping_tools.py
git commit -m "feat(shopping): Phase 1 list tools (show/add/remove/mark_bought/known_items)"
```

---

### Task 3: Wire shopping tools into the running bot

**Files:**
- Modify: `src/home_agent/telegram_app.py`
- Test: `tests/home_agent/test_telegram_handler.py`, `tests/home_agent/test_telegram_app.py`

**Interfaces:**
- Consumes: `build_shopping_tools`, `ShoppingStore`, `Config.db_path`.
- Produces: `build_application` composes the shopping tools (always, independent of `devices.yaml`) into the
  tool list passed to `handle_message`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_telegram_handler.py`:

```python
def test_handle_message_runs_add_to_list_through_composed_tools(tmp_path, make_fake_client):
    from home_agent.shopping import build_shopping_tools
    from home_agent.shopping_store import ShoppingStore
    from home_agent.tools import DEFAULT_TOOLS
    store = ShoppingStore(str(tmp_path / "sh.db"))
    tools = list(DEFAULT_TOOLS) + build_shopping_tools(store)
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "add_to_list", "arguments": {"item": "חלב"}}]},
        {"content": "הוספתי חלב"},
    ])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "תוסיף חלב", config=_cfg(tmp_path, {1}),
                           conversation=conv, client=client, tools=tools)
    assert reply == "הוספתי חלב"
    assert store.pending()[0]["item"] == "חלב"
```

Append to `tests/home_agent/test_telegram_app.py`:

```python
def test_build_application_composes_shopping_tools(tmp_path, monkeypatch, make_fake_client):
    import home_agent.telegram_app as ta
    from home_agent.config import Config
    from home_agent.shopping_store import ShoppingStore
    cfg = Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                 allowed_chat_ids={1}, db_path=str(tmp_path / "m.db"),
                 devices_path=str(tmp_path / "none.yaml"))
    seen = {}
    real = ta.build_shopping_tools

    def spy(store, **kw):
        seen["store"] = store
        return real(store, **kw)

    monkeypatch.setattr(ta, "build_shopping_tools", spy)
    app = build_application(cfg, client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    assert isinstance(seen.get("store"), ShoppingStore)   # shopping tools were composed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_telegram_app.py -k composes_shopping -v`
Expected: FAIL — `AttributeError: ... has no attribute 'build_shopping_tools'` (not imported yet).

- [ ] **Step 3: Wire into `telegram_app.build_application`**

Add imports at the top of `telegram_app.py` (next to the other `.` imports):
```python
from .shopping import build_shopping_tools
from .shopping_store import ShoppingStore
```
In `build_application`, immediately after the `tools = list(DEFAULT_TOOLS)` line (before the `registry`
block), add:
```python
    tools += build_shopping_tools(ShoppingStore(config.db_path))
```
(The shopping tools do not need the device registry, so they are always available — even without
`devices.yaml`. Leave the existing `registry` / home / schedule composition unchanged below it.)

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (all tests, including the two new ones).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/telegram_app.py tests/home_agent/
git commit -m "feat(shopping): compose Phase 1 list tools into the bot at startup"
```

---

### Task 4: Live smoke test (manual)

**Files:** none (verification + docs).

**Interfaces:** none.

- [ ] **Step 1: Full automated suite**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS.

- [ ] **Step 2: Drive it from Telegram (one instance only)**

Start the bot: `PYTHONPATH=src .venv/bin/python -m home_agent` (ensure no other instance is running).
In the chat, exercise the flow in Hebrew and confirm each reply + that both phones see the same list:
- `תוסיף חלב, ביצים ולחם` → three items added.
- `מה יש ברשימה?` → lists milk, eggs, bread.
- `קניתי חלב` → milk leaves the list (and is logged to history).
- `תמחק לחם` → bread removed.
Stop the bot with Ctrl+C.

- [ ] **Step 3: Confirm the history landed (for Phase 2 later)**

Run:
```bash
.venv/bin/python -c "from home_agent.shopping_store import ShoppingStore; import pprint; pprint.pprint(ShoppingStore('home_agent.db').purchases_for('חלב'))"
```
Expected: one purchase row for `חלב` with today's date and `source='chat'`.

- [ ] **Step 4: Update the roadmap**

In `docs/ROADMAP.md`, note under Epic C/family-mcp that the shopping list Phase 1 (shared add/remove/show/mark-bought) shipped.

- [ ] **Step 5: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "docs(shopping): Phase 1 shared list shipped"
```
