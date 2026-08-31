"""Savings must be reported separately from spending in the proactive nudges.

Money moved to a deposit or a gmal is not consumption — but it IS money the family can no
longer spend this month, so it still comes off the available amount. The nudge therefore states
all three parts explicitly: what was spent, what was saved, and what is left to spend (the same
figure cash_flow_status reports, reused rather than re-derived).
"""
import os
import tempfile
from datetime import datetime

from home_agent.finance_nudges import build_month_recap, build_weekly_summary
from home_agent.finance_store import FinanceStore


def _store():
    store = FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))
    store.add_rule("שופרסל", "groceries")
    store.add_rule("פיקדון", "transfer")
    store.add_rule("גמל", "transfer")
    store.add_rule("משכורת", "salary")
    return store


def _row(amount_agorot, description, txn_date):
    return {
        "source": "discount", "account": "checking", "identifier": None,
        "fingerprint": f"h:{description}:{amount_agorot}:{txn_date}",
        "txn_date": txn_date, "processed_date": None, "amount_agorot": amount_agorot,
        "currency": "ILS", "description": description, "status": "completed", "raw_json": "{}",
    }


def _seed_month(store, month):
    store.upsert_transactions([
        _row(-45000, "שופרסל", f"{month}-05"),
        _row(-400000, "הפקדה לפיקדון", f"{month}-01"),
        _row(-300000, "י.ל גמל לה חיוב", f"{month}-01"),
        _row(2000000, "משכורת", f"{month}-01"),
    ])


# --- month recap -------------------------------------------------------------------

def test_month_recap_separates_spent_from_saved():
    store = _store()
    _seed_month(store, "2026-07")
    text = build_month_recap(store, datetime(2026, 8, 30, 9, 0))

    assert "450.00" in text, "spend total should exclude the savings"
    assert "7,000.00" in text, "savings total should be stated"
    assert "חסכתם" in text
    assert "הוצאתם" in text
    # the old behaviour folded 7,000 into the spend line
    assert "7,450.00" not in text


def test_month_recap_omits_the_savings_line_when_nothing_was_saved():
    store = _store()
    store.upsert_transactions([_row(-45000, "שופרסל", "2026-07-05")])
    text = build_month_recap(store, datetime(2026, 8, 30, 9, 0))
    assert "450.00" in text
    assert "חסכתם" not in text


def test_month_recap_does_not_list_savings_as_a_spending_category():
    """It is called out on its own line — listing it again under categories double-shows it."""
    store = _store()
    _seed_month(store, "2026-07")
    text = build_month_recap(store, datetime(2026, 8, 30, 9, 0))
    body = text.split("\n", 1)[1] if "\n" in text else ""
    assert "העברות/חיסכון" not in body


# --- weekly summary ----------------------------------------------------------------

def test_weekly_summary_states_spent_saved_and_left():
    store = _store()
    for month in ("2026-05", "2026-06", "2026-07"):  # 3 full months feed the expectation
        _seed_month(store, month)
    _seed_month(store, "2026-08")
    text = build_weekly_summary(store, datetime(2026, 8, 30, 20, 0))

    assert "הוצאתם" in text
    assert "חסכתם" in text
    assert "7,000.00" in text
    assert "נשאר להוציא" in text, "the family asked to be told what is left for spending"


def test_weekly_summary_survives_a_store_too_thin_for_a_cash_flow_estimate():
    """A nudge must never crash or invent a number: with no history the 'left' line is omitted
    rather than guessed."""
    store = _store()
    store.upsert_transactions([_row(-45000, "שופרסל", "2026-08-05")])
    text = build_weekly_summary(store, datetime(2026, 8, 30, 20, 0))
    assert "450.00" in text
    assert text  # no exception, still a sendable message


# --- the 'left to spend' line must stay honest ---------------------------------------

def _seed_overspend(store):
    """3 full months of modest income, then a month that blows past it."""
    for month in ("2026-05", "2026-06", "2026-07"):
        store.upsert_transactions([
            _row(1000000, "משכורת", f"{month}-01"),
            _row(-500000, "שכר דירה", f"{month}-02"),
        ])
    store.upsert_transactions([_row(-2000000, "שופרסל", "2026-08-05")])
    store.add_rule("שכר דירה", "rent")
    return store


def test_overspend_is_phrased_as_an_overrun_not_a_negative_amount():
    """'נשאר להוציא: ₪-10,223.90' is not a sentence anyone should receive on a Sunday night."""
    text = build_weekly_summary(_seed_overspend(_store()), datetime(2026, 8, 30, 20, 0))
    assert "חרגתם" in text
    assert "נשאר להוציא" not in text
    assert "₪-" not in text, f"negative amount leaked into the nudge: {text}"


def test_overspend_line_omits_the_per_week_figure():
    """A weekly allowance of a negative number is meaningless."""
    text = build_weekly_summary(_seed_overspend(_store()), datetime(2026, 8, 30, 20, 0))
    assert "לשבוע" not in text


def test_left_to_spend_warns_when_income_is_being_understated():
    """cash_flow_status fail-safe EXCLUDES uncategorized credits from income, so the 'left'
    figure can read far too low. The nudge must say so rather than assert it flatly."""
    store = _seed_overspend(_store())
    store.upsert_transactions([_row(734755, "העברה מ-פלוני", "2026-06-15")])  # no rule -> uncategorized
    text = build_weekly_summary(store, datetime(2026, 8, 30, 20, 0))
    assert "ייתכן שההכנסה בפועל גבוהה יותר" in text
