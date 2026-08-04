# Max Card Itemized Finance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Import the Max credit card's itemized purchases, enable real spending categories, and compute spend/cash-flow correctly (no double-counting the bank's card bills).

**Architecture:** Multi-source finance store (`source='discount'|'max'`). Spend lens (`financial_summary`, `spending_by_category`) uses Max purchases and drops the bank card-bills for imported+covered cards; cash-flow lens (`cash_flow_forecast`) is bank-only. Card-bill detection is a protected internal matcher, not a user rule. Coverage is explicit per-source metadata Python owns.

**Tech Stack:** Python 3.11, `israeli-bank-scrapers` (Node/Puppeteer, `companyId=max`), stdlib, SQLite.

**Spec:** `docs/superpowers/specs/2026-08-04-max-card-itemized-finance-design.md` (read for full rationale).

## Global Constraints

- Python `>=3.11`; `pytest` is the only gate; **no network/bank in the automated suite** (inject fake fetchers; the `.js` is exercised only in the live verify).
- Money is **integer agorot**; never float.
- **Never log** `MAX_USERNAME`/`MAX_PASSWORD` or Discount creds.
- Card-bill detection = **protected internal matcher** (`_is_card_payment`), NOT a `category_rules` row: invisible to `list_category_rules`, not deletable, priority over user rules.
- Real Discount card-bill descriptions (fixtures): `'חיוב לכרטיס ויזה 1743'`, `'זיכוי לכרטיס ויזה 1743'`, `'חיוב לכרטיס ויזה 6146'`.
- Coverage from explicit `source_coverage` window Python owns — **not** `MAX(txn_date)`.
- Exactly one side counts per card/period: covered → Max purchases (drop bill); uncovered/un-imported → bank bill (drop that card's Max rows). Excluded card-payment lines drop from **both** income and expense.
- Run tests: `.venv/bin/pytest -q --ignore=integration_tests`.

---

### Task 1: Config — Max creds, collector script, scrape window

**Files:** Modify `src/home_agent/config.py`; Test `tests/home_agent/test_config_max.py`

**Interfaces — Produces:** `Config.max_username/max_password/max_collector_script/finance_start_days:int=400`; `max_configured(config)->bool`.

- [ ] **Step 1 — failing test**
```python
# tests/home_agent/test_config_max.py
from home_agent.config import load_config, max_configured
def test_max_config(monkeypatch, tmp_path):
    for k,v in {"OPENAI_API_KEY":"k","TELEGRAM_BOT_TOKEN":"t","ALLOWED_CHAT_IDS":"1",
                "MAX_USERNAME":"u","MAX_PASSWORD":"p"}.items(): monkeypatch.setenv(k,v)
    c = load_config(str(tmp_path/"no.env"))
    assert c.max_username=="u" and c.max_password=="p" and max_configured(c)
    assert c.max_collector_script.endswith("scrape_max.js") and c.finance_start_days>=365
```
- [ ] **Step 2** run → FAIL (no `max_username`). `.venv/bin/pytest tests/home_agent/test_config_max.py -q`
- [ ] **Step 3 — implement:** add fields to `Config` + reads in `load_config` (mirror the DISCOUNT block): `max_username=os.environ.get("MAX_USERNAME","")`, `max_password=os.environ.get("MAX_PASSWORD","")`, `max_collector_script=os.environ.get("MAX_COLLECTOR_SCRIPT", "collector/scrape_max.js")`, `finance_start_days=int(os.environ.get("FINANCE_START_DAYS","400"))`. Add module-level `def max_configured(config): return bool(config.max_username and config.max_password)`.
- [ ] **Step 4** run → PASS; also full suite green.
- [ ] **Step 5** commit `feat(config): Max card creds + scrape window`

---

### Task 2: `_is_card_payment` protected matcher

**Files:** Modify `src/home_agent/finance.py`; Test `tests/home_agent/test_card_payment_matcher.py`

**Interfaces — Produces:** `_is_card_payment(description: str, card_numbers: set[str]) -> bool` — True iff the normalized description is a Discount card-bill/credit line for one of `card_numbers`. Pure; no DB.

- [ ] **Step 1 — failing test (real fixtures)**
```python
from home_agent.finance import _is_card_payment
CARDS={"1743"}
def test_matches_real_charge():   assert _is_card_payment("חיוב לכרטיס ויזה 1743", CARDS)
def test_matches_real_credit():   assert _is_card_payment("זיכוי לכרטיס ויזה 1743", CARDS)
def test_other_card_not_matched():assert not _is_card_payment("חיוב לכרטיס ויזה 6146", CARDS)
def test_non_card_line_not_matched(): assert not _is_card_payment("תחנת דלק יעד כפר קאסם", CARDS)
def test_spacing_robust():        assert _is_card_payment("חיוב  לכרטיס   ויזה 1743", CARDS)
```
- [ ] **Step 2** run → FAIL.
- [ ] **Step 3 — implement** (reuse `_norm_desc`; robust to spacing, match the card number as a distinct token after "ויזה"):
```python
import re
_CARD_BILL_RE = re.compile(r"(חיוב|זיכוי)\s+לכרטיס\s+ויזה\s+(\d+)")
def _is_card_payment(description, card_numbers):
    m = _CARD_BILL_RE.search(_norm_desc(description))
    return bool(m and m.group(2) in card_numbers)
```
(`_norm_desc` already collapses/normalizes; confirm it doesn't strip digits.)
- [ ] **Step 4** run → PASS; full suite green.
- [ ] **Step 5** commit `feat(finance): protected card-payment matcher (per card, real-format)`

---

### Task 3: `source_coverage` in FinanceStore

**Files:** Modify `src/home_agent/finance_store.py`; Test `tests/home_agent/test_source_coverage.py`

**Interfaces — Produces:** `record_coverage(source, account, coverage_start, coverage_end, scraped_at)` (upsert per source+account); `is_covered(source, account, from_date, to_date) -> bool` (`coverage_start<=from_date and coverage_end>=to_date`); `covered_cards(source, from_date, to_date) -> set[str]` (accounts of `source` covering the range).

- [ ] **Step 1 — failing test**
```python
from home_agent.finance_store import FinanceStore
def test_coverage(tmp_path):
    s=FinanceStore(str(tmp_path/"f.db"))
    s.record_coverage("max","1743","2025-08-01","2026-08-04","2026-08-04T10:00")
    assert s.is_covered("max","1743","2026-04-01","2026-06-30")
    assert not s.is_covered("max","1743","2024-01-01","2024-02-01")  # before window
    assert not s.is_covered("max","1743","2026-04-01","2026-09-01")  # past coverage_end
    assert s.covered_cards("max","2026-04-01","2026-06-30")=={"1743"}
```
- [ ] **Step 2** run → FAIL.
- [ ] **Step 3 — implement:** add table to the `executescript` in `__init__`: `CREATE TABLE IF NOT EXISTS source_coverage (source TEXT, account TEXT, coverage_start TEXT, coverage_end TEXT, scraped_at TEXT, PRIMARY KEY(source,account));`. Add the three methods (connection-per-op; `record_coverage` = `INSERT … ON CONFLICT(source,account) DO UPDATE`).
- [ ] **Step 4** run → PASS; full suite green.
- [ ] **Step 5** commit `feat(finance-store): source_coverage window + is_covered/covered_cards`

---

### Task 4: `scrape_max.js` + per-source `make_collector_fetch(config, source)`

**Files:** Create `collector/scrape_max.js`; Modify `collector/scrape_discount.js` (read start-date env); Modify `src/home_agent/finance.py`; Test `tests/home_agent/test_collector_fetch.py`

**Interfaces — Produces:** `make_collector_fetch(config, source: str) -> callable` returning a fetch that runs the source's collector with `FINANCE_START_DATE` + the source creds in env, and returns the parsed contract dict (via existing `normalize_contract`). `'discount'`→`finance_collector_script`+`DISCOUNT_*`; `'max'`→`max_collector_script`+`MAX_*`.

- [ ] **Step 1 — `scrape_max.js`** (mirror `scrape_discount.js`): `createScraper({companyId: CompanyTypes.max, startDate: new Date(process.env.FINANCE_START_DATE), combineInstallments:false, showBrowser:false})`; `scrape({username:process.env.MAX_USERNAME, password:process.env.MAX_PASSWORD})`; emit the same JSON contract with `source:'max'`, looping `result.accounts`. In `scrape_discount.js` change the hardcoded `startDate` to `new Date(process.env.FINANCE_START_DATE || Date.now()-365*864e5)`.
- [ ] **Step 2 — failing test** (Python side; no JS/network — inject a fake runner):
```python
# assert make_collector_fetch builds correct env + parses a max-shaped payload via normalize_contract
# (spec §Testing: normalize_contract on a source='max' multi-card payload → correct rows)
```
Write a test that calls `normalize_contract` on a `source='max'` two-card payload and asserts rows have `source='max'`, correct accounts, agorot amounts. And a test that `make_collector_fetch(config,'max')` passes `MAX_USERNAME`/`FINANCE_START_DATE` into the subprocess env (inject a fake `subprocess` runner capturing env) and returns the parsed contract.
- [ ] **Step 3 — implement:** refactor `make_collector_fetch(config)` → `make_collector_fetch(config, source)`: pick script+creds by source, set `env["FINANCE_START_DATE"]` = `(_now()-timedelta(days=config.finance_start_days)).date().isoformat()`, run collector, `normalize_contract(json.loads(stdout))`. Keep the file-lock usage in the caller (Task 5), not here.
- [ ] **Step 4** run → PASS; full suite green.
- [ ] **Step 5** commit `feat(finance): scrape_max.js + per-source make_collector_fetch(config, source)`

---

### Task 5: Multi-source sync + coverage recording + `build_finance_tools(fetch_fns)` + wiring

**Files:** Modify `src/home_agent/finance.py`, `src/home_agent/telegram_app.py`; Test `tests/home_agent/test_sync_multisource.py`

**Interfaces:**
- Consumes: `make_collector_fetch` (T4), `record_coverage` (T3).
- Produces: `_sync_impl(args, *, store, fetch_fns, now_fn)` — iterate `fetch_fns: dict[str, callable]`, each in own try/except, upsert + `record_coverage(source, account, start, now, now)` (start = the window start; pass via closure or config), per-source counts/errors. `build_finance_tools(store, *, now_fn=None, fetch_fns=None)` (map, not `fetch_fn`).

- [ ] **Step 1 — failing tests:** (a) fetcher map `{'discount': ok, 'max': raises}` → discount rows committed + a Max error reported, discount unaffected; (b) after sync, `is_covered('max', account, …)` true for the imported card.
- [ ] **Step 2** run → FAIL.
- [ ] **Step 3 — implement:** rewrite `_sync_impl` to loop the map (file-lock once around the loop); on each source success, `record_coverage`. Change `build_finance_tools` signature to `fetch_fns`. In `telegram_app.build_application` (line 133) build the map: `fetch_fns={'discount': make_collector_fetch(config,'discount')} ; if max_configured(config): fetch_fns['max']=make_collector_fetch(config,'max')` and pass it. Update the `finance.py` import list in telegram_app to include `max_configured`.
- [ ] **Step 4** run → PASS; full suite green.
- [ ] **Step 5** commit `feat(finance): multi-source sync + coverage recording; build_finance_tools(fetch_fns) + wiring`

---

### Task 6: Option A in `financial_summary` (exclusion + coverage + income/expense)

**Files:** Modify `src/home_agent/finance.py`; Test `tests/home_agent/test_summary_option_a.py`

**Interfaces:** Consumes `_is_card_payment` (T2), `covered_cards`/`is_covered` (T3). Produces a spendable-filter helper used by T6/T7: `_spendable_rows(store, frm, to) -> (rows, partial_flag)` — drops covered imported cards' bank card-bill lines AND drops uncovered cards' `source='max'` rows; `partial_flag` True if any card-bill was kept because its card wasn't covered.

- [ ] **Step 1 — failing tests** (per spec §Testing): reconcile (covered) excludes card-bill from **both** income/expense; un-imported card …6146 bill stays counted; coverage-window gate (range past `coverage_end` → include bill + flag); symmetric uncovered (bank bill + partial Max rows → only bill counts); positive `זיכוי` card line excluded from income.
- [ ] **Step 2** run → FAIL.
- [ ] **Step 3 — implement `_spendable_rows`** and rewrite `_summary_impl` to use it: iterate rows; `covered = covered_cards('max', frm, to)`; for each row, if `_is_card_payment(desc, covered)` → drop (both sides); if `row.source=='max'` and its account not covered → drop; else keep. income=Σ kept positives, expense=Σ kept negatives. Append the partial/stale flag line when set. Balance unchanged.
- [ ] **Step 4** run → PASS; full suite green.
- [ ] **Step 5** commit `feat(finance): Option A spendable filter in financial_summary`

---

### Task 7: Option A in `spending_by_category` (exclusion + uncategorized total + flag)

**Files:** Modify `src/home_agent/finance.py`; Test `tests/home_agent/test_spending_option_a.py`

**Interfaces:** Consumes `_spendable_rows` (T6). Produces `spending_by_category` output including an **uncategorized total amount** + the partial flag.

- [ ] **Step 1 — failing tests:** breakdown omits card-payment when covered; includes bills + flag when uncovered; returns a summable uncategorized **amount**; `Σ category totals + uncategorized total == financial_summary expense` for the same covered range.
- [ ] **Step 2** run → FAIL.
- [ ] **Step 3 — implement:** rewrite `_spending_impl` to iterate `_spendable_rows` (same filter as T6), categorize the kept negatives via `_categorize`, accumulate per-category totals + an `uncategorized_agorot` total (not just count), append the flag when partial. Output the uncategorized line with its amount.
- [ ] **Step 4** run → PASS; full suite green.
- [ ] **Step 5** commit `feat(finance): Option A + uncategorized total in spending_by_category`

---

### Task 8: `cash_flow_forecast` bank-only

**Files:** Modify `src/home_agent/finance.py`; Test `tests/home_agent/test_forecast_bank_only.py`

- [ ] **Step 1 — failing test:** data has a recurring bank card-bill + a recurring Max purchase → forecast's recurring set includes the **bank bill** and **excludes** the Max purchase (counts the debit once).
- [ ] **Step 2** run → FAIL.
- [ ] **Step 3 — implement:** in `_forecast_impl`, feed `_detect_recurring` only `source='discount'` rows (filter before detection). Add a one-line comment: cash-flow = bank debits; Max purchases are already inside the card bill.
- [ ] **Step 4** run → PASS; full suite green.
- [ ] **Step 5** commit `fix(finance): cash_flow_forecast is bank-only (no card double-count)`

---

### Task 9: Deploy + live verify

**Files:** none (uses `deploy-box`).

- [ ] **Step 1** `deploy-box` (rsync incl. `collector/scrape_max.js`; `.env` already has `MAX_*`); on the box `cd collector && npm install` is not needed (same deps); restart `home-agent`; verify active/one-instance/log clean.
- [ ] **Step 2** live: run `sync_finances` → confirm Max …1743 itemized txns imported + `source_coverage` recorded.
- [ ] **Step 3** ask Menashe (via `scripts/agent_smoke.py` or Telegram) for this-period category breakdown → confirm real merchants, card-bills excluded (matcher), uncategorized total present, total reconciles, no stale flag (Max fresh).
- [ ] **Step 4** confirm an un-imported card (…6146) bill still counts (bank-level) and forecast unaffected.

---

## Self-Review
- **Spec coverage:** collector+window (T4,T1), matcher (T2), coverage metadata (T3), multi-source sync+wiring+seam (T5), Option A summary (T6), Option A breakdown+uncat total (T7), bank-only forecast (T8), deploy/verify (T9). All spec sections mapped.
- **Placeholders:** Task 4 Step 2 references the spec's test list rather than inline code for the JS-adjacent parts (the JS is live-only) — acceptable; the Python contract test is concrete.
- **Type consistency:** `_is_card_payment(desc, card_numbers:set)`, `covered_cards(...)->set`, `_spendable_rows(store,frm,to)->(rows,flag)`, `make_collector_fetch(config,source)`, `build_finance_tools(...,fetch_fns=dict)` — consistent across T2/T3/T4/T5/T6/T7.
