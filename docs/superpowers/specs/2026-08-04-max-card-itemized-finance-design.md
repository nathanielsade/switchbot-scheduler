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
`make_collector_fetch(config, source)` returns a fetcher **for one source** (`'discount'` → `scrape_discount.js` + `DISCOUNT_*`; `'max'` → `scrape_max.js` + `MAX_*`). `_sync_impl` iterates the injected `(source → fetcher)` map, running each in its **own try/except**, upserting into the store and **recording its coverage window** (`source_coverage`, §4) on success; a failure records a per-source error and **does not abort the others**. Returns per-source counts + errors. File-lock once around the whole sync.

**Seam change (P2) — call it out explicitly:** `build_finance_tools(store, *, now_fn=None, fetch_fns=None)` now takes a **map** `fetch_fns={source: fetcher}` (was a single `fetch_fn` at `finance.py:324`). `telegram_app.build_application` (currently `build_finance_tools(FinanceStore(...), fetch_fn=make_collector_fetch(config))`) changes to build the map from what's configured: `{'discount': make_collector_fetch(config,'discount'), **({'max': make_collector_fetch(config,'max')} if max_configured(config) else {})}`. Tests inject a fake map (e.g. `{'discount': ok, 'max': raises}`).

### 4. Option A — no double-counting (the core change)
- **Card-payment = protected internal matcher (P1), NOT a user category rule.** A hardcoded `_is_card_payment(description, cards) -> bool` in `finance.py` — **not** stored in `category_rules`, so it's invisible to `list_category_rules`, can't be removed by `delete_category_rule`, and is applied with **priority over user rules** (a matched line is a card-payment regardless of any user rule, so a longer user pattern can never reintroduce the double-count). It matches a bank line as the payment for card `n` when the normalized description contains that card's bill phrase — matched **robustly** against the real Discount format (see the captured-fixture requirement in Testing), not a brittle exact substring.
- **Reliable coverage via explicit metadata (P1a) — not `MAX(txn_date)`.** New `source_coverage` table; on each sync record per `(source, account)`: `coverage_start` (the scrape's startDate), `coverage_end` (scrape time), `scraped_at`. A card is **covered for `[frm,to]`** iff `coverage_start ≤ frm AND coverage_end ≥ to`. (A purchase-free month, or a range older than the scraper's ~1-year window, would mislead a `MAX(txn_date)` proxy — hence explicit windows.)
- **Exclusion rule.** A bank line is excluded from **spend** iff `_is_card_payment` matches it for a card that is **both imported AND covered** for the requested period. Bills for un-imported cards (…6146) or any uncovered period are **not** excluded — they stay **counted** (bank-level) and the tool output carries the flag "(פירוט הכרטיס אינו זמין לתקופה זו — מציג סכומים ברמת הבנק)". Never under-reports, never double-counts.
- **`financial_summary` category-aware:** iterate `store.transactions_between(frm,to)`; income = Σ positives, expense = Σ negatives, **minus** lines the exclusion rule catches. Balance unchanged.
- **`spending_by_category`:** apply the same exclusion (covered card-payments dropped from the breakdown; uncovered → surface the bills + flag). Also **return an uncategorized TOTAL amount** (P2), not just count/examples, so summaries reconcile.
- **`cash_flow_forecast` — bank-only, NOT spend (P2a):** cash-flow is bank-balance timing; the real future debit is the **card bill**. `_forecast_impl` runs `_detect_recurring` on **`source='discount'` only** — keeps the recurring card bills, excludes the Max purchases. (Opposite lens from spend; documented in Architecture.)
- Reconciliation (period covered): `financial_summary` expense == Σ `spending_by_category` totals + uncategorized total, purchase-date basis; imported+covered card bills count zero.

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
- **Coverage window gate (P1a) — both tools:** drive it via `source_coverage` (not txn dates): a `[frm,to]` **inside** the window → card-bills excluded; a range whose `to` is **past** `coverage_end` (or before `coverage_start`) → card-bills **included** (bank-level) + flag, in `financial_summary` **and** `spending_by_category`. Include a "purchase-free covered month" case to prove `MAX(txn_date)` isn't the signal.
- **Real Discount bill fixture (P2):** `_is_card_payment` is tested against a **captured, real normalized Discount description** (e.g. the actual `חיוב לכרטיס ויזה 1743` text from the account), not only synthetic strings, so it can't pass tests yet fail on live text.
- **Protected matcher (P1):** the card-payment matcher is invisible to `list_category_rules` and unaffected by `delete_category_rule`; and a **longer user category rule** that also matches the bill line does **not** override the exclusion (matcher wins) → no reintroduced double-count. A user `transfer`-rule on an unrelated txn is still counted as spend.
- **Forecast is bank-only (P2a):** recurring bank card-bill + recurring Max purchase → `cash_flow_forecast` uses the **bank bill** and **ignores** the Max purchase (counts the debit once, timed on the bill's payment date).
- **Billing-cycle skew (P2c):** a bank card-bill dated month N paying Max purchases dated month N-1 → for **spend**, the bill is excluded regardless of date and the purchases count in **N-1**; for **forecast**, the bill is timed in **N**.
- **Uncategorized total (P2):** `spending_by_category` returns a summable uncategorized **amount** (not only a count).
- **Seed-rules idempotency:** running the seed twice doesn't duplicate the `card_payment` rules.
- Full suite stays green.

## Deploy + live verify (`deploy-box`)
1. rsync (incl. `collector/scrape_max.js`); `.env` already has Max creds on the box.
2. Restart `home-agent`.
3. Live: run `sync_finances` → confirm Max …1743 itemized txns land; ask Menashe for a category breakdown → confirm it uses real merchants, the bank card-bills are tagged `card_payment` (excluded), the total reconciles, and (Max being fresh) no partial/stale flag appears.
