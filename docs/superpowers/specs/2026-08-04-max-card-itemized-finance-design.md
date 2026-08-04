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

## Architecture

Multi-source finance store (already keyed by `source, account, fingerprint`). `source='discount'` (bank) and `source='max'` (card) coexist. **Option A** removes the double-count by tagging the bank's card-payment lines as `transfer` and excluding `transfer` from spend/income.

## Components

### 1. `collector/scrape_max.js` (new)
Mirror of `scrape_discount.js`, `companyId: CompanyTypes.max`, creds from `process.env.MAX_USERNAME`/`MAX_PASSWORD`. Emits the **same JSON contract** the bank collector does (so `normalize_contract` handles it unchanged): `source:'max'`, one entry per card `account`, `transactions[]` with `identifier,date,processedDate,chargedAmount,chargedCurrency,description,status`. Loops `result.accounts` (multi-card ready). Prints JSON to stdout only; errors to stderr + non-zero exit.

### 2. Config (`config.py`)
Add `max_username`, `max_password` (env `MAX_USERNAME`/`MAX_PASSWORD`), and `max_collector_script` (default `collector/scrape_max.js`). Add `max_configured(config) -> bool` (both creds set), mirroring `finance_configured`.

### 3. `sync_finances` syncs both sources (`finance.py`)
`make_collector_fetch` gains a per-source script (or a second fetch). `_sync_impl` runs the bank fetch **and**, when `max_configured`, the Max fetch — each `upsert_transactions` + `record_snapshot` into the same store. **One source failing must not abort the other** (independent try/except; report per-source counts). Reuse the existing file-lock.

### 4. Option A — no double-counting (the core change)
- **`NON_SPENDING_CATEGORIES = {"transfer"}`** constant in `finance.py`.
- **Seed transfer rules** (idempotent, on sync when `max_configured`): ensure rules exist mapping `חיוב לכרטיס ויזה` → `transfer` and `זיכוי לכרטיס ויזה` → `transfer`. Add only if not already present (check `active_rules`).
- **`financial_summary` becomes category-aware:** instead of `store.sum_amounts` (sign-only), iterate `store.transactions_between(frm,to)`, categorize each via `_categorize(desc, rules)`, and sum **excluding** `NON_SPENDING_CATEGORIES`: income = Σ positives not-in-non-spending, expense = Σ negatives not-in-non-spending. Balance unchanged.
- **`spending_by_category` skips `NON_SPENDING_CATEGORIES`** (don't list `transfer`).
- Result: `financial_summary` expense == Σ `spending_by_category` category totals + uncategorized. They reconcile, and the bank↔card payments count zero.

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
- **Multi-source sync:** inject a fake `fetch_fn` returning both a bank and a max payload; assert both upserted; assert one source raising still commits the other.
- **Option A:** seed a bank `חיוב לכרטיס ויזה -₪4,717` (→transfer) + Max purchases summing to ₪4,717 + a bank non-card expense; assert `financial_summary` expense **excludes** the transfer (counts card purchases + non-card once), and `spending_by_category` omits `transfer` and reconciles.
- **Seed-rules idempotency:** running twice doesn't duplicate the transfer rules.
- Full suite stays green.

## Deploy + live verify (`deploy-box`)
1. rsync (incl. `collector/scrape_max.js`); `.env` already has Max creds on the box.
2. Restart `home-agent`.
3. Live: run `sync_finances` → confirm Max …1743 itemized txns land; ask Menashe for a category breakdown → confirm it uses real merchants, the bank card-bills show as `transfer` (excluded), and the total reconciles.
