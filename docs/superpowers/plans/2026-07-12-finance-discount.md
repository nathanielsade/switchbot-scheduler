# finance-mcp (Discount → local SQLite) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only, self-hosted Discount-bank finance capability in the Hebrew Telegram agent — collect transactions on the box, store them locally, and answer money questions.

**Architecture:** A Node collector (pinned `israeli-bank-scrapers`, Discount only) prints a fixed JSON contract; a Python importer normalizes it (→agorot via `Decimal`, →ISO date, fingerprint) into a local SQLite `finance_store` (`transactions` + `account_snapshots` + `category_rules`); eight in-process `Tool`s read/act on the store. The injectable seam is `fetch_fn` (real = runs the collector; fake in tests = canned contract JSON). Categories are derived at read time from `category_rules`. Everything is offline-testable; only the live run needs the box.

**Tech Stack:** Python 3.11+ (venv is 3.14), stdlib `sqlite3`/`hashlib`/`decimal`/`subprocess`/`fcntl`, `pytest`. Node (collector only, not in the test path).

**Spec:** `docs/superpowers/specs/2026-07-12-finance-discount-design.md`.

## Global Constraints

- **Test command (the whole CI gate):** `PYTHONPATH=src .venv/bin/pytest -q --ignore=integration_tests`. No `ruff`/`mypy`.
- **No network / Node / bank in the automated suite.** The only side effect (running the collector) is behind the injectable **`fetch_fn`**, filled with a fake in tests.
- **Money never as float.** Store integer **agorot**; parse via `Decimal` from the contract's **string** amounts; quantize with `ROUND_HALF_UP`. Display via `Decimal(agorot)/100`.
- **Categories are rules, derived at read time.** Never stamp a category onto a transaction row as truth. Uncategorized = no matching `active` rule (distinct from the `"other"` slug).
- **Canonical category slugs (fixed):** `groceries, rent, salary, utilities, transport, health, restaurants, subscriptions, shopping, cash, transfer, other`.
- **Read-only:** no tool may move money. **Graceful-if-unconfigured:** finance tools load only when all three Discount creds are set; partial config → disabled + warning.
- **Tools are `home_agent.tools.Tool(name, schema, impl)`**, `impl(args)->str`, schema description tells the model when/how + to report in the user's language.
- **Stores:** connection-per-op (`with closing(sqlite3.connect(self.db_path)) as conn:`), like `shopping_store`.
- **`FAMILY_SYSTEM_PROMPT` stays digit-free + byte-stable** (tests enforce).

## File structure

- Create `src/home_agent/finance_store.py` — `FinanceStore` (schema + upsert + snapshots + rule CRUD + read queries).
- Create `src/home_agent/finance.py` — `finance_configured`, `make_collector_fetch`, the importer, `build_finance_tools`, all eight tools.
- Create `collector/scrape_discount.js` + `collector/package.json` + `collector/package-lock.json` — the Node collector (untested; manual).
- Modify `src/home_agent/config.py` — Discount + collector config keys.
- Modify `src/home_agent/telegram_app.py` — wire finance tools when configured.
- Modify `src/home_agent/prompts.py` — categorization policy (digit-free).
- Modify `.env.example`, `src/home_agent/CLAUDE.md`.
- Tests under `tests/home_agent/`: `test_finance_config.py`, `test_finance_store.py`, `test_finance_import.py`, `test_finance_tools.py`, plus a `finance_fakes.py` helper.

---

### Task 1: Config keys + `finance_configured` (graceful / partial-safe)

**Files:** Modify `src/home_agent/config.py`; Create `src/home_agent/finance.py` (config helper only this task); Test `tests/home_agent/test_finance_config.py`.

**Interfaces — Produces:**
- `Config` gains `discount_id/discount_password/discount_num: str = ""`, `finance_node_bin: str = "node"`, `finance_collector_script: str = DEFAULT_COLLECTOR_SCRIPT`.
- `finance.finance_configured(config) -> bool` — `True` iff all three Discount creds are set; if **some but not all** are set, logs a warning and returns `False`.

- [ ] **Step 1: Failing tests**

Create `tests/home_agent/test_finance_config.py`:
```python
from home_agent.config import Config
from home_agent.finance import finance_configured


def _cfg(**kw):
    base = dict(openai_api_key="k", telegram_bot_token="t", allowed_chat_ids=set())
    base.update(kw)
    return Config(**base)


def test_unconfigured_is_false():
    assert finance_configured(_cfg()) is False


def test_all_three_set_is_true():
    assert finance_configured(_cfg(discount_id="1", discount_password="p", discount_num="9")) is True


def test_partial_config_is_false_and_warns(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="home_agent"):
        assert finance_configured(_cfg(discount_id="1")) is False
    assert any("partial" in r.message.lower() or "finance" in r.message.lower() for r in caplog.records)
```

- [ ] **Step 2: Run — expect fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/home_agent/test_finance_config.py -v`
Expected: ImportError (`finance` missing) / AttributeError (`discount_id`).

- [ ] **Step 3: config.py fields**

Add constant near the others:
```python
DEFAULT_COLLECTOR_SCRIPT = "collector/scrape_discount.js"
```
Add to `Config` (after roborock fields):
```python
    discount_id: str = ""
    discount_password: str = ""
    discount_num: str = ""
    finance_node_bin: str = "node"
    finance_collector_script: str = DEFAULT_COLLECTOR_SCRIPT
```
Add to `load_config`'s `Config(...)`:
```python
        discount_id=os.environ.get("DISCOUNT_ID", ""),
        discount_password=os.environ.get("DISCOUNT_PASSWORD", ""),
        discount_num=os.environ.get("DISCOUNT_NUM", ""),
        finance_node_bin=os.environ.get("FINANCE_NODE_BIN", "node"),
        finance_collector_script=os.environ.get("FINANCE_COLLECTOR_SCRIPT", DEFAULT_COLLECTOR_SCRIPT),
```

- [ ] **Step 4: finance.py config helper**

Create `src/home_agent/finance.py`:
```python
import logging

log = logging.getLogger("home_agent")

CATEGORIES = ("groceries", "rent", "salary", "utilities", "transport", "health",
              "restaurants", "subscriptions", "shopping", "cash", "transfer", "other")


