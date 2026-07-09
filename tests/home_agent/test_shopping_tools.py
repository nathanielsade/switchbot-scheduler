from datetime import datetime, timezone
from home_agent.shopping_store import ShoppingStore
from home_agent.shopping import build_shopping_tools


def _fixed_now():
    return datetime(2026, 7, 9, 18, 0, tzinfo=timezone.utc)


def _tools(tmp_path):
    store = ShoppingStore(str(tmp_path / "sh.db"))
    return build_shopping_tools(store, now_fn=_fixed_now), store


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_show_list_empty(tmp_path):
    tools, _ = _tools(tmp_path)
    assert "empty" in _tool(tools, "show_list").impl({}).lower()


def test_add_then_show(tmp_path):
    tools, store = _tools(tmp_path)
    out = _tool(tools, "add_to_list").impl({"item": "חלב", "quantity": "2"})
    assert "חלב" in out and "✅" in out
    shown = _tool(tools, "show_list").impl({})
    assert "חלב" in shown
    assert store.pending()[0]["item"] == "חלב"


def test_remove_present_and_absent(tmp_path):
    tools, _ = _tools(tmp_path)
    _tool(tools, "add_to_list").impl({"item": "חלב"})
    assert "✅" in _tool(tools, "remove_from_list").impl({"item": "חלב"})
    assert "isn't on the list" in _tool(tools, "remove_from_list").impl({"item": "חלב"})


def test_mark_bought_logs_purchase_with_frozen_date(tmp_path):
    tools, store = _tools(tmp_path)
    _tool(tools, "add_to_list").impl({"item": "חלב"})
    out = _tool(tools, "mark_bought").impl({"item": "חלב", "price": 6.9})
    assert "✅" in out
    assert store.pending() == []
    assert store.purchases_for("חלב") == [
        {"purchased_on": "2026-07-09", "quantity": None, "unit_price": 6.9, "source": "chat"}
    ]


def test_known_items(tmp_path):
    tools, _ = _tools(tmp_path)
    _tool(tools, "add_to_list").impl({"item": "חלב"})
    _tool(tools, "add_to_list").impl({"item": "קפה"})
    out = _tool(tools, "known_items").impl({})
    assert "חלב" in out and "קפה" in out
