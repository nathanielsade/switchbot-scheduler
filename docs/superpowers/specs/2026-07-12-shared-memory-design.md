# Shared memory — remember / recall / forget — design

- **Date:** 2026-07-12.
- **Roadmap:** Epic D (partial) "shared memory" — `remember(fact)` / `recall(question)` over SQLite. In-process agent tools.
- **Status:** approved design, pre-implementation.
- **Predecessors:** agent core, home-mcp, scheduling, shopping, calendar — same in-process `Tool` pattern,
  connection-per-op SQLite stores, injectable seams, graceful wiring. Closest analogs: `memory.Conversation`
  (the connection-per-op store shape) and `shopping_store`/`shopping` (append-only store + `build_*_tools`).

## Goal

Give Menashe a durable, curated **family fact store** so it can answer questions about things the family
told it: *"תזכור שהדרכונים בכספת"* → later *"איפה הדרכונים?"* → *"בכספת"*. Also *"מה קוד השער?"*,
*"מה הסיסמה לוויי-פיי?"*, *"מתי חוגג יום הולדת אבא?"*. Facts are stored **only when explicitly asked**,
retrieved by the model reasoning over the stored set, and retired **on request**.

This is **distinct from `memory.Conversation`** — that is the rolling per-chat message log (last N turns);
this is a small, deliberately-curated, long-lived knowledge base that never rolls off.

## Scope

**In scope:** three in-process tools — `remember`, `recall`, `forget` — over a new append-only `FactStore`
(SQLite). Family-wide (shared, not per-chat). Each fact records who saved it and when.

**Out of scope (deferred / YAGNI):** automatic/proactive fact capture (v1 is **explicit-only**); semantic /
vector search (recall returns the facts and the model reasons — the family scale makes search unnecessary;
a keyword pre-filter can be added later behind the same tool if the store ever grows huge); editing a fact
in place (a changed value is just a new `remember`; the old one is retired via `forget` or ignored as older);
cross-turn confirmation (remember is a harmless append; forget is reversible).

## Decisions (from brainstorming)

- **Append-only, newest wins.** A new value for something is a new row; nothing is auto-superseded on
  `remember`. `recall` returns facts **newest-first**, so the model naturally answers with the current value.
- **Explicit-only capture.** Menashe stores a fact only when clearly told to ("תזכור ש…", "remember that…").
- **`forget` on request**, reversible — flips a `status` column, never `DELETE` (house style; recoverable).
- **Records author.** Each fact stores the speaker's name (the `sender` already threaded into `handle_message`
  by the Menashe-identity work) — useful context in a shared memory ("נתנאל saved this").
- **Recall = Approach A.** The tool hands the model the stored facts; the model does the matching (paraphrase,
  Hebrew synonyms). Matches the codebase split: storage deterministic, language/judgment is the model's.

## Architecture & module

**New `src/home_agent/facts.py`** (named `facts`, not `memory`, to avoid clashing with `memory.Conversation`).

- **`FactStore(db_path)`** — SQLite, **connection-per-operation** (thread-safe; mirrors `memory.Conversation`
  and `shopping_store`). One table, created in `__init__`:
  ```sql
  CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT,                       -- short label the model assigns ("gate code", "passports")
    fact TEXT NOT NULL,                 -- the detail ("in the safe", "5678")
    author TEXT,                        -- who told Menashe (the turn's sender); nullable
    created_at TEXT,                    -- ISO timestamp (injected now_fn)
    status TEXT NOT NULL DEFAULT 'active'  -- 'active' | 'forgotten'; forget flips it, never DELETE
  )
  ```
  Methods:
  - `add(subject, fact, author, created_at) -> int` — insert an `active` row, return its id.
  - `active() -> list[dict]` — all `active` rows, **newest-first** (`ORDER BY id DESC`), each
    `{id, subject, fact, author, created_at}`.
  - `find_active(query) -> list[dict]` — active rows whose `subject` OR `fact` contains `query`
    (case-insensitive `LIKE`), newest-first. Backs `forget`'s matching.
  - `forget(id) -> None` — set `status='forgotten'` for that id (idempotent; no error if already forgotten).

