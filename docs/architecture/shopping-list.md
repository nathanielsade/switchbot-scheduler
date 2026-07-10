# Smart Shared Shopping List — Architecture

*Companion to the spec (`docs/superpowers/specs/2026-07-09-smart-shopping-list-design.md`) and the
Phase-1 plan. This is the "hold it all in your head" document: what it's for, how it feels day to day,
how it's built, and exactly which part is responsible for what.*

Status: **Phase 1 shipped** (list CRUD). Phases 2 (cycle prediction) & 3 (receipts) designed, not built.

---

## 1. The goal

Kill the everyday friction of *"did we run out of milk? did you already buy bread? where's the list?"*
Replace the paper list / forgotten Notes app / back-and-forth texting with **one shared list that lives
inside the family Telegram chat**, that both spouses drive by just talking (Hebrew), and that gets
smarter over time:

- **Phase 1 — the shared list.** One always-current list, both phones, add/remove/show/check-off by chat.
- **Phase 2 — anticipation.** It learns your rebuy rhythms and answers *"what are we probably out of?"*
- **Phase 3 — receipts.** Snap the supermarket receipt; it logs the whole basket (items + prices) and
  ticks off what was on the list.

Guiding principles (inherited from the rest of this project):
- **In-process, local, no server.** Just SQLite + the OpenAI calls the agent already makes. Runs on the
  laptop today; no always-on box required (unlike Grocy et al., which we evaluated and rejected).
- **The agent reasons; the tools are deterministic.** Language understanding lives in the model + system
  prompt; storage and math live in plain, testable Python tools.
- **Append-only truth.** Nothing is destroyed — purchases and list changes are kept, so the data can fuel
  patterns we haven't thought of yet.

---

## 2. How it feels day to day

**Adding & checking (Phase 1, live now):**
> Wife, morning: `נגמר הקפה, תוסיף לרשימה` → *"added coffee ✅"*
> You, at the store: `מה יש ברשימה?` → *coffee, milk, diapers*
> You: `קניתי הכל` → list cleared; each item logged to history with today's date
> You: `תמחק חיתולים` → removed

Both phones see the same list because it's one row set in one database behind one bot.

**Anticipation (Phase 2):**
> You, Friday: `מה כדאי לקנות?`
> Bot: *"You're probably out of: milk (last bought 6 days ago, usually every 5), coffee (7 days / every 6).
> Bread you bought 2 days ago, so probably fine."*

**Receipts (Phase 3):**
> You photograph the Shufersal receipt →
> Bot: *"I read 11 items, ₪214.30 (matches the printed total). Milk ₪6.90, eggs ₪12.90, … — save it?"*
> You: `כן` → all 11 logged with prices + today's date; milk & eggs (which were on the list) ticked off.

---

## 3. Where it fits: the big picture

The shopping list is **not a separate app**. It's a set of *tools* plugged into the agent you already
have. The agent is an OpenAI function-calling loop; a "capability" = a bundle of tools + guidance in the
system prompt. Home control and scheduling already work exactly this way.

```
 ┌───────────────┐   text / photo    ┌──────────────────────────┐
 │ Telegram chat │ ────────────────▶ │  telegram_app (PTB)      │
 │ (you + wife)  │ ◀──────────────── │  on_message / on_photo   │
 └───────────────┘   Hebrew reply    └────────────┬─────────────┘
                                                   │ handle_message(text, tools=…)
                                                   ▼
                                   ┌──────────────────────────────────┐
                                   │  agent.run_turn (OpenAI loop)     │
                                   │  system prompt + tools + history  │
                                   └────────────┬──────────────────────┘
                                     model picks │ tool calls
                                                 ▼
              ┌───────────── shopping tools (in-process) ─────────────┐
              │ show_list · add_to_list · remove_from_list ·          │
              │ mark_bought · known_items · suggest_restock ·         │
              │ purchase_history · commit_receipt · …                 │
              └───────────────────────┬───────────────────────────────┘
                                      ▼
                         ┌────────────────────────┐        ┌───────────────────┐
                         │  ShoppingStore (SQLite) │        │ OpenAI vision      │
                         │  home_agent.db          │        │ (receipt parsing)  │
                         └────────────────────────┘        └───────────────────┘
```

Everything in the shaded box is our own code. The only external calls are the OpenAI chat completion
(already happening every turn) and, for Phase 3, one OpenAI vision call per receipt photo.

---

## 4. What it stores (the data model)

One SQLite database (`home_agent.db`, shared with conversation memory & schedules). **Append-only:** we
never `DELETE` from `list` or `purchases`; state changes flip a status and stamp a timestamp.

