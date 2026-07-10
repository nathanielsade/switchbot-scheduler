# Shopping list — Phase 2 (cycle prediction) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot answers "what are we probably out of?" — learning rebuy rhythms from the append-only `purchases` log and surfacing items whose usual interval has elapsed, plus a `purchase_history` lookup. Deterministic math in tools; the agent reasons over and phrases it.

**Architecture:** Two new read methods on `ShoppingStore` and two new tools in `build_shopping_tools`. The math (median gap between distinct purchase dates vs. days-since-last) is pure Python behind an injectable clock. The tools are auto-composed into the bot (Phase 1 already wired `build_shopping_tools` into `build_application`), so **no new wiring**. Also lifts the shopping canonicalization policy into the system prompt (a design fix from the architecture review).

**Tech Stack:** Python 3.11+, `sqlite3` + `statistics` (stdlib), `pytest`. No new dependencies.

## Global Constraints

- Python **3.11+**.
- **No network in tests.** `suggest_restock` uses an injectable `now_fn()` (the one already threaded into `build_shopping_tools`); seed purchases with fixed dates and freeze the clock.
- **Same-day dedup lives in the MATH, not the store** (keep append-only): cadence is computed over **distinct** `purchased_on` dates per item, so a double `mark_bought` on one day can't create a 0-day gap. Do NOT change `buy()` / delete rows.
- `suggest_restock` **requires ≥2 distinct purchase dates** for an item (else no signal → omit) and **excludes items currently on the pending list** (don't re-suggest what's already listed).
- Tools are `home_agent.tools.Tool`; added to the existing `build_shopping_tools(store, *, now_fn=None)` — they are then auto-composed into the bot with no `telegram_app` change.
- The canonicalization policy moves into `FAMILY_SYSTEM_PROMPT` as one cross-tool rule (Phase 1 had it only in the `add_to_list` tool description). The prompt must stay **digit-free** (an existing test asserts no digits) and byte-stable across turns.
- Make NO changes to `switchbot_scheduler`. `.venv/bin/pytest` is the runner. Commit after every task.

---

### Task 1: `ShoppingStore` read methods for cadence + history

**Files:**
- Modify: `src/home_agent/shopping_store.py`
- Test: `tests/home_agent/test_shopping_store.py`