def finance_configured(config) -> bool:
    """True iff all three Discount creds are set. Partial config → warn + disable (fail safe)."""
    creds = [config.discount_id, config.discount_password, config.discount_num]
    if all(creds):
        return True
    if any(creds):
        log.warning("partial Discount config — finance disabled (need DISCOUNT_ID + PASSWORD + NUM)")
    return False
```

- [ ] **Step 5: Run — expect pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/home_agent/test_finance_config.py -v` → 3 passed.

- [ ] **Step 6: Full suite + commit**

```bash
PYTHONPATH=src .venv/bin/pytest -q --ignore=integration_tests
git add src/home_agent/config.py src/home_agent/finance.py tests/home_agent/test_finance_config.py
git commit -m "feat(finance): config keys + finance_configured (partial-safe)"
```

---

### Task 2: `finance_store.py` — schema + upsert + snapshots + rule CRUD

**Files:** Create `src/home_agent/finance_store.py`; Test `tests/home_agent/test_finance_store.py`.

**Interfaces — Produces `FinanceStore(db_path)` with:**
- `upsert_transactions(rows: list[dict]) -> tuple[int,int]` — returns `(inserted, updated)`. Each row already **normalized**: `source, account, identifier|None, fingerprint, txn_date, processed_date|None, amount_agorot:int, currency, description, status, raw_json`. Upsert key `(source, account, fingerprint)`; on conflict update `status, amount_agorot, txn_date, processed_date, description, raw_json, imported_at`.
- `record_snapshot(source, account, scraped_at, balance_agorot)` — append to `account_snapshots`.
- `current_balance_agorot() -> int` — Σ over accounts of each account's latest-`scraped_at` `balance_agorot`.
- `sum_amounts(from_date, to_date) -> tuple[int,int]` — `(income_agorot, expense_agorot)` where income=Σ amount>0, expense=Σ amount<0, over `txn_date` in `[from_date, to_date]`.
- `search(from_date=None, to_date=None, min_abs=None, max_abs=None, direction=None, query=None, limit=50) -> list[dict]`.
- `transactions_between(from_date, to_date) -> list[dict]` — rows for category/forecast aggregation.
- `add_rule(merchant_pattern, category) -> int` (rule id); `active_rules() -> list[dict]` (id, merchant_pattern, category); `remove_rule(rule_id) -> bool` (soft-delete: status→removed).

Uses `now_fn` for `imported_at`? Keep store clock-free: caller passes timestamps. `imported_at` set by the store via `CURRENT_TIMESTAMP` default is fine (not asserted). Tests inject `txn_date`/`scraped_at` explicitly.

- [ ] **Step 1: Failing tests**

Create `tests/home_agent/test_finance_store.py`:
```python
from home_agent.finance_store import FinanceStore


def _store(tmp_path):
    return FinanceStore(str(tmp_path / "fin.db"))


def _row(**kw):
    base = dict(source="discount", account="1", identifier="A1", fingerprint="id:A1",
                txn_date="2026-07-01", processed_date=None, amount_agorot=-45000,
                currency="ILS", description="שופרסל", status="completed", raw_json="{}")
    base.update(kw)
    return base


def test_upsert_inserts_then_dedups(tmp_path):
    s = _store(tmp_path)
    assert s.upsert_transactions([_row()]) == (1, 0)
    assert s.upsert_transactions([_row()]) == (0, 1)  # same fingerprint → update, not duplicate


def test_upsert_pending_to_settled_mutates_row(tmp_path):
    s = _store(tmp_path)
    s.upsert_transactions([_row(status="pending", amount_agorot=-45000)])
    s.upsert_transactions([_row(status="completed", amount_agorot=-45050)])
    got = s.search()
    assert len(got) == 1 and got[0]["status"] == "completed" and got[0]["amount_agorot"] == -45050


def test_current_balance_sums_latest_snapshot_per_account(tmp_path):
    s = _store(tmp_path)
    s.record_snapshot("discount", "1", "2026-07-01T00:00:00Z", 100000)
    s.record_snapshot("discount", "1", "2026-07-12T00:00:00Z", 120000)  # newer for acct 1
    s.record_snapshot("discount", "2", "2026-07-05T00:00:00Z", 30000)
    assert s.current_balance_agorot() == 150000  # 120000 + 30000


def test_sum_amounts_income_and_expense(tmp_path):
    s = _store(tmp_path)
    s.upsert_transactions([
        _row(identifier="i", fingerprint="id:i", amount_agorot=1000000, description="משכורת"),
        _row(identifier="e", fingerprint="id:e", amount_agorot=-45000),
    ])
    assert s.sum_amounts("2026-07-01", "2026-07-31") == (1000000, -45000)


def test_search_absolute_amount_and_direction(tmp_path):
    s = _store(tmp_path)
    s.upsert_transactions([
        _row(identifier="a", fingerprint="id:a", amount_agorot=-45000, description="chargeX"),
        _row(identifier="b", fingerprint="id:b", amount_agorot=1000000, description="salary"),
    ])
    hit = s.search(min_abs=45000, max_abs=45000)
    assert len(hit) == 1 and hit[0]["description"] == "chargeX"
    assert len(s.search(direction="income")) == 1


def test_rule_add_list_remove(tmp_path):
    s = _store(tmp_path)
    rid = s.add_rule("שופרסל", "groceries")
    assert [r["merchant_pattern"] for r in s.active_rules()] == ["שופרסל"]
    assert s.remove_rule(rid) is True
    assert s.active_rules() == []
```

- [ ] **Step 2: Run — expect fail** (`ModuleNotFoundError: finance_store`).

- [ ] **Step 3: Implement `finance_store.py`**