```
items                         list                              purchases
──────────                    ──────────                        ──────────
id        (pk)                id         (pk)                    id           (pk)
name      UNIQUE   ◀───┐      item_id    ─────▶ items.id        item_id      ─────▶ items.id
created_at             │      quantity   TEXT?                   quantity     REAL?
                       │      note       TEXT?                   unit_price   REAL?
                       └──────status     'pending'|'removed'|'bought'     purchased_on TEXT (ISO date)
                              added_at                            source       'chat'|'receipt'
                              resolved_at?                        receipt_id   TEXT?   (groups a shop)
                                                                  created_at

pending_receipts   (Phase 3 only)
──────────
chat_id     (pk)      parsed_json TEXT      created_at        ← one in-flight receipt awaiting "כן"
```

- **`items`** — the *canonical* products. The single answer to "is this the same thing as before?"
  `name` is UNIQUE; the store's only identity logic is get-or-create by exact name.
- **`list`** — the shopping list as an append-only log of entries. The **current list** is the view
  `WHERE status='pending'`. Adding = a new row; removing/buying flips the status + stamps `resolved_at`
  (the row is never deleted, so "what did we add & when" is preserved).
- **`purchases`** — the history log, the **fuel for Phase 2 and cost**. One row per thing bought
  (from `mark_bought` or a receipt). Append-only. A photographed shop shares one `receipt_id` — that's how
  "the Thursday shop" is a group.
- **`pending_receipts`** — a parsed-but-unconfirmed receipt, one per chat, cleared on approve/cancel.
  Exists only so the confirm-first receipt flow can span two chat turns. **`created_at` is load-bearing:**
  a pending receipt older than an expiry window (~15 min) is treated as stale and refused at commit, so a
  forgotten receipt can't be committed later by an unrelated "כן".

Why "purchases", not "saved lists": history = what you **bought** (reality), which is exactly what
prediction and cost need. The list is just current intentions; its changes are logged too, but we don't
snapshot old intention-lists as documents (derivable from timestamps if ever wanted).

---

## 5. The tools (the agent's whole shopping vocabulary)

Each tool is a `home_agent.tools.Tool(name, schema, impl)` — a name, an OpenAI function schema (this *is*
the instruction the model reads about when/how to use it), and a deterministic Python impl.

| Tool | Phase | Reads / Writes | Purpose |
|---|---|---|---|
| `show_list()` | 1 ✅ | reads `list` (pending) | Show the current list. |
| `add_to_list(item, quantity?, note?)` | 1 ✅ | writes `items`,`list` | Add an item (canonical name). |
| `remove_from_list(item)` | 1 ✅ | writes `list` | Flip a pending entry to removed. |
| `mark_bought(item, quantity?, price?)` | 1 ✅ | writes `list`,`purchases` | Record a buy (→ history) + tick off the list. |
| `known_items()` | 1 ✅ | reads `items` | List canonical names, so the model reuses them instead of making near-duplicates. |
| `suggest_restock()` | 2 | reads `purchases` | Deterministic median-gap math → items likely out, **with the numbers**. |
| `purchase_history(item?)` | 2 | reads `purchases` | Dates/prices for one item or the recent log (follow-ups). |
| `commit_receipt(corrections?)` | 3 | reads `pending_receipts`; writes `purchases`,`list` | Apply the pending receipt. **`chat_id` is bound per-turn in Python, NOT a model arg** (see §6). Refuses a stale/expired pending. |
| `cancel_receipt()` | 3 | writes `pending_receipts` | Discard the pending receipt (`chat_id` bound per-turn). |

Plus two **non-tool** pieces in Phase 3, both run by the Telegram **photo handler** (not the model):
- `parse_receipt(image_bytes)` — a dedicated OpenAI **vision** call (behind an injectable `vision_fn` seam)
  → structured `{store, date, printed_total, lines}`.
- `stage_receipt(chat_id, parsed)` — **deterministic**: stores the parse as the chat's pending receipt and
  returns the read-back + sum-vs-total text the handler replies with. **No model turn on the photo.**
  Canonicalization + commit happen on the *next* (text) turn, when the user approves.

---

## 6. Who is in charge of what (prompt vs. tools vs. the model)

This is the heart of the design. Three actors, one clear line between them.

### 6a. The **system prompt** (`FAMILY_SYSTEM_PROMPT`) — identity, language, policy
It does **not** contain shopping logic. It sets the standing rules the model applies every turn:
- Who the bot is, tone, **respond in Hebrew**.
- **Capability honesty** (added in the polish batch): it states what the agent really can do and cannot —
  so it won't promise reminders/scheduling it lacks.
