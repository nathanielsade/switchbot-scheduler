# finance-mcp (Discount → local SQLite) — design

- **Date:** 2026-07-12.
- **Roadmap:** Epic E, **scope revised** from Firefly III (Docker) to a lightweight local SQLite store.
- **Status:** design under review, pre-implementation.
- **Predecessors / patterns:** same in-process `Tool` pattern, deterministic-store + model-judgment split,
  injectable seam (fake in tests), graceful-if-unconfigured wiring — as home/shopping/calendar/roborock.

## Goal

Let the family agent answer money questions in Hebrew from Telegram — *"איך אנחנו החודש?"*, *"מה הייתה
ההוצאה של ₪450?"*, *"כמה על סופר החודש מול הממוצע?"*, *"אנחנו הולכים למינוס?"* — over **read-only** Discount
bank-account data (balances + transactions) collected on the home box. **Discount-only** to start (the single
audited scraper adapter); more banks / itemized credit-card issuers are a later add.

## Scope

**In scope:** a Node collector (pinned `israeli-bank-scrapers`, Discount only) → JSON → Python importer → a
local SQLite `finance_store`; six in-process tools (below); read-time category derivation; a simple recurring
detection + end-of-month forecast. Fully offline-testable with a **fake collector**.

**Out of scope / deferred:** Firefly III + `firefly-iii-mcp` (dropped); other banks; **itemized credit-card
purchases** (the bank account shows only the lump card charge — the card issuer is a separate scraper);
weekly-summary cron (needs the box + `schedule_task`); a per-transaction category **override UI** (the
column exists, unused in v1); an append-only **audit** table (see "Durability").

## Architecture & modules (mirror roborock/shopping)

- **`finance_store.py`** — SQLite (in `config.db_path`), connection-per-op. Tables `transactions` +
  `category_rules` (schema below). Owns all writes (single writer).
- **`finance.py`** — `build_finance_tools(store, *, now_fn=None, fetch_fn=None) -> list[Tool]`;
  `load_finance_config(config)`. The **injectable seam is `fetch_fn`** — real = runs the Node collector and
  parses its JSON; **fake in tests = returns a canned dict** (no Node, no bank, no network).
- **`collector/scrape_discount.js`** + **`collector/package.json`** — the only Node code: pinned
  `israeli-bank-scrapers`, reads `DISCOUNT_*` from env, prints transactions as JSON to **stdout only**, logs
  to stderr, exits. Nothing else.
- **Wiring** (`telegram_app.build_application`): build the store + tools once at startup **iff** Discount is
  configured; otherwise the finance tools simply don't load (bot still runs). Chat-agnostic → no per-turn binding.

## Data model

**Money is stored as integer agorot** (never floats): `amount_agorot INTEGER`, `currency TEXT DEFAULT 'ILS'`.
Signed: **+ income / − expense**. Tools format agorot→shekels for display (deterministic; the model never does
the division).

### `transactions` — *durable current transaction state* (not strictly append-only)

