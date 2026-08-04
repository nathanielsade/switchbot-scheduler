# Max card itemized finance (real spending categories)

- **Date:** 2026-08-04
- **Status:** Approved (design)
- **Builds on:** finance guardrail (`fix/finance-guardrail`, live on box)

## Problem

The Discount **bank** feed shows only lump credit-card bills (`חיוב לכרטיס ויזה … ₪7,009`), not what was bought. So Menashe cannot give a real category breakdown (groceries vs restaurants vs fuel) — which is exactly why he fabricated one before. The itemized, per-merchant purchases live only in the **card issuer's** system.

**Proven (spike, 2026-08-04):** logging into **Max** (`companyId=max`) with the user's `MAX_USERNAME`/`MAX_PASSWORD` returns itemized per-merchant transactions (card …1743, 102 txns; e.g. `תחנת דלק … ₪211.91`, `HERTZ … ₪3,605.34`). No OTP wall.

## Goals

1. Sync the **Max card's itemized purchases** into the finance store alongside the bank feed.
2. Make **spending totals and category breakdowns correct and reconciling** — no double-counting the bank's card-bill lines against the card's itemized purchases (**Option A**).
3. Real, persistent **categorization** on the now-itemized merchant data.

## Non-goals (YAGNI)

- Card **…6146** (didn't appear under this Max login) — design is multi-card, but we ship for …1743; 6146 added later once its issuer/login is known.
- No new UI; answers flow through the existing finance tools.
- Savings deposits / cash withdrawals stay classified as spend for v1 (only the bank↔card payment lines are excluded).
- We do **not** exclude the user-facing `transfer` category from spend — only a **narrow internal `card_payment`** tag (below). Real/user transfers are unaffected.

## Date basis (explicit)

Spend is computed on a **purchase-date basis**: Max transactions are dated by **purchase date**; the bank's card-**bill** lines are dated by **payment date** (they pay a *previous* period's purchases). Because Option A **excludes the bank card-bill lines entirely** (they're `card_payment`, non-spending), their payment date never enters a spend total — so there is no billing-cycle skew in the total. "Spend this month" therefore means "purchases dated this month" (the intuitive meaning), plus this-month bank non-card expenses. This is a deliberate product choice and is covered by a billing-cycle-skew test.

## Architecture

Multi-source finance store (already keyed by `source, account, fingerprint`). `source='discount'` (bank) and `source='max'` (card) coexist. **Option A** removes the double-count by tagging the bank's card-payment lines — **per imported card** — with an internal `card_payment` tag and excluding it from **spend** (for periods Max covers). Two distinct lenses, deliberately different: **spend analytics** (`financial_summary`, `spending_by_category`) use Max purchases (purchase-date) and drop the card bills; **cash-flow** (`cash_flow_forecast`) is bank-balance timing and uses the **bank feed only** (the card *bill* is the real debit) — never the Max purchases.

## Components

### 1. `collector/scrape_max.js` (new)
Mirror of `scrape_discount.js`, `companyId: CompanyTypes.max`, creds from `process.env.MAX_USERNAME`/`MAX_PASSWORD`. Emits the **same JSON contract** the bank collector does (so `normalize_contract` handles it unchanged): `source:'max'`, one entry per card `account`, `transactions[]` with `identifier,date,processedDate,chargedAmount,chargedCurrency,description,status`. Loops `result.accounts` (multi-card ready). Prints JSON to stdout only; errors to stderr + non-zero exit.

### 2. Config (`config.py`)
Add `max_username`, `max_password` (env `MAX_USERNAME`/`MAX_PASSWORD`), and `max_collector_script` (default `collector/scrape_max.js`). Add `max_configured(config) -> bool` (both creds set), mirroring `finance_configured`.

### 3. `sync_finances` syncs both sources (`finance.py`) — per-source fetchers
`make_collector_fetch(config, source)` returns a fetcher **for one source** (`'discount'` → `scrape_discount.js` + `DISCOUNT_*`; `'max'` → `scrape_max.js` + `MAX_*`). `_sync_impl` builds a list of `(source, fetcher)` (bank always; Max only when `max_configured`) and runs each in its **own try/except**, upserting into the store; a failure records a per-source error and **does not abort the others**. Returns per-source counts + any per-source error. Reuse the existing file-lock (once, around the whole sync). This per-source shape is what makes "Max raises, Discount commits" directly testable (inject a fake fetcher map).

### 4. Option A — no double-counting (the core change)
- **Narrow internal tag `card_payment`** (NOT in the user-facing `CATEGORIES` enum, so `set_category_rule` can't assign it): `NON_SPENDING_CATEGORIES = {"card_payment"}` in `finance.py`. The user-facing `transfer` category is untouched.
- **Seed `card_payment` rules PER IMPORTED CARD (P1):** on sync, for each `account` the Max fetch returned (e.g. `1743`), seed (idempotent, via `store.add_rule`) rules whose pattern includes that card's number — `לכרטיס ויזה 1743` → `card_payment` (and the `זיכוי…` credit variant). **Only bills for cards we actually import get excluded.** A bill for an un-imported card (…6146) matches **no** rule → stays **counted** (as normal spend), so its money is never silently hidden.
- **Coverage gate (P1a — never under-report):** `_max_covers(store, to)` = the `max` source's latest `txn_date` ≥ `to`. The `card_payment` exclusion applies **only when Max covers the period**. If Max is stale/absent for the range, **do not exclude** the card-bill lines (bank-level totals) and **flag** the output: "(פירוט הכרטיס אינו זמין לתקופה זו — מציג סכומים ברמת הבנק)".
- **`financial_summary` category-aware:** iterate `store.transactions_between(frm,to)`, categorize via `_categorize`, and — **only when `_max_covers`** — sum excluding `NON_SPENDING_CATEGORIES` (income = Σ non-excluded positives, expense = Σ non-excluded negatives). Not covered → include everything (bank-level) + flag. Balance unchanged.
- **`spending_by_category` — same coverage gate (P1 fix):** when `_max_covers`, skip `card_payment` (don't list it). When **not** covered, do **not** hide the card bills — surface them (as their own `card_payment` line or under uncategorized) + the flag, so the breakdown never under-reports either. Also **return an uncategorized TOTAL amount** (P2), not just the count/examples, so summaries reconcile and Menashe can report it.
- **`cash_flow_forecast` — bank-only, NOT the spendable set (P2a, corrected):** cash-flow is bank-balance timing, where the real future debit is the **card bill** (payment date), not the purchases. So `_forecast_impl` runs `_detect_recurring` on **`source='discount'` transactions only** — it **keeps** the recurring card bills and **excludes** the Max (`source='max'`) purchases entirely, so it never double-counts and debits are timed correctly. (Opposite of the spend lens — documented in Architecture.)
- Reconciliation (when Max covers the period): `financial_summary` expense == Σ `spending_by_category` category totals + uncategorized total, on a purchase-date basis; imported-card bills count zero.

### 5. Categorization (now real)
No code change to `set_category_rule`/`_categorize` — they finally have merchant names (from Max) to match. The existing prompt already tells Menashe to auto-rule obvious merchants and ask on ambiguous ones. The guardrail (shipped) keeps him honest while rules are sparse.

## Config / creds
`MAX_USERNAME`/`MAX_PASSWORD` already in the box `.env` (from the spike). `Config` reads them. `.env` stays `chmod 600`, git-ignored, never logged.

## Error handling
- Max login may OTP-block in future even though the spike didn't — `_sync_impl` reports a clear per-source error and still commits the bank sync.
- Missing Max creds → Max sync skipped silently (bank-only), like the existing gate.
- Collector never logs creds.

## Testing (offline, no network/bank)
- **Collector contract:** unit-test `normalize_contract` on a Max-shaped payload (`source='max'`, multi-card) → correct rows. (The `.js` itself is exercised only in the live verify.)
- **Per-source independent failure (P2b):** inject a fetcher map `{'discount': ok, 'max': raises}`; assert Discount rows are committed and a per-source Max error is reported (and vice-versa).
- **Option A reconcile (Max covers):** bank `חיוב לכרטיס ויזה 1743 -₪4,717` (→`card_payment`) + Max …1743 purchases summing to ₪4,717 + a bank non-card expense, in-range, Max covers; assert `financial_summary` expense **excludes** `card_payment` and `spending_by_category` omits it and returns an **uncategorized total**; assert expense == Σ category totals + uncategorized total.
- **Per-card, un-imported card stays counted (P1):** add a bank `חיוב לכרטיס ויזה 6146` bill with **no** Max …6146 import → it matches no `card_payment` rule → it is **still counted** as spend (never hidden). …1743's bill is excluded.
- **Coverage gate (P1a) — both tools:** Max **not** synced through the range → `financial_summary` **and** `spending_by_category` **include** the card-bills (bank-level) with the partial/stale flag; never under-report.
- **Narrow tag (P1b):** a user `transfer`-category rule + txn is **still counted** as spend (only `card_payment` is excluded).
- **Forecast is bank-only (P2a):** recurring bank card-bill + recurring Max purchase → `cash_flow_forecast` uses the **bank bill** and **ignores** the Max purchase (counts the debit once, timed on the bill's payment date).
- **Billing-cycle skew (P2c):** a bank card-bill dated month N paying Max purchases dated month N-1 → for **spend**, the bill is excluded regardless of date and the purchases count in **N-1**; for **forecast**, the bill is timed in **N**.
- **Uncategorized total (P2):** `spending_by_category` returns a summable uncategorized **amount** (not only a count).
- **Seed-rules idempotency:** running the seed twice doesn't duplicate the `card_payment` rules.
- Full suite stays green.

## Deploy + live verify (`deploy-box`)
1. rsync (incl. `collector/scrape_max.js`); `.env` already has Max creds on the box.
2. Restart `home-agent`.
3. Live: run `sync_finances` → confirm Max …1743 itemized txns land; ask Menashe for a category breakdown → confirm it uses real merchants, the bank card-bills are tagged `card_payment` (excluded), the total reconciles, and (Max being fresh) no partial/stale flag appears.
