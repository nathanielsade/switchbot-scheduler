"""Regression tests for the accruing-pending-bill duplicate bug (live 2026-08-31).

A pending card bill has no bank `identifier` and its amount GROWS every night as purchases
accrue. The original `_fingerprint` hashed the amount, so each nightly sync minted a fresh
fingerprint and INSERTed a new row: one ~₪5,218 bill was booked six times (₪24,278 phantom),
which is how Menashe reported ₪55,860 for August. Two fixes are covered here:
  1. a pending row's fingerprint must not depend on its (provisional) amount;
  2. when the bill settles as a `completed` row, its stale pending row must be superseded.
"""
import os
import tempfile

from home_agent.finance import _fingerprint, build_finance_tools, run_finance_sync
from home_agent.finance_store import FinanceStore
from finance_fakes import make_fetch


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _bill_contract(amount, *, status="pending", identifier=None,
                   date="2026-08-06", processed="2026-09-01", card="6146"):
    return {
        "source": "discount", "scraped_at": "2026-08-31T02:00:00+03:00",
        "accounts": [{"account": "0216686964", "balance": "0.00", "transactions": [
            {"identifier": identifier, "date": f"{date}T00:00:00.000Z",
             "processedDate": f"{processed}T00:00:00.000Z", "chargedAmount": amount,
             "chargedCurrency": "ILS", "description": f"חיוב לכרטיס ויזה {card}",
             "status": status},
        ]}],
    }


# --- fix 1: fingerprint ---------------------------------------------------------------

def test_pending_fingerprint_ignores_the_provisional_amount():
    """The accruing bill: same pending txn, amount grew overnight -> SAME fingerprint."""
    a = _fingerprint("discount", "0216686964", None, "2026-08-06", -467000,
                     "חיוב לכרטיס ויזה 6146", status="pending")
    b = _fingerprint("discount", "0216686964", None, "2026-08-06", -521808,
                     "חיוב לכרטיס ויזה 6146", status="pending")
    assert a == b


def test_completed_fingerprint_still_distinguishes_by_amount():
    """A settled row's amount is final, so it stays part of the identity (two same-day,
    same-merchant purchases of different amounts are genuinely two transactions)."""
    a = _fingerprint("discount", "1", None, "2026-08-06", -1000, "טופ מרקט", status="completed")
    b = _fingerprint("discount", "1", None, "2026-08-06", -2000, "טופ מרקט", status="completed")
    assert a != b


def test_pending_and_completed_fingerprints_do_not_collide():
    p = _fingerprint("discount", "1", None, "2026-08-06", -1000, "x", status="pending")
    c = _fingerprint("discount", "1", None, "2026-08-06", -1000, "x", status="completed")
    assert p != c


def test_accruing_pending_bill_upserts_instead_of_duplicating():
    """End-to-end: six nightly syncs of the same growing bill -> ONE row at the latest amount."""
    store = _store()
    for amount in ("-4670.00", "-4706.00", "-4756.00", "-4985.80", "-5160.60", "-5218.08"):
        run_finance_sync(store=store, fetch_fns={"discount": make_fetch(_bill_contract(amount))})
    rows = store.transactions_between("2026-08-01", "2026-08-31")
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    assert rows[0]["amount_agorot"] == -521808
    assert sum(r["amount_agorot"] for r in rows) == -521808


# --- fix 2: pending -> completed reconciliation ---------------------------------------

def test_settled_bill_supersedes_its_stale_pending_row():
    """The pending bill dated 07-03 (processed 08-01) settles as a completed row dated 08-01.
    Different txn_date and a different final amount, so it cannot upsert onto the pending row —
    the pending one must be retired or the bill is counted in BOTH July and August."""
    store = _store()
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-7009.19", date="2026-07-03", processed="2026-08-01"))})
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-8321.99", date="2026-08-01", processed="2026-08-01",
                       status="completed", identifier="206"))})

    july = store.transactions_between("2026-07-01", "2026-07-31")
    assert july == [], f"stale pending bill still inflates July: {july}"
    august = store.transactions_between("2026-08-01", "2026-08-31")
    assert [r["amount_agorot"] for r in august] == [-832199]


def test_unsettled_pending_bill_is_kept():
    """Only a bill that actually settled gets superseded — a still-outstanding one stays."""
    store = _store()
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-5218.08", date="2026-08-06", processed="2026-09-01"))})
    rows = store.transactions_between("2026-08-01", "2026-08-31")
    assert [r["amount_agorot"] for r in rows] == [-521808]


def test_settlement_does_not_supersede_a_different_card():
    """Card 1743's settlement must not retire card 6146's outstanding pending bill."""
    store = _store()
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-5000.00", date="2026-07-03", processed="2026-08-01", card="6146"))})
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-1234.00", date="2026-08-01", processed="2026-08-01", card="1743",
                       status="completed", identifier="207"))})
    july = store.transactions_between("2026-07-01", "2026-07-31")
    assert [r["amount_agorot"] for r in july] == [-500000]


# --- superseded rows are invisible to every read path ---------------------------------

def test_superseded_rows_excluded_from_search_and_sums():
    store = _store()
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-7009.19", date="2026-07-03", processed="2026-08-01"))})
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-8321.99", date="2026-08-01", processed="2026-08-01",
                       status="completed", identifier="206"))})

    assert store.search(query="ויזה", from_date="2026-07-01", to_date="2026-07-31") == []
    income, expense = store.sum_amounts("2026-07-01", "2026-07-31")
    assert (income, expense) == (0, 0)


# --- fix 3: backfill of rows written under the old scheme ------------------------------

def test_backfill_collapses_legacy_duplicate_pending_rows():
    """Rows already in the live DB were written with amount-bearing fingerprints. The backfill
    keeps the newest of each duplicate group and supersedes the rest."""
    store = _store()
    for amount in ("-4670.00", "-4706.00", "-5218.08"):
        # simulate the OLD behaviour: a distinct fingerprint per amount
        store.upsert_transactions([{
            "source": "discount", "account": "0216686964", "identifier": None,
            "fingerprint": f"h:legacy{amount}", "txn_date": "2026-08-06",
            "processed_date": "2026-09-01", "amount_agorot": int(float(amount) * 100),
            "currency": "ILS", "description": "חיוב לכרטיס ויזה 6146",
            "status": "pending", "raw_json": "{}"}])

    collapsed = store.backfill_pending_duplicates()

    assert collapsed == 2
    rows = store.transactions_between("2026-08-01", "2026-08-31")
    assert [r["amount_agorot"] for r in rows] == [-521808]


def test_backfill_is_idempotent():
    store = _store()
    for amount in ("-4670.00", "-5218.08"):
        store.upsert_transactions([{
            "source": "discount", "account": "0216686964", "identifier": None,
            "fingerprint": f"h:legacy{amount}", "txn_date": "2026-08-06",
            "processed_date": "2026-09-01", "amount_agorot": int(float(amount) * 100),
            "currency": "ILS", "description": "חיוב לכרטיס ויזה 6146",
            "status": "pending", "raw_json": "{}"}])
    assert store.backfill_pending_duplicates() == 1
    assert store.backfill_pending_duplicates() == 0
