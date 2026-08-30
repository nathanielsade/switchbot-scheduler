"""Acceptance tests for Task D-1: proactive finance nudges (message builders).

Deterministic, LLM-free — pure functions over a seeded FinanceStore + frozen clock. No network,
no bot; see docs/superpowers/sdd/d-1-plan.md.
"""
import os
import tempfile
from datetime import datetime

from home_agent.finance_nudges import (
    build_charge_alert,
    build_month_recap,
    build_weekly_summary,
    find_new_large_charges,
)
from home_agent.finance_store import FinanceStore


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _row(source, account, amount_agorot, description, txn_date, identifier=None):
    return {
        "source": source, "account": account, "identifier": identifier,
        "fingerprint": f"h:{source}:{account}:{description}:{amount_agorot}:{txn_date}",
        "txn_date": txn_date, "processed_date": None,
        "amount_agorot": amount_agorot, "currency": "ILS",
        "description": description, "status": "completed", "raw_json": "{}",
    }


def _now(year=2026, month=8, day=30):
    return datetime(year, month, day, 9, 0, 0)


# --- 1. month recap builder --------------------------------------------------

def test_build_month_recap_totals_and_categories():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -45000, "שופרסל", "2026-07-05"),
        _row("discount", "checking", -12000, "שופרסל", "2026-07-20"),
        _row("discount", "checking", -30000, "פז", "2026-07-10"),
        _row("discount", "checking", 500000, "משכורת", "2026-07-01"),  # income, excluded
    ])
    store.add_rule("שופרסל", "groceries")
    store.add_rule("פז", "transport")

    text = build_month_recap(store, _now())  # "now" is Aug 30 -> last month = July

    assert text is not None
    assert "2026-07" in text
    assert "870.00" in text  # total expense = 450+120+300 = 870
    assert "groceries" in text
    assert "570.00" in text  # groceries category total (450 + 120)
    assert "transport" in text
    assert "300.00" in text  # transport category total


def test_build_month_recap_returns_none_when_no_data():
    store = _store()
    assert build_month_recap(store, _now()) is None


# --- 2. weekly summary builder ----------------------------------------------

def test_build_weekly_summary_month_to_date():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -20000, "שופרסל", "2026-08-05"),
        _row("discount", "checking", -10000, "שופרסל", "2026-08-15"),
        _row("discount", "checking", -5000, "פז", "2026-08-20"),
        _row("discount", "checking", -1000, "פז", "2026-09-01"),  # next month, excluded
    ])
    store.add_rule("שופרסל", "groceries")
    store.add_rule("פז", "transport")

    text = build_weekly_summary(store, _now())  # MTD through Aug 30

    assert "350.00" in text  # 200 + 100 + 50
    assert "groceries" in text
    assert "transport" in text


def test_build_weekly_summary_handles_no_spending_yet():
    store = _store()
    text = build_weekly_summary(store, _now())
    assert isinstance(text, str) and text.strip()
    assert "0.00" in text


# --- 3. unusual-charge detection --------------------------------------------

def test_find_new_large_charges_flags_only_big_ones_and_does_not_repeat():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -200000, "רהיטים", "2026-08-10"),  # ₪2,000 — big
        _row("discount", "checking", -5000, "שופרסל", "2026-08-11"),    # ₪50 — small
        _row("discount", "checking", -140000, "טיסה", "2026-08-12"),    # ₪1,400 — below ₪1,500 threshold
    ])

    threshold = 150000  # ₪1,500

    first = find_new_large_charges(store, threshold)
    assert len(first) == 1
    assert first[0]["description"] == "רהיטים"
    assert first[0]["amount_agorot"] == -200000

    # Running again with the same store: the fingerprint is now marked alerted -> nothing new.
    second = find_new_large_charges(store, threshold)
    assert second == []


def test_build_charge_alert_text():
    rows = [{"source": "discount", "account": "checking", "fingerprint": "x",
             "txn_date": "2026-08-10", "amount_agorot": -200000, "description": "רהיטים"}]
    text = build_charge_alert(rows)
    assert "2,000.00" in text
    assert "רהיטים" in text
