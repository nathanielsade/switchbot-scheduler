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
  parses its JSON (the **Collector JSON contract** below); **fake in tests = returns a canned dict of exactly
  that contract shape** (no Node, no bank, no network). Because the contract is pinned, offline tests exercise
  the same shape the real collector emits.
- **`collector/scrape_discount.js`** + **`collector/package.json`** + **`collector/package-lock.json`** — the
  only Node code: reads `DISCOUNT_*` from env, calls `israeli-bank-scrapers` (Discount), prints the contract
  JSON to **stdout only**, logs to stderr, exits. Dependencies are **pinned via a committed
  `package-lock.json` and installed with `npm ci`** (exact transitive tree — direct-pin alone allows
  transitive drift). The real `fetch_fn` resolves the script path **relative to the repo/package root** (not
  the process CWD, so it survives systemd) and runs it with **`shell=False`, argv list** (`["node", <abs
  script>]`) — never a shell string.
- **Wiring** (`telegram_app.build_application`): build the store + tools once at startup **iff** Discount is
  configured; otherwise the finance tools simply don't load (bot still runs). Chat-agnostic → no per-turn binding.

## Collector JSON contract

The single canonical shape the collector prints and `fetch_fn` returns — **the fake in tests emits exactly
this**, so offline tests can't pass against a shape the real collector never produces. **Money crosses the
wire as decimal *strings*, never JSON floats** — so no float ever exists on the Python side. Amounts are the
**signed shekel values** (negative = debit/expense, positive = credit/income); the **Python importer** does the
rest of the normalization (→agorot via `Decimal`, →ISO date, →lowercase status), which keeps that logic
offline-testable.

```jsonc
// jsonc (annotated) — the real wire format is strict JSON with these exact keys.
{
  "source": "discount",
  "scraped_at": "2026-07-12T18:30:00+03:00",
  "accounts": [
    {
      "account": "12345",                 // scraper accountNumber, as string
      "balance": "12345.67",              // account balance, decimal STRING, signed shekels
      "transactions": [
        {
          "identifier": "987654",         // bank txn id as string, or null when absent
          "date": "2026-07-01T00:00:00.000Z",   // scraper ISO datetime
          "processedDate": "2026-07-02T00:00:00.000Z", // or null
          "chargedAmount": "-450.00",     // decimal STRING, SIGNED (− expense / + income)
          "chargedCurrency": "ILS",       // → currency (default ILS if missing)
          "description": "שופרסל דיל",
          "status": "completed"           // "completed" | "pending"
        }
      ]
    }
  ]
}
```

**Importer normalization rules (Python, tested):**
- **Money:** parse with `json.loads(..., parse_float=Decimal)` (so any stray numeric field becomes `Decimal`,
  never `float`), then `amount_agorot = int((Decimal(chargedAmount) * 100).quantize(Decimal("1"),
  rounding=ROUND_HALF_UP))` — sign preserved (**− expense / + income**), **no float, no banker's rounding**.
  `balance` → agorot the same way (into `account_snapshots`, below).
- `date`/`processedDate` (ISO datetime) → `txn_date`/`processed_date` as `YYYY-MM-DD` (date-only).
- `status` → lowercased `completed`/`pending`; `chargedCurrency` → `currency` (default `ILS`).
- `account` → `account`; `identifier` (string|null) drives the fingerprint (below).
- Unknown/extra fields are ignored; a missing required field (amount/date/description) drops that row and is
  counted, not fatal.

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

### `account_snapshots` — balances live here, not on transaction rows

Balance is **account-level**, so storing it only on transaction rows means an account with **no new
transactions this sync can never update its balance**. Instead each sync writes one snapshot per account:

| column | type | notes |
|---|---|---|
| `source` | TEXT | `'discount'` |
| `account` | TEXT | account id |
| `scraped_at` | TEXT | ISO timestamp of the sync (from the contract's `scraped_at`) |
| `balance_agorot` | INTEGER | account balance at that sync |

**Current balance = Σ over accounts of the `balance_agorot` from each account's latest `scraped_at`.** Append
each sync (small, effectively a balance history); `financial_summary` reads the latest per account and sums.

### `category_rules` — categories are derived at read time (rules are the truth, not per-row category)

We **do not** stamp a `category` onto each transaction as the source of truth (that would force a backfill
every time a rule changes). Instead we store **rules**, and **derive** each transaction's category at read
time.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | newer = higher id |
| `merchant_pattern` | TEXT | case-insensitive substring match on `description` (normalized) |
| `category` | TEXT | a **canonical slug** (below) |
| `status` | TEXT | `active` \| `removed` — **soft-delete** so a bad auto-rule is a flip, not DB surgery |
| `created_at` | TEXT | ISO |

**Read-time category of a transaction** = `category_override` if set, else the **matching `active` rule** by
precedence (`removed` rules are ignored), else **`NULL` = *uncategorized*** (a distinct state — *not*
`"other"`). **Rule precedence: longest
`merchant_pattern` wins; ties broken by newest (highest `id`).** Deriving is pure SQL/Python — deterministic,
no model, no backfill. Keeping uncategorized as `NULL` preserves the "no rule matched" signal that
`spending_by_category` needs to surface; **`"other"` is just one assignable slug** you reach by an explicit
rule, meaning "categorized, and it's miscellaneous" — the two must not be conflated.

**Canonical category slugs (fixed set):** `groceries, rent, salary, utilities, transport, health,
restaurants, subscriptions, shopping, cash, transfer, other`. `set_category_rule` rejects anything else.
`"other"` is an explicit choice; *uncategorized* is the absence of any rule (`NULL`).

## The tools (eight)

Deterministic math/formatting in Python; language/judgment (classifying a new merchant, phrasing) in the
model. Dates are **explicit ISO `from_date`/`to_date`**, with optional convenience `period` shortcuts
(`this_month` | `last_month` | `last_30_days`) resolved via `now_fn`. When both are given, explicit dates win.

- **`sync_finances()`** — run the collector (via `fetch_fn`) and import. **Hardened:** the real `fetch_fn`
  runs the collector via `subprocess` with **`shell=False` + argv list** and a **repo-root-resolved absolute
  script path** (CWD-independent); a **cross-process single-flight lock** — an OS file lock (`fcntl.flock` on a
  lockfile, non-blocking) so a Telegram-triggered sync and the **nightly cron** sync can't run at once (an
  in-process `threading.Lock` alone wouldn't cover the separate cron process); if the lock is held → friendly
  "sync already running". A **timeout** on the collector (killed + friendly failure); a **JSON-only stdout contract** (anything unparseable → friendly
  failure, stderr **sanitized**, never surfaced raw); creds passed via the child **env** only. Returns to
  Telegram **only counts + date range** (e.g. *"נמשכו נתונים: 12 חדשות, 3 עודכנו, טווח 2026-06-01…2026-07-12"*)
  — never raw transactions or errors. Upserts by `(source, account, fingerprint)`.

- **`financial_summary(from_date?, to_date?, period?)`** — deterministic sums over the range: **income**
  (Σ amount>0), **expenses** (Σ amount<0), **net**, and **current balance**. Because Discount can return
  **multiple accounts**, "current balance" = **Σ over accounts of each account's latest `account_snapshots`
  row** (latest `scraped_at`) — *not* a single global snapshot, and independent of whether an account had new
  transactions this sync. Returns formatted shekels (per-account breakdown available if asked).

- **`find_transactions(from_date?, to_date?, min_abs_agorot?, max_abs_agorot?, direction?, query?)`** —
  filtered list (capped, e.g. 50). Amount filters are **absolute** (`min_abs_agorot`/`max_abs_agorot` match
  `abs(amount_agorot)`) so *"that ₪450 charge"* → `min_abs_agorot=45000, max_abs_agorot=45000` regardless of
  sign; optional `direction` ∈ `expense|income` filters by sign. Each row = date, description, amount (₪),
  status, derived category. For "what was that charge?".

- **`spending_by_category(from_date?, to_date?, period?)`** — expenses grouped by **read-time-derived**
  category; returns per-category totals **plus** the count of **uncategorized** transactions and a few example
  uncategorized merchants — so the model can offer to classify them.

- **`set_category_rule(merchant_pattern, category)`** — validates `category` ∈ the canonical slugs, inserts an
  `active` rule, and returns **how many existing transactions it now matches + a few examples** (so the effect
  is visible). Precedence is longest-pattern / newest-rule (above).

- **`list_category_rules()`** — list the `active` rules (id, pattern, category) — the correction path, so the
  model/user can see what's been auto-persisted.

- **`delete_category_rule(rule_id)`** — **soft-delete** (flip `status → removed`) so a bad auto-rule is one
  call to undo, not DB surgery. Categories re-derive immediately (removed rules ignored); no row backfill.

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

- **Read-only by construction:** none of the tools can move money; the collector only issues GETs. (The
  spec's own review gate: confirm no transaction-executing capability exists.)
- **Collector isolation (box ops, documented here):** dependencies pinned via a **committed
  `collector/package-lock.json`, installed with `npm ci`** (exact transitive tree; upgrades are manual +
  reviewed — direct-`package.json` pinning alone allows transitive drift). Run the collector under an
  **egress allow-list to `start.telebank.co.il` only** — so even a poisoned transitive dependency has no route
  to exfiltrate.
- **Credentials:** `DISCOUNT_ID` / `DISCOUNT_PASSWORD` / `DISCOUNT_NUM` in git-ignored `.env` on the box;
  passed to the collector via the child **env** only; **never logged, never sent to OpenAI, never committed.**
  Only the collector process ever sees them.

### Privacy boundary (explicit — resolves the "who sees the data" question)

Two different things, not to be conflated:
- **The collector's raw output + stderr never leave the box.** `sync_finances` returns to Telegram **only
  counts + date ranges** — never raw rows, never raw errors. That's what "sanitized" means here.
- **The query tools *do* return finance data, and that data *does* go to OpenAI.** Per the agent loop
  (`agent.py` feeds every tool result back to the model as a tool message), `find_transactions` rows and the
  summary/category/forecast numbers are sent to OpenAI to phrase the Hebrew answer. **Explicit decision:
  finance details may go to OpenAI in *minimized* form** — the user has accepted query-time egress (it's not
  among their concerns). Minimization is still required: return only the fields the answer needs, **cap
  `find_transactions` at ≤50 rows**, and prefer aggregates over raw rows where a question allows.
- **If that ever becomes unacceptable,** the alternative is a **local-only answer path** (deterministic tools
  compose the reply text themselves, sending only a tiny summary to the model) — noted as the fallback, not
  built in v1. Bank **credentials** never enter this path regardless.

## Config (new `.env` keys, read in `config.py`)

- `DISCOUNT_ID`, `DISCOUNT_PASSWORD`, `DISCOUNT_NUM` — finance tools load **only when all three are set**. If
  **some but not all** are set (partial config), finance is **disabled and a warning is logged** (fail safe,
  don't half-load) — matching the test contract. All unset → silently off. Bot always still runs.
- `FINANCE_NODE_BIN` — node binary (default `node`, resolved on PATH). `FINANCE_COLLECTOR_SCRIPT` — path to
  the collector JS, **default resolved relative to the repo/package root** (`<repo>/collector/scrape_discount.js`,
  via `__file__`), **not** the process CWD. The collector is invoked as `subprocess.run([node_bin, script],
  shell=False, env=…, timeout=…)` — an argv list, never a shell command string.
- Finance data lives in the shared `config.db_path` (separate tables).

## Dependencies

- Python: `hashlib.sha1` + `decimal.Decimal` (stdlib); no new Python deps. `israeli-bank-scrapers` is a **Node** dep of the
  **collector** (its own `collector/package.json`, pinned) — **not** a Python/`pyproject` dep, and **not**
  needed by the test suite (tests inject a fake `fetch_fn`).

## Testing (offline — the hard rule)

Inject a **fake `fetch_fn`** returning a canned dict **in the Collector JSON contract shape** (a
multi-account fixture: salary income, rent, a couple of merchants, a pending charge, one txn with a `null`
identifier) + a real `finance_store` on a tmp SQLite, under a **frozen `now_fn`**:
- **Import normalization:** money parsed via `Decimal` from the **string** field (no float) → signed
  `amount_agorot` **int** (`"-450.00" → -45000`), with a fractional-agora input asserting `ROUND_HALF_UP` (not
  banker's rounding); ISO datetime → `YYYY-MM-DD`; `status` lowercased; missing `chargedCurrency` → `ILS`; a
  row missing a required field is dropped + counted, not fatal.
- **Dedup:** same `identifier` twice → one row; **hash fingerprint fallback** when `identifier` is null;
  **pending→settled** mutates the row in place (status/amount), not a duplicate.
- **`financial_summary`:** income/expense/net over explicit `from_date`/`to_date`; `period` shortcut under the
  frozen clock; **balance = Σ of the latest `account_snapshots` row per account** (2-account fixture, not a
  single global latest); **an account with no new transactions this sync still updates its balance** (proves
  balance comes from `account_snapshots`, not from txn rows).
- **`find_transactions`:** **absolute** amount filters — a `−45000` expense is matched by
  `min_abs_agorot=45000`; `direction=expense|income` filters by sign; date/query filters; **≤50 cap** enforced.
- **`spending_by_category`:** categories **derived at read time** from rules; **uncategorized (`NULL`, no rule)
  surfaced distinctly** from a txn matched by an explicit `"other"` rule; **changing a rule changes the summary
  with no row backfill.**
- **`set_category_rule`:** rejects a non-canonical slug; returns affected count + examples; **precedence**
  (longest pattern wins; tie → newest) proven with overlapping rules.
- **`list_category_rules` / `delete_category_rule`:** delete **soft-removes** (status→`removed`); the removed
  rule stops affecting derivation immediately (no backfill); `list_category_rules` shows only `active`.
- **`cash_flow_forecast`:** recurring detected only when description+sign+day-of-month+amount criteria hold;
  overdraft flagged when projection < 0; confidence labels present; a one-off is **not** treated as recurring.
- **`sync_finances`:** single-flight (a second concurrent call is rejected/queued, not run twice);
  **malformed collector stdout → friendly failure**, no raw stderr leak; returns only counts + range.
- **Config:** `load_finance_config` builds tools **only when all three Discount keys are set**; **partial
  config → disabled + warning logged**; none set → silently off.

**Manual (real box, outside CI):** with `DISCOUNT_*` set + the pinned collector installed under the egress
sandbox — a real `sync_finances` pulls transactions; summary/search/category/forecast answer over real data.

## Build order (this spec → one plan, ~6 tasks)

1. Config keys + `load_finance_config` (graceful) + startup wiring + the fake-`fetch_fn` test seam.
2. `finance_store`: `transactions` upsert (fingerprint incl. hash fallback) + `account_snapshots` +
   `category_rules` (with `status`).
3. `collector/scrape_discount.js` + `package-lock.json` (`npm ci`, JSON-only contract) + the Python **importer**
   (contract→**`Decimal`**-agorot / ISO normalization, fingerprint, per-account snapshot) + `sync_finances`
   (subprocess `shell=False` resolved path, timeout, **cross-process file lock**, sanitized).
4. `financial_summary` (balance from `account_snapshots`) + `find_transactions` (absolute filters + optional shortcuts).
5. `spending_by_category` (read-time derivation) + `set_category_rule` + `list_category_rules` /
   `delete_category_rule` (slug validation, precedence, soft-delete).
6. `cash_flow_forecast` (recurring detection + confidence + overdraft) + categorization prompt policy (digit-free).

## What still needs the box

Only the **live run**: install Node + Chromium (`npm ci` in `collector/`), set `DISCOUNT_*`, run the collector
under the egress sandbox (nightly via cron later — the cron sync and the Telegram `sync_finances` share the
**cross-process file lock**, so they can't collide). The code builds, tests, and merges now — **dormant until
configured**.