**Interfaces:**
- Consumes: existing `ShoppingStore` (items/list/purchases).
- Produces: `purchase_dates_by_item() -> dict[str, list[str]]` (canonical name → **distinct** ISO dates ascending; same-day collapsed); `recent_purchases(limit=20) -> list[dict]` (keys: item, purchased_on, unit_price; newest first).

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_shopping_store.py`:

```python
def test_purchase_dates_by_item_collapses_same_day(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.buy("חלב", "2026-07-01")
    s.buy("חלב", "2026-07-01")   # same day again — must collapse to one date
    s.buy("חלב", "2026-07-06")
    s.buy("קפה", "2026-07-03")
    d = s.purchase_dates_by_item()
    assert d["חלב"] == ["2026-07-01", "2026-07-06"]   # distinct, ascending
    assert d["קפה"] == ["2026-07-03"]


def test_recent_purchases_newest_first_with_limit(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.buy("חלב", "2026-07-01", unit_price=6.9)
    s.buy("קפה", "2026-07-05")
    s.buy("לחם", "2026-07-03")
    recent = s.recent_purchases(limit=2)
    assert [r["item"] for r in recent] == ["קפה", "לחם"]   # newest first, limited
    assert recent[0]["purchased_on"] == "2026-07-05"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_shopping_store.py -k "dates_by_item or recent_purchases" -v`
Expected: FAIL — `AttributeError: 'ShoppingStore' object has no attribute 'purchase_dates_by_item'`.

- [ ] **Step 3: Add the methods to `shopping_store.py`**

Add inside the `ShoppingStore` class (after `purchases_for`):

```python
    def purchase_dates_by_item(self):
        """{canonical name: [distinct ISO purchase dates ascending]} — same-day duplicates collapsed
        (via GROUP BY), so cadence math never sees a 0-day gap from a double log."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT i.name, p.purchased_on FROM purchases p JOIN items i ON i.id = p.item_id "
                "GROUP BY i.name, p.purchased_on ORDER BY i.name, p.purchased_on").fetchall()
        out = {}
        for name, d in rows:
            out.setdefault(name, []).append(d)
        return out

    def recent_purchases(self, limit=20):
        """Most recent purchases across all items, newest first."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT i.name, p.purchased_on, p.unit_price FROM purchases p "
                "JOIN items i ON i.id = p.item_id ORDER BY p.purchased_on DESC, p.id DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"item": n, "purchased_on": d, "unit_price": u} for n, d, u in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_shopping_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/shopping_store.py tests/home_agent/test_shopping_store.py
git commit -m "feat(shopping): store read methods for cadence (distinct dates) + recent history"
```

---

### Task 2: `suggest_restock` + `purchase_history` tools

**Files:**
- Modify: `src/home_agent/shopping.py`
- Test: `tests/home_agent/test_shopping_tools.py`

**Interfaces:**
- Consumes: `store.purchase_dates_by_item()`, `store.recent_purchases()`, `store.purchases_for(name)`, `store.pending()` (Task 1 + Phase 1); the `now_fn` already in `build_shopping_tools`.
- Produces: two tools appended to `build_shopping_tools`'s returned list — `suggest_restock` (no params) and `purchase_history(item?)`; helper `_days_between(a_iso, b_iso) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_shopping_tools.py`:

```python
def _tools_at(tmp_path, today_iso):
    from datetime import date
    store = ShoppingStore(str(tmp_path / "sh.db"))

    class _Now:
        def date(self):
            return date.fromisoformat(today_iso)

    return build_shopping_tools(store, now_fn=lambda: _Now()), store


def test_suggest_restock_flags_overdue_with_numbers(tmp_path):
    tools, store = _tools_at(tmp_path, "2026-07-17")
    for d in ("2026-07-01", "2026-07-06", "2026-07-11"):   # milk every 5 days
        store.buy("חלב", d)
    out = _tool(tools, "suggest_restock").impl({})
    assert "חלב" in out
    assert "5" in out and "6" in out          # usual gap 5, 6 days since last (2026-07-11 → 17)


def test_suggest_restock_skips_recent_and_sparse_and_listed(tmp_path):
    tools, store = _tools_at(tmp_path, "2026-07-12")
    # recent: bought today-ish, not due
    for d in ("2026-07-01", "2026-07-06", "2026-07-11"):
        store.buy("חלב", d)                    # last 07-11, gap 5, only 1 day since → NOT due
    # sparse: only one purchase → no signal
    store.buy("קפה", "2026-07-01")
    # due by history but already on the list → excluded
    for d in ("2026-06-01", "2026-06-11", "2026-06-21"):
        store.buy("סוכר", d)                   # gap 10, ~21 days since on 07-12 → would be due
    store.add("סוכר")                          # ...but it's on the list now
    out = _tool(tools, "suggest_restock").impl({})
    assert "חלב" not in out and "קפה" not in out and "סוכר" not in out
    assert "nothing" in out.lower()


def test_suggest_restock_collapses_same_day(tmp_path):
    tools, store = _tools_at(tmp_path, "2026-07-17")
    store.buy("חלב", "2026-07-01")
    store.buy("חלב", "2026-07-01")             # duplicate same-day log
    store.buy("חלב", "2026-07-11")
    out = _tool(tools, "suggest_restock").impl({})
    # distinct dates [07-01, 07-11] → gap 10, 6 days since on 07-17 → NOT due (no phantom 0-gap)
    assert "nothing" in out.lower()


def test_purchase_history_for_item_and_recent(tmp_path):
    tools, store = _tools_at(tmp_path, "2026-07-17")
    store.buy("חלב", "2026-07-01", unit_price=6.9)
    out_item = _tool(tools, "purchase_history").impl({"item": "חלב"})
    assert "2026-07-01" in out_item and "6.9" in out_item
    out_all = _tool(tools, "purchase_history").impl({})
    assert "חלב" in out_all
    assert "no purchase history" in _tool(tools, "purchase_history").impl({"item": "לא-קיים"}).lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_shopping_tools.py -k "restock or purchase_history" -v`
Expected: FAIL — `StopIteration` (no `suggest_restock` tool yet).

- [ ] **Step 3: Implement in `shopping.py`**

Add imports at the top (next to the existing `from datetime import datetime`):

```python
import statistics
from datetime import date
```

Add the schemas near the other `_*_SCHEMA` constants:

```python
_RESTOCK_SCHEMA = {"type": "function", "function": {
    "name": "suggest_restock",
    "description": "Suggest what to restock based on purchase history: items bought regularly whose usual "
                   "interval has elapsed and that aren't already on the list. Use when the user asks what "
                   "to buy or what they're out of. Returns items with the numbers (last bought, usual gap, "
                   "days since); phrase them warmly in the user's language.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_HISTORY_SCHEMA = {"type": "function", "function": {
    "name": "purchase_history",
    "description": "Show purchase history — for one item (its dates and prices) or, with no item, the "
                   "recent purchases across everything. Use for 'when did we last buy X' / 'how much was X'.",
    "parameters": {"type": "object", "properties": {
        "item": {"type": "string", "description": "Canonical item name; omit for the recent log."},
    }, "additionalProperties": False},
}}
```

Add the helper and impls:

```python
def _days_between(a_iso, b_iso):
    return (date.fromisoformat(b_iso) - date.fromisoformat(a_iso)).days


def _restock_impl(args, *, store, now_fn):
    today = now_fn().date()
    pending = {r["item"] for r in store.pending()}
    due = []
    for name, dates in store.purchase_dates_by_item().items():
        if name in pending or len(dates) < 2:
            continue
        gaps = [_days_between(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]
        median_gap = statistics.median(gaps)
        days_since = (today - date.fromisoformat(dates[-1])).days
        if days_since >= median_gap:
            due.append((days_since - median_gap, name, dates[-1], median_gap, days_since))
    if not due:
        return "nothing looks due to restock right now"
    due.sort(reverse=True)   # most overdue first
    return "\n".join(
        f"{name}: last bought {last}, usually every {int(gap)} days, {ds} days since (due)"
        for _, name, last, gap, ds in due)


def _history_impl(args, *, store):
    item = (args.get("item") or "").strip()
    if item:
        rows = store.purchases_for(item)
        if not rows:
            return f"no purchase history for {item}"
        return "\n".join(
            f"{r['purchased_on']}: {item}" + (f" ₪{r['unit_price']}" if r["unit_price"] is not None else "")
            for r in rows)
    rows = store.recent_purchases()
    if not rows:
        return "no purchases logged yet"
    return "\n".join(
        f"{r['purchased_on']}: {r['item']}" + (f" ₪{r['unit_price']}" if r["unit_price"] is not None else "")
        for r in rows)
```

Append the two tools to `build_shopping_tools`'s returned list (after `known_items`):

```python
        Tool(name="suggest_restock", schema=_RESTOCK_SCHEMA,
             impl=lambda a: _restock_impl(a, store=store, now_fn=now_fn)),
        Tool(name="purchase_history", schema=_HISTORY_SCHEMA,
             impl=lambda a: _history_impl(a, store=store)),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/home_agent/test_shopping_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/shopping.py tests/home_agent/test_shopping_tools.py
git commit -m "feat(shopping): suggest_restock (median-gap, dedup same-day, excludes listed) + purchase_history"
```

---

### Task 3: Lift the canonicalization policy into the system prompt

**Files:**
- Modify: `src/home_agent/prompts.py`
- Modify: `src/home_agent/shopping.py` (trim the now-duplicated sentence from `add_to_list`'s description)
- Test: `tests/home_agent/test_system_prompt.py`

**Interfaces:**
- Produces: `FAMILY_SYSTEM_PROMPT` gains one cross-tool canonicalization sentence (digit-free); `add_to_list`'s description no longer repeats it.

- [ ] **Step 1: Update the prompt test (RED)**

In `tests/home_agent/test_system_prompt.py`, add an anchor assertion to `test_prompt_is_nonempty_and_stable` (keep the existing `"Hebrew"` and no-digit assertions):

```python
    assert "canonical" in FAMILY_SYSTEM_PROMPT.lower()   # shopping canonicalization policy present
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_system_prompt.py -v`
Expected: FAIL — the assertion `"canonical" in ...` fails (prompt has no shopping policy yet).

- [ ] **Step 3: Add the policy to `prompts.py`**

Insert this sentence into `FAMILY_SYSTEM_PROMPT`, right before the final "If a request is ambiguous…" line (no digits — keep the no-digit test green):

```python
    "For the shared shopping list, always use the canonical item name: if the user's wording is a variant "
    "of something already on the known-items list, reuse that known name (use known_items if unsure) "
    "rather than creating a near-duplicate. "
```

- [ ] **Step 4: Trim the duplicate from `add_to_list`'s description in `shopping.py`**

Replace the `_ADD_SCHEMA` description string with the concise version (the policy now lives in the prompt):

```python
        "Add an item to the shared shopping list. Report back in the user's language.",
```

(Leave the `item`/`quantity`/`note` parameter descriptions unchanged.)

- [ ] **Step 5: Run the full suite to verify green**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (prompt tests green; no other test asserts the `add_to_list` description text).

- [ ] **Step 6: Commit**

```bash
git add src/home_agent/prompts.py src/home_agent/shopping.py tests/home_agent/test_system_prompt.py
git commit -m "feat(shopping): lift canonicalization policy into system prompt (one cross-tool rule)"
```

---

### Task 4: Live smoke test (manual)

**Files:** none (verification + docs).

**Interfaces:** none.

- [ ] **Step 1: Full automated suite**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS.

- [ ] **Step 2: Seed a little history, then ask (one bot instance)**

Start the bot: `PYTHONPATH=src .venv/bin/python -m home_agent` (ensure no other instance running).
In Telegram, log a couple of buys across "different days" to give the math signal (or just exercise the flow), then:
- `קניתי חלב` (a few times over days, or seed via chat) → builds history.
- `מה כדאי לקנות?` → the bot should call `suggest_restock` and reply with items + reasons, skipping anything already on the list.
- `מתי קנינו חלב לאחרונה?` → the bot should call `purchase_history` and answer.
Stop the bot with Ctrl+C.

- [ ] **Step 3: Update the roadmap**

In `docs/ROADMAP.md`, mark the shopping-list **Phase 2** bullet ✅ (cycle prediction shipped: `suggest_restock` + `purchase_history`).

- [ ] **Step 4: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "docs(shopping): Phase 2 cycle prediction shipped"
```
