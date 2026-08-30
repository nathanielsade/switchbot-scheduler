"""Task D-1: proactive finance nudges — pure, deterministic message builders.

No OpenAI call, no network, no bot: these functions read from the FinanceStore and return a
plain Hebrew string (or None) that a scheduled job callback sends verbatim via the injectable
send_fn seam (see telegram_app.build_application). They deliberately REUSE finance.py's internal
helpers (_spendable_rows / _categorize / _period_range / _shekels) rather than re-deriving spend
logic, so a nudge can never diverge from what financial_summary/spending_by_category report —
see docs/superpowers/sdd/d-1-plan.md.
"""
from .finance import CATEGORIES, _CARD_BILL_RE, _categorize, _norm_desc, _period_range, _shekels, _spendable_rows

_TOP_CATEGORIES = 3

# Hebrew labels for finance.CATEGORIES — the nudge messages are Hebrew end-to-end, so the raw
# English enum values (used internally by finance.py/category_rules) must never be printed
# verbatim here. Covers every value in CATEGORIES; uncategorized rows use "אחר" directly (see
# _category_breakdown), which is why it's included too.
_CATEGORY_HE = {
    "rent": "שכירות", "transport": "תחבורה", "groceries": "מכולת", "restaurants": "מסעדות",
    "subscriptions": "מנויים", "health": "בריאות", "shopping": "קניות", "utilities": "חשבונות",
    "transfer": "העברות/חיסכון", "salary": "הכנסה", "cash": "מזומן", "other": "אחר",
}
assert set(CATEGORIES) <= set(_CATEGORY_HE)


def _category_label(cat):
    return _CATEGORY_HE.get(cat, cat)


def _is_card_bill(description):
    """True iff description is a routine bank card-bill line (e.g. 'חיוב לכרטיס ויזה 1743') —
    those are monthly bills, never an "unusual charge"; reuses finance.py's own matcher so this
    can never diverge from how finance.py itself recognizes card bills."""
    return bool(_CARD_BILL_RE.search(_norm_desc(description)))


def _category_breakdown(rows, store):
    """expense-only rows -> {category_or_'אחר': total_agorot} (negative amounts)."""
    rules = store.active_rules()
    totals = {}
    for r in rows:
        if r["amount_agorot"] >= 0:
            continue  # expenses only
        cat = _categorize(r["description"], rules) or "אחר"
        totals[cat] = totals.get(cat, 0) + r["amount_agorot"]
    return totals


def build_month_recap(store, now):
    """Last full month's total spend + category breakdown, for the day-2 09:00 monthly job.
    Returns None when there's no expense data for last month (e.g. a brand-new install) —
    the caller must treat None as "nothing to send"."""
    frm, to = _period_range("last_month", now)
    rows, _partial = _spendable_rows(store, frm, to)
    totals = _category_breakdown(rows, store)
    if not totals:
        return None
    total = sum(totals.values())
    lines = [f"סיכום חודש שעבר ({frm[:7]}): הוצאתם {_shekels(-total)}"]
    for cat, amt in sorted(totals.items(), key=lambda kv: kv[1]):  # most negative (biggest spend) first
        lines.append(f"  {_category_label(cat)}: {_shekels(-amt)}")
    return "\n".join(lines)


def build_weekly_summary(store, now):
    """This month's spend so far (month-to-date) + top categories, for the Sunday-20:00 job.
    Always returns a non-empty string, even with zero spend so far this month."""
    frm, to = _period_range("this_month", now)
    rows, _partial = _spendable_rows(store, frm, to)
    totals = _category_breakdown(rows, store)
    total = sum(totals.values())
    lines = [f"החודש עד כה הוצאתם {_shekels(-total)}"]
    if totals:
        top = sorted(totals.items(), key=lambda kv: kv[1])[:_TOP_CATEGORIES]
        lines.append("קטגוריות מובילות: " + ", ".join(
            f"{_category_label(c)} {_shekels(-amt)}" for c, amt in top))
    return "\n".join(lines)


def find_new_large_charges(store, threshold_agorot):
    """Expense transactions at/beyond threshold_agorot (absolute value) not yet alerted on.
    Idempotent via FinanceStore's fingerprint-keyed finance_alerts_sent table (not a date
    cursor): as a side effect, any rows returned are immediately marked alerted, so a second
    call over the same data — e.g. the next nightly sync — returns nothing for them. This is
    the one place in this module with a store side effect; the callers (the nightly-sync job
    callback) rely on that to avoid re-alerting the same charge.

    Routine bank card-bill lines (e.g. "חיוב לכרטיס ויזה 1743") are excluded — those are monthly
    bills, never an "unusual charge" — and are deliberately left OUT of finance_alerts_sent (not
    marked), since they were never alert candidates in the first place."""
    rows = [r for r in store.unalerted_large(threshold_agorot) if not _is_card_bill(r["description"])]
    if rows:
        store.mark_alerted([r["fingerprint"] for r in rows])
    return rows


def build_charge_alert(rows):
    """Format one heads-up message listing every newly-detected large charge."""
    lines = ["חיוב חריג:"]
    for r in rows:
        lines.append(f"  {_shekels(-r['amount_agorot'])} ב{r['description']} ({r['txn_date']})")
    return "\n".join(lines)
