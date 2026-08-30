import os
import tempfile
from datetime import datetime

from home_agent.finance import build_finance_tools, _cash_flow_terms, _shekels
from home_agent.finance_store import FinanceStore


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


_counter = {"n": 0}


def _txn(desc, amount_agorot, txn_date, source="discount", account="1"):
    _counter["n"] += 1
    ident = f"t{_counter['n']}"
    return dict(source=source, account=account, identifier=ident, fingerprint=f"id:{ident}",
                txn_date=txn_date, processed_date=None, amount_agorot=amount_agorot,
                currency="ILS", description=desc, status="completed", raw_json="{}")


def _frozen_mid_july():
    # 2026-07-10: current month (July) is partial; last 3 FULL completed months are Apr/May/Jun.
    return datetime(2026, 7, 10, 12, 0, 0)


def _frozen_start_of_july():
    return datetime(2026, 7, 1, 9, 0, 0)


# Baseline window: two salaries folded into one "salary" line/month for simplicity, rent, subscriptions,
# and a committed savings deposit (transfer, negative) — every window month identical so the median
# equals any individual month's figure (keeps test 1 unambiguous; test 6 covers the actual median-vs-mean
# behavior with a real spike).
_SALARY = 2_000_000       # ₪20,000/mo
_RENT = 800_000           # ₪8,000/mo
_SUBSCRIPTIONS = 5_000    # ₪50/mo
_DEPOSIT = 400_000        # ₪4,000/mo (savings deposit, transfer, negative)


def _seed_window(store, months=("2026-04", "2026-05", "2026-06"), salary=_SALARY, rent=_RENT,
                  subscriptions=_SUBSCRIPTIONS, deposit=_DEPOSIT):
    rows = []
    for i, ym in enumerate(months):
        rows.append(_txn("משכורת", salary, f"{ym}-05"))
        rows.append(_txn(f"שכירות {i}", -rent, f"{ym}-01"))
        rows.append(_txn("נטפליקס", -subscriptions, f"{ym}-03"))
        rows.append(_txn("הפקדה לפיקדון", -deposit, f"{ym}-10"))
    store.upsert_transactions(rows)


def _seed_rules(store):
    store.add_rule("משכורת", "salary")
    store.add_rule("שכירות 0", "rent")
    store.add_rule("שכירות 1", "rent")
    store.add_rule("שכירות 2", "rent")
    store.add_rule("נטפליקס", "subscriptions")
    store.add_rule("פיקדון", "transfer")   # matches deposit AND withdrawal


def test_exact_terms_and_weekly_is_int():
    store = _store()
    _seed_rules(store)
    _seed_window(store)
    # this month (July, partial): groceries + one uncategorized negative -> both variable
    store.upsert_transactions([
        _txn("שופרסל", -300_000, "2026-07-05"),
        _txn("מסתורי", -20_000, "2026-07-08"),
    ])
    t = _cash_flow_terms(store, _frozen_mid_july)
    assert t["income_expected"] == _SALARY
    assert t["fixed_expected"] == _RENT + _SUBSCRIPTIONS + _DEPOSIT
    assert t["variable_spent"] == 300_000 + 20_000
    expected_safe = _SALARY - (_RENT + _SUBSCRIPTIONS + _DEPOSIT) - (300_000 + 20_000)
    assert t["safe_to_spend"] == expected_safe
    # July 10 -> 22 days left (incl. today) -> ceil(22/7) = 4 weeks
    assert t["weeks_left"] == 4
    assert t["weekly"] == expected_safe // 4
    assert isinstance(t["weekly"], int)


def test_savings_withdrawal_excluded_from_income_exact():
    store_a = _store()
    _seed_rules(store_a)
    _seed_window(store_a)
    t_a = _cash_flow_terms(store_a, _frozen_mid_july)

    store_b = _store()
    _seed_rules(store_b)
    _seed_window(store_b)
    # a big savings WITHDRAWAL (positive, transfer category) in a window month
    store_b.upsert_transactions([_txn("משיכה מפיקדון", 1_600_000, "2026-06-20")])
    t_b = _cash_flow_terms(store_b, _frozen_mid_july)

    assert t_b["income_expected"] == t_a["income_expected"]  # byte-for-byte identical, exact equality


def test_savings_deposit_counted_as_fixed_not_variable():
    store = _store()
    _seed_rules(store)
    _seed_window(store)
    baseline = _cash_flow_terms(store, _frozen_mid_july)
    assert baseline["fixed_categories"].get("transfer") == _DEPOSIT * 3

    # this month's OWN deposit must not leak into variable_spent
    store.upsert_transactions([_txn("הפקדה לפיקדון", -_DEPOSIT, "2026-07-10")])
    after = _cash_flow_terms(store, _frozen_mid_july)
    assert after["variable_spent"] == baseline["variable_spent"] == 0
    assert after["safe_to_spend"] == baseline["safe_to_spend"]


