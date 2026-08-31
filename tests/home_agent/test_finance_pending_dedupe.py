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

from home_agent.finance import (_collapse_duplicate_pending_bills, _fingerprint,
                                 _spendable_rows, _supersede_settled_bills,
                                 build_finance_tools, run_finance_sync)
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
    bill = "חיוב לכרטיס ויזה 6146"
    p = _fingerprint("discount", "1", None, "2026-08-06", -1000, bill, status="pending")
    c = _fingerprint("discount", "1", None, "2026-08-06", -1000, bill, status="completed")
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
    """Rows already in the live DB were written with amount-bearing fingerprints. The collapse
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

    collapsed = _collapse_duplicate_pending_bills(store)

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
    assert _collapse_duplicate_pending_bills(store) == 1
    assert _collapse_duplicate_pending_bills(store) == 0


# --- the amount-free identity is for CARD BILLS only ----------------------------------

def test_ordinary_pending_purchases_keep_the_amount_in_their_identity():
    """Two same-day purchases at one merchant are two transactions. Collapsing them would
    under-report spend and leave no superseded row to audit — the opposite of the bug above."""
    a = _fingerprint("max", "1743", None, "2026-08-06", -4500, "ארומה", status="pending")
    b = _fingerprint("max", "1743", None, "2026-08-06", -12000, "ארומה", status="pending")
    assert a != b


def test_two_distinct_same_day_pending_purchases_survive_a_sync():
    store = _store()
    store.upsert_transactions([{
        "source": "discount", "account": "0216686964", "identifier": None,
        "fingerprint": _fingerprint("discount", "0216686964", None, "2026-08-06", amt,
                                    "ארומה", status="pending"),
        "txn_date": "2026-08-06", "processed_date": None, "amount_agorot": amt,
        "currency": "ILS", "description": "ארומה", "status": "pending", "raw_json": "{}",
    } for amt in (-4500, -12000)])
    _collapse_duplicate_pending_bills(store)
    kept = store.transactions_between("2026-08-01", "2026-08-31")
    assert sorted(r["amount_agorot"] for r in kept) == [-12000, -4500]


# --- a refund is not a settlement ------------------------------------------------------

def test_a_credit_does_not_retire_an_outstanding_bill():
    """זיכוי (refund) and חיוב (charge) both match _CARD_BILL_RE. A refund landing on the bill's
    charge date must NOT retire a bill that has not been paid."""
    store = _store()
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-5218.08", date="2026-08-06", processed="2026-09-01"))})
    store.upsert_transactions([{
        "source": "discount", "account": "0216686964", "identifier": "300",
        "fingerprint": "id:300", "txn_date": "2026-09-01", "processed_date": "2026-09-01",
        "amount_agorot": 3000, "currency": "ILS",
        "description": "זיכוי לכרטיס ויזה 6146", "status": "completed", "raw_json": "{}"}])
    assert _supersede_settled_bills(store) == 0
    sep, _ = _spendable_rows(store, "2026-09-01", "2026-09-30")
    assert -521808 in [r["amount_agorot"] for r in sep]


# --- the bank moves a charge off a weekend/holiday -------------------------------------

def test_a_charge_that_slips_a_day_still_retires_its_pending_bill():
    """If the exact-date match missed, _effective_date would put the un-retired pending row in
    the SAME month as the real charge — one bill counted twice inside one month."""
    store = _store()
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-5218.08", date="2026-08-06", processed="2026-09-01"))})
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-5300.00", date="2026-09-02", processed="2026-09-02",
                       status="completed", identifier="301"))})
    sep, _ = _spendable_rows(store, "2026-09-01", "2026-09-30")
    assert [r["amount_agorot"] for r in sep] == [-530000]


def test_a_charge_far_from_the_due_date_does_not_retire_the_bill():
    store = _store()
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-5218.08", date="2026-08-06", processed="2026-09-01"))})
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-5300.00", date="2026-09-20", processed="2026-09-20",
                       status="completed", identifier="302"))})
    rows, _ = _spendable_rows(store, "2026-09-01", "2026-09-30")
    assert sorted(r["amount_agorot"] for r in rows) == [-530000, -521808]


# --- multiple cards settling in one pass ------------------------------------------------

def test_two_cards_settling_together_both_retire():
    store = _store()
    for card in ("6146", "1743"):
        run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
            _bill_contract("-5000.00", date="2026-07-03", processed="2026-08-01", card=card))})
    for i, card in enumerate(("6146", "1743")):
        run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
            _bill_contract("-5100.00", date="2026-08-01", processed="2026-08-01", card=card,
                           status="completed", identifier=f"40{i}"))})
    assert store.transactions_between("2026-07-01", "2026-07-31") == []


def test_a_small_mid_cycle_charge_does_not_retire_the_monthly_bill():
    """The bank uses the same 'חיוב לכרטיס ויזה NNNN' wording for small mid-cycle charges. One
    landing near the bill's due date must not retire it — found on live data, where a ₪51.90
    charge dated 08-29 sat inside the date window of the ₪5,130.18 bill due 09-01."""
    store = _store()
    run_finance_sync(store=store, fetch_fns={"discount": make_fetch(
        _bill_contract("-5130.18", date="2026-08-09", processed="2026-09-01", card="1743"))})
    store.upsert_transactions([{
        "source": "discount", "account": "0216686964", "identifier": "500",
        "fingerprint": "id:500", "txn_date": "2026-08-29", "processed_date": "2026-08-29",
        "amount_agorot": -5190, "currency": "ILS",
        "description": "חיוב לכרטיס ויזה 1743", "status": "completed", "raw_json": "{}"}])
    assert _supersede_settled_bills(store) == 0
    rows, _ = _spendable_rows(store, "2026-09-01", "2026-09-30")
    assert [r["amount_agorot"] for r in rows] == [-513018]