```python
import sqlite3
from contextlib import closing


class FinanceStore:
    """Local finance data (SQLite), connection-per-op like shopping_store. Money is integer agorot.
    `transactions` is durable current state (upsert by fingerprint); `account_snapshots` is append;
    `category_rules` are soft-deletable (status flip)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS transactions ("
                " source TEXT NOT NULL, account TEXT NOT NULL, identifier TEXT,"
                " fingerprint TEXT NOT NULL, txn_date TEXT NOT NULL, processed_date TEXT,"
                " amount_agorot INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'ILS',"
                " description TEXT NOT NULL, status TEXT NOT NULL, category_override TEXT,"
                " raw_json TEXT, imported_at TEXT DEFAULT CURRENT_TIMESTAMP,"
                " PRIMARY KEY (source, account, fingerprint));"
                "CREATE TABLE IF NOT EXISTS account_snapshots ("
                " source TEXT NOT NULL, account TEXT NOT NULL, scraped_at TEXT NOT NULL,"
                " balance_agorot INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS category_rules ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_pattern TEXT NOT NULL,"
                " category TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',"
                " created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
            )
            conn.commit()

    def upsert_transactions(self, rows):
        inserted = updated = 0
        with closing(sqlite3.connect(self.db_path)) as conn:
            for r in rows:
                exists = conn.execute(
                    "SELECT 1 FROM transactions WHERE source=? AND account=? AND fingerprint=?",
                    (r["source"], r["account"], r["fingerprint"])).fetchone()
                conn.execute(
                    "INSERT INTO transactions (source, account, identifier, fingerprint, txn_date,"
                    " processed_date, amount_agorot, currency, description, status, raw_json)"
                    " VALUES (:source,:account,:identifier,:fingerprint,:txn_date,:processed_date,"
                    " :amount_agorot,:currency,:description,:status,:raw_json)"
                    " ON CONFLICT(source, account, fingerprint) DO UPDATE SET"
                    " status=excluded.status, amount_agorot=excluded.amount_agorot,"
                    " txn_date=excluded.txn_date, processed_date=excluded.processed_date,"
                    " description=excluded.description, raw_json=excluded.raw_json,"
                    " imported_at=CURRENT_TIMESTAMP", r)
                if exists:
                    updated += 1
                else:
                    inserted += 1
            conn.commit()
        return inserted, updated

    def record_snapshot(self, source, account, scraped_at, balance_agorot):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO account_snapshots (source, account, scraped_at, balance_agorot)"
                         " VALUES (?,?,?,?)", (source, account, scraped_at, balance_agorot))
            conn.commit()

    def current_balance_agorot(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT a.balance_agorot FROM account_snapshots a WHERE a.scraped_at ="
                " (SELECT MAX(b.scraped_at) FROM account_snapshots b"
                "  WHERE b.source=a.source AND b.account=a.account)").fetchall()
        return sum(r[0] for r in rows)

    def sum_amounts(self, from_date, to_date):
        with closing(sqlite3.connect(self.db_path)) as conn:
            income = conn.execute(
                "SELECT COALESCE(SUM(amount_agorot),0) FROM transactions"
                " WHERE amount_agorot>0 AND txn_date BETWEEN ? AND ?", (from_date, to_date)).fetchone()[0]
            expense = conn.execute(
                "SELECT COALESCE(SUM(amount_agorot),0) FROM transactions"
                " WHERE amount_agorot<0 AND txn_date BETWEEN ? AND ?", (from_date, to_date)).fetchone()[0]
        return income, expense

    def _rows(self, conn, where="", params=()):
        cols = "source,account,identifier,fingerprint,txn_date,processed_date,amount_agorot,currency,description,status"
        q = f"SELECT {cols} FROM transactions {where}"
        return [dict(zip(cols.split(","), row)) for row in conn.execute(q, params).fetchall()]

    def transactions_between(self, from_date, to_date):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return self._rows(conn, "WHERE txn_date BETWEEN ? AND ? ORDER BY txn_date", (from_date, to_date))

    def search(self, from_date=None, to_date=None, min_abs=None, max_abs=None,
               direction=None, query=None, limit=50):
        clauses, params = [], []
        if from_date: clauses.append("txn_date >= ?"); params.append(from_date)
        if to_date: clauses.append("txn_date <= ?"); params.append(to_date)
        if min_abs is not None: clauses.append("ABS(amount_agorot) >= ?"); params.append(min_abs)
        if max_abs is not None: clauses.append("ABS(amount_agorot) <= ?"); params.append(max_abs)
        if direction == "income": clauses.append("amount_agorot > 0")
        elif direction == "expense": clauses.append("amount_agorot < 0")
        if query: clauses.append("description LIKE ?"); params.append(f"%{query}%")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with closing(sqlite3.connect(self.db_path)) as conn:
            return self._rows(conn, f"{where} ORDER BY txn_date DESC LIMIT ?", (*params, limit))

    def add_rule(self, merchant_pattern, category):
        with closing(sqlite3.connect(self.db_path)) as conn:
            rid = conn.execute("INSERT INTO category_rules (merchant_pattern, category) VALUES (?,?)",
                               (merchant_pattern, category)).lastrowid
            conn.commit()
        return rid

    def active_rules(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute("SELECT id, merchant_pattern, category FROM category_rules"
                                " WHERE status='active' ORDER BY id").fetchall()
        return [{"id": i, "merchant_pattern": p, "category": c} for i, p, c in rows]

    def remove_rule(self, rule_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute("UPDATE category_rules SET status='removed'"
                               " WHERE id=? AND status='active'", (rule_id,))
            conn.commit()
        return cur.rowcount > 0
```

- [ ] **Step 4: Run — expect pass** (6 passed).
- [ ] **Step 5: Full suite + commit**
```bash
git add src/home_agent/finance_store.py tests/home_agent/test_finance_store.py
git commit -m "feat(finance): finance_store (transactions/snapshots/rules)"
```

---

### Task 3: Collector + importer (Decimal/fingerprint) + `sync_finances`

**Files:** Create `collector/scrape_discount.js`, `collector/package.json`, `collector/package-lock.json`; Modify `src/home_agent/finance.py`; Create `tests/home_agent/finance_fakes.py`, `tests/home_agent/test_finance_import.py`.

**Interfaces — Produces (in `finance.py`):**
- `normalize_contract(data: dict) -> tuple[list[dict], list[dict], dict]` — returns `(txn_rows, snapshots, counts)`. Converts contract → store rows: `Decimal` money → `amount_agorot`/`balance_agorot`, ISO date slice, lowercased status, fingerprint, dropped-row count.
- `_to_agorot(s) -> int`, `_fingerprint(source, account, identifier, txn_date, amount_agorot, description) -> str`.
- `make_collector_fetch(config) -> Callable[[], dict]` — real seam: file-lock + `subprocess.run([node, script], shell=False, env, timeout)`, parse stdout JSON.
- `build_finance_tools(store, *, now_fn=None, fetch_fn=None) -> list[Tool]` — this task registers **`sync_finances`** only.

- [ ] **Step 1: Fake + failing tests**

