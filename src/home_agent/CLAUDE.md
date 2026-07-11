# CLAUDE.md — src/home_agent

The agent package. See the repo-root `CLAUDE.md` for the big picture, run/test commands, and conventions.
This file is the module map + the local recipes. Durable facts only (status → `docs/ROADMAP.md`/memory).

## Module map

| File | Responsibility |
|---|---|
| `__main__.py` | Entry point (`python -m home_agent`): logging, `load_config`, build + `run_polling`. Quiets the httpx logger so the bot token never lands in logs. |
| `config.py` | `Config` dataclass + `load_config()` — reads `.env` via `switchbot_scheduler.config.load_env` (override=False: shell exports win). Defaults live as module constants (`DEFAULT_MODEL`, etc.); malformed `ALLOWED_CHAT_IDS` → clean `SystemExit`. |
| `agent.py` | `run_turn(user_text, history, *, client, model, system, tools, max_steps=10)` — the OpenAI function-calling loop. Injectable `client` (tests pass a fake). Bounded; drops tools on the final step so the model must answer in text. |
| `tools.py` | `Tool(name, schema, impl)` dataclass + `DEFAULT_TOOLS` (currently `get_current_time`). |
| `prompts.py` | `FAMILY_SYSTEM_PROMPT` — identity, Hebrew, honest capabilities, canonicalization policy. **Digit-free + byte-stable** (tests enforce). |
| `memory.py` | `Conversation` — per-chat message store (SQLite). The **connection-per-op thread-safety pattern** all stores copy. |
| `telegram_app.py` | `build_application(config)` composes the tool list + wires PTB handlers; `handle_message(chat_id, text, *, tools=…)` runs one turn (allow-list, discovery mode, load-history-before-persist, error fallback, `_split_for_telegram` chunking, typing indicator, error handler). **Where every capability's tools get composed.** |
| `home.py` | Home control (`build_home_tools`): `control_device` / `list_devices` / `battery_status`, wrapping `switchbot_scheduler.run_immediate`. `load_registry` / `load_home_tools`. Reports the user's *requested* action (inverted-safe). |
| `schedule_store.py` + `schedules.py` | On-device Bot-timer scheduling: `ScheduleStore` (SQLite record = source of truth, Bots can't be read back) + `build_schedule_tools` (`schedule_device`/`get_schedule`/`cancel_schedule`). |
| `shopping_store.py` + `shopping.py` | Shared shopping list: `ShoppingStore` (`items`/`list`/`purchases`, append-only) + `build_shopping_tools` (`show_list`/`add_to_list`/`remove_from_list`/`mark_bought`/`known_items`/`suggest_restock`/`purchase_history`). |
| `roborock_rooms.py` + `roborock.py` | Roborock Q Revo vacuum control: `RoomRegistry`/`load_room_registry` (segment id ↔ Hebrew room name, mirrors `devices.yaml`) + `build_roborock_tools` (`list_rooms`/`clean`/`control_vacuum`/`dock_action`/`vacuum_status`/`consumables` + the schedule trio `schedule_clean`/`get_cleaning_schedule`/`cancel_cleaning_schedule`), behind an injectable `RoborockClient` seam (`load_roborock_client`) so tests stay offline. |

## Adding a new in-process tool

```python
_MY_SCHEMA = {"type": "function", "function": {
    "name": "do_thing",
    "description": "WHEN and HOW to use this — the model reads only this. Report back in the user's language.",
    "parameters": {"type": "object", "properties": {
        "x": {"type": "string", "description": "..."}},
        "required": ["x"], "additionalProperties": False}}}

def _do_thing_impl(args, *, store):   # keyword-only deps injected by the factory
    ...
    return "human-readable result string"   # impl MUST return str; run_turn feeds str(result) back

def build_x_tools(store, *, now_fn=None):     # inject seams (now_fn/write_fn/vision_fn) for offline tests
    return [Tool(name="do_thing", schema=_MY_SCHEMA, impl=lambda a: _do_thing_impl(a, store=store))]
```

Then compose in `telegram_app.build_application`. **Chat-agnostic** tools are built once at startup;
tools that act on a specific chat's state must be **bound per turn in `handle_message`** with `chat_id`
captured in a Python closure and **omitted from the schema** (the model must never pass a Telegram id).

## Store conventions

- Subclass the `memory.Conversation` shape: `db_path`, table created in `__init__`, **every method opens its
  own connection** (`with closing(sqlite3.connect(self.db_path)) as conn:`), commit inside.
- History tables are **append-only**: no `DELETE`; flip a `status` + stamp a timestamp (see `shopping_store`,
  `schedule_store`). Canonical entities via exact `_get_or_create_item`-style helpers; the model does fuzzy
  mapping, not the store.

## Testing

- Tests in `tests/home_agent/`. Use the `make_fake_client` fixture for the OpenAI loop; inject `now_fn`
  (frozen clock), `write_fn`/`actuate_fn`/`vision_fn` (no real BLE/vision). No network in the suite.
- Loop/behavior tests assert *which tools the agent called* (script the fake client), not mocks of the DB.