def test_rent_by_check_via_category_not_text():
    store = _store()
    # three window months, rent rows with DIFFERENT check-number descriptions, each explicitly
    # categorized "rent" via its own rule (the v1 recurrence-by-description bug this replaces).
    store.upsert_transactions([
        _txn("צ'ק 1001", -_RENT, "2026-04-01"),
        _txn("צ'ק 2002", -_RENT, "2026-05-01"),
        _txn("צ'ק 3003", -_RENT, "2026-06-01"),
    ])
    store.add_rule("צ'ק 1001", "rent")
    store.add_rule("צ'ק 2002", "rent")
    store.add_rule("צ'ק 3003", "rent")
    t = _cash_flow_terms(store, _frozen_mid_july)
    assert t["fixed_expected"] == _RENT
    assert t["fixed_categories"]["rent"] == _RENT * 3


def test_rent_check_skips_calendar_months_median_of_payments_not_undercounted():
    # Real-world bug: rent is paid by check ~once/month, but checks land on irregular dates that
    # can skip calendar months within the 3-month window (e.g. a check on the 31st then the next
    # on the 1st of the month-after-next -> the month in between gets NO check at all). Summing
    # each fixed category PER CALENDAR MONTH and taking the median of those monthly totals
    # undercounts rent whenever fewer than a "majority" of the 3 window months happen to contain a
    # check (here: only 1 of 3, since two consecutive months are skipped) -> the old code's
    # per-month median collapses to 0 for rent even though a real ~_RENT check landed in the
    # window. Fix: rent's contribution to fixed_expected is the median of the INDIVIDUAL
    # rent-payment amounts across the whole window (which month each lands in doesn't matter),
    # not the per-calendar-month sum's median.
    store = _store()
    _seed_rules(store)
    _seed_window(store, rent=0)  # keep subscriptions/deposit/salary baseline, no per-month rent here
    # Single rent check landing in month 3 (Jun) only — months 1 (Apr) and 2 (May) are both
    # skipped, exactly the "checks land on irregular dates that skip calendar months" pattern.
    store.upsert_transactions([_txn("צ'ק 9002", -_RENT, "2026-06-01")])
    store.add_rule("צ'ק 9002", "rent")
    t = _cash_flow_terms(store, _frozen_mid_july)
    # Typical single rent check (_RENT) — NOT undercounted to 0 by a per-calendar-month median
    # (Apr=0, May=0, Jun=_RENT -> median 0) that only "sees" whichever month the check landed in.
    assert t["fixed_categories"]["rent"] == _RENT  # transparency total across window unaffected
    assert t["fixed_expected"] == _SUBSCRIPTIONS + _DEPOSIT + _RENT


def test_uncategorized_positive_excluded_and_flagged():
    store_a = _store()
    _seed_rules(store_a)
    _seed_window(store_a)
    t_a = _cash_flow_terms(store_a, _frozen_mid_july)

    store_b = _store()
    _seed_rules(store_b)
    _seed_window(store_b)
    store_b.upsert_transactions([_txn("בונוס לא ידוע", 5_000_000, "2026-05-15")])  # no rule -> None
    t_b = _cash_flow_terms(store_b, _frozen_mid_july)

    assert t_b["income_expected"] == t_a["income_expected"]  # excluded, does not raise income
    assert t_b["uncategorized_income_agorot"] == 5_000_000

    tools = build_finance_tools(store_b, now_fn=_frozen_mid_july)
    out = _tool(tools, "cash_flow_status").impl({})
    assert "⚠" in out
    assert _shekels(5_000_000) in out


def test_median_resists_spike():
    store = _store()
    _seed_rules(store)
    store.upsert_transactions([
        _txn("משכורת", 1_000_000, "2026-04-05"),
        _txn("משכורת", 1_000_000, "2026-05-05"),
        _txn("משכורת", 50_000_000, "2026-06-05"),  # anomalous spike
    ])
    t = _cash_flow_terms(store, _frozen_mid_july)
    assert t["income_expected"] == 1_000_000  # median, not mean (~17.3M)