An **upsert** target: a pending charge that later settles **updates its row in place** (status/amount/date).
So this table holds the *current best state* of each transaction, not an immutable log. (True auditability —
an `import_batches` / `transaction_import_events` append-only table — is a **deferred** add, noted here so we
don't pretend the upsert is append-only.)

| column | type | notes |
|---|---|---|
| `source` | TEXT | `'discount'` (room for more banks later) |
| `account` | TEXT | account id from the scraper |
| `identifier` | TEXT NULL | the bank's transaction id, **when present** |
| `fingerprint` | TEXT | dedup key (below) |
| `txn_date` | TEXT | ISO `YYYY-MM-DD` |
| `processed_date` | TEXT NULL | ISO |
| `amount_agorot` | INTEGER | signed, agorot |
| `currency` | TEXT | default `'ILS'` |
| `description` | TEXT | merchant/description |
| `status` | TEXT | `completed` \| `pending` |
| `category_override` | TEXT NULL | optional per-txn override (forward-compat; unused in v1) |
| `balance_snapshot_agorot` | INTEGER NULL | account balance at scrape time |
| `raw_json` | TEXT | the raw scraper row (for debugging / future fields) |
| `imported_at` | TEXT | ISO timestamp (via `now_fn`) |

**UNIQUE `(source, account, fingerprint)`** → the upsert key.

**Fallback dedup fingerprint.** Bank ids are ideal but scrapers sometimes have unstable/missing ids around
the pending→settled boundary, so:
- `identifier` present & non-empty → `fingerprint = "id:" + identifier`.
- else → `fingerprint = "h:" + sha1(normalize(txn_date) | amount_agorot | normalize(description))`.
  **Status is deliberately excluded** from the hash so a pending row and its settled twin collapse to one.
  *Honest caveat:* if a bank mutates date/amount when settling **and** there's no id, a rare duplicate can
  slip through — the id path (the common case) is exact. Documented, not hidden.

### `category_rules` — categories are derived at read time (rules are the truth, not per-row category)

We **do not** stamp a `category` onto each transaction as the source of truth (that would force a backfill
every time a rule changes). Instead we store **rules**, and **derive** each transaction's category at read
time.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | newer = higher id |
| `merchant_pattern` | TEXT | case-insensitive substring match on `description` (normalized) |
| `category` | TEXT | a **canonical slug** (below) |
| `created_at` | TEXT | ISO |

**Read-time category of a transaction** = `category_override` if set, else the **matching rule** by
precedence, else `"other"`. **Rule precedence: longest `merchant_pattern` wins; ties broken by newest
(highest `id`).** Deriving is pure SQL/Python — deterministic, no model, no backfill.

**Canonical category slugs (fixed set):** `groceries, rent, salary, utilities, transport, health,
restaurants, subscriptions, shopping, cash, transfer, other`. `set_category_rule` rejects anything else.

## The six tools

Deterministic math/formatting in Python; language/judgment (classifying a new merchant, phrasing) in the
model. Dates are **explicit ISO `from_date`/`to_date`**, with optional convenience `period` shortcuts
(`this_month` | `last_month` | `last_30_days`) resolved via `now_fn`. When both are given, explicit dates win.

- **`sync_finances()`** — run the collector (via `fetch_fn`) and import. **Hardened:** a **single-flight lock**
  (no concurrent syncs), a **timeout** on the collector, a **JSON-only stdout contract** (anything
  unparseable → friendly failure, stderr **sanitized**, never surfaced raw), and it returns to Telegram
  **only counts + date range** (e.g. *"נמשכו נתונים: 12 חדשות, 3 עודכנו, טווח 2026-06-01…2026-07-12"*) — never
  raw transactions or errors. Upserts by `(source, account, fingerprint)`.

- **`financial_summary(from_date?, to_date?, period?)`** — deterministic sums over the range: **income**
  (Σ amount>0), **expenses** (Σ amount<0), **net**, and **current balance** (latest
  `balance_snapshot_agorot`). Returns formatted shekels.

- **`find_transactions(from_date?, to_date?, min_agorot?, max_agorot?, query?)`** — filtered list (capped,
  e.g. 50), each row = date, description, amount (₪), status, derived category. For "what was that charge?".

- **`spending_by_category(from_date?, to_date?, period?)`** — expenses grouped by **read-time-derived**
  category; returns per-category totals **plus** the count of **uncategorized** transactions and a few example
  uncategorized merchants — so the model can offer to classify them.

- **`set_category_rule(merchant_pattern, category)`** — validates `category` ∈ the canonical slugs, inserts a
  rule, and returns **how many existing transactions it now matches + a few examples** (so the effect is
  visible). Precedence is longest-pattern / newest-rule (above).

- **`cash_flow_forecast(...)`** — detect **recurring** items and project month-end. A recurring item requires:
  **same normalized description, same sign, similar day-of-month (±few days), and exact-or-near amount
  (±small %)**, across ≥2–3 months. Projects **end-of-month balance** = current balance + expected remaining
  recurring income − expected remaining recurring expenses, and **flags overdraft** if it goes negative.
  Returns the **projected balance + overdraft flag + a "detected recurring items" list with confidence
  labels** (based on #occurrences + amount stability) so the model can **explain its assumptions** rather than
  assert a number.

## Categorization policy (system prompt — digit-free)

The learn-and-persist pattern (like shopping `known_items`): `spending_by_category` surfaces uncategorized
merchants; the **model classifies** them to a canonical slug and persists via `set_category_rule`. Two
guardrails in the prompt:
1. **Auto-persist the obvious** (e.g. שופרסל→groceries, משכורת→salary) without asking.
2. **Ask when ambiguous** (a merchant that could be several things) rather than guessing.

## Security

- **Read-only by construction:** none of the six tools can move money; the collector only issues GETs. (The
  spec's own review gate: confirm no transaction-executing capability exists.)
- **Collector isolation (box ops, documented here):** **pin the exact `israeli-bank-scrapers` version**
  (`collector/package.json`, manual reviewed upgrades) and run it under an **egress allow-list to
  `start.telebank.co.il` only** — so even a poisoned transitive dependency has no route to exfiltrate.
- **Credentials:** `DISCOUNT_ID` / `DISCOUNT_PASSWORD` / `DISCOUNT_NUM` in git-ignored `.env` on the box;
  passed to the collector via env; never logged, never sent to OpenAI, never committed.
- **Telegram output is sanitized** — only counts/ranges/aggregates leave the box; collector stderr is never
  surfaced raw.

## Config (new `.env` keys, read in `config.py`)

- `DISCOUNT_ID`, `DISCOUNT_PASSWORD`, `DISCOUNT_NUM` — **all three unset → finance tools don't load** (bot
  still runs), same graceful pattern as calendar/roborock.
- `FINANCE_COLLECTOR_CMD` — command to run the collector (default `node collector/scrape_discount.js`).
- Finance data lives in the shared `config.db_path` (separate tables).

## Dependencies

- Python: `sha1` from stdlib; no new Python deps. `israeli-bank-scrapers` is a **Node** dep of the
  **collector** (its own `collector/package.json`, pinned) — **not** a Python/`pyproject` dep, and **not**
  needed by the test suite (tests inject a fake `fetch_fn`).

## Testing (offline — the hard rule)

Inject a **fake `fetch_fn`** returning a canned Discount dict (income salary, rent, a couple of merchants, a
pending charge) + a real `finance_store` on a tmp SQLite, under a **frozen `now_fn`**:
- **Import/dedup:** same `identifier` twice → one row; **fingerprint fallback** when id missing; a
  **pending→settled** update mutates the row in place (status/amount), not a duplicate; `amount_agorot` stored
  as **int**.
- **`financial_summary`:** income/expense/net over explicit `from_date`/`to_date`; `period` shortcut under the
  frozen clock; balance = latest snapshot.
- **`find_transactions`:** date/amount/query filters; cap respected.
- **`spending_by_category`:** categories **derived at read time** from rules; uncategorized surfaced;
  **changing a rule changes the summary with no row backfill.**
- **`set_category_rule`:** rejects a non-canonical slug; returns affected count + examples; **precedence**
  (longest pattern wins; tie → newest) proven with overlapping rules.
- **`cash_flow_forecast`:** recurring detected only when description+sign+day-of-month+amount criteria hold;
  overdraft flagged when projection < 0; confidence labels present; a one-off is **not** treated as recurring.
- **`sync_finances`:** single-flight (a second concurrent call is rejected/queued, not run twice);
  **malformed collector stdout → friendly failure**, no raw stderr leak; returns only counts + range.
- **Config:** `load_finance_config` → tools absent when any Discount key is unset.

**Manual (real box, outside CI):** with `DISCOUNT_*` set + the pinned collector installed under the egress
sandbox — a real `sync_finances` pulls transactions; summary/search/category/forecast answer over real data.

## Build order (this spec → one plan, ~6 tasks)

1. Config keys + `load_finance_config` (graceful) + startup wiring + the fake-`fetch_fn` test seam.
2. `finance_store`: `transactions` upsert (fingerprint incl. hash fallback; agorot) + `category_rules`.
3. `collector/scrape_discount.js` (pinned, JSON-only) + `sync_finances` (subprocess, timeout, single-flight,
   sanitized) + the importer.
4. `financial_summary` + `find_transactions` (ISO dates + optional period shortcuts).
5. `spending_by_category` (read-time derivation) + `set_category_rule` (slug validation, precedence, affected count).
6. `cash_flow_forecast` (recurring detection + confidence + overdraft) + categorization prompt policy (digit-free).

## What still needs the box

Only the **live run**: install Node + Chromium, set `DISCOUNT_*`, run the collector under the egress sandbox
(nightly via cron later). The code builds, tests, and merges now — **dormant until configured**.
