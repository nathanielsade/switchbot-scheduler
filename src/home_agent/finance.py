import hashlib
import json
import logging
import re
import statistics
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .config import DEFAULT_FINANCE_START_DAYS
from .tools import Tool

log = logging.getLogger("home_agent")

CATEGORIES = ("groceries", "rent", "salary", "utilities", "transport", "health",
              "restaurants", "subscriptions", "shopping", "cash", "transfer", "other")

# Hebrew labels for CATEGORIES — finance replies are Hebrew end-to-end, so the raw English enum
# values (used internally by finance.py/category_rules) must never be printed verbatim. Covers
# every value in CATEGORIES; uncategorized rows use "אחר" directly. Lives here (not in
# finance_nudges) because finance.py is the leaf module — finance_nudges imports FROM finance.
_CATEGORY_HE = {
    "rent": "שכירות", "transport": "תחבורה", "groceries": "מכולת", "restaurants": "מסעדות",
    "subscriptions": "מנויים", "health": "בריאות", "shopping": "קניות", "utilities": "חשבונות",
    "transfer": "העברות/חיסכון", "salary": "הכנסה", "cash": "מזומן", "other": "אחר",
}
assert set(CATEGORIES) <= set(_CATEGORY_HE)

# cash_flow_status classification (v2.1, category-driven — see docs/superpowers/sdd/c-1-plan.md).
# Fixed = committed monthly outflow: named categories + committed savings/gmal (transfer negatives).
FIXED_CATEGORIES = frozenset({"rent", "utilities", "subscriptions"})
TRANSFER_CATEGORY = "transfer"

_WS = re.compile(r"\s+")
_CARD_BILL_RE = re.compile(r"(חיוב|זיכוי)\s+לכרטיס\s+ויזה\s+(\d+)")
# Appended by spend tools when a card's itemized data isn't available for the range and we fall
# back to the bank-level card-bill figures (Option A, spec §4). Shared by summary + by-category.
_PARTIAL_FLAG = "(פירוט הכרטיס אינו זמין לתקופה זו — מציג סכומים ברמת הבנק)"
# Safety net for when the nightly sync FAILS one or more nights (collector auth expired, network
# blip): coverage_end freezes in the past; grace prevents an immediate hard fallback to bank-level
# for a range that's only a few days stale. NOT meant to close a normal day-to-day gap — after a
# successful nightly sync, coverage_end == today, so a same-day query is fully covered (gap = 0).
_COVERAGE_GRACE_DAYS = 3
# An un-itemized card's bank card-bill line is nobody's fixed commitment — it's variable spending
# whose only distinguishing feature is which family member's card it is. Map card last-4 -> display
# name for labeling that variable-spending line in cash_flow_status (spec: docs/superpowers/sdd/
# c-1-saraycard-*). Unknown cards fall back to a generic "כרטיס NNNN" label.
_CARD_HOLDERS = {"6146": "שרי"}


def _card_bill_label(card4: str) -> str:
    name = _CARD_HOLDERS.get(card4)
    return f"כרטיס {name}" if name else f"כרטיס {card4}"


def finance_configured(config) -> bool:
    """True iff all three Discount creds are set. Partial config → warn + disable (fail safe)."""
    creds = [config.discount_id, config.discount_password, config.discount_num]
    if all(creds):
        return True
    if any(creds):
        log.warning("partial Discount config — finance disabled (need DISCOUNT_ID + PASSWORD + NUM)")
    return False


