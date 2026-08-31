"""Task D-1: proactive finance nudges — pure, deterministic message builders.

No OpenAI call, no network, no bot: these functions read from the FinanceStore and return a
plain Hebrew string (or None) that a scheduled job callback sends verbatim via the injectable
send_fn seam (see telegram_app.build_application). They deliberately REUSE finance.py's internal
helpers (_spendable_rows / _categorize / _period_range / _shekels / _cash_flow_terms) rather than
re-deriving spend logic, so a nudge can never diverge from what financial_summary /
spending_by_category / cash_flow_status report — see docs/superpowers/sdd/d-1-plan.md.

Savings are reported SEPARATELY from spending. A deposit or gmal transfer is not consumption, so
folding it into "you spent X" overstates the number (live 2026-08: ₪7,002.71 of savings inside a
reported ₪31,582.49). It is still money that cannot be spent this month, so it stays deducted
from what is left — the nudge just names all three parts: spent, saved, and left to spend.
"""
import logging

from .finance import (_CATEGORY_HE, _cash_flow_terms, _categorize, _period_range, _shekels,
                      _spendable_rows, TRANSFER_CATEGORY)

log = logging.getLogger("home_agent")

_TOP_CATEGORIES = 3


def _category_label(cat):
    return _CATEGORY_HE.get(cat, cat)


def _split_spend_and_savings(rows, store):
    """expense rows -> ({category: total_agorot} for real spending, saved_agorot).

    Savings is the same notion cash_flow_status uses for a committed outflow: a NEGATIVE row in
    the transfer category (deposit/gmal). Amounts stay negative in the breakdown, as the callers'
    sort order expects; `saved` is returned positive.
    """
    rules = store.active_rules()
    totals = {}
    saved = 0
    for r in rows:
        if r["amount_agorot"] >= 0:
            continue  # expenses only
        cat = _categorize(r["description"], rules)
        if cat == TRANSFER_CATEGORY:
            saved += -r["amount_agorot"]
            continue
        key = cat or "אחר"
        totals[key] = totals.get(key, 0) + r["amount_agorot"]
    return totals, saved


def _spent_and_saved_line(prefix, totals, saved):
    total = sum(totals.values())
    line = f"{prefix} הוצאתם {_shekels(-total)}"
    if saved:
        line += f", חסכתם {_shekels(saved)}"
    return line


def _safe_to_spend_line(store, now):
    """The 'what's left' figure, taken verbatim from cash_flow_status so the two never disagree.

    Phrasing matters here in a way it doesn't for a tool the model can caveat: a nudge arrives
    unprompted and is read at face value. So an overrun is stated AS an overrun rather than as a
    negative "left to spend" (and without a per-week figure, which is meaningless once negative),
    and when the fail-safe has excluded uncategorized credits from income — which pushes the
    figure DOWN, live 2026-08 by ₪7,347.55 over the window — the line says the real income may be
    higher. Returns None when there's no basis for an estimate at all.
    """
    try:
        t = _cash_flow_terms(store, lambda: now)
    except Exception as e:  # a nudge must never crash the bot over a missing estimate
        log.warning("nudge: cash-flow estimate unavailable: %s", e)
        return None
    if not t.get("income_expected"):
        return None  # no income history -> "left to spend" would be a guess
    safe = t["safe_to_spend"]
    if safe < 0:
        line = f"חרגתם מהמסגרת החודשית ב-{_shekels(-safe)}"
    else:
        line = f"נשאר להוציא החודש: {_shekels(safe)} (~{_shekels(t['weekly'])} לשבוע)"
    if t.get("uncategorized_income_agorot"):
        line += " — ייתכן שההכנסה בפועל גבוהה יותר (יש זיכויים ללא קטגוריה שלא נספרו)"
    return line


def build_month_recap(store, now):
    """Last full month's spend + savings + category breakdown, for the day-2 09:00 monthly job.
    Returns None when there's no expense data for last month (e.g. a brand-new install) —
    the caller must treat None as "nothing to send"."""
    frm, to = _period_range("last_month", now)
    rows, _partial = _spendable_rows(store, frm, to)
    totals, saved = _split_spend_and_savings(rows, store)
    if not totals and not saved:
        return None
    lines = [_spent_and_saved_line(f"סיכום חודש שעבר ({frm[:7]}):", totals, saved)]
    for cat, amt in sorted(totals.items(), key=lambda kv: kv[1]):  # biggest spend first
        lines.append(f"  {_category_label(cat)}: {_shekels(-amt)}")
    return "\n".join(lines)


def build_weekly_summary(store, now):
    """This month's spend + savings so far (month-to-date), what's left to spend, and the top
    categories, for the Sunday-20:00 job. Always returns a non-empty string, even with zero
    spend so far this month."""
    frm, to = _period_range("this_month", now)
    rows, _partial = _spendable_rows(store, frm, to)
    totals, saved = _split_spend_and_savings(rows, store)
    lines = [_spent_and_saved_line("החודש עד כה", totals, saved)]
    left = _safe_to_spend_line(store, now)
    if left:
        lines.append(left)
    if totals:
        top = sorted(totals.items(), key=lambda kv: kv[1])[:_TOP_CATEGORIES]
        lines.append("קטגוריות מובילות: " + ", ".join(
            f"{_category_label(c)} {_shekels(-amt)}" for c, amt in top))
    return "\n".join(lines)