def test_trailing_window_boundary():
    store = _store()
    _seed_rules(store)
    _seed_window(store)  # Apr/May/Jun baseline salary = _SALARY each
    # current partial month (July) income must NOT count
    store.upsert_transactions([_txn("משכורת", 9_000_000, "2026-07-09")])
    # income >3 full months ago (March, the 4th month back) must NOT count either
    store.upsert_transactions([_txn("משכורת", 9_000_000, "2026-03-05")])
    t = _cash_flow_terms(store, _frozen_mid_july)
    assert t["income_expected"] == _SALARY


def test_uncovered_card_bill_counted_as_fixed_not_variable():
    store = _store()
    _seed_rules(store)
    _seed_window(store)
    _CARD_BILL = 800_000  # ~8,000 lump bill for an un-itemized ("6146") card
    # Un-itemized card-bill lines in each window month + this month. No record_coverage for
    # "6146" -> the card is NOT covered/itemized (per _spendable_rows Option A it's kept, not
    # excluded), so its bill must count as FIXED (committed), never as this month's variable spend.
    store.upsert_transactions([
        _txn("חיוב לכרטיס ויזה 6146", -_CARD_BILL, "2026-04-15"),
        _txn("חיוב לכרטיס ויזה 6146", -_CARD_BILL, "2026-05-15"),
        _txn("חיוב לכרטיס ויזה 6146", -_CARD_BILL, "2026-06-15"),
        _txn("חיוב לכרטיס ויזה 6146", -_CARD_BILL, "2026-07-05"),
    ])
    t = _cash_flow_terms(store, _frozen_mid_july)
    assert t["fixed_expected"] == _RENT + _SUBSCRIPTIONS + _DEPOSIT + _CARD_BILL
    assert t["fixed_categories"].get("card_bills") == _CARD_BILL * 3
    # this month's own card-bill lump must NOT leak into variable_spent
    assert t["variable_spent"] == 0


def test_covered_card_bill_excluded_from_fixed_no_double_count():
    store = _store()
    _seed_rules(store)
    _seed_window(store)
    _COVERED_CARD = "1743"      # Netanel's card: itemized on Max -> covered
    _UNCOVERED_CARD = "6146"    # Saray's card: no Max detail -> un-itemized
    _COVERED_BILL = 400_000     # bank-level lump bill for the covered card (must NOT count as fixed)
    _UNCOVERED_BILL = 800_000   # bank-level lump bill for the uncovered card (must count as fixed)
    _ITEMIZED_GROCERY = 150_000  # one itemized Max purchase behind the covered card's bill

    # record_coverage upserts on (source, account) — one row per account, like the real nightly
    # sync — so record ONE coverage span across the whole window (2026-04-01..2026-07-09), not a
    # separate call per month (which would just overwrite itself and only "cover" the last month).
    store.record_coverage("max", _COVERED_CARD, "2026-04-01", "2026-07-09", "2026-07-09T00:00:00")

    for ym in ("2026-04", "2026-05", "2026-06"):
        store.upsert_transactions([
            _txn(f"חיוב לכרטיס ויזה {_COVERED_CARD}", -_COVERED_BILL, f"{ym}-20",
                 source="discount", account="checking"),
            _txn("שופרסל", -_ITEMIZED_GROCERY, f"{ym}-12", source="max", account=_COVERED_CARD),
        ])
        # Uncovered card: only the bank bill line, no coverage recorded -> stays fixed.
        store.upsert_transactions([
            _txn(f"חיוב לכרטיס ויזה {_UNCOVERED_CARD}", -_UNCOVERED_BILL, f"{ym}-15",
                 source="discount", account="checking"),
        ])

    t = _cash_flow_terms(store, _frozen_mid_july)
    # fixed must include ONLY the uncovered card's bill, never the covered card's bank bill.
    assert t["fixed_categories"].get("card_bills") == _UNCOVERED_BILL * 3
    assert t["fixed_expected"] == _RENT + _SUBSCRIPTIONS + _DEPOSIT + _UNCOVERED_BILL


def test_start_of_month_and_overspent_no_clamp():
    store = _store()
    _seed_rules(store)
    _seed_window(store)
    t = _cash_flow_terms(store, _frozen_start_of_july)
    assert t["variable_spent"] == 0
    assert t["safe_to_spend"] == _SALARY - (_RENT + _SUBSCRIPTIONS + _DEPOSIT) == t["income_expected"] - t["fixed_expected"]

    # overspend this month beyond income - fixed -> negative, no clamping
    store.upsert_transactions([_txn("קניות מטורפות", -(_SALARY * 2), "2026-07-01")])
    t2 = _cash_flow_terms(store, _frozen_start_of_july)
    assert t2["safe_to_spend"] < 0
    assert t2["safe_to_spend"] == t["income_expected"] - t["fixed_expected"] - _SALARY * 2