- **`build_memory_tools(store, *, sender, now_fn=None) -> list[Tool]`** — the factory. Built **per-turn** in
  `handle_message` with `sender` (author) and the clock captured in a closure and **omitted from the tool
  schemas** (the model never passes an author or a timestamp). `now_fn` defaults to real UTC/local now.

## The tools

- **`remember(subject, fact)`** — store one fact.
  - `subject`: a short canonical label the model derives from the wording (e.g. "gate code", "passports").
  - `fact`: the detail.
  - Author = the captured `sender`; `created_at` = `now_fn()` — both injected, **not** model arguments.
  - Calls `store.add(...)`. Returns a short confirmation (e.g. `remembered — passports: in the safe`).
  - The system prompt restricts this to **explicit** requests only.

- **`recall()`** — **no arguments.** Returns `store.active()` formatted newest-first, one line per fact:
  `subject — fact (author, date)`. If empty → "I have not been told to remember anything yet." The model
  already holds the user's question in context and answers from these lines. (No `query` arg in v1: the set is
  small; returning all is simpler and cannot miss a paraphrase. A keyword pre-filter is a future, invisible
  optimization behind the same tool.)

- **`forget(query)`** — retire fact(s) on request. `query` = free text ("gate code", "the passports thing").
  Uses `store.find_active(query)`:
  - **exactly one match** → `store.forget(id)`, confirm what was removed;
  - **several matches** → do **not** guess; return the matching lines so the model asks the user which one;
  - **no match** → "nothing matching to forget."
  Retired facts stop appearing in `recall` but remain in the table (recoverable).

## Safety & side effects

No cross-turn confirm. `remember` only appends; `forget` only flips a status and is reversible. Both are
deliberate user commands. The agent loop's try/except is the backstop for any store error (returned as a
readable message). The store is **not** exposed to un-curated growth: explicit-only capture keeps it small.

## Wiring (`telegram_app`)

- Create the `FactStore(config.db_path)` **once at startup** in `build_application`.
- In `handle_message`, build the memory tools **per-turn** (like the calendar tools): the store is fixed, but
  `sender` and the clock are bound in the closure so `remember` records the right author/timestamp. Compose
  them into `turn_tools` alongside the existing per-turn calendar tools. No new config keys; always enabled
  (SQLite is always available — like the shopping tools, not gated).

## System prompt (additions; digit-free + byte-stable)

State that Menashe keeps a durable family memory: it can **remember** a fact when explicitly asked
("תזכור ש…"); it should call **recall** whenever the user asks about something that might have been saved and
answer from what it finds (preferring the most recent when values conflict); and it can **forget** a fact on
request. It must **only** store a fact when clearly told to — never proactively. Respond in Hebrew.

## Testing (no network)

- **`FactStore`** (real tmp SQLite): `add` then `active()` returns newest-first; `find_active` matches on
  subject and on fact text, case-insensitive, active-only; `forget(id)` flips status (row gone from `active()`
  but still in the table), idempotent; connection-per-op (a fresh instance on the same path sees prior rows).
- **Tools** (frozen `now_fn`, injected `sender="נתנאל"`): `remember` stores subject/fact/author/timestamp
  (assert the row); `recall` returns newest-first formatted lines incl. author + date, and the empty-store
  message; `forget` — one-match retires + confirms; multi-match retires nothing and lists them; no-match
  friendly. Author/timestamp are **not** in the tool schemas.
- **End-to-end** (`handle_message`, fake OpenAI client scripting the tool calls): "תזכור שהדרכונים בכספת"
  → `remember` persists; a later turn "איפה הדרכונים?" → `recall` returns the fact and the model answers.
  Mirrors `test_handle_message_runs_add_to_list_through_composed_tools`.

**Manual (real bot):** tell Menashe to remember a fact in Telegram, ask about it in a later message, then
ask it to forget it — verify recall reflects each step.

## Build order (this spec → one plan, ~5 tasks)

1. `FactStore` (schema + `add`/`active`/`find_active`/`forget`, connection-per-op).
2. `remember` tool + `build_memory_tools` skeleton (author/clock injected).
3. `recall` tool (newest-first formatting, empty-store message).
4. `forget` tool (one / several / none matching).
5. Prompt additions + per-turn wiring in `handle_message` + end-to-end test.
