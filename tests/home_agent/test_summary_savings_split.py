"""financial_summary must split savings out of spending, exactly as the nudges do.

Otherwise one month has two answers: the nudge says "הוצאתם ₪19,659.06" while asking Menashe
"כמה הוצאנו החודש?" answers ₪26,661.77 for the same period — the ₪7,002.71 difference being
deposits and gmal transfers. Same rows, same rules, one number.
"""
import os
import tempfile
from datetime import datetime

from home_agent.finance import build_finance_tools
from home_agent.finance_store import FinanceStore


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _store():
    store = FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))
    store.add_rule("שופרסל", "groceries")
    store.add_rule("פיקדון", "transfer")
    return store


def _row(amount_agorot, description, txn_date):
    return {
        "source": "discount", "account": "checking", "identifier": None,
        "fingerprint": f"h:{description}:{amount_agorot}:{txn_date}", "txn_date": txn_date,
        "processed_date": None, "amount_agorot": amount_agorot, "currency": "ILS",
        "description": description, "status": "completed", "raw_json": "{}",
    }


def _summary(store, **args):
    tools = build_finance_tools(store, now_fn=lambda: datetime(2026, 8, 30, 12, 0))
    return _tool(tools, "financial_summary").impl(args)


def _line(out, prefix):
    return next(ln for ln in out.split("\n") if ln.startswith(prefix))


def test_summary_reports_savings_separately_from_spending():
    store = _store()
    store.upsert_transactions([
        _row(-45000, "שופרסל", "2026-08-05"),
        _row(-400000, "הפקדה לפיקדון", "2026-08-01"),
    ])
    out = _summary(store, period="this_month")
    assert "450.00" in _line(out, "הוצאות")
    assert "4,450.00" not in _line(out, "הוצאות"), "savings must not be folded into spending"
    assert "4,000.00" in _line(out, "חיסכון")


def test_summary_omits_the_savings_line_when_nothing_was_saved():
    store = _store()
    store.upsert_transactions([_row(-45000, "שופרסל", "2026-08-05")])
    out = _summary(store, period="this_month")
    assert "450.00" in _line(out, "הוצאות")
    assert "חיסכון" not in out


def test_summary_net_still_subtracts_savings():
    """Savings leave the account, so net must not improve by pulling them out of expenses."""
    store = _store()
    store.upsert_transactions([
        _row(1000000, "משכורת", "2026-08-01"),
        _row(-45000, "שופרסל", "2026-08-05"),
        _row(-400000, "הפקדה לפיקדון", "2026-08-01"),
    ])
    out = _summary(store, period="this_month")
    assert "5,550.00" in _line(out, "נטו")  # 10,000 - 450 - 4,000
