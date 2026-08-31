"""Task D-1: proactive finance nudges — pure, deterministic message builders.

No OpenAI call, no network, no bot: these functions read from the FinanceStore and return a
plain Hebrew string (or None) that a scheduled job callback sends verbatim via the injectable
send_fn seam (see telegram_app.build_application). They deliberately REUSE finance.py's internal
helpers (_spendable_rows / _categorize / _period_range / _shekels) rather than re-deriving spend
logic, so a nudge can never diverge from what financial_summary/spending_by_category report —
see docs/superpowers/sdd/d-1-plan.md.
"""
from .finance import _CATEGORY_HE, _categorize, _period_range, _shekels, _spendable_rows

_TOP_CATEGORIES = 3


def _category_label(cat):
    return _CATEGORY_HE.get(cat, cat)


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
