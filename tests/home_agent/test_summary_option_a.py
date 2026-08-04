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


def _row(source, account, amount_agorot, description, txn_date="2026-06-15", identifier=None):
    return {
        "source": source, "account": account, "identifier": identifier,
        "fingerprint": f"h:{source}:{account}:{description}:{amount_agorot}:{txn_date}",
        "txn_date": txn_date, "processed_date": None,
        "amount_agorot": amount_agorot, "currency": "ILS",
        "description": description, "status": "completed", "raw_json": "{}",
    }


def _summary(store, frm="2026-06-01", to="2026-06-30"):
    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns={})
    return _tool(tools, "financial_summary").impl({"from_date": frm, "to_date": to})


FLAG = "(פירוט הכרטיס אינו זמין לתקופה זו — מציג סכומים ברמת הבנק)"


def test_reconcile_covered_card_bill_excluded():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743"),
        _row("max", "1743", -300000, "רמי לוי"),
        _row("max", "1743", -171700, "דלק"),
        _row("discount", "checking", -10000, "שכירות"),
    ])
    store.record_coverage("max", "1743", "2026-06-01", "2026-06-30", "2026-06-30T00:00:00")

    out = _summary(store)

    assert FLAG not in out
    # expense == Max purchases (-471700) + non-card expense (-10000) = -481700 => ₪4,817.00
    assert "4,817.00" in out


def test_positive_credit_for_covered_card_excluded_from_income():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", 471700, "זיכוי לכרטיס ויזה 1743"),
        _row("discount", "checking", 100000, "משכורת"),
    ])
    store.record_coverage("max", "1743", "2026-06-01", "2026-06-30", "2026-06-30T00:00:00")

    out = _summary(store)

    # income should be only the salary (100000 -> ₪1,000.00), NOT +471700 credit
    assert "1,000.00" in out
    assert "5,717.00" not in out


def test_uncovered_card_bill_stays_counted_and_flag_appears():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -50000, "חיוב לכרטיס ויזה 6146"),
    ])
    # No coverage recorded for max/6146 at all.

    out = _summary(store)

    assert FLAG in out
    assert "500.00" in out


def test_coverage_window_gate_range_extends_past_coverage_end():
    store = _store()
    store.record_coverage("max", "1743", "2026-06-01", "2026-06-30", "2026-06-30T00:00:00")
    store.upsert_transactions([
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743", txn_date="2026-07-10"),
    ])

    out = _summary(store, frm="2026-06-01", to="2026-07-15")

    assert FLAG in out
    assert "4,717.00" in out


def test_symmetric_uncovered_range_drops_max_rows_keeps_bank_bill():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743"),
        _row("max", "1743", -300000, "רמי לוי"),
    ])
    # No coverage recorded for max/1743.

    out = _summary(store)

    assert FLAG in out
    assert "4,717.00" in out
    assert "3,000.00" not in out
