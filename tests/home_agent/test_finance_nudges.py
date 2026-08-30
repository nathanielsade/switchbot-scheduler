"""Acceptance tests for Task D-1: proactive finance nudges (message builders).

Deterministic, LLM-free — pure functions over a seeded FinanceStore + frozen clock. No network,
no bot; see docs/superpowers/sdd/d-1-plan.md.
"""
import os
import tempfile
from datetime import datetime

from home_agent.finance_nudges import build_month_recap, build_weekly_summary
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
    assert "מכולת" in text  # groceries, Hebrew label
    assert "570.00" in text  # groceries category total (450 + 120)
    assert "תחבורה" in text  # transport, Hebrew label
    assert "300.00" in text  # transport category total
    # Raw English enum values must never leak into the Hebrew message.
    assert "groceries" not in text
    assert "transport" not in text


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
    assert "מכולת" in text  # groceries, Hebrew label
    assert "תחבורה" in text  # transport, Hebrew label
    # Raw English enum values must never leak into the Hebrew message.
    assert "groceries" not in text
    assert "transport" not in text


def test_build_weekly_summary_handles_no_spending_yet():
    store = _store()
    text = build_weekly_summary(store, _now())
    assert isinstance(text, str) and text.strip()
    assert "0.00" in text
