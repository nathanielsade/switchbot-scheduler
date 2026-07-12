import tempfile, os
from datetime import datetime
from home_agent.finance import build_finance_tools
from home_agent.finance_store import FinanceStore
from finance_fakes import contract, make_fetch


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _frozen():
    return datetime(2026, 7, 12, 12, 0, 0)


def _seeded():
    store = _store()
    tools = build_finance_tools(store, now_fn=_frozen, fetch_fn=make_fetch(contract()))
    _tool(tools, "sync_finances").impl({})
    return store, tools


def test_financial_summary_income_expense_balance():
    store, tools = _seeded()
    out = _tool(tools, "financial_summary").impl({"from_date": "2026-07-01", "to_date": "2026-07-31"})
    assert "1,000.00" in out and "450.00" in out and "1,200.50" in out  # income, expense, balance ₪


def test_financial_summary_period_shortcut():
    store, tools = _seeded()
    out = _tool(tools, "financial_summary").impl({"period": "this_month"})
    assert "₪" in out


def test_find_transactions_absolute_amount():
    store, tools = _seeded()
    out = _tool(tools, "find_transactions").impl({"min_abs_agorot": 45000, "max_abs_agorot": 45000})
    assert "שופרסל" in out and "משכורת" not in out
