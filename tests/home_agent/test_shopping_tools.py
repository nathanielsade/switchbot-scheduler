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


def _tools_at(tmp_path, today_iso):
    from datetime import date
    store = ShoppingStore(str(tmp_path / "sh.db"))

    class _Now:
        def date(self):
            return date.fromisoformat(today_iso)

    return build_shopping_tools(store, now_fn=lambda: _Now()), store


def test_suggest_restock_flags_overdue_with_numbers(tmp_path):
    tools, store = _tools_at(tmp_path, "2026-07-17")
    for d in ("2026-07-01", "2026-07-06", "2026-07-11"):   # milk every 5 days
        store.buy("חלב", d)
    out = _tool(tools, "suggest_restock").impl({})
    assert "חלב" in out
    assert "5" in out and "6" in out          # usual gap 5, 6 days since last (2026-07-11 → 17)


def test_suggest_restock_skips_recent_and_sparse_and_listed(tmp_path):
    tools, store = _tools_at(tmp_path, "2026-07-12")
    # recent: bought today-ish, not due
    for d in ("2026-07-01", "2026-07-06", "2026-07-11"):
        store.buy("חלב", d)                    # last 07-11, gap 5, only 1 day since → NOT due
    # sparse: only one purchase → no signal
    store.buy("קפה", "2026-07-01")
    # due by history but already on the list → excluded
    for d in ("2026-06-01", "2026-06-11", "2026-06-21"):
        store.buy("סוכר", d)                   # gap 10, ~21 days since on 07-12 → would be due
    store.add("סוכר")                          # ...but it's on the list now
    out = _tool(tools, "suggest_restock").impl({})
    assert "חלב" not in out and "קפה" not in out and "סוכר" not in out
    assert "nothing" in out.lower()


def test_suggest_restock_collapses_same_day(tmp_path):
    tools, store = _tools_at(tmp_path, "2026-07-17")
    store.buy("חלב", "2026-07-01")
    store.buy("חלב", "2026-07-01")             # duplicate same-day log
    store.buy("חלב", "2026-07-11")
    out = _tool(tools, "suggest_restock").impl({})
    # distinct dates [07-01, 07-11] → gap 10, 6 days since on 07-17 → NOT due (no phantom 0-gap)
    assert "nothing" in out.lower()


def test_purchase_history_for_item_and_recent(tmp_path):
    tools, store = _tools_at(tmp_path, "2026-07-17")
    store.buy("חלב", "2026-07-01", unit_price=6.9)
    out_item = _tool(tools, "purchase_history").impl({"item": "חלב"})
    assert "2026-07-01" in out_item and "6.9" in out_item
    out_all = _tool(tools, "purchase_history").impl({})
    assert "חלב" in out_all
    assert "no purchase history" in _tool(tools, "purchase_history").impl({"item": "לא-קיים"}).lower()
