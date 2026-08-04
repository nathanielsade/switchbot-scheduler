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


def _row(source, account, amount_agorot, description, txn_date, identifier=None):
    return {
        "source": source, "account": account, "identifier": identifier,
        "fingerprint": f"h:{source}:{account}:{description}:{amount_agorot}:{txn_date}",
        "txn_date": txn_date, "processed_date": None,
        "amount_agorot": amount_agorot, "currency": "ILS",
        "description": description, "status": "completed", "raw_json": "{}",
    }


def _forecast(store):
    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns={})
    return _tool(tools, "cash_flow_forecast").impl({})


def test_bank_card_bill_recurring_included_max_purchase_excluded():
    store = _store()
    store.upsert_transactions([
        # Recurring bank card-bill: same ~amount, same day-of-month, 3 distinct months.
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743", "2026-05-15"),
        _row("discount", "checking", -472000, "חיוב לכרטיס ויזה 1743", "2026-06-15"),
        _row("discount", "checking", -471500, "חיוב לכרטיס ויזה 1743", "2026-07-15"),
        # Recurring Max purchase (itemized) - already inside the bank card-bill above.
        _row("max", "1743", -30000, "רמי לוי", "2026-05-16", identifier="1743"),
        _row("max", "1743", -30200, "רמי לוי", "2026-06-16", identifier="1743"),
        _row("max", "1743", -29800, "רמי לוי", "2026-07-16", identifier="1743"),
    ])

    out = _forecast(store)

    assert "פריטים קבועים שזוהו" in out
    assert "חיוב לכרטיס ויזה 1743" in out
    assert "רמי לוי" not in out
