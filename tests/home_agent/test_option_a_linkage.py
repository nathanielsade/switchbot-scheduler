"""End-to-end sync -> exclusion linkage tests (pins C1: last-4 card matching).

Every Option-A summary test hardcodes matching account strings, so a bug in the card-number
match (exact `==` instead of last-4-digit normalization) slips through unnoticed. These tests
drive the REAL path: sync_finances (real normalize_contract + FinanceStore) -> financial_summary
(real _spendable_rows exclusion), so a format mismatch between the bank bill's captured digits
and the Max account string surfaces as a silent double-count.
"""
import os
import tempfile
from datetime import datetime

from home_agent.finance import build_finance_tools
from home_agent.finance_store import FinanceStore
from finance_fakes import make_fetch


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _frozen():
    return datetime(2026, 7, 12, 12, 0, 0)


def _discount_contract(**over):
    data = {
        "source": "discount", "scraped_at": "2026-07-12T18:00:00+03:00",
        "accounts": [{
            "account": "checking", "balance": "1000.00",
            "transactions": [
                {"identifier": "D1", "date": "2026-07-05T00:00:00.000Z", "processedDate": None,
                 "chargedAmount": "-4717.00", "chargedCurrency": "ILS",
                 "description": "חיוב לכרטיס ויזה 1743", "status": "completed"},
            ],
        }],
    }
    data.update(over)
    return data


def _max_contract(account, **over):
    data = {
        "source": "max", "scraped_at": "2026-07-12T18:00:00+03:00",
        "accounts": [{
            "account": account, "balance": "0.00",
            "transactions": [
                {"identifier": "M1", "date": "2026-07-03T00:00:00.000Z", "processedDate": None,
                 "chargedAmount": "-3000.00", "chargedCurrency": "ILS",
                 "description": "רמי לוי", "status": "completed"},
                {"identifier": "M2", "date": "2026-07-04T00:00:00.000Z", "processedDate": None,
                 "chargedAmount": "-1717.00", "chargedCurrency": "ILS",
                 "description": "דלק", "status": "completed"},
            ],
        }],
    }
    data.update(over)
    return data


def _sync_and_summarize(max_account):
    store = _store()
    disc = _discount_contract()
    mx = _max_contract(max_account)
    fetch_fns = {"discount": make_fetch(disc), "max": make_fetch(mx)}
    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns=fetch_fns)
    _tool(tools, "sync_finances").impl({})
    return _tool(tools, "financial_summary").impl({"from_date": "2026-07-01", "to_date": "2026-07-12"})


def test_linkage_plain_match_single_counts():
    """Test A: Max account is a bare '1743' — matches the bank bill's captured digits exactly."""
    out = _sync_and_summarize("1743")

    # Single-counted: itemized total only (-3000 - 1717 = -4717.00), bank bill's own -4,717.00
    # figure is excluded (it would otherwise ALSO show as -4,717.00, masking the double-count
    # in this particular case since sums coincide — the amount assertions below disambiguate
    # by checking the merchant lines are absent from a doubled total).
    assert "4,717.00" in out
    assert "9,434.00" not in out


def test_linkage_masked_account_pins_c1():
    """Test B (the C1 pin): Max account arrives masked/padded as '****1743'.

    Under the pre-fix exact-`==` match, `_card4`-less code compares "1743" (captured from the
    bank bill's description) against "****1743" (the raw Max account) and they never match:
    the covered-card bank bill is NOT dropped AND the Max rows are NOT dropped -> double-count
    (-4717 bank bill + -4717 itemized = -9434). This test MUST fail pre-fix and pass post-fix.
    """
    out = _sync_and_summarize("****1743")

    assert "4,717.00" in out
    assert "9,434.00" not in out
