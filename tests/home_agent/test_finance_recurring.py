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


def _row(i, d, amt, desc, account="1"):
    return dict(source="discount", account=account, identifier=i, fingerprint=f"id:{i}",
                txn_date=d, processed_date=None, amount_agorot=amt, currency="ILS",
                description=desc, status="completed", raw_json="{}")


def test_spotify_varying_descriptions_caught_as_one_item():
    # Real-world Spotify billing: a different charge code every month. Exact-text grouping
    # (the old design) would treat these as 3 separate one-offs and miss them entirely.
    store = _store()
    store.upsert_transactions([
        _row("sp1", "2026-05-08", -3390, "SPOTIFY P3D38A9A90"),
        _row("sp2", "2026-06-09", -3390, "SPOTIFY P3E3701264"),
        _row("sp3", "2026-07-10", -3390, "SPOTIFY P3F1122334"),
    ])
    store.add_rule("spotify", "subscriptions")
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    # Listed once, at the stable rule-name identifier "spotify", not the raw varying descriptions.
    assert out.count("spotify") == 1
    assert "33.90" in out
    assert "מנויים" in out


def test_rent_by_varying_numbered_checks_caught():
    store = _store()
    store.upsert_transactions([
        _row("r1", "2026-05-01", -530000, "משיכת שיק:0001"),
        _row("r2", "2026-06-03", -530000, "משיכת שיק:0002"),
        _row("r3", "2026-07-02", -530000, "משיכת שיק:0003"),
    ])
    store.add_rule("משיכת שיק", "rent")
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert out.count("משיכת שיק") == 1
    assert "5,300.00" in out
    assert "שכירות" in out


def test_savings_listed_income_and_one_off_excluded():
    store = _store()
    store.upsert_transactions([
        # recurring transfer (savings) outflow -> included
        _row("t1", "2026-05-15", -80000, "הפקדה לחיסכון 001"),
        _row("t2", "2026-06-15", -80000, "הפקדה לחיסכון 002"),
        _row("t3", "2026-07-15", -80000, "הפקדה לחיסכון 003"),
        # recurring salary (income, sign +) -> excluded even though it's regular
        _row("s1", "2026-05-10", 1000000, "משכורת"),
        _row("s2", "2026-06-10", 1000000, "משכורת"),
        _row("s3", "2026-07-10", 1000000, "משכורת"),
    ])
    store.add_rule("הפקדה לחיסכון", "transfer")
    store.add_rule("משכורת", "salary")
    # a one-off large discretionary purchase, uncategorized/non-committed -> excluded
    store.upsert_transactions([_row("o1", "2026-06-20", -500000, "רהיטים לבית")])
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert "הפקדה לחיסכון" in out
    assert "העברות/חיסכון" in out
    assert "משכורת" not in out
    assert "רהיטים לבית" not in out


def test_hebrew_category_labels_digit_free_prompt_stays_green():
    store = _store()
    store.upsert_transactions([
        _row("sp1", "2026-05-08", -3390, "SPOTIFY P3D38A9A90"),
        _row("sp2", "2026-06-09", -3390, "SPOTIFY P3E3701264"),
        _row("sp3", "2026-07-10", -3390, "SPOTIFY P3F1122334"),
    ])
    store.add_rule("spotify", "subscriptions")
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert "מנויים" in out
    assert "subscriptions" not in out
    assert "חודשים" in out


def test_empty_case_plain_hebrew_message():
    store = _store()
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert out.strip()
    assert "לא זוהו" in out
