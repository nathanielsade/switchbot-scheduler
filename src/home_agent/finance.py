import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .config import DEFAULT_FINANCE_START_DAYS
from .tools import Tool

log = logging.getLogger("home_agent")

CATEGORIES = ("groceries", "rent", "salary", "utilities", "transport", "health",
              "restaurants", "subscriptions", "shopping", "cash", "transfer", "other")

_WS = re.compile(r"\s+")
_CARD_BILL_RE = re.compile(r"(חיוב|זיכוי)\s+לכרטיס\s+ויזה\s+(\d+)")


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


def _sync_impl(args, *, store, fetch_fns, now_fn) -> str:
    import fcntl
    import os

    # File-lock ONCE around the whole multi-source sync (spec §3): a second concurrent
    # sync_finances is refused wholesale rather than interleaving between sources. The lock
    # lives next to the DB; per-source fetchers no longer lock individually.
    lock_path = os.path.join(os.path.dirname(store.db_path) or ".", ".finance_sync.lock")
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "סנכרון פיננסי כבר רץ כרגע. נסו שוב עוד רגע."
        return _sync_locked(store=store, fetch_fns=fetch_fns, now_fn=now_fn)


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


def _summary_impl(args, *, store, now_fn) -> str:
    frm, to = _resolve_range(args, now_fn)
    income, expense = store.sum_amounts(frm, to)
    net = income + expense
    bal = store.current_balance_agorot()
    return (f"טווח {frm}…{to}:\nהכנסות: {_shekels(income)}\nהוצאות: {_shekels(expense)}\n"
            f"נטו: {_shekels(net)}\nיתרה נוכחית: {_shekels(bal)}")


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
    rules = store.active_rules()
    totals, uncategorized, examples = {}, 0, []
    for t in store.transactions_between(frm, to):
        if t["amount_agorot"] >= 0:
            continue  # expenses only
        cat = _categorize(t["description"], rules)
        if cat is None:
            uncategorized += 1
            if t["description"] not in examples:
                examples.append(t["description"])
        else:
            totals[cat] = totals.get(cat, 0) + t["amount_agorot"]
    lines = [f"{c}: {_shekels(v)}" for c, v in sorted(totals.items(), key=lambda kv: kv[1])]
    if uncategorized:
        lines.append(f"ללא קטגוריה: {uncategorized} (למשל: {', '.join(examples[:3])})")
    return "\n".join(lines) if lines else "אין הוצאות בטווח."


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
    recurring = _detect_recurring(store.transactions_between(lookback, now.date().isoformat()))
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
        Tool(name="set_category_rule", schema=_SET_RULE_SCHEMA,
             impl=lambda a: _set_rule_impl(a, store=store)),
        Tool(name="list_category_rules", schema=_LIST_RULES_SCHEMA,
             impl=lambda a: _list_rules_impl(a, store=store)),
        Tool(name="delete_category_rule", schema=_DEL_RULE_SCHEMA,
             impl=lambda a: _del_rule_impl(a, store=store)),
        Tool(name="cash_flow_forecast", schema=_FORECAST_SCHEMA,
             impl=lambda a: _forecast_impl(a, store=store, now_fn=now_fn)),
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
        # Concurrency is guarded once around the whole sync in _sync_impl (spec §3); no lock here.
        env = {**os.environ, "FINANCE_START_DATE": start_date_str, **creds}
        proc = subprocess.run([config.finance_node_bin, script], capture_output=True,
                              text=True, env=env, timeout=180, shell=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(f"collector failed (rc={proc.returncode})")  # stderr NOT surfaced
        return json.loads(proc.stdout, parse_float=Decimal)
    _fetch.coverage_start = start_date_str  # so _sync_impl records the SAME window the collector used
    return _fetch
