from datetime import datetime

from .tools import Tool

_SHOW_SCHEMA = {"type": "function", "function": {
    "name": "show_list",
    "description": "Show the current shared shopping list (what still needs to be bought). Use when "
                   "the user asks what's on the list.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_ADD_SCHEMA = {"type": "function", "function": {
    "name": "add_to_list",
    "description": "Add an item to the shared shopping list. Use the canonical item name; if the user's "
                   "wording is a variant of something already known, reuse the known name (call "
                   "known_items if unsure). Report back in the user's language.",
    "parameters": {"type": "object", "properties": {
        "item": {"type": "string", "description": "Canonical item name (Hebrew or English)."},
        "quantity": {"type": "string", "description": "Optional free-text amount, e.g. '2' or '2 ליטר'."},
        "note": {"type": "string", "description": "Optional note, e.g. a brand or '3%'."},
    }, "required": ["item"], "additionalProperties": False},
}}

_REMOVE_SCHEMA = {"type": "function", "function": {
    "name": "remove_from_list",
    "description": "Remove an item from the shared shopping list (it's no longer needed). Use the "
                   "canonical item name.",
    "parameters": {"type": "object", "properties": {
        "item": {"type": "string", "description": "Canonical item name to remove."},
    }, "required": ["item"], "additionalProperties": False},
}}

_BOUGHT_SCHEMA = {"type": "function", "function": {
    "name": "mark_bought",
    "description": "Record that an item was bought (removes it from the list if present and logs it to "
                   "purchase history with today's date). Use when the user says they bought something.",
    "parameters": {"type": "object", "properties": {
        "item": {"type": "string", "description": "Canonical item name that was bought."},
        "quantity": {"type": "number", "description": "Optional amount bought."},
        "price": {"type": "number", "description": "Optional price paid, in shekels."},
    }, "required": ["item"], "additionalProperties": False},
}}

_KNOWN_SCHEMA = {"type": "function", "function": {
    "name": "known_items",
    "description": "List the canonical item names already known, so you can map a user's wording to an "
                   "existing item instead of creating a near-duplicate.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}


def _now():
    return datetime.now().astimezone()


def _show_impl(args, *, store):
    rows = store.pending()
    if not rows:
        return "the shopping list is empty"
    lines = []
    for r in rows:
        q = f" ({r['quantity']})" if r["quantity"] else ""
        n = f" — {r['note']}" if r["note"] else ""
        lines.append(f"- {r['item']}{q}{n}")
    return "\n".join(lines)


def _add_impl(args, *, store):
    item = (args.get("item") or "").strip()
    if not item:
        return "no item given"
    store.add(item, args.get("quantity"), args.get("note"))
    return f"added {item} to the list ✅"


def _remove_impl(args, *, store):
    item = (args.get("item") or "").strip()
    if store.remove(item) == 0:
        return f"{item} isn't on the list"
    return f"removed {item} from the list ✅"


def _bought_impl(args, *, store, now_fn):
    item = (args.get("item") or "").strip()
    if not item:
        return "no item given"
    store.buy(item, now_fn().date().isoformat(), args.get("quantity"), args.get("price"))
    return f"logged {item} as bought ✅"


def _known_impl(args, *, store):
    names = store.known_items()
    return ", ".join(names) if names else "(no items known yet)"


def build_shopping_tools(store, *, now_fn=None) -> list[Tool]:
    now_fn = now_fn or _now
    return [
        Tool(name="show_list", schema=_SHOW_SCHEMA, impl=lambda a: _show_impl(a, store=store)),
        Tool(name="add_to_list", schema=_ADD_SCHEMA, impl=lambda a: _add_impl(a, store=store)),
        Tool(name="remove_from_list", schema=_REMOVE_SCHEMA, impl=lambda a: _remove_impl(a, store=store)),
        Tool(name="mark_bought", schema=_BOUGHT_SCHEMA,
             impl=lambda a: _bought_impl(a, store=store, now_fn=now_fn)),
        Tool(name="known_items", schema=_KNOWN_SCHEMA, impl=lambda a: _known_impl(a, store=store)),
    ]