Create `tests/home_agent/finance_fakes.py`:
```python
def contract(**over):
    """Canonical Collector JSON contract (strings for money)."""
    data = {
        "source": "discount", "scraped_at": "2026-07-12T18:00:00+03:00",
        "accounts": [{
            "account": "1", "balance": "1200.50",
            "transactions": [
                {"identifier": "A1", "date": "2026-07-01T00:00:00.000Z", "processedDate": None,
                 "chargedAmount": "-450.00", "chargedCurrency": "ILS", "description": "שופרסל", "status": "completed"},
                {"identifier": None, "date": "2026-07-02T00:00:00.000Z", "processedDate": None,
                 "chargedAmount": "1000.00", "chargedCurrency": "ILS", "description": "משכורת", "status": "completed"},
            ],
        }],
    }
    data.update(over)
    return data


def make_fetch(data):
    calls = {"n": 0}
    def _fetch():
        calls["n"] += 1
        return data
    _fetch.calls = calls
    return _fetch
```

Create `tests/home_agent/test_finance_import.py`:
```python
from decimal import Decimal
from home_agent.finance import normalize_contract, _to_agorot, _fingerprint, build_finance_tools
from home_agent.finance_store import FinanceStore
from finance_fakes import contract, make_fetch


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_to_agorot_uses_decimal_half_up():
    assert _to_agorot("-450.00") == -45000
    assert _to_agorot("0.005") == 1        # ROUND_HALF_UP, not banker's (would be 0)
    assert isinstance(_to_agorot("1.00"), int)


def test_fingerprint_prefers_identifier_else_hash():
    assert _fingerprint("discount", "1", "A1", "2026-07-01", -45000, "x") == "id:A1"
    h = _fingerprint("discount", "1", None, "2026-07-01", -45000, "שופרסל")
    assert h.startswith("h:") and h == _fingerprint("discount", "1", None, "2026-07-01", -45000, " שופרסל ")


def test_normalize_contract_shapes_rows_and_snapshots():
    txns, snaps, counts = normalize_contract(contract())
    assert snaps == [{"source": "discount", "account": "1",
                      "scraped_at": "2026-07-12T18:00:00+03:00", "balance_agorot": 120050}]
    amounts = sorted(r["amount_agorot"] for r in txns)
    assert amounts == [-45000, 100000]
    assert all(r["txn_date"] == "2026-07-01" or r["txn_date"] == "2026-07-02" for r in txns)
    assert counts["dropped"] == 0


def test_sync_finances_imports_and_reports_counts():
    import tempfile, os
    store = FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))
    tools = build_finance_tools(store, fetch_fn=make_fetch(contract()))
    out = _tool(tools, "sync_finances").impl({})
    assert "2" in out  # 2 imported (model-facing count)
    assert store.current_balance_agorot() == 120050


def test_sync_finances_malformed_is_friendly():
    store = FinanceStore(__import__("tempfile").mktemp())
    def boom(): raise ValueError("collector produced no JSON")
    out = _tool(build_finance_tools(store, fetch_fn=boom), "sync_finances").impl({})
    assert "לא הצלחתי" in out or "couldn" in out.lower() or "failed" in out.lower()
```
*(Note: use a tmp file DB — SQLite `:memory:` is per-connection, incompatible with connection-per-op.)*

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement importer + `sync_finances` in `finance.py`**

Add:
```python
import hashlib
import json
import re
from decimal import Decimal, ROUND_HALF_UP

from .tools import Tool

_WS = re.compile(r"\s+")


def _to_agorot(amount_str) -> int:
    return int((Decimal(str(amount_str)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _norm_desc(description) -> str:
    return _WS.sub(" ", (description or "").strip().lower())


def _fingerprint(source, account, identifier, txn_date, amount_agorot, description) -> str:
    if identifier:
        return f"id:{identifier}"
    raw = f"{source}|{account}|{txn_date}|{amount_agorot}|{_norm_desc(description)}"
    return "h:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def normalize_contract(data):
    source = data.get("source", "discount")
    txn_rows, snapshots, dropped = [], [], 0
    for acc in data.get("accounts", []):
        account = str(acc.get("account"))
        snapshots.append({"source": source, "account": account,
                          "scraped_at": data.get("scraped_at"),
                          "balance_agorot": _to_agorot(acc.get("balance", "0"))})
        for t in acc.get("transactions", []):
            try:
                amount = _to_agorot(t["chargedAmount"])
                txn_date = str(t["date"])[:10]
                desc = t["description"]
                if not desc or not txn_date:
                    raise KeyError("missing field")
            except (KeyError, TypeError, ValueError, ArithmeticError):
                dropped += 1
                continue
            identifier = t.get("identifier")
            txn_rows.append({
                "source": source, "account": account, "identifier": identifier,
                "fingerprint": _fingerprint(source, account, identifier, txn_date, amount, desc),
                "txn_date": txn_date,
                "processed_date": (str(t["processedDate"])[:10] if t.get("processedDate") else None),
                "amount_agorot": amount, "currency": t.get("chargedCurrency") or "ILS",
                "description": desc, "status": str(t.get("status", "completed")).lower(),
                "raw_json": json.dumps(t, ensure_ascii=False),
            })
    return txn_rows, snapshots, {"dropped": dropped}


_SYNC_SCHEMA = {"type": "function", "function": {
    "name": "sync_finances",
    "description": (
        "Pull the latest Discount bank transactions into the local store. Use when the user asks to "
        "refresh/update finances or before answering if data looks stale. Reports how many were imported "
        "and the date range — not the transactions themselves. Report back in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}


def _sync_impl(args, *, store, fetch_fn) -> str:
    try:
        data = fetch_fn()
        txns, snaps, counts = normalize_contract(data)
    except Exception as e:
        log.warning("sync_finances failed: %s", e)
        return "לא הצלחתי למשוך נתונים מהבנק כרגע. נסו שוב עוד רגע."
    for s in snaps:
        store.record_snapshot(s["source"], s["account"], s["scraped_at"], s["balance_agorot"])
    inserted, updated = store.upsert_transactions(txns)
    dates = sorted(t["txn_date"] for t in txns) or [""]
    dropped = f", {counts['dropped']} דולגו" if counts["dropped"] else ""
    return (f"נמשכו נתונים: {inserted} חדשות, {updated} עודכנו{dropped} "
            f"(טווח {dates[0]}…{dates[-1]}) ✅")


def build_finance_tools(store, *, now_fn=None, fetch_fn=None):
    return [
        Tool(name="sync_finances", schema=_SYNC_SCHEMA,
             impl=lambda a: _sync_impl(a, store=store, fetch_fn=fetch_fn)),
    ]
```
Also add `make_collector_fetch(config)` (real seam; not used by tests):
```python
def make_collector_fetch(config):
    import fcntl
    import os
    import subprocess
    script = config.finance_collector_script
    if not os.path.isabs(script):
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))  # src/home_agent -> repo
        script = os.path.join(repo_root, script)
    lock_path = os.path.join(os.path.dirname(config.db_path) or ".", ".finance_sync.lock")

    def _fetch():
        env = {**os.environ, "DISCOUNT_ID": config.discount_id,
               "DISCOUNT_PASSWORD": config.discount_password, "DISCOUNT_NUM": config.discount_num}
        with open(lock_path, "w") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("a finance sync is already running")
            proc = subprocess.run([config.finance_node_bin, script], capture_output=True,
                                  text=True, env=env, timeout=180, shell=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(f"collector failed (rc={proc.returncode})")  # stderr NOT surfaced
        return json.loads(proc.stdout)
    return _fetch
```

