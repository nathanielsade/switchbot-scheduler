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


def test_monthly_amount_is_the_median_not_mean_or_max():
    # A real subscription's charge drifts month to month; the reported monthly amount must be the
    # MEDIAN of the individual charges. Amounts chosen so median (33.00) != mean (34.67) != max (39.00),
    # so a regression to mean/max/first/last would change the rendered ₪ and fail here.
    store = _store()
    store.upsert_transactions([
        _row("v1", "2026-05-08", -3200, "SPOTIFY AAA"),
        _row("v2", "2026-06-09", -3300, "SPOTIFY BBB"),
        _row("v3", "2026-07-10", -3900, "SPOTIFY CCC"),
    ])
    store.add_rule("spotify", "subscriptions")
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert "33.00" in out          # median
    assert "39.00" not in out      # not the max / last
    assert "32.00" not in out      # not the min / first


def test_multiple_items_ordered_by_category_then_amount_with_correct_total():
    store = _store()
    store.upsert_transactions([
        _row("rent1", "2026-05-01", -530000, "משיכת שיק:11"),
        _row("rent2", "2026-06-01", -530000, "משיכת שיק:12"),
        _row("ap1", "2026-05-08", -3990, "APPLE.COM/BILL 1"),
        _row("ap2", "2026-06-08", -3990, "APPLE.COM/BILL 2"),
        _row("sp1", "2026-05-09", -3390, "SPOTIFY 1"),
        _row("sp2", "2026-06-09", -3390, "SPOTIFY 2"),
    ])
    store.add_rule("משיכת שיק", "rent")
    store.add_rule("apple.com", "subscriptions")
    store.add_rule("spotify", "subscriptions")
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    # Category order: rent (₪5,300 max) before subscriptions (₪39.90 max).
    # Match the section-label lines ("<label>:"), not the "מנויים" inside the header.
    assert out.index("שכירות:") < out.index("מנויים:")
    # Within subscriptions: apple (₪39.90) before spotify (₪33.90).
    assert out.index("apple.com") < out.index("spotify")
    # Total = 5300 + 39.90 + 33.90 = ₪5,373.80.
    assert "5,373.80" in out


def test_partial_coverage_flag_surfaced():
    # An uncovered card-bill line (no matching Max itemization) is kept at bank level → partial.
    # The tool must warn, because a card's itemized subscriptions are exactly what's then missing.
    store = _store()
    store.upsert_transactions([
        _row("sp1", "2026-05-08", -3390, "SPOTIFY 1"),
        _row("sp2", "2026-06-09", -3390, "SPOTIFY 2"),
        _row("sp3", "2026-07-10", -3390, "SPOTIFY 3"),
        _row("cb", "2026-07-05", -50000, "חיוב לכרטיס ויזה 6146"),  # uncovered → partial
    ])
    store.add_rule("spotify", "subscriptions")
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert "spotify" in out
    assert "פירוט הכרטיס אינו זמין" in out  # _PARTIAL_FLAG


def test_empty_case_plain_hebrew_message():
    store = _store()
    tools = build_finance_tools(store, now_fn=_frozen)
    out = _tool(tools, "list_recurring_commitments").impl({})
    assert out.strip()
    assert "לא זוהו" in out
