"""Protected-matcher regression tests (spec Testing P1/P3).

The card-bill exclusion in `_spendable_rows` is a hardcoded matcher (`_CARD_BILL_RE` +
`_is_card_payment`), NOT a user-facing category rule: it must be invisible to
list_category_rules/delete_category_rule, it must not be re-introducible (double-counted) by a
user rule that happens to match the bank bill's text, and syncing must never create rows in
`category_rules` for it.
"""
import os
import tempfile
from datetime import datetime

from home_agent.finance import build_finance_tools
from home_agent.finance_store import FinanceStore
from finance_fakes import contract, max_contract, make_fetch


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


def test_no_user_rules_card_bill_pattern_invisible_to_rule_tools():
    store = _store()
    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns={})

    out = _tool(tools, "list_category_rules").impl({})
    assert out == "אין כללי קטגוריה."
    assert "ויזה" not in out
    assert "חיוב" not in out

    # Deleting an arbitrary id (no rules exist) reports "not found" and never touches the
    # card-bill matcher (it isn't a row, so there's nothing here to accidentally re-enable).
    del_out = _tool(tools, "delete_category_rule").impl({"id": 1})
    assert "לא נמצא" in del_out

    store.upsert_transactions([
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743"),
        _row("max", "1743", -471700, "רמי לוי"),
    ])
    store.record_coverage("max", "1743", "2026-06-01", "2026-06-30", "2026-06-30T00:00:00")
    out = _tool(tools, "financial_summary").impl({"from_date": "2026-06-01", "to_date": "2026-06-30"})
    # card bill still excluded (single-counted): -471700 -> ₪4,717.00, NOT doubled to ₪9,434.00
    assert "4,717.00" in out
    assert "9,434.00" not in out


def test_longer_user_rule_matching_bank_bill_text_cannot_reintroduce_double_count():
    store = _store()
    # Bank bill amount is deliberately DIFFERENT from the itemized Max total so a stray "the
    # bill's figure leaked into the breakdown" bug can't hide behind a coincidental sum match.
    store.upsert_transactions([
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743"),
        _row("max", "1743", -300000, "רמי לוי"),
        _row("max", "1743", -150000, "דלק"),
    ])
    store.record_coverage("max", "1743", "2026-06-01", "2026-06-30", "2026-06-30T00:00:00")

    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns={})
    # A long, exact-text user rule that matches the bank bill's own description.
    _tool(tools, "set_category_rule").impl(
        {"merchant_pattern": "חיוב לכרטיס ויזה 1743", "category": "transfer"})

    breakdown = _tool(tools, "spending_by_category").impl(
        {"from_date": "2026-06-01", "to_date": "2026-06-30"})
    # The bank bill is dropped in _spendable_rows BEFORE _categorize ever sees it, so it can't
    # land in "transfer" (or any category) even though the rule text matches it verbatim.
    assert "transfer" not in breakdown
    assert "4,717.00" not in breakdown  # bank bill amount must not surface anywhere

    summary = _tool(tools, "financial_summary").impl(
        {"from_date": "2026-06-01", "to_date": "2026-06-30"})
    # Reconciliation holds: single-counted itemized total only (-300000-150000=-450000), never
    # the bank-bill figure and never a doubled total.
    assert "4,500.00" in summary
    assert "4,717.00" not in summary
    assert "9,217.00" not in summary


def test_sync_creates_no_category_rule_rows_for_card_bill_matcher():
    store = _store()
    fetch_fns = {"discount": make_fetch(contract()), "max": make_fetch(max_contract())}
    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns=fetch_fns)
    _tool(tools, "sync_finances").impl({})

    # The matcher is pure code (_CARD_BILL_RE / _is_card_payment), never persisted as data.
    assert store.active_rules() == []

    _tool(tools, "set_category_rule").impl({"merchant_pattern": "שופרסל", "category": "groceries"})
    rules = store.active_rules()
    assert len(rules) == 1
    assert rules[0]["merchant_pattern"] == "שופרסל"