- [ ] **Step 4: Collector (Node — untested, transcribe)**

Create `collector/package.json` (pin the version confirmed at build time — use the current major, e.g. `^6`, then `npm i` to generate the lock):
```json
{
  "name": "smart-home-finance-collector",
  "private": true,
  "type": "commonjs",
  "dependencies": { "israeli-bank-scrapers": "6.8.0" }
}
```
Run `cd collector && npm install` once to produce **`package-lock.json`** (commit it; deploy with `npm ci`).
Create `collector/scrape_discount.js`:
```javascript
// Discount collector: prints the finance JSON contract to stdout ONLY. Reads DISCOUNT_* from env.
const { createScraper, CompanyTypes } = require('israeli-bank-scrapers');

(async () => {
  try {
    const scraper = createScraper({ companyId: CompanyTypes.discount, startDate: new Date(Date.now() - 365 * 864e5), combineInstallments: false, showBrowser: false });
    const result = await scraper.scrape({ id: process.env.DISCOUNT_ID, password: process.env.DISCOUNT_PASSWORD, num: process.env.DISCOUNT_NUM });
    if (!result.success) { console.error(`scrape failed: ${result.errorType} ${result.errorMessage || ''}`); process.exit(2); }
    const out = {
      source: 'discount', scraped_at: new Date().toISOString(),
      accounts: (result.accounts || []).map(a => ({
        account: String(a.accountNumber),
        balance: a.balance == null ? '0' : Number(a.balance).toFixed(2),
        transactions: (a.txns || []).map(t => ({
          identifier: t.identifier == null ? null : String(t.identifier),
          date: t.date, processedDate: t.processedDate || null,
          chargedAmount: Number(t.chargedAmount).toFixed(2),
          chargedCurrency: t.originalCurrency || 'ILS',
          description: t.description || '', status: t.status || 'completed',
        })),
      })),
    };
    process.stdout.write(JSON.stringify(out));
  } catch (e) { console.error(String(e && e.stack || e)); process.exit(1); }
})();
```
*(Confirm `israeli-bank-scrapers` API names against the pinned version at build time; adjust field mapping if the installed version differs. This file is not exercised by CI.)*

- [ ] **Step 5: Run tests — expect pass.** `PYTHONPATH=src .venv/bin/pytest tests/home_agent/test_finance_import.py -v`
- [ ] **Step 6: Full suite + commit**
```bash
git add collector src/home_agent/finance.py tests/home_agent/finance_fakes.py tests/home_agent/test_finance_import.py
git commit -m "feat(finance): collector + importer (Decimal/fingerprint) + sync_finances"
```

---

### Task 4: `financial_summary` + `find_transactions`

**Files:** Modify `src/home_agent/finance.py`; Test `tests/home_agent/test_finance_tools.py`.

**Interfaces — Consumes** `store.sum_amounts/current_balance_agorot/search`. **Produces** the two tools + `_period_range(period, now)` helper (`this_month|last_month|last_30_days` → `(from,to)`), and `_shekels(agorot)`.

- [ ] **Step 1: Failing tests** — create `tests/home_agent/test_finance_tools.py`:
```python
import tempfile, os
from datetime import datetime
from home_agent.finance import build_finance_tools
from home_agent.finance_store import FinanceStore
from finance_fakes import contract, make_fetch


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _frozen():
    return datetime(2026, 7, 12, 12, 0, 0)


def _seeded():
    store = _store()
    tools = build_finance_tools(store, now_fn=_frozen, fetch_fn=make_fetch(contract()))
    _tool(tools, "sync_finances").impl({})
    return store, tools


def test_financial_summary_income_expense_balance():
    store, tools = _seeded()
    out = _tool(tools, "financial_summary").impl({"from_date": "2026-07-01", "to_date": "2026-07-31"})
    assert "1,000.00" in out and "450.00" in out and "1,200.50" in out  # income, expense, balance ₪


def test_financial_summary_period_shortcut():
    store, tools = _seeded()
    out = _tool(tools, "financial_summary").impl({"period": "this_month"})
    assert "₪" in out


def test_find_transactions_absolute_amount():
    store, tools = _seeded()
    out = _tool(tools, "find_transactions").impl({"min_abs_agorot": 45000, "max_abs_agorot": 45000})
    assert "שופרסל" in out and "משכורת" not in out
```

- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** — add to `finance.py`:
```python
from datetime import datetime, timedelta

_PERIODS = ("this_month", "last_month", "last_30_days")


def _now():
    return datetime.now().astimezone()


def _shekels(agorot) -> str:
    return f"₪{Decimal(agorot) / 100:,.2f}"


def _period_range(period, now):
    d = now.date()
    if period == "last_30_days":
        return (d - timedelta(days=30)).isoformat(), d.isoformat()
    if period == "last_month":
        first_this = d.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1).isoformat(), last_prev.isoformat()
    return d.replace(day=1).isoformat(), d.isoformat()  # this_month (default)


def _resolve_range(args, now_fn):
    frm, to = args.get("from_date"), args.get("to_date")
    if frm and to:
        return frm, to
    return _period_range(args.get("period") or "this_month", now_fn())


_SUMMARY_SCHEMA = {"type": "function", "function": {
    "name": "financial_summary",
    "description": (
        "Summarize money for a period: income, expenses, net, and current balance (all accounts). Give "
        "explicit from_date/to_date (YYYY-MM-DD) when known, or a period shortcut. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "from_date": {"type": "string", "description": "YYYY-MM-DD"},
        "to_date": {"type": "string", "description": "YYYY-MM-DD"},
        "period": {"type": "string", "enum": list(_PERIODS)},
    }, "additionalProperties": False}}}

_FIND_SCHEMA = {"type": "function", "function": {
    "name": "find_transactions",
    "description": (
        "Find transactions by date range, ABSOLUTE amount in agorot (min_abs_agorot/max_abs_agorot; e.g. "
        "45000 = ₪450 regardless of income/expense), direction (income|expense), or a text query on the "
        "description. Returns up to fifty. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "from_date": {"type": "string"}, "to_date": {"type": "string"},
        "min_abs_agorot": {"type": "integer"}, "max_abs_agorot": {"type": "integer"},
        "direction": {"type": "string", "enum": ["income", "expense"]},
        "query": {"type": "string"},
    }, "additionalProperties": False}}}


def _summary_impl(args, *, store, now_fn) -> str:
    frm, to = _resolve_range(args, now_fn)
    income, expense = store.sum_amounts(frm, to)
    net = income + expense
    bal = store.current_balance_agorot()
    return (f"טווח {frm}…{to}:\nהכנסות: {_shekels(income)}\nהוצאות: {_shekels(expense)}\n"
            f"נטו: {_shekels(net)}\nיתרה נוכחית: {_shekels(bal)}")


def _find_impl(args, *, store) -> str:
    rows = store.search(from_date=args.get("from_date"), to_date=args.get("to_date"),
                        min_abs=args.get("min_abs_agorot"), max_abs=args.get("max_abs_agorot"),
                        direction=args.get("direction"), query=args.get("query"))
    if not rows:
        return "לא נמצאו תנועות תואמות."
    return "\n".join(f"{r['txn_date']}  {r['description']}  {_shekels(r['amount_agorot'])}  ({r['status']})"
                     for r in rows)
```
Register both in `build_finance_tools` (thread `now_fn = now_fn or _now`):
```python
    now_fn = now_fn or _now
    ...
        Tool(name="financial_summary", schema=_SUMMARY_SCHEMA,
             impl=lambda a: _summary_impl(a, store=store, now_fn=now_fn)),
        Tool(name="find_transactions", schema=_FIND_SCHEMA,
             impl=lambda a: _find_impl(a, store=store)),
```

- [ ] **Step 4: Run — expect pass.** **Step 5: Full suite + commit**
```bash
git add src/home_agent/finance.py tests/home_agent/test_finance_tools.py
git commit -m "feat(finance): financial_summary + find_transactions"
```

---

### Task 5: `spending_by_category` + rule tools (`set`/`list`/`delete`)

**Files:** Modify `src/home_agent/finance.py`; Test `tests/home_agent/test_finance_tools.py`.

**Interfaces — Produces** `spending_by_category`, `set_category_rule`, `list_category_rules`, `delete_category_rule`, and `_categorize(description, rules) -> str|None` (read-time; longest-pattern then newest-id precedence; `None` = uncategorized).

- [ ] **Step 1: Failing tests** (append):
```python
def test_spending_by_category_derives_and_surfaces_uncategorized():
    store, tools = _seeded()
    _tool(tools, "set_category_rule").impl({"merchant_pattern": "שופרסל", "category": "groceries"})
    out = _tool(tools, "spending_by_category").impl({"from_date": "2026-07-01", "to_date": "2026-07-31"})
    assert "groceries" in out and "450.00" in out  # שופרסל expense categorized


def test_set_category_rule_rejects_bad_slug():
    store, tools = _seeded()
    out = _tool(tools, "set_category_rule").impl({"merchant_pattern": "x", "category": "nonsense"})
    assert "nonsense" in out and "groceries" in out  # lists valid slugs


def test_rule_precedence_longest_then_newest():
    from home_agent.finance import _categorize
    rules = [{"id": 1, "merchant_pattern": "super", "category": "shopping"},
             {"id": 2, "merchant_pattern": "super pharm", "category": "health"}]
    assert _categorize("SUPER PHARM tlv", rules) == "health"          # longest wins
    rules2 = [{"id": 1, "merchant_pattern": "abc", "category": "shopping"},
              {"id": 2, "merchant_pattern": "abc", "category": "groceries"}]
    assert _categorize("abc", rules2) == "groceries"                  # tie → newest


def test_delete_category_rule_soft_removes():
    store, tools = _seeded()
    out = _tool(tools, "set_category_rule").impl({"merchant_pattern": "שופרסל", "category": "groceries"})
    rid = store.active_rules()[0]["id"]
    assert "✅" in _tool(tools, "delete_category_rule").impl({"id": rid})
    assert store.active_rules() == []
```

- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** — add to `finance.py`:
```python
def _categorize(description, rules):
    desc = _norm_desc(description)
    best = None
    for r in rules:  # rules come ordered by id asc; keep the best by (len, id)
        if r["merchant_pattern"].strip().lower() in desc:
            if best is None or (len(r["merchant_pattern"]), r["id"]) >= (len(best["merchant_pattern"]), best["id"]):
                best = r
    return best["category"] if best else None


_SPENDING_SCHEMA = {"type": "function", "function": {
    "name": "spending_by_category",
    "description": (
        "Break expenses into categories for a period (explicit from_date/to_date or a period shortcut). "
        "Returns per-category totals plus the uncategorized count and example merchants — offer to "
        "categorize those via set_category_rule. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "from_date": {"type": "string"}, "to_date": {"type": "string"},
        "period": {"type": "string", "enum": list(_PERIODS)}}, "additionalProperties": False}}}

_SET_RULE_SCHEMA = {"type": "function", "function": {
    "name": "set_category_rule",
    "description": (
        "Persist a rule mapping a merchant substring to a category so spending is grouped consistently. "
        "Auto-create the rule for obvious merchants; ask the user when ambiguous. Category must be one of: "
        + ", ".join(CATEGORIES) + ". Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "merchant_pattern": {"type": "string"}, "category": {"type": "string", "enum": list(CATEGORIES)}},
        "required": ["merchant_pattern", "category"], "additionalProperties": False}}}

_LIST_RULES_SCHEMA = {"type": "function", "function": {
    "name": "list_category_rules",
    "description": "List the active merchant→category rules (id, pattern, category). Report in the user's language.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}

_DEL_RULE_SCHEMA = {"type": "function", "function": {
    "name": "delete_category_rule",
    "description": "Remove a category rule by its id (from list_category_rules). Report in the user's language.",
    "parameters": {"type": "object", "properties": {"id": {"type": "integer"}},
                   "required": ["id"], "additionalProperties": False}}}


def _spending_impl(args, *, store, now_fn) -> str:
    frm, to = _resolve_range(args, now_fn)
    rules = store.active_rules()
    totals, uncategorized, examples = {}, 0, []
    for t in store.transactions_between(frm, to):
        if t["amount_agorot"] >= 0:
            continue  # expenses only
        cat = _categorize(t["description"], rules)
        if cat is None:
            uncategorized += 1
            if t["description"] not in examples:
                examples.append(t["description"])
        else:
            totals[cat] = totals.get(cat, 0) + t["amount_agorot"]
    lines = [f"{c}: {_shekels(v)}" for c, v in sorted(totals.items(), key=lambda kv: kv[1])]
    if uncategorized:
        lines.append(f"ללא קטגוריה: {uncategorized} (למשל: {', '.join(examples[:3])})")
    return "\n".join(lines) if lines else "אין הוצאות בטווח."


def _set_rule_impl(args, *, store) -> str:
    cat = (args.get("category") or "").strip().lower()
    if cat not in CATEGORIES:
        return f"קטגוריה לא חוקית '{cat}'. בחרו מתוך: {', '.join(CATEGORIES)}"
    pattern = (args.get("merchant_pattern") or "").strip()
    store.add_rule(pattern, cat)
    affected = [t["description"] for t in store.search(query=pattern, limit=1000)]
    ex = ", ".join(sorted(set(affected))[:3])
    return f"נוסף כלל: '{pattern}' → {cat} (משפיע על {len(affected)} תנועות{': ' + ex if ex else ''}) ✅"


def _list_rules_impl(args, *, store) -> str:
    rules = store.active_rules()
    if not rules:
        return "אין כללי קטגוריה."
    return "\n".join(f"[{r['id']}] {r['merchant_pattern']} → {r['category']}" for r in rules)


def _del_rule_impl(args, *, store) -> str:
    ok = store.remove_rule(args.get("id"))
    return f"כלל {args.get('id')} הוסר ✅" if ok else f"לא נמצא כלל פעיל עם מזהה {args.get('id')}."
```
Register all four in `build_finance_tools` (spending/list/delete pass `store`; spending also `now_fn`).

- [ ] **Step 4: Run — expect pass.** **Step 5: Full suite + commit**
```bash
git add src/home_agent/finance.py tests/home_agent/test_finance_tools.py
git commit -m "feat(finance): spending_by_category + category-rule tools"
```

---

### Task 6: `cash_flow_forecast`

**Files:** Modify `src/home_agent/finance.py`; Test `tests/home_agent/test_finance_tools.py`.

**Interfaces — Produces** `cash_flow_forecast` + `_detect_recurring(txns) -> list[dict]` (each: description, sign, typical amount, day-of-month, occurrences, confidence). A recurring item requires **≥2 months**, **same normalized description**, **same sign**, **day-of-month within ±3**, **amount within ±10%**. Forecast projects month-end from `current_balance` + remaining-this-month recurring in/out; flags overdraft.

- [ ] **Step 1: Failing tests** (append) — seed recurring salary + rent across two months and a one-off, assert detection + overdraft flag + confidence + one-off excluded. (Use a store seeded directly via `upsert_transactions` + a snapshot, under `now_fn=_frozen`.)
```python
def test_cash_flow_detects_recurring_and_flags_overdraft():
    from home_agent.finance import build_finance_tools
    store = _store()
    store.record_snapshot("discount", "1", "2026-07-12T00:00:00Z", 20000)  # ₪200 balance
    def r(i, d, amt, desc):
        return dict(source="discount", account="1", identifier=i, fingerprint=f"id:{i}",
                    txn_date=d, processed_date=None, amount_agorot=amt, currency="ILS",
                    description=desc, status="completed", raw_json="{}")
    store.upsert_transactions([
        r("s1", "2026-05-10", 1000000, "משכורת"), r("s2", "2026-06-10", 1000000, "משכורת"),
        r("t1", "2026-05-15", -800000, "שכירות"), r("t2", "2026-06-15", -800000, "שכירות"),
        r("o1", "2026-06-03", -50000, "חד פעמי"),
    ])
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "cash_flow_forecast").impl({})
    assert "משכורת" in out and "שכירות" in out and "חד פעמי" not in out
    assert "מינוס" in out or "overdraft" in out.lower() or "-" in out  # 200 +1000 -800 due 15th → tight/negative path
```

- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** — add to `finance.py`:
```python
def _detect_recurring(txns):
    from collections import defaultdict
    groups = defaultdict(list)
    for t in txns:
        groups[(_norm_desc(t["description"]), 1 if t["amount_agorot"] > 0 else -1)].append(t)
    recurring = []
    for (desc, sign), items in groups.items():
        months = {t["txn_date"][:7] for t in items}
        if len(months) < 2:
            continue
        days = [int(t["txn_date"][8:10]) for t in items]
        amts = [abs(t["amount_agorot"]) for t in items]
        if max(days) - min(days) > 3:
            continue
        typical = sorted(amts)[len(amts) // 2]
        if typical and (max(amts) - min(amts)) / typical > 0.10:
            continue
        occ = len(months)
        recurring.append({"description": items[-1]["description"], "sign": sign,
                          "amount_agorot": sign * typical, "day": round(sum(days) / len(days)),
                          "occurrences": occ, "confidence": "high" if occ >= 3 else "medium"})
    return recurring


_FORECAST_SCHEMA = {"type": "function", "function": {
    "name": "cash_flow_forecast",
    "description": (
        "Forecast end-of-month balance from current balance + detected recurring income/expenses, and flag "
        "a likely overdraft. Returns the projection AND the detected recurring items (with confidence) so you "
        "can explain the assumptions. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def _forecast_impl(args, *, store, now_fn) -> str:
    now = now_fn()
    lookback = (now.date() - timedelta(days=95)).isoformat()
    recurring = _detect_recurring(store.transactions_between(lookback, now.date().isoformat()))
    balance = store.current_balance_agorot()
    day = now.day
    remaining = sum(r["amount_agorot"] for r in recurring if r["day"] >= day)
    projected = balance + remaining
    lines = [f"יתרה נוכחית: {_shekels(balance)}",
             f"צפי לסוף החודש: {_shekels(projected)}" + (" ⚠️ צפוי מינוס" if projected < 0 else "")]
    if recurring:
        lines.append("פריטים קבועים שזוהו:")
        for r in recurring:
            lines.append(f"  {r['description']}: {_shekels(r['amount_agorot'])} (~יום {r['day']}, "
                         f"{r['occurrences']} חודשים, ביטחון {r['confidence']})")
    return "\n".join(lines)
```
Register in `build_finance_tools` (pass `store`, `now_fn`).

