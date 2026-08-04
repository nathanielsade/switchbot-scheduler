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


def _spending(store, frm="2026-06-01", to="2026-06-30"):
    tools = _tools(store)
    return _tool(tools, "spending_by_category").impl({"from_date": frm, "to_date": to})


def _summary(store, frm="2026-06-01", to="2026-06-30"):
    tools = _tools(store)
    return _tool(tools, "financial_summary").impl({"from_date": frm, "to_date": to})


FLAG = "(פירוט הכרטיס אינו זמין לתקופה זו — מציג סכומים ברמת הבנק)"


def test_breakdown_omits_covered_card_bank_bill():
    store = _store()
    store.add_rule("רמי לוי", "groceries")
    store.upsert_transactions([
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743"),
        _row("max", "1743", -300000, "רמי לוי"),
        _row("max", "1743", -171700, "תחנת דלק לא ידועה"),
    ])
    store.record_coverage("max", "1743", "2026-06-01", "2026-06-30", "2026-06-30T00:00:00")

    out = _spending(store)

    assert FLAG not in out
    assert "groceries" in out
    assert "3,000.00" in out  # the Max groceries purchase
    # uncategorized total should include the gas station purchase amount
    assert "1,717.00" in out
    # the raw bank bill amount must not appear as its own line
    assert "4,717.00" not in out


def test_uncovered_card_bill_included_with_flag():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -50000, "חיוב לכרטיס ויזה 6146"),
    ])
    # No coverage recorded for max/6146.

    out = _spending(store)

    assert FLAG in out
    assert "500.00" in out


def test_uncategorized_line_has_summable_amount():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -12345, "מסעדה מסתורית"),
        _row("discount", "checking", -6789, "חנות סתומה"),
    ])

    out = _spending(store)

    total = _shekels(-12345 + -6789)
    assert total in out
    assert "2" in out  # count of uncategorized transactions
    assert "מסעדה מסתורית" in out
    assert "חנות סתומה" in out


def test_reconciles_with_financial_summary_for_covered_range():
    store = _store()
    store.add_rule("רמי לוי", "groceries")
    store.add_rule("פז", "transport")
    store.upsert_transactions([
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743"),
        _row("max", "1743", -300000, "רמי לוי"),
        _row("max", "1743", -100000, "פז תחנת דלק"),
        _row("max", "1743", -71700, "חנות סתומה"),
        _row("discount", "checking", -10000, "שכירות"),
    ])
    store.add_rule("שכירות", "rent")
    store.record_coverage("max", "1743", "2026-06-01", "2026-06-30", "2026-06-30T00:00:00")

    summary_out = _summary(store)
    spending_out = _spending(store)

    assert FLAG not in spending_out

    # Expected category + uncategorized totals (agorot, negative):
    groceries = -300000
    transport = -100000
    rent = -10000
    uncategorized = -71700
    expense_total = groceries + transport + rent + uncategorized  # == -481700

    assert _shekels(expense_total) in summary_out
    assert _shekels(groceries) in spending_out
    assert _shekels(transport) in spending_out
    assert _shekels(rent) in spending_out
    assert _shekels(uncategorized) in spending_out