- **The canonicalization policy** (the crucial shopping bit): *"use the canonical item name; if the user's
  wording is a variant of something already known, reuse the known name — call `known_items` if unsure."*
  **Accurate status:** today this guidance lives only in the `add_to_list` **tool description**, not in
  `FAMILY_SYSTEM_PROMPT` (the shipped prompt says nothing about shopping). As Phase 2/3 add more tools that
  canonicalize (`mark_bought`, the receipt commit), the policy will be **lifted into `FAMILY_SYSTEM_PROMPT`**
  as one cross-tool rule instead of being repeated in each tool's description. This is what makes `חלב`,
  `חלב 3%`, and a receipt's `חלב תנובה` all land on one `items` row.

### 6b. The **tools** — deterministic storage & math (no judgment)
Everything that must be *correct and testable*:
- All DB reads/writes (`ShoppingStore`).
- The **cycle math** (`suggest_restock`): median gap between purchases vs. days-since-last, ≥2 purchases
  required. No LLM in this computation — it returns hard numbers the model can trust.
- The **date stamping** (`mark_bought` uses an injectable clock).
- **Receipt parsing** structure (`parse_receipt` returns a strict JSON schema) and the **sum-vs-printed-total
  sanity check**.
- The **commit** of a receipt (append purchases, tick off list, clear pending).

### 6c. The **model's reasoning** (runtime) — language & judgment
What only an LLM can do well:
- Turn free Hebrew (*"נגמר הקפה"*, *"תדליק... תוסיף... בעוד..."*) into the right tool calls with the right args.
- **Canonical mapping**: decide that `חלב 3% תנובה` → the existing `חלב` (informed by `known_items`).
- **Compose the answer**: on `suggest_restock`, cross-check against `show_list` to drop things already
  listed, and phrase the suggestion warmly in Hebrew.
- **Present a receipt** for confirmation and interpret the user's `כן` / `תקן…` into `commit_receipt`
  (with corrections) or `cancel_receipt`.

> The line: **if it must be exactly right, it's a tool; if it needs to understand language or use judgment,
> it's the model, steered by the prompt.** Cost math is never left to the model; Hebrew mapping is never
> hard-coded in Python.