- [ ] **Step 4: Run — expect pass.** **Step 5: Full suite + commit**
```bash
git add src/home_agent/finance.py tests/home_agent/test_finance_tools.py
git commit -m "feat(finance): cash_flow_forecast (recurring detection + overdraft)"
```

---

### Task 7: Wiring + prompt policy + docs

**Files:** Modify `telegram_app.py`, `prompts.py`, `.env.example`, `src/home_agent/CLAUDE.md`; Test `tests/home_agent/test_telegram_app.py`, `tests/home_agent/test_system_prompt.py`.

**Interfaces — Consumes** `finance_configured`, `make_collector_fetch`, `FinanceStore`, `build_finance_tools`. **Produces** finance tools in the composed list iff configured.

- [ ] **Step 1: Failing wiring test** — append to `tests/home_agent/test_telegram_app.py` (mirror the existing roborock wiring test's style; reuse its helpers/monkeypatch approach):
```python
def test_build_application_includes_finance_tools_when_configured(monkeypatch, tmp_path):
    import home_agent.telegram_app as ta
    from home_agent.config import Config
    monkeypatch.setattr(ta, "finance_configured", lambda cfg: True)
    monkeypatch.setattr(ta, "make_collector_fetch", lambda cfg: (lambda: {"accounts": []}))
    monkeypatch.setattr(ta, "load_registry", lambda cfg: None)
    monkeypatch.setattr(ta, "load_calendar_service", lambda cfg: None)
    monkeypatch.setattr(ta, "load_roborock_client", lambda cfg: None)
    captured = {}
    orig = ta.build_finance_tools
    monkeypatch.setattr(ta, "build_finance_tools",
                        lambda store, **kw: captured.setdefault("tools", orig(store, **kw)))
    # reuse the file's fake Application/Builder helpers (as the roborock test does)
    _install_fake_app(monkeypatch, ta)   # helper already in this file for the roborock test
    cfg = Config(openai_api_key="k", telegram_bot_token="t", allowed_chat_ids={1},
                 db_path=str(tmp_path / "t.db"), discount_id="1", discount_password="p", discount_num="9")
    ta.build_application(cfg, client=object(), conversation=object())
    names = {t.name for t in captured["tools"]}
    assert {"sync_finances", "financial_summary", "find_transactions", "spending_by_category",
            "cash_flow_forecast"} <= names
```
*(If the roborock test used inline fakes rather than a shared `_install_fake_app` helper, mirror that exact structure instead.)*

- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Wire in `telegram_app.build_application`** — add imports:
```python
from .finance import build_finance_tools, finance_configured, make_collector_fetch
from .finance_store import FinanceStore
```
After the roborock block:
```python
    if finance_configured(config):
        tools += build_finance_tools(FinanceStore(config.db_path), fetch_fn=make_collector_fetch(config))
```

- [ ] **Step 4: Prompt policy (digit-free)** — append to `FAMILY_SYSTEM_PROMPT` a sentence (no digits):
```
You can also answer questions about the family's money from the Discount bank data: totals of income and
expenses, current balance, finding a specific charge, spending by category, and a simple end-of-month
forecast. When a merchant is clearly one category (a known supermarket is groceries), persist a category
rule without asking; when it is ambiguous, ask before saving. Report amounts as shown; never invent numbers.
```
Run `tests/home_agent/test_system_prompt.py` — keep digit-free + byte-stability green (update the pinned expectation in the same commit if that test compares an exact string/length).

- [ ] **Step 5: Docs** — add a `finance_store.py` + `finance.py` row to `src/home_agent/CLAUDE.md`; add the Discount/finance section to `.env.example`:
```
# --- finance (Discount bank, read-only) ---
# All three required to enable finance tools; partial config disables them.
DISCOUNT_ID=your-id
DISCOUNT_PASSWORD=your-password
DISCOUNT_NUM=your-user-code
# FINANCE_NODE_BIN=node
# FINANCE_COLLECTOR_SCRIPT=collector/scrape_discount.js
```

- [ ] **Step 6: Full suite + commit**
```bash
PYTHONPATH=src .venv/bin/pytest -q --ignore=integration_tests
git add src/home_agent/telegram_app.py src/home_agent/prompts.py src/home_agent/CLAUDE.md .env.example \
        tests/home_agent/test_telegram_app.py tests/home_agent/test_system_prompt.py
git commit -m "feat(finance): system-prompt policy + startup wiring + docs"
```

---

## Self-Review notes

- **Spec coverage:** config/graceful → T1; store (3 tables) → T2; collector+contract+importer(Decimal/fingerprint)+sync(lock/timeout/sanitized) → T3; summary(multi-account balance)+search(abs filters) → T4; category read-time derivation + rule set/list/delete(soft) → T5; forecast(recurring+confidence+overdraft) → T6; wiring+prompt+docs → T7.
- **Money:** never float — `Decimal` + `ROUND_HALF_UP`, agorot ints, `Decimal(agorot)/100` for display only.
- **Type consistency:** `normalize_contract` row dict keys match `FinanceStore.upsert_transactions` columns; `_categorize` consumes `active_rules()` dicts; tools thread `now_fn`/`store`/`fetch_fn` as the factory injects them.
- **Offline:** every test uses the fake `fetch_fn`/seeded store; the Node collector + `make_collector_fetch` are never exercised by CI (documented). SQLite uses a tmp **file** (not `:memory:`) because of connection-per-op.
- **Deviation:** none from the spec; the eight tools + three tables land exactly as specified.
