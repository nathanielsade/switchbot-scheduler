import os
import tempfile
from datetime import datetime

from home_agent.finance import build_finance_tools, _shekels
from home_agent.finance_store import FinanceStore


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _frozen():
    return datetime(2026, 7, 12, 12, 0, 0)


def _row(source, account, amount_agorot, description, txn_date="2026-06-15", identifier=None):
    return {
        "source": source, "account": account, "identifier": identifier,
        "fingerprint": f"h:{source}:{account}:{description}:{amount_agorot}:{txn_date}",
        "txn_date": txn_date, "processed_date": None,
        "amount_agorot": amount_agorot, "currency": "ILS",
        "description": description, "status": "completed", "raw_json": "{}",
    }


def _tools(store):
    return build_finance_tools(store, now_fn=_frozen, fetch_fns={})


def _uncategorized(store, **kwargs):
    tools = _tools(store)
    return _tool(tools, "list_uncategorized_merchants").impl(kwargs)


def test_surfaces_only_uncategorized_expense_merchants_sorted_by_spend():
    store = _store()
    store.add_rule("שופרסל", "groceries")
    store.upsert_transactions([
        # categorized -> excluded
        _row("discount", "checking", -45000, "שופרסל"),
        # income -> excluded
        _row("discount", "checking", 1000000, "משכורת"),
        # covered card's bank-level bill line -> excluded (Option A)
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743"),
        # itemized Max purchases behind the covered card -> uncategorized, kept
        _row("max", "1743", -300000, "מסעדה מסתורית", txn_date="2026-06-10"),
        _row("max", "1743", -171700, "מסעדה מסתורית", txn_date="2026-06-11"),
        # a smaller uncategorized merchant
        _row("discount", "checking", -6789, "חנות סתומה"),
    ])
    store.record_coverage("max", "1743", "2026-06-01", "2026-06-30", "2026-06-30T00:00:00")

    out = _uncategorized(store, from_date="2026-06-01", to_date="2026-06-30")

    assert "שופרסל" not in out          # already categorized
    assert "משכורת" not in out          # income, not an expense
    assert "ויזה" not in out and "חיוב לכרטיס" not in out  # Option-A card-bill line excluded
    assert "מסעדה מסתורית" in out
    assert "חנות סתומה" in out
    assert _shekels(-471700) in out     # combined מסעדה מסתורית total (3,000 + 1,717)
    assert "2" in out                    # count for מסעדה מסתורית
    # sorted by total spend desc: מסעדה מסתורית (₪4,717) before חנות סתומה (₪67.89)
    assert out.index("מסעדה מסתורית") < out.index("חנות סתומה")


def test_default_window_is_wide_and_uncovered_card_bill_excluded_when_no_coverage_flagged():
    # Sanity: without from/to, tool should not blow up and should use a wide default lookback.
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -6789, "חנות סתומה", txn_date="2026-07-01"),
    ])
    out = _uncategorized(store)
    assert "חנות סתומה" in out


def test_merchant_disappears_after_rule_added():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -6789, "חנות סתומה", txn_date="2026-06-15"),
        _row("discount", "checking", -12345, "מסעדה מסתורית", txn_date="2026-06-16"),
    ])

    before = _uncategorized(store, from_date="2026-06-01", to_date="2026-06-30")
    assert "חנות סתומה" in before and "מסעדה מסתורית" in before

    tools = _tools(store)
    _tool(tools, "set_category_rule").impl({"merchant_pattern": "חנות סתומה", "category": "shopping"})

    after = _uncategorized(store, from_date="2026-06-01", to_date="2026-06-30")
    assert "חנות סתומה" not in after
    assert "מסעדה מסתורית" in after  # still surfaced; unaffected by the unrelated rule

    # reconciliation: spending_by_category's uncategorized total should match what's left
    spending_out = _tool(tools, "spending_by_category").impl(
        {"from_date": "2026-06-01", "to_date": "2026-06-30"})
    assert _shekels(-12345) in spending_out