### 6d. Chat scoping — the model never handles `chat_id`
The receipt tools (`commit_receipt`, `cancel_receipt`) act on *this chat's* pending receipt, so they need a
`chat_id`. That is an **infrastructure fact the model must not carry**. So unlike the always-on Phase 1/2
tools (built once at startup), the **receipt tools are bound per turn**: `handle_message` already has the
`chat_id`, so it constructs the receipt tools with `chat_id` **captured in a Python closure** and **omitted
from the schema**. The model just calls `commit_receipt()` / `commit_receipt(corrections)`; the right chat
is baked in. (Phase 1/2 tools stay startup-built since they're chat-agnostic — the list/history are shared.)

---

## 7. Detailed flows

### Flow A — "add coffee" (Phase 1)
```
user: "נגמר הקפה, תוסיף לרשימה"
  → handle_message(text, tools=[…,shopping…])
  → run_turn: model reads system prompt (canonical policy) + tools
  → model calls add_to_list(item="קפה")           # maps "קפה" to canonical
  → ShoppingStore.add: get_or_create item "קפה", INSERT list(status='pending')
  → tool returns "added קפה to the list ✅"
  → model replies in Hebrew: "הוספתי קפה לרשימה"
```

### Flow B — "what should I buy?" (Phase 2)
```
user: "מה כדאי לקנות?"
  → model calls suggest_restock()
      → for each item with ≥2 purchases: median gap vs days-since-last (frozen-testable clock)
      → returns e.g. [{item:"חלב", last:"…6 days ago", median_gap:5, days_since:6}, …]
  → model calls show_list()  (to avoid re-suggesting what's already listed)
  → model reasons: drop on-list items, phrase the rest
  → reply: "כדאי לקנות: חלב (נגמר לפי הקצב), קפה. לחם קנית לפני יומיים, כנראה בסדר."
```

### Flow C — receipt photo (Phase 3, confirm-first, two distinct turns)
This flow spans **two turns with a hard boundary between them**, because a photo is not a text turn and
the model must not run on the raw image or canonicalize during the photo.

**Turn 1 — the photo (deterministic; NO model turn):**
```
user: [photo of receipt]
  → on_photo handler (Telegram): download image bytes
  → parse_receipt(bytes) [OpenAI vision, injectable vision_fn]:
        → {store, date, printed_total, lines:[{name, qty, unit_price}]}
  → stage_receipt(chat_id, parsed):  deterministic — store parsed JSON as pending_receipts[chat_id]
        (raw line names, NOT yet canonicalized), stamp created_at
  → handler replies deterministically: the read-back + sum-vs-printed-total check + "לשמור?"
```
**Turn 2 — the approval (a normal text turn through run_turn):**
```
user: "כן"   (or "תקן: חלב 7.90")
  → run_turn as usual; the chat's receipt tools were bound with chat_id in Python (§6d)
  → model reads the pending receipt, maps each raw line name → canonical item (known_items),
    then calls commit_receipt(corrections?)   # no chat_id arg — it's closed over
      → refuse if the pending is expired/absent
      → append one purchases row per line (item, qty, price, today, source='receipt', shared receipt_id)
      → flip matching pending list rows → 'bought'
      → clear pending_receipts[chat_id]
  → reply: "נשמר: 11 פריטים, ₪214.30. סימנתי חלב וביצים כנקנו."
```
So canonicalization is a normal turn-2 responsibility (model + `known_items`), never a magic step on the
photo. A pending receipt **expires** (~15 min via `created_at`): a later, unrelated "כן" won't commit a
stale one — `commit_receipt` refuses it and asks the user to resend; a new photo replaces any prior pending.

---

## 8. Files (module layout)

```
src/home_agent/
  shopping_store.py     # ShoppingStore: SQLite items/list/purchases (+ pending_receipts in P3).
                        #   Thread-safe (connection-per-op). The ONLY place that touches the DB.
  shopping.py           # build_shopping_tools(store, *, now_fn=None): the Tool objects + their impls.
                        #   P2 adds suggest_restock/purchase_history; P3 adds commit/cancel_receipt.
  receipts.py  (P3)     # parse_receipt(image_bytes, *, vision_fn=None): the vision call + schema; and
                        #   stage_receipt(chat_id, parsed): deterministic pending-receipt store + read-back.
                        #   (Kept separate so the vision seam lives in one focused file.)
  prompts.py            # FAMILY_SYSTEM_PROMPT — identity, Hebrew, honesty, canonicalization policy.
  telegram_app.py       # build_application composes chat-agnostic shopping tools (unconditionally).
                        #   P3: adds the photo handler (on_photo → parse_receipt + stage_receipt), and
                        #   handle_message binds the chat-scoped receipt tools per turn (chat_id closure).
  tools.py              # the shared Tool dataclass + DEFAULT_TOOLS (unchanged).

tests/home_agent/
  test_shopping_store.py   # store CRUD, append-only, thread-safe.
  test_shopping_tools.py   # each tool; P2 math with frozen clock; P3 pipeline with fake vision.
  test_telegram_*.py       # wiring + loop tests (agent calls the right tools).
```

Each file has one job: the store owns the database, `shopping.py` owns the tool surface, `receipts.py`
owns the vision boundary, the prompt owns policy, `telegram_app` owns wiring/transport.

---

## 9. How it's tested (the seams)

The whole point is that CI runs with **no network, no real vision, no real clock**. Three injection points:
- **`now_fn()`** — a frozen clock, so `mark_bought` dates and `suggest_restock` math are deterministic.
- **`vision_fn(image)`** — a fake receipt parser returning canned JSON, so the entire Phase-3 pipeline
  (canonicalize → pending → sum-check → commit) is tested without OCR.
- **`make_fake_client`** — a scripted OpenAI client for loop tests that assert *the agent called the right
  tools* (behavior, not mocks).

Honestly out of scope for CI, covered otherwise: the model's Hebrew **mapping quality** (opt-in eval script
+ the receipt confirm-step as a safety net), **real OCR accuracy** (a manual receipt spike), and the
**Telegram photo handler** (manual live check) — same pattern as the BLE/typing manual verifications.

---

## 10. Boundaries & the future

- **Cost is stored, not analyzed.** Prices ride along in `purchases`; budgets/insights are the finance epic
  (Epic 5). No spending analysis here.
- **Proactive nudges** ("it's Friday, you're low on milk") need something awake at a time → the **always-on
  box** (Epic 2). For now suggestions are on-demand only.
- **No barcode/mobile-app/recipes.** If an always-on box ever appears and you want those, the schema is
  shaped along Grocy's lines so a migration stays open.

### Build notes carried into Phase 2/3 (from the Phase-1 review)
- **Phase 2 must dedupe purchases** by `(item_id, purchased_on)` (or the prompt must guard against
  re-logging) — otherwise a double `mark_bought` for one real buy creates two same-day rows and a 0-day
  gap that skews the "every N days" cadence.
- **Phase 3 / finance:** `mark_bought`'s `price` is stored in `purchases.unit_price` but described loosely
  as "price paid"; clarify it to **price per unit** when cost actually gets used.