def _to_agorot(amount_str) -> int:
    return int((Decimal(str(amount_str)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _norm_desc(description) -> str:
    return _WS.sub(" ", (description or "").strip().lower())


def _card4(value) -> str:
    """Canonical trailing-4-digits key for matching a bank card-bill's 'ויזה NNNN' number
    against a Max account, which may arrive masked/padded (e.g. '****1743'). Digits only, last 4."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-4:]


def _is_card_payment(description, card_numbers):
    """True iff the normalized description is a Discount card-bill/credit line for one of card_numbers.

    Matches patterns like 'חיוב לכרטיס ויזה 1743' or 'זיכוי לכרטיס ויזה 1743'.
    Robust to multiple spaces; card_numbers must be a set of digit strings.
    Pure function; no DB access.
    """
    m = _CARD_BILL_RE.search(_norm_desc(description))
    return bool(m and m.group(2) in card_numbers)


def _fingerprint(source, account, identifier, txn_date, amount_agorot, description) -> str:
    if identifier:
        return f"id:{identifier}"
    raw = f"{source}|{account}|{txn_date}|{amount_agorot}|{_norm_desc(description)}"
    return "h:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def normalize_contract(data):
    source = data.get("source", "discount")
    txn_rows, snapshots, dropped = [], [], 0
    for acc in data.get("accounts", []):
        account = str(acc.get("account"))
        snapshots.append({"source": source, "account": account,
                          "scraped_at": data.get("scraped_at"),
                          "balance_agorot": _to_agorot(acc.get("balance", "0"))})
        for t in acc.get("transactions", []):
            try:
                amount = _to_agorot(t["chargedAmount"])
                txn_date = str(t["date"])[:10]
                desc = t["description"]
                if not desc or not txn_date:
                    raise KeyError("missing field")
            except (KeyError, TypeError, ValueError, ArithmeticError):
                dropped += 1
                continue
            identifier = t.get("identifier")
            txn_rows.append({
                "source": source, "account": account, "identifier": identifier,
                "fingerprint": _fingerprint(source, account, identifier, txn_date, amount, desc),
                "txn_date": txn_date,
                "processed_date": (str(t["processedDate"])[:10] if t.get("processedDate") else None),
                "amount_agorot": amount, "currency": t.get("chargedCurrency") or "ILS",
                "description": desc, "status": str(t.get("status", "completed")).lower(),
                "raw_json": json.dumps(t, ensure_ascii=False),
            })
    return txn_rows, snapshots, {"dropped": dropped}


_PERIODS = ("this_month", "last_month", "last_30_days")


def _now():
    return datetime.now().astimezone()


def _shekels(agorot) -> str:
    return f"₪{Decimal(agorot) / 100:,.2f}"


def _period_range(period, now):
    d = now.date()
    if period == "last_30_days":
        return (d - timedelta(days=30)).isoformat(), d.isoformat()
    if period == "last_month":
        first_this = d.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1).isoformat(), last_prev.isoformat()
    return d.replace(day=1).isoformat(), d.isoformat()  # this_month (default)


def _resolve_range(args, now_fn):
    frm, to = args.get("from_date"), args.get("to_date")
    if frm and to:
        return frm, to
    return _period_range(args.get("period") or "this_month", now_fn())


def _categorize(description, rules):
    """Categorize a transaction by matching merchant patterns. Read-time derivation.
    Precedence: longest merchant_pattern wins; tie → newest id.
    Returns category str or None if uncategorized."""
    desc = _norm_desc(description)
    best = None
    for r in rules:  # rules come ordered by id asc; keep the best by (len, id)
        if r["merchant_pattern"].strip().lower() in desc:
            if best is None or (len(r["merchant_pattern"]), r["id"]) >= (len(best["merchant_pattern"]), best["id"]):
                best = r
    return best["category"] if best else None


_SYNC_SCHEMA = {"type": "function", "function": {
    "name": "sync_finances",
    "description": (
        "Pull the latest Discount bank transactions into the local store. Use when the user asks to "
        "refresh/update finances or before answering if data looks stale. Reports how many were imported "
        "and the date range — not the transactions themselves. Report back in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_SUMMARY_SCHEMA = {"type": "function", "function": {
    "name": "financial_summary",
    "description": (
        "THE TOOL FOR TOTALS. Use this for any 'how much did we spend / earn / what's our balance / are we "
        "positive this month' question — it returns total income, total expenses, net, and current balance "
        "for a period. (For a breakdown BY CATEGORY, use spending_by_category instead; for one specific "
        "charge, use find_transactions.) Give explicit from_date/to_date (YYYY-MM-DD), or a period shortcut. "
        "Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "from_date": {"type": "string", "description": "YYYY-MM-DD"},
        "to_date": {"type": "string", "description": "YYYY-MM-DD"},
        "period": {"type": "string", "enum": list(_PERIODS)},
    }, "additionalProperties": False}}}

_FIND_SCHEMA = {"type": "function", "function": {
    "name": "find_transactions",
    "description": (
        "Look up individual transactions (e.g. 'what was that ₪450 charge', 'find the rent payments'). "
        "Filter by date range, ABSOLUTE amount in agorot (min_abs_agorot/max_abs_agorot; e.g. 45000 = ₪450 "
        "regardless of income/expense), direction (income|expense), or `query`. For `query`, pass a SHORT "
        "keyword or merchant name (one or two words, e.g. 'פיקדון', 'שופרסל') — NOT a full sentence. Returns "
        "up to fifty. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "from_date": {"type": "string"}, "to_date": {"type": "string"},
        "min_abs_agorot": {"type": "integer"}, "max_abs_agorot": {"type": "integer"},
        "direction": {"type": "string", "enum": ["income", "expense"]},
        "query": {"type": "string"},
    }, "additionalProperties": False}}}


def run_finance_sync(*, store, fetch_fns, now_fn=None) -> str:
    """Plain sync callable shared by the `sync_finances` tool and the nightly job (Part 1).
    Takes the file lock once around the whole multi-source sync (spec §3): a second concurrent
    sync is refused wholesale rather than interleaving between sources. The lock lives next to
    the DB; per-source fetchers no longer lock individually."""
    import fcntl
    import os

    now_fn = now_fn or _now
    lock_path = os.path.join(os.path.dirname(store.db_path) or ".", ".finance_sync.lock")
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "סנכרון פיננסי כבר רץ כרגע. נסו שוב עוד רגע."
        return _sync_locked(store=store, fetch_fns=fetch_fns, now_fn=now_fn)


def _sync_impl(args, *, store, fetch_fns, now_fn) -> str:
    return run_finance_sync(store=store, fetch_fns=fetch_fns, now_fn=now_fn)


def _sync_locked(*, store, fetch_fns, now_fn) -> str:
    now = now_fn()
    lines = []
    any_ok = False
    for source, fetch_fn in fetch_fns.items():
        try:
            data = fetch_fn()
            txns, snaps, counts = normalize_contract(data)
            for s in snaps:
                store.record_snapshot(s["source"], s["account"], s["scraped_at"], s["balance_agorot"])
            inserted, updated = store.upsert_transactions(txns)
            coverage_start = getattr(fetch_fn, "coverage_start", None) or \
                (now.date() - timedelta(days=DEFAULT_FINANCE_START_DAYS)).isoformat()
            coverage_end = now.date().isoformat()
            accounts = {t["account"] for t in txns} | {s["account"] for s in snaps}
            for account in accounts:
                store.record_coverage(source, account, coverage_start, coverage_end, now.isoformat())
        except Exception as e:
            log.warning("sync_finances failed for source=%s: %s", source, e)
            lines.append(f"{source}: שגיאה (לא עודכן)")
            continue
        any_ok = True
        dates = sorted(t["txn_date"] for t in txns) or [""]
        dropped = f", {counts['dropped']} דולגו" if counts["dropped"] else ""
        lines.append(f"{source}: {inserted} חדשות, {updated} עודכנו{dropped} (טווח {dates[0]}…{dates[-1]})")
    if not any_ok:
        return "לא הצלחתי למשוך נתונים מהבנק כרגע. נסו שוב עוד רגע."
    return "נמשכו נתונים:\n" + "\n".join(lines) + " ✅"


def _spendable_rows(store, frm, to):
    """Option A: drop the double-count between the bank's lump card-bill line and the itemized
    Max purchases behind it. For a card whose Max data covers [frm, to], exclude the bank's
    card-bill line (both income & expense sides) and keep the Max rows. For a card NOT covered,
    keep the bank card-bill line and drop that card's Max rows. Never both, never neither.
    Returns (kept_rows, partial_flag) — partial_flag is True iff a bank-level fallback (an
    uncovered card's bill) was kept, meaning the itemized detail isn't available for this period.
    """
    covered = {_card4(c) for c in store.covered_cards("max", frm, to, grace_days=_COVERAGE_GRACE_DAYS)}
    kept = []
    partial_flag = False
    for row in store.transactions_between(frm, to):
        m = _CARD_BILL_RE.search(_norm_desc(row["description"]))
        card = _card4(m.group(2)) if m else None
        if card is not None and card in covered:
            continue  # covered card's bank bill -> exclude (both income & expense)
        if row["source"] == "max" and _card4(row["account"]) not in covered:
            continue  # uncovered Max rows -> exclude (symmetric)
        kept.append(row)
        if card is not None:  # a card-bill line we KEPT (its card is not covered)
            partial_flag = True
    return kept, partial_flag


def _summary_impl(args, *, store, now_fn) -> str:
    frm, to = _resolve_range(args, now_fn)
    rows, partial = _spendable_rows(store, frm, to)
    income = sum(r["amount_agorot"] for r in rows if r["amount_agorot"] > 0)
    expense = sum(r["amount_agorot"] for r in rows if r["amount_agorot"] < 0)
    net = income + expense
    bal = store.current_balance_agorot()
    out = (f"טווח {frm}…{to}:\nהכנסות: {_shekels(income)}\nהוצאות: {_shekels(expense)}\n"
           f"נטו: {_shekels(net)}\nיתרה נוכחית: {_shekels(bal)}")
    if partial:
        out += "\n" + _PARTIAL_FLAG
    return out


def _find_impl(args, *, store) -> str:
    rows = store.search(from_date=args.get("from_date"), to_date=args.get("to_date"),
                        min_abs=args.get("min_abs_agorot"), max_abs=args.get("max_abs_agorot"),
                        direction=args.get("direction"), query=args.get("query"))
    if not rows:
        return "לא נמצאו תנועות תואמות."
    rules = store.active_rules()
    return "\n".join(f"{r['txn_date']}  {r['description']}  {_shekels(r['amount_agorot'])}  ({r['status']})  [{_categorize(r['description'], rules) or '—'}]"
                     for r in rows)


_SPENDING_SCHEMA = {"type": "function", "function": {
    "name": "spending_by_category",
    "description": (
        "Use ONLY when the user asks for a per-category BREAKDOWN of spending (how much on groceries vs "
        "eating-out, etc.). Do NOT use this for the total amount spent — that's financial_summary. Returns "
        "per-category totals plus the uncategorized count and example merchants; offer to categorize those "
        "via set_category_rule. Explicit from_date/to_date or a period shortcut. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "from_date": {"type": "string"}, "to_date": {"type": "string"},
        "period": {"type": "string", "enum": list(_PERIODS)}}, "additionalProperties": False}}}

_UNCATEGORIZED_MERCHANTS_LIMIT = 40
_UNCATEGORIZED_MERCHANTS_LOOKBACK_DAYS = 365

_UNCATEGORIZED_MERCHANTS_SCHEMA = {"type": "function", "function": {
    "name": "list_uncategorized_merchants",
    "description": (
        "List the distinct uncategorized expense merchants (with total spent and transaction count, "
        "sorted by spend), so you can bulk-create category rules with set_category_rule. Defaults to a "
        "wide lookback window; pass explicit from_date/to_date or a period shortcut to narrow it. "
        "Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "from_date": {"type": "string", "description": "YYYY-MM-DD"},
        "to_date": {"type": "string", "description": "YYYY-MM-DD"},
        "period": {"type": "string", "enum": list(_PERIODS)},
    }, "additionalProperties": False}}}

_SET_RULE_SCHEMA = {"type": "function", "function": {
    "name": "set_category_rule",
    "description": (
        "Persist a rule mapping a merchant substring to a category so spending is grouped consistently. "
        "Auto-create the rule for obvious merchants; ask the user when ambiguous. Category must be one of: "
        + ", ".join(CATEGORIES) + ". Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "merchant_pattern": {"type": "string"}, "category": {"type": "string", "enum": list(CATEGORIES)}},
        "required": ["merchant_pattern", "category"], "additionalProperties": False}}}

_LIST_RULES_SCHEMA = {"type": "function", "function": {
    "name": "list_category_rules",
    "description": "List the active merchant→category rules (id, pattern, category). Report in the user's language.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}

_DEL_RULE_SCHEMA = {"type": "function", "function": {
    "name": "delete_category_rule",
    "description": "Remove a category rule by its id (from list_category_rules). Report in the user's language.",
    "parameters": {"type": "object", "properties": {"id": {"type": "integer"}},
                   "required": ["id"], "additionalProperties": False}}}


def _spending_impl(args, *, store, now_fn) -> str:
    frm, to = _resolve_range(args, now_fn)
    rows, partial = _spendable_rows(store, frm, to)
    rules = store.active_rules()
    totals, uncategorized, uncategorized_agorot, examples = {}, 0, 0, []
    for t in rows:
        if t["amount_agorot"] >= 0:
            continue  # expenses only
        cat = _categorize(t["description"], rules)
        if cat is None:
            uncategorized += 1
            uncategorized_agorot += t["amount_agorot"]
            if t["description"] not in examples:
                examples.append(t["description"])
        else:
            totals[cat] = totals.get(cat, 0) + t["amount_agorot"]
    lines = [f"{c}: {_shekels(v)}" for c, v in sorted(totals.items(), key=lambda kv: kv[1])]
    if uncategorized:
        lines.append(f"ללא קטגוריה: {_shekels(uncategorized_agorot)} ({uncategorized} תנועות, "
                     f"למשל: {', '.join(examples[:3])})")
    out = "\n".join(lines) if lines else "אין הוצאות בטווח."
    if partial:
        out += "\n" + _PARTIAL_FLAG
    return out


def _uncategorized_merchants_range(args, now_fn):
    frm, to = args.get("from_date"), args.get("to_date")
    if frm and to:
        return frm, to
    if args.get("period"):
        return _period_range(args["period"], now_fn())
    now = now_fn()
    return (now.date() - timedelta(days=_UNCATEGORIZED_MERCHANTS_LOOKBACK_DAYS)).isoformat(), now.date().isoformat()


def _uncategorized_merchants_impl(args, *, store, now_fn) -> str:
    frm, to = _uncategorized_merchants_range(args, now_fn)
    rows, _partial = _spendable_rows(store, frm, to)
    rules = store.active_rules()
    merchants = {}  # norm_desc -> {"display": str, "total": int, "count": int}
    for t in rows:
        if t["amount_agorot"] >= 0:
            continue  # expenses only
        if _CARD_BILL_RE.search(_norm_desc(t["description"])):
            continue  # card-bill lump lines aren't categorizable merchants (e.g. an un-itemized card)
        if _categorize(t["description"], rules) is not None:
            continue  # already categorized
        key = _norm_desc(t["description"])
        m = merchants.get(key)
        if m is None:
            merchants[key] = {"display": t["description"], "total": t["amount_agorot"], "count": 1}
        else:
            m["total"] += t["amount_agorot"]
            m["count"] += 1
    if not merchants:
        return "אין סוחרים ללא קטגוריה בטווח."
    ordered = sorted(merchants.values(), key=lambda m: m["total"])[:_UNCATEGORIZED_MERCHANTS_LIMIT]
    return "\n".join(f"{m['display']}: {_shekels(m['total'])} ({m['count']} תנועות)" for m in ordered)


def _set_rule_impl(args, *, store) -> str:
    cat = (args.get("category") or "").strip().lower()
    if cat not in CATEGORIES:
        return f"קטגוריה לא חוקית '{cat}'. בחרו מתוך: {', '.join(CATEGORIES)}"
    pattern = (args.get("merchant_pattern") or "").strip()
    store.add_rule(pattern, cat)
    affected = [t["description"] for t in store.search(query=pattern, limit=1000)]
    ex = ", ".join(sorted(set(affected))[:3])
    return f"נוסף כלל: '{pattern}' → {cat} (משפיע על {len(affected)} תנועות{': ' + ex if ex else ''}) ✅"


def _list_rules_impl(args, *, store) -> str:
    rules = store.active_rules()
    if not rules:
        return "אין כללי קטגוריה."
    return "\n".join(f"[{r['id']}] {r['merchant_pattern']} → {r['category']}" for r in rules)


def _del_rule_impl(args, *, store) -> str:
    ok = store.remove_rule(args.get("id"))
    return f"כלל {args.get('id')} הוסר ✅" if ok else f"לא נמצא כלל פעיל עם מזהה {args.get('id')}."


def _detect_recurring(txns):
    from collections import defaultdict
    groups = defaultdict(list)
    for t in txns:
        groups[(_norm_desc(t["description"]), 1 if t["amount_agorot"] > 0 else -1)].append(t)
    recurring = []
    for (desc, sign), items in groups.items():
        months = {t["txn_date"][:7] for t in items}
        if len(months) < 2:
            continue
        days = [int(t["txn_date"][8:10]) for t in items]
        amts = [abs(t["amount_agorot"]) for t in items]
        if max(days) - min(days) > 3:
            continue
        typical = sorted(amts)[len(amts) // 2]
        if typical and (max(amts) - min(amts)) / typical > 0.10:
            continue
        occ = len(months)
        recurring.append({"description": items[-1]["description"], "sign": sign,
                          "amount_agorot": sign * typical, "day": round(sum(days) / len(days)),
                          "occurrences": occ, "confidence": "high" if occ >= 3 else "medium"})
    return recurring


_FORECAST_SCHEMA = {"type": "function", "function": {
    "name": "cash_flow_forecast",
    "description": (
        "Forecast end-of-month balance from current balance + detected recurring income/expenses, and flag "
        "a likely overdraft. Returns the projection AND the detected recurring items (with confidence) so you "
        "can explain the assumptions. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def _forecast_impl(args, *, store, now_fn) -> str:
    now = now_fn()
    lookback = (now.date() - timedelta(days=95)).isoformat()
    rows = store.transactions_between(lookback, now.date().isoformat())
    # cash-flow = bank debits; a card's Max purchases are already inside its bank card-bill,
    # so forecasting uses the bank feed only to avoid double-counting the debit (spec §4 P2a).
    bank_rows = [t for t in rows if t["source"] == "discount"]
    recurring = _detect_recurring(bank_rows)
    balance = store.current_balance_agorot()
    day = now.day
    remaining = sum(r["amount_agorot"] for r in recurring if r["day"] >= day)
    projected = balance + remaining
    lines = [f"יתרה נוכחית: {_shekels(balance)}",
             f"צפי לסוף החודש: {_shekels(projected)}" + (" ⚠️ צפוי מינוס" if projected < 0 else "")]
    if recurring:
        lines.append("פריטים קבועים שזוהו:")
        for r in recurring:
            lines.append(f"  {r['description']}: {_shekels(r['amount_agorot'])} (~יום {r['day']}, "
                         f"{r['occurrences']} חודשים, ביטחון {r['confidence']})")
    return "\n".join(lines)


_RECURRING_LOOKBACK_DAYS = 95  # same constant _forecast_impl uses (tuned for the ≤3-day drift gate)

_RECURRING_COMMITMENTS_SCHEMA = {"type": "function", "function": {
    "name": "list_recurring_commitments",
    "description": (
        "List the family's detected recurring/fixed commitments — subscriptions and other regular "
        "committed outflows (e.g. Spotify, Google One, a savings/gmal deposit) — with typical amount, "
        "roughly how many months seen, and confidence. Use this for 'what are our subscriptions / fixed "
        "commitments' questions. This is DIFFERENT from cash_flow_forecast: this tool detects recurring "
        "items on a SPEND basis (itemized card purchases included), so it catches card-itemized "
        "subscriptions that cash_flow_forecast — which only looks at the bank feed for its balance "
        "projection — would miss. Do not conflate the two totals. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def _recurring_commitments_impl(args, *, store, now_fn) -> str:
    now = now_fn()
    lookback = (now.date() - timedelta(days=_RECURRING_LOOKBACK_DAYS)).isoformat()
    rows, _partial = _spendable_rows(store, lookback, now.date().isoformat())
    recurring = _detect_recurring(rows)
    rules = store.active_rules()
    kept = []
    for r in recurring:
        if r["sign"] > 0:
            continue  # recurring income excluded
        cat = _categorize(r["description"], rules)
        if cat is not None and cat != TRANSFER_CATEGORY and cat not in CATEGORIES:
            continue  # defensive; shouldn't happen
        kept.append((r, cat))
    if not kept:
        return "לא זוהו הוצאות קבועות/מנויים חוזרים עדיין."
    kept.sort(key=lambda rc: abs(rc[0]["amount_agorot"]), reverse=True)
    total = sum(r["amount_agorot"] for r, _cat in kept)
    lines = ["הוצאות קבועות/מנויים שזוהו:"]
    for r, cat in kept:
        label = f" [{_CATEGORY_HE.get(cat, cat)}]" if cat else ""
        lines.append(f"{r['description']}: {_shekels(-r['amount_agorot'])} (~{r['occurrences']} חודשים, "
                     f"~יום {r['day']}, ביטחון {r['confidence']}){label}")
    lines.append(f"סה\"כ: {_shekels(-total)}")
    return "\n".join(lines)


def _month_start(d):
    return d.replace(day=1)


def _prev_month_start(d):
    return (_month_start(d) - timedelta(days=1)).replace(day=1)


def _month_end(start):
    nxt = start.replace(year=start.year + 1, month=1, day=1) if start.month == 12 \
        else start.replace(month=start.month + 1, day=1)
    return nxt - timedelta(days=1)


_CASH_FLOW_STATUS_SCHEMA = {"type": "function", "function": {
    "name": "cash_flow_status",
    "description": (
        "Use for 'how much is safe/left to spend this month or this week', budget-remaining, or "
        "'can we afford X' questions. Computes expected income and fixed costs from the last few full "
        "months' medians, this month's variable spend so far, and what's left to spend this month and "
        "per week — with a transparency breakdown of what counted as income/fixed. Do NOT use "
        "financial_summary or cash_flow_forecast for this. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def _cash_flow_terms(store, now_fn) -> dict:
    """Pure (besides store reads), testable core of cash_flow_status. See
    docs/superpowers/sdd/c-1-plan.md 'Decisions (locked)' for the exact rules this encodes:
    income/fixed are MEDIANS over the last 3 FULL completed calendar months, classified by
    canonical category name (never Hebrew text); uncategorized positives are fail-safe EXCLUDED
    from income (and flagged); variable_spent is this month's actual non-fixed, non-transfer spend."""
    today = now_fn().date()
    this_month_start = _month_start(today)

    m3 = _prev_month_start(this_month_start)  # most recent full month
    m2 = _prev_month_start(m3)
    m1 = _prev_month_start(m2)
    window_starts = [m1, m2, m3]  # oldest -> newest

    rules = store.active_rules()
    income_by_month, fixed_by_month = [], []
    income_categories, fixed_categories = {}, {}
    uncategorized_income_agorot = 0
    window_months = []
    # Rent is a once-a-month bill of a stable amount, but the individual checks land on
    # irregular dates that can skip a calendar month within the window (e.g. a check on the
    # 31st, then the next on the 1st of the month-after-next -> the month in between gets none).
    # Summing rent PER CALENDAR MONTH and then medianing those monthly sums undercounts rent
    # whenever the checks don't land one-per-window-month. Instead, collect the individual rent
    # PAYMENT amounts across the whole window (irrespective of which month each lands in) and use
    # their median as rent's monthly contribution below — robust to the calendar-month skew.
    rent_payments_agorot = []

    for start in window_starts:
        window_months.append(start.strftime("%Y-%m"))
        month_end = _month_end(start)
        # Option A (same helper the current-month variable calc uses, see _spendable_rows):
        # a card whose Max detail covers this window month is itemized (its purchases are
        # categorized individually below via the kept Max rows); its bank card-bill line is
        # dropped here so it never double-counts against those itemized rows. An UN-itemized
        # card's bank bill-line IS kept but ignored here (window months only feed fixed_expected,
        # and un-itemized card spending is variable, not fixed — see the current-month calc
        # below). Income rows are unaffected — only card-bill lines are ever excluded here.
        rows, _partial = _spendable_rows(store, start.isoformat(), month_end.isoformat())
        month_income = month_fixed = 0
        for row in rows:
            amt = row["amount_agorot"]
            cat = _categorize(row["description"], rules)
            if amt > 0:
                if cat is None:
                    uncategorized_income_agorot += amt  # fail-safe: excluded from income, flagged
                elif cat == TRANSFER_CATEGORY:
                    continue  # own money returning (savings withdrawal) -> not income
                else:
                    month_income += amt
                    income_categories[cat] = income_categories.get(cat, 0) + amt
            elif amt < 0:
                if _CARD_BILL_RE.search(_norm_desc(row["description"])):
                    # a bill line surviving _spendable_rows can only belong to an un-itemized
                    # card (a covered card's bill was already dropped above). Un-itemized card
                    # spending is now VARIABLE, not a committed monthly bill — it never feeds
                    # fixed_expected/fixed_categories at all; see the current-month calc below
                    # for how it counts toward variable_spent instead.
                    continue
                if cat == "rent":
                    # Kept OUT of month_fixed (the per-calendar-month sum): rent's monthly figure
                    # is derived separately below from the median individual payment, not from
                    # summing-then-medianing per calendar month. Still tracked in
                    # fixed_categories for the transparency breakdown.
                    rent_payments_agorot.append(-amt)
                    fixed_categories["rent"] = fixed_categories.get("rent", 0) + (-amt)
                elif cat in FIXED_CATEGORIES:
                    month_fixed += -amt
                    fixed_categories[cat] = fixed_categories.get(cat, 0) + (-amt)
                elif cat == TRANSFER_CATEGORY:
                    month_fixed += -amt  # committed savings/gmal deposit
                    fixed_categories[TRANSFER_CATEGORY] = fixed_categories.get(TRANSFER_CATEGORY, 0) + (-amt)
        income_by_month.append(month_income)
        fixed_by_month.append(month_fixed)

    income_expected = int(statistics.median(income_by_month))
    non_rent_fixed_expected = int(statistics.median(fixed_by_month))
    rent_expected = int(statistics.median(rent_payments_agorot)) if rent_payments_agorot else 0
    fixed_expected = non_rent_fixed_expected + rent_expected
    assert isinstance(income_expected, int) and isinstance(fixed_expected, int)

    rows_this_month, partial_flag = _spendable_rows(store, this_month_start.isoformat(), today.isoformat())
    variable_spent = 0
    card_bill_categories = {}
    for row in rows_this_month:
        amt = row["amount_agorot"]
        if amt >= 0:
            continue
        bill = _CARD_BILL_RE.search(_norm_desc(row["description"]))
        if bill:
            # a bill line surviving _spendable_rows can only belong to an un-itemized card (a
            # covered card's bill was already dropped): its spending is VARIABLE, labeled by
            # cardholder (_CARD_HOLDERS) rather than lumped into a generic "card_bills" bucket.
            card4 = _card4(bill.group(2))
            card_bill_categories[card4] = card_bill_categories.get(card4, 0) + (-amt)
            variable_spent += -amt
            continue
        cat = _categorize(row["description"], rules)
        if cat in FIXED_CATEGORIES or cat == TRANSFER_CATEGORY:
            continue  # this month's own fixed/transfer payments aren't "variable"
        variable_spent += -amt

    safe_to_spend = income_expected - fixed_expected - variable_spent
    days_left = (_month_end(this_month_start) - today).days + 1  # inclusive of today
    weeks_left = max(1, -(-days_left // 7))  # ceil(days_left/7), min 1
    weekly = safe_to_spend // weeks_left
    assert isinstance(weekly, int)

    return {
        "income_expected": income_expected,
        "fixed_expected": fixed_expected,
        "variable_spent": variable_spent,
        "safe_to_spend": safe_to_spend,
        "weeks_left": weeks_left,
        "weekly": weekly,
        "days_left": days_left,
        "window_months": window_months,
        "income_categories": income_categories,
        "fixed_categories": fixed_categories,
        "card_bill_categories": card_bill_categories,
        "uncategorized_income_agorot": uncategorized_income_agorot,
        "partial_flag": partial_flag,
    }


def _cash_flow_status_impl(args, *, store, now_fn) -> str:
    t = _cash_flow_terms(store, now_fn)
    lines = []
    if t["uncategorized_income_agorot"]:
        lines.append(
            f"⚠️ נמצאו זיכויים ללא קטגוריה בסך {_shekels(t['uncategorized_income_agorot'])} בחודשי הבדיקה "
            "— לא נספרו כהכנסה (שמרני, כדי לא לנפח את הסכום הפנוי). מומלץ להוסיף כלל קטגוריה עבורם."
        )
    lines.append(f"הכנסה חודשית צפויה (חציון 3 חודשים מלאים): {_shekels(t['income_expected'])}")
    lines.append(f"הוצאות קבועות צפויות (שכירות/חשבונות/מנויים + חיסכון מחויב): {_shekels(t['fixed_expected'])}")
    lines.append(f"הוצאות משתנות עד כה החודש: {_shekels(t['variable_spent'])}")
    for card4, amt in sorted(t["card_bill_categories"].items(), key=lambda kv: -kv[1]):
        lines.append(f"{_card_bill_label(card4)}: {_shekels(amt)}")
    lines.append(f"נשאר להוציא החודש: {_shekels(t['safe_to_spend'])} (~{_shekels(t['weekly'])} לשבוע)")
    if t["partial_flag"]:
        lines.append(_PARTIAL_FLAG)
    lines.append("שקיפות — חודשים שנבדקו: " + ", ".join(t["window_months"]))
    if t["income_categories"]:
        lines.append("נספר כהכנסה: " + ", ".join(
            f"{c} {_shekels(v)}" for c, v in sorted(t["income_categories"].items(), key=lambda kv: -kv[1])))
    if t["fixed_categories"]:
        lines.append("נספר כקבוע: " + ", ".join(
            f"{c} {_shekels(v)}" for c, v in sorted(t["fixed_categories"].items(), key=lambda kv: -kv[1])))
    lines.append("הערה: כסף שנמשך מהחיסכון החודש אינו מתווסף אוטומטית לסכום הפנוי.")
    return "\n".join(lines)


def build_finance_tools(store, *, now_fn=None, fetch_fns=None):
    now_fn = now_fn or _now
    fetch_fns = fetch_fns or {}
    return [
        Tool(name="sync_finances", schema=_SYNC_SCHEMA,
             impl=lambda a: _sync_impl(a, store=store, fetch_fns=fetch_fns, now_fn=now_fn)),
        Tool(name="financial_summary", schema=_SUMMARY_SCHEMA,
             impl=lambda a: _summary_impl(a, store=store, now_fn=now_fn)),
        Tool(name="find_transactions", schema=_FIND_SCHEMA,
             impl=lambda a: _find_impl(a, store=store)),
        Tool(name="spending_by_category", schema=_SPENDING_SCHEMA,
             impl=lambda a: _spending_impl(a, store=store, now_fn=now_fn)),
        Tool(name="list_uncategorized_merchants", schema=_UNCATEGORIZED_MERCHANTS_SCHEMA,
             impl=lambda a: _uncategorized_merchants_impl(a, store=store, now_fn=now_fn)),
        Tool(name="set_category_rule", schema=_SET_RULE_SCHEMA,
             impl=lambda a: _set_rule_impl(a, store=store)),
        Tool(name="list_category_rules", schema=_LIST_RULES_SCHEMA,
             impl=lambda a: _list_rules_impl(a, store=store)),
        Tool(name="delete_category_rule", schema=_DEL_RULE_SCHEMA,
             impl=lambda a: _del_rule_impl(a, store=store)),
        Tool(name="cash_flow_forecast", schema=_FORECAST_SCHEMA,
             impl=lambda a: _forecast_impl(a, store=store, now_fn=now_fn)),
        Tool(name="cash_flow_status", schema=_CASH_FLOW_STATUS_SCHEMA,
             impl=lambda a: _cash_flow_status_impl(a, store=store, now_fn=now_fn)),
        Tool(name="list_recurring_commitments", schema=_RECURRING_COMMITMENTS_SCHEMA,
             impl=lambda a: _recurring_commitments_impl(a, store=store, now_fn=now_fn)),
    ]


def make_collector_fetch(config, source="discount"):
    import os
    import subprocess

    # Pick script and creds by source
    if source == "discount":
        script = config.finance_collector_script
        creds = {"DISCOUNT_ID": config.discount_id,
                 "DISCOUNT_PASSWORD": config.discount_password, "DISCOUNT_NUM": config.discount_num}
    elif source == "max":
        script = config.max_collector_script
        creds = {"MAX_USERNAME": config.max_username, "MAX_PASSWORD": config.max_password}
    else:
        raise ValueError(f"unknown finance source: {source}")

    if not os.path.isabs(script):
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))  # src/home_agent -> repo
        script = os.path.join(repo_root, script)

    # Set FINANCE_START_DATE from config
    start_date_str = (_now() - timedelta(days=config.finance_start_days)).date().isoformat()

    def _fetch():
        # Concurrency is guarded once around the whole sync in run_finance_sync (spec §3); no lock here.
        env = {**os.environ, "FINANCE_START_DATE": start_date_str, **creds}
        proc = subprocess.run([config.finance_node_bin, script], capture_output=True,
                              text=True, env=env, timeout=180, shell=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(f"collector failed (rc={proc.returncode})")  # stderr NOT surfaced
        return json.loads(proc.stdout, parse_float=Decimal)
    _fetch.coverage_start = start_date_str  # so run_finance_sync records the SAME window the collector used
    return _fetch
