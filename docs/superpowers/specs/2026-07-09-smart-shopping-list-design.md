# Smart shared shopping list — design

- **Date:** 2026-07-09
- **Roadmap:** part of Epic 4 "family-mcp" (the shopping-list piece). In-process agent tools.
- **Status:** approved design, pre-implementation.
- **Predecessors:** agent core (Epic 1), home-mcp control tools, scheduling — same in-process `Tool`
  pattern, thread-safe SQLite stores (`memory.Conversation`, `ScheduleStore`), injectable seams.

## Goal

One shared grocery list both spouses drive from the Telegram chat, that (2) learns rebuy rhythms and
tells them what they're probably out of, and (3) ingests supermarket receipt photos to log the full
basket with prices. Kills "did we run out? did you already buy it? where's the list?"

## Build vs buy — decision: BUILD

Researched Grocy, KitchenOwl, Mealie, Tandoor, Bring!, and Python receipt-OCR libs. None fit:
consumption/rebuy prediction exists in *no* tool (open feature request in Grocy itself); Hebrew receipt
OCR is best done by the OpenAI vision model we already call (existing parsers are stale English-only
Tesseract); and every self-hostable option needs an always-on server/Docker we don't have yet, just to
cover the *easy* list part. Building on SQLite + OpenAI vision is less code than adapting+hosting Grocy.
Mitigation for the future: the schema is shaped loosely along Grocy's lines (products + purchase log) so
a later migration stays open if an always-on box appears.

## Data model (SQLite, in `home_agent.db`; thread-safe connection-per-op like the other stores)

Design principle: **append-only history**. Purchases are never deleted; list changes are logged, not
hard-deleted. The *current list* is a view (pending rows), not a mutable blob.

- **`items`** — canonical products. `id, name TEXT UNIQUE, created_at`. The single source of "same item."
- **`list`** — append-only list entries. `id, item_id→items, quantity TEXT?, note TEXT?,
  status TEXT ('pending'|'bought'|'removed'), added_at, resolved_at?`. Current list = `status='pending'`.
  Adding = a new row; removing/buying flips status + stamps `resolved_at` (never DELETE).
