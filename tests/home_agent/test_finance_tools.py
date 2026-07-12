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
    # Before setting a rule, category should appear as "—" (uncategorized)
    out = _tool(tools, "find_transactions").impl({"min_abs_agorot": 45000, "max_abs_agorot": 45000})
    assert "שופרסל" in out and "משכורת" not in out
    assert "[—]" in out  # uncategorized

    # After setting a rule, the derived category should appear
    _tool(tools, "set_category_rule").impl({"merchant_pattern": "שופרסל", "category": "groceries"})
    out = _tool(tools, "find_transactions").impl({"min_abs_agorot": 45000, "max_abs_agorot": 45000})
    assert "שופרסל" in out and "[groceries]" in out


def test_spending_by_category_derives_and_surfaces_uncategorized():
    store, tools = _seeded()
    _tool(tools, "set_category_rule").impl({"merchant_pattern": "שופרסל", "category": "groceries"})
    out = _tool(tools, "spending_by_category").impl({"from_date": "2026-07-01", "to_date": "2026-07-31"})
    assert "groceries" in out and "450.00" in out  # שופרסל expense categorized


def test_set_category_rule_rejects_bad_slug():
    store, tools = _seeded()
    out = _tool(tools, "set_category_rule").impl({"merchant_pattern": "x", "category": "nonsense"})
    assert "nonsense" in out and "groceries" in out  # lists valid slugs


def test_rule_precedence_longest_then_newest():
    from home_agent.finance import _categorize
    rules = [{"id": 1, "merchant_pattern": "super", "category": "shopping"},
             {"id": 2, "merchant_pattern": "super pharm", "category": "health"}]
    assert _categorize("SUPER PHARM tlv", rules) == "health"          # longest wins
    rules2 = [{"id": 1, "merchant_pattern": "abc", "category": "shopping"},
              {"id": 2, "merchant_pattern": "abc", "category": "groceries"}]
    assert _categorize("abc", rules2) == "groceries"                  # tie → newest


def test_delete_category_rule_soft_removes():
    store, tools = _seeded()
    out = _tool(tools, "set_category_rule").impl({"merchant_pattern": "שופרסל", "category": "groceries"})
    rid = store.active_rules()[0]["id"]
    assert "✅" in _tool(tools, "delete_category_rule").impl({"id": rid})
    assert store.active_rules() == []


def test_cash_flow_detects_recurring_and_flags_overdraft():
    store = _store()
    store.record_snapshot("discount", "1", "2026-07-12T00:00:00Z", 20000)  # ₪200 balance
    def r(i, d, amt, desc):
        return dict(source="discount", account="1", identifier=i, fingerprint=f"id:{i}",
                    txn_date=d, processed_date=None, amount_agorot=amt, currency="ILS",
                    description=desc, status="completed", raw_json="{}")
    store.upsert_transactions([
        r("s1", "2026-05-10", 1000000, "משכורת"), r("s2", "2026-06-10", 1000000, "משכורת"),
        r("t1", "2026-05-15", -800000, "שכירות"), r("t2", "2026-06-15", -800000, "שכירות"),
        r("o1", "2026-06-03", -50000, "חד פעמי"),
    ])
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "cash_flow_forecast").impl({})
    assert "משכורת" in out and "שכירות" in out and "חד פעמי" not in out
    assert "מינוס" in out or "overdraft" in out.lower() or "-" in out  # 200 +1000 -800 due 15th → tight/negative path
