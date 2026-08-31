"""A pending card bill belongs to the month the bank will CHARGE it, not the month it is dated.

Live 2026-08: the accruing bill for card 6146 was dated 2026-08-06 but carries
processed_date 2026-09-01 — the bank's own statement of when it lands. Bucketing it by
txn_date put it in August, so August carried TWO 6146 bills (₪8,321.99 settled on 08-01 for
July's spending + ₪5,218.08 accruing for September) and read ₪5,218.08 too high. Every settled
bill in the live history has txn_date == processed_date, so only the pending accrual is affected.
"""
import os
import tempfile
from datetime import datetime

from home_agent.finance import _spendable_rows
from home_agent.finance_store import FinanceStore


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _bill(amount, txn_date, processed_date, *, status="pending", identifier=None, card="6146"):
    return {
        "source": "discount", "account": "0216686964", "identifier": identifier,
        "fingerprint": f"h:{card}:{txn_date}:{amount}:{status}",
        "txn_date": txn_date, "processed_date": processed_date,
        "amount_agorot": amount, "currency": "ILS",
        "description": f"חיוב לכרטיס ויזה {card}", "status": status, "raw_json": "{}",
    }


def _amounts(store, frm, to):
    rows, _ = _spendable_rows(store, frm, to)
    return sorted(r["amount_agorot"] for r in rows)


def test_pending_bill_is_excluded_from_the_month_it_is_dated():
    store = _store()
    store.upsert_transactions([_bill(-521808, "2026-08-06", "2026-09-01")])
    assert _amounts(store, "2026-08-01", "2026-08-31") == []


def test_pending_bill_lands_in_the_month_it_will_be_charged():
    store = _store()
    store.upsert_transactions([_bill(-521808, "2026-08-06", "2026-09-01")])
    assert _amounts(store, "2026-09-01", "2026-09-30") == [-521808]


def test_august_carries_only_the_bill_actually_charged_in_august():
    """The live shape: July's bill settles on 08-01, September's accrues dated 08-06."""
    store = _store()
    store.upsert_transactions([
        _bill(-832199, "2026-08-01", "2026-08-01", status="completed", identifier="206"),
        _bill(-521808, "2026-08-06", "2026-09-01"),
    ])
    assert _amounts(store, "2026-08-01", "2026-08-31") == [-832199]


def test_settled_bills_are_still_bucketed_by_txn_date():
    """Only PENDING bills move. A settled row stays where it is even if the bank's processed_date
    sits in a neighbouring month — it has already been charged."""
    store = _store()
    store.upsert_transactions([
        _bill(-157793, "2026-07-29", "2026-07-28", status="completed", identifier="199")])
    assert _amounts(store, "2026-07-01", "2026-07-31") == [-157793]


def test_non_card_pending_rows_are_not_moved():
    """The rule is about card bills, whose processed_date is a real charge date. An ordinary
    pending purchase keeps its own date."""
    store = _store()
    store.upsert_transactions([{
        "source": "discount", "account": "0216686964", "identifier": None, "fingerprint": "h:x",
        "txn_date": "2026-08-28", "processed_date": "2026-09-01", "amount_agorot": -9474,
        "currency": "ILS", "description": "טופ מרקט", "status": "pending", "raw_json": "{}"}])
    rows, _ = _spendable_rows(store, "2026-08-01", "2026-08-31")
    assert [r["amount_agorot"] for r in rows] == [-9474]


def test_pending_bill_without_processed_date_stays_put():
    store = _store()
    store.upsert_transactions([_bill(-521808, "2026-08-06", None)])
    assert _amounts(store, "2026-08-01", "2026-08-31") == [-521808]


def test_pending_bill_is_not_double_counted_across_adjacent_months():
    store = _store()
    store.upsert_transactions([_bill(-521808, "2026-08-06", "2026-09-01")])
    aug = _amounts(store, "2026-08-01", "2026-08-31")
    sep = _amounts(store, "2026-09-01", "2026-09-30")
    assert len(aug) + len(sep) == 1