- **`purchases`** — the history log (pattern fuel). `id, item_id→items, quantity REAL?, unit_price REAL?,
  purchased_on TEXT (ISO date), source TEXT ('chat'|'receipt'), receipt_id TEXT?, created_at`.
  Append-only. A photographed shop shares one `receipt_id` (that's how "the Thursday shop" is grouped).
- **`pending_receipts`** — a parsed-but-unconfirmed receipt awaiting approval. `chat_id (PK),
  parsed_json TEXT, created_at`. One per chat; a new photo replaces it; cleared on commit/cancel.

## Canonicalization = the agent, not an algorithm

There is no fuzzy-matching engine. A `known_items()` tool returns the existing canonical names; the agent
maps the user's free text (`חלב 3% תנובה`) to the right existing item — or a new one — and passes that
**canonical name** to the write tools. The store's deterministic job is only `_get_or_create_item(name)`
(exact canonical name → id, create if absent). The *language mapping* is the agent's reasoning, informed
by `known_items()`.

## Tools

### Phase 1 — the shared list (foundation + trivial CRUD)
- `show_list()` → the current pending list (item, qty, note).
- `add_to_list(item, quantity?, note?)` → get-or-create the canonical item, append a `pending` row.
- `remove_from_list(item)` → mark matching pending row(s) `removed`.
- `mark_bought(item, quantity?, price?)` → mark the pending row `bought` (or create+bought if not listed)
  **and** append a `purchases` row (`source='chat'`, `purchased_on`=today from the injectable clock).
- `known_items()` → existing canonical names (the agent's mapping aid).

### Phase 2 — "what am I probably out of?" (deterministic math, agent reasons)
- `suggest_restock()` → for each item with **≥2 purchases**: compute the **median gap** (days between
  consecutive purchases) and **days-since-last**; flag as due when `days_since_last ≥ median_gap`. Returns
  the due items **with the numbers** (`{item, last_bought, median_gap_days, days_since}`). Items with <2
  purchases are omitted (not enough signal). Deterministic; uses the injectable clock.
- `purchase_history(item?)` → purchase dates + prices for one item, or the recent log — for follow-ups
  ("when did we last buy coffee?", "how much was milk last time?").
- **Agent behavior:** on "מה כדאי לקנות?" it calls `suggest_restock`, drops anything already on the
  pending list (via `show_list`), and phrases it in Hebrew with the reasons. On-demand only.

### Phase 3 — receipt photo → vision → confirm → log
1. **Photo handler (new Telegram surface):** the bot currently handles text only; add a handler for photo
   messages → download the image bytes.
2. **`parse_receipt(image_bytes)`** → a dedicated OpenAI **vision** call with a structured-output schema →
   `{store?, date?, printed_total?, lines:[{name, quantity?, unit_price?}]}`. This is an **injectable
   seam** (`vision_fn`) so tests never hit the network.
3. The agent **canonicalizes each line** (via `known_items`) and the parse is stored as the chat's
   `pending_receipt`. The bot replies with the read-back, a **sum-vs-printed-total sanity check**
   (flag if the line prices don't add up to the printed total), and asks to approve/correct.
4. **`commit_receipt(chat_id, corrections?)`** → for each line append a `purchases` row (item, qty, price,
   today's date, `source='receipt'`, a shared `receipt_id`); flip any matching `pending` list rows to
   `bought`; clear the `pending_receipt`. `cancel_receipt(chat_id)` discards it. The image is **not kept**
   after parsing.

## Boundaries / deferred

- **Cost is stored, not analyzed.** Per-item prices feed history now; budgets/insights are finance (Epic 5).
- **Proactive "shopping-day" nudge** (a message at a time) needs the always-on box → deferred; suggestions
  are on-demand only for now.
- **Barcode scanning, mobile app, recipe integration** — out of scope (that's where Grocy would come in
  later, if ever).

## Build order (this spec → three implementation plans)

Too large for one plan; build incrementally, each its own plan/review/merge (like the prior epics):
1. **Foundation + Phase 1** — the three tables, `known_items`/get-or-create, and the list CRUD tools.
2. **Phase 2** — `suggest_restock` + `purchase_history` (+ the `mark_bought`→purchases link if not already).
3. **Phase 3** — `parse_receipt` (vision seam), the pending/confirm flow, `commit_receipt`/`cancel_receipt`,
   and the Telegram photo handler.

---

## Testing plan

**Principle:** all logic lives behind deterministic, injectable seams so the automated suite runs with **no
network, no real vision, no real clock** — exactly like the scheduling/battery work (`write_fn`, `now_fn`,
`make_fake_client`). Three seams: `now_fn()` (clock), `vision_fn(image)` (receipt parse), and the
`make_fake_client` OpenAI fake for loop tests.

### 1. Stores (pure SQLite, unit) — the bedrock
- `items`: get-or-create is idempotent (same name → same id; new name → new row); UNIQUE respected.
- `list`: add appends a pending row; remove/mark-bought flip status + stamp `resolved_at` and **never
  delete** (assert the row still exists with new status); `show_list` returns only pending; re-adding a
  previously-bought item creates a fresh pending row.
- `purchases`: append-only; rows carry item/qty/price/date/source/receipt_id; nothing deletes them.
- `pending_receipts`: upsert-per-chat (a second parse replaces the first); cleared on commit/cancel.
- Thread-safety: a cross-thread write test (mirror `test_memory.py`), since PTB runs handlers off-thread.

### 2. Phase 2 math (`suggest_restock`, `purchase_history`) — the highest-value tests
Seed `purchases` with **known dates** under a **frozen `now_fn`**, then assert:
- median-gap computation (e.g. milk bought on days 0,5,10 → median gap 5; if now=day 16 → due,
  `days_since=6`); an item bought on a tight cadence but recently → **not** due.
- **<2 purchases → omitted** (cold start, no false suggestions).
- exactly-at-interval boundary (`days_since == median_gap` → due).
- `purchase_history(item)` returns the right dates/prices in order; `purchase_history()` returns recent all.
- A small **scenario test**: seed a realistic multi-item history across simulated dates, assert the due set
  is sensible and the numbers are right — this is the "does the prediction actually make sense" guard.

### 3. Phase 3 receipt pipeline (`vision_fn` faked) — no real OCR in CI
- `parse_receipt` with a **fake `vision_fn`** returning a canned `{lines, printed_total}` → assert the
  parse is canonicalized and stored as the chat's pending receipt.
- **Sum-vs-total sanity check**: fake a receipt whose line prices don't sum to `printed_total` → assert the
  read-back flags the mismatch (this is the safety the confirm-first flow rests on).
- `commit_receipt`: given a pending receipt, assert it appends one `purchases` row per line (right price,
  date, `source='receipt'`, shared `receipt_id`), flips matching pending list rows to `bought`, and clears
  the pending receipt. With `corrections` → assert the corrected values are what get stored.
- `cancel_receipt` clears pending and writes nothing.
- Confirm state machine: pending → commit; pending → cancel; new photo replaces prior pending.

### 4. Agent orchestration (loop tests with `make_fake_client`) — behavior, not mocks
Script the fake OpenAI client to emit tool calls and assert the real tools ran and fed results back:
- "add milk and eggs" → two `add_to_list` calls → `show_list` reflects them.
- "what should I buy?" → `suggest_restock` called; assert the agent's final reply excludes an item already
  on the list (the drop-what's-listed behavior).
- a receipt-approval turn: pending receipt present → user "כן" → `commit_receipt` called.

### 5. What can NOT be auto-tested — and how we cover it (stated honestly, no silent gaps)
- **The AI's canonical mapping quality** (does it map `חלב 3% תנובה`→`חלב`?): not deterministic, so it's
  **not** a CI unit test. Coverage: (a) the deterministic get-or-create + `known_items` contract IS tested;
  (b) an **opt-in eval script** (not in CI) with ~15 Hebrew phrasing→expected-canonical pairs, run against
  the real model when we touch the prompt; (c) the confirm-first receipt flow lets the user correct a bad
  mapping before it lands.
- **Real vision OCR accuracy on Hebrew receipts:** can't unit-test a model's reading. Coverage: a **manual
  spike** with a few real receipts (like the BLE spikes) to validate the schema/prompt, plus confirm-first
  as the runtime safety net.
- **The Telegram photo handler** (download + dispatch): an async PTB closure — **manual-verified** on a live
  run (like the typing indicator / BLE fires), while `parse_receipt`/`commit_receipt` (the logic it calls)
  are fully unit-tested via the seams.

### 6. Regression bar
Each plan ends green on the full suite with no network/BLE/vision in it; the "can't-auto-test" items above
are tracked as explicit manual/eval steps in the plan, not glossed over.
