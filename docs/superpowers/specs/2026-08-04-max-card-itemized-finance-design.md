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

Multi-source finance store (already keyed by `source, account, fingerprint`). `source='discount'` (bank) and `source='max'` (card) coexist. **Option A** removes the double-count by tagging the bank's card-payment lines as `transfer` and excluding `transfer` from spend/income.

## Components

### 1. `collector/scrape_max.js` (new)
Mirror of `scrape_discount.js`, `companyId: CompanyTypes.max`, creds from `process.env.MAX_USERNAME`/`MAX_PASSWORD`. Emits the **same JSON contract** the bank collector does (so `normalize_contract` handles it unchanged): `source:'max'`, one entry per card `account`, `transactions[]` with `identifier,date,processedDate,chargedAmount,chargedCurrency,description,status`. Loops `result.accounts` (multi-card ready). Prints JSON to stdout only; errors to stderr + non-zero exit.

### 2. Config (`config.py`)
Add `max_username`, `max_password` (env `MAX_USERNAME`/`MAX_PASSWORD`), and `max_collector_script` (default `collector/scrape_max.js`). Add `max_configured(config) -> bool` (both creds set), mirroring `finance_configured`.

### 3. `sync_finances` syncs both sources (`finance.py`) — per-source fetchers
`make_collector_fetch(config, source)` returns a fetcher **for one source** (`'discount'` → `scrape_discount.js` + `DISCOUNT_*`; `'max'` → `scrape_max.js` + `MAX_*`). `_sync_impl` builds a list of `(source, fetcher)` (bank always; Max only when `max_configured`) and runs each in its **own try/except**, upserting into the store; a failure records a per-source error and **does not abort the others**. Returns per-source counts + any per-source error. Reuse the existing file-lock (once, around the whole sync). This per-source shape is what makes "Max raises, Discount commits" directly testable (inject a fake fetcher map).

### 4. Option A — no double-counting (the core change)
- **Narrow internal tag `card_payment`** (NOT added to the user-facing `CATEGORIES` enum, so `set_category_rule` can't assign it): `NON_SPENDING_CATEGORIES = {"card_payment"}` in `finance.py`. Only the bank↔card payment lines get it; the user-facing `transfer` category is untouched.
- **Seed `card_payment` rules** (idempotent, on sync when `max_configured`): via `store.add_rule` directly (bypassing the enum-validated tool), ensure rules exist mapping `חיוב לכרטיס ויזה` → `card_payment` and `זיכוי לכרטיס ויזה` → `card_payment`. Add only if not already present.
- **Coverage gate (P1a — never under-report):** define `_max_covers(store, to)` = the `max` source's latest `txn_date` ≥ `to` (Max is synced through the period). The `card_payment` exclusion is applied **only when Max covers the period**. If Max is missing/stale for the range, **do not exclude** the card-bill lines (fall back to counting them, i.e. bank-level totals) and have the tool output **flag it**: "(פירוט הכרטיס אינו זמין לתקופה זו — מציג סכומים ברמת הבנק)". So a stale/absent Max can never silently hide the bills.
- **`financial_summary` becomes category-aware:** instead of `store.sum_amounts` (sign-only), iterate `store.transactions_between(frm,to)`, categorize each via `_categorize(desc, rules)`, and — **when `_max_covers`** — sum **excluding** `NON_SPENDING_CATEGORIES` (income = Σ positives not-excluded, expense = Σ negatives not-excluded). When not covered, include everything (bank-level) + the flag. Balance unchanged.
- **`spending_by_category` skips `NON_SPENDING_CATEGORIES`** (never lists `card_payment`); same coverage flag when Max is stale.
- **`cash_flow_forecast` (P2a):** its recurring-item detection must run on the **same spendable set** — exclude `card_payment` transactions before `_detect_recurring`, so it never counts the recurring bank card-bill *and* the recurring Max purchases.
- Result (when Max covers the period): `financial_summary` expense == Σ `spending_by_category` totals + uncategorized, on a purchase-date basis; the bank↔card payments count zero.

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
- **Option A reconcile (Max covers):** seed bank `חיוב לכרטיס ויזה -₪4,717` (→`card_payment`) + Max purchases summing to ₪4,717 + a bank non-card expense, all in-range, with Max coverage; assert `financial_summary` expense **excludes** `card_payment` (counts purchases + non-card once) and `spending_by_category` omits `card_payment` and reconciles.
- **Coverage gate (P1a):** same data but Max **not** synced through the range (`_max_covers` false) → `financial_summary` **includes** the card-bill (bank-level) and the output carries the partial/stale flag; assert we never under-report.
- **Narrow tag (P1b):** a user `transfer`-category rule + txn is **still counted** as spend (only `card_payment` is excluded).
- **Forecast (P2a):** recurring bank card-bill + recurring Max purchase in the data → `cash_flow_forecast` counts them **once** (card-bill excluded), not both.
- **Billing-cycle skew (P2c):** a bank card-bill dated month N that pays Max purchases dated month N-1 → the bill is excluded regardless of its date, and the purchases count in **N-1** (purchase-date basis).
- **Seed-rules idempotency:** running the seed twice doesn't duplicate the `card_payment` rules.
- Full suite stays green.

## Deploy + live verify (`deploy-box`)
1. rsync (incl. `collector/scrape_max.js`); `.env` already has Max creds on the box.
2. Restart `home-agent`.
3. Live: run `sync_finances` → confirm Max …1743 itemized txns land; ask Menashe for a category breakdown → confirm it uses real merchants, the bank card-bills are tagged `card_payment` (excluded), the total reconciles, and (Max being fresh) no partial/stale flag appears.
