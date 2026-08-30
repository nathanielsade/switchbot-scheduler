import os
import tempfile
from datetime import datetime

from home_agent.finance import build_finance_tools
from home_agent.finance_store import FinanceStore


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _frozen():
    return datetime(2026, 7, 12, 12, 0, 0)


def _row(i, d, amt, desc, account="1"):
    return dict(source="discount", account=account, identifier=i, fingerprint=f"id:{i}",
                txn_date=d, processed_date=None, amount_agorot=amt, currency="ILS",
                description=desc, status="completed", raw_json="{}")


def test_lists_recurring_expenses_excludes_one_off():
    store = _store()
    store.upsert_transactions([
        _row("sp1", "2026-05-10", -3390, "Spotify"),
        _row("sp2", "2026-06-10", -3390, "Spotify"),
        _row("sp3", "2026-07-10", -3390, "Spotify"),
        _row("g1", "2026-05-05", -1200, "Google One"),
        _row("g2", "2026-06-05", -1200, "Google One"),
        _row("g3", "2026-07-05", -1200, "Google One"),
        _row("o1", "2026-06-20", -50000, "רכישה חד פעמית"),
    ])
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert "Spotify" in out
    assert "33.90" in out
    assert "Google One" in out
    assert "12.00" in out
    assert "רכישה חד פעמית" not in out
    assert "500.00" not in out


def test_recurring_income_excluded_transfer_outflow_included():
    store = _store()
    store.upsert_transactions([
        _row("s1", "2026-05-10", 1000000, "משכורת"),
        _row("s2", "2026-06-10", 1000000, "משכורת"),
        _row("s3", "2026-07-10", 1000000, "משכורת"),
        _row("t1", "2026-05-15", -80000, "הפקדה לחיסכון"),
        _row("t2", "2026-06-15", -80000, "הפקדה לחיסכון"),
        _row("t3", "2026-07-15", -80000, "הפקדה לחיסכון"),
    ])
    store.add_rule("הפקדה לחיסכון", "transfer")
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert "משכורת" not in out  # recurring income excluded
    assert "הפקדה לחיסכון" in out  # recurring transfer outflow included
    assert "העברות/חיסכון" in out  # Hebrew category label for transfer


def test_confidence_and_sort_order_biggest_first():
    store = _store()
    store.upsert_transactions([
        _row("sp1", "2026-05-10", -3390, "Spotify"),
        _row("sp2", "2026-06-10", -3390, "Spotify"),
        _row("sp3", "2026-07-10", -3390, "Spotify"),
        _row("g1", "2026-05-05", -1200, "Google One"),
        _row("g2", "2026-06-05", -1200, "Google One"),
        _row("g3", "2026-07-05", -1200, "Google One"),
    ])
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert "ביטחון" in out
    # Spotify (bigger amount) must be listed before Google One (smaller amount)
    assert out.index("Spotify") < out.index("Google One")


def test_hebrew_category_labels_no_raw_english_enum():
    store = _store()
    store.upsert_transactions([
        _row("sp1", "2026-05-10", -3390, "Spotify"),
        _row("sp2", "2026-06-10", -3390, "Spotify"),
        _row("sp3", "2026-07-10", -3390, "Spotify"),
    ])
    store.add_rule("Spotify", "subscriptions")
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert "מנויים" in out
    assert "subscriptions" not in out


def test_empty_case_plain_hebrew_message():
    store = _store()
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert out.strip()
    assert "לא זוהו" in out
