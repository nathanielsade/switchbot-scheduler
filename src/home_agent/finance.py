import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .tools import Tool

log = logging.getLogger("home_agent")

CATEGORIES = ("groceries", "rent", "salary", "utilities", "transport", "health",
              "restaurants", "subscriptions", "shopping", "cash", "transfer", "other")

_WS = re.compile(r"\s+")


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
        "Summarize money for a period: income, expenses, net, and current balance (all accounts). Give "
        "explicit from_date/to_date (YYYY-MM-DD) when known, or a period shortcut. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "from_date": {"type": "string", "description": "YYYY-MM-DD"},
        "to_date": {"type": "string", "description": "YYYY-MM-DD"},
        "period": {"type": "string", "enum": list(_PERIODS)},
    }, "additionalProperties": False}}}

_FIND_SCHEMA = {"type": "function", "function": {
    "name": "find_transactions",
    "description": (
        "Find transactions by date range, ABSOLUTE amount in agorot (min_abs_agorot/max_abs_agorot; e.g. "
        "45000 = ₪450 regardless of income/expense), direction (income|expense), or a text query on the "
        "description. Returns up to fifty. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "from_date": {"type": "string"}, "to_date": {"type": "string"},
        "min_abs_agorot": {"type": "integer"}, "max_abs_agorot": {"type": "integer"},
        "direction": {"type": "string", "enum": ["income", "expense"]},
        "query": {"type": "string"},
    }, "additionalProperties": False}}}


def _sync_impl(args, *, store, fetch_fn) -> str:
    try:
        data = fetch_fn()
        txns, snaps, counts = normalize_contract(data)
        for s in snaps:
            store.record_snapshot(s["source"], s["account"], s["scraped_at"], s["balance_agorot"])
        inserted, updated = store.upsert_transactions(txns)
    except Exception as e:
        log.warning("sync_finances failed: %s", e)
        return "לא הצלחתי למשוך נתונים מהבנק כרגע. נסו שוב עוד רגע."
    dates = sorted(t["txn_date"] for t in txns) or [""]
    dropped = f", {counts['dropped']} דולגו" if counts["dropped"] else ""
    return (f"נמשכו נתונים: {inserted} חדשות, {updated} עודכנו{dropped} "
            f"(טווח {dates[0]}…{dates[-1]}) ✅")


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
    return "\n".join(f"{r['txn_date']}  {r['description']}  {_shekels(r['amount_agorot'])}  ({r['status']})"
                     for r in rows)


def build_finance_tools(store, *, now_fn=None, fetch_fn=None):
    now_fn = now_fn or _now
    return [
        Tool(name="sync_finances", schema=_SYNC_SCHEMA,
             impl=lambda a: _sync_impl(a, store=store, fetch_fn=fetch_fn)),
        Tool(name="financial_summary", schema=_SUMMARY_SCHEMA,
             impl=lambda a: _summary_impl(a, store=store, now_fn=now_fn)),
        Tool(name="find_transactions", schema=_FIND_SCHEMA,
             impl=lambda a: _find_impl(a, store=store)),
    ]


def make_collector_fetch(config):
    import fcntl
    import os
    import subprocess
    script = config.finance_collector_script
    if not os.path.isabs(script):
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))  # src/home_agent -> repo
        script = os.path.join(repo_root, script)
    lock_path = os.path.join(os.path.dirname(config.db_path) or ".", ".finance_sync.lock")

    def _fetch():
        env = {**os.environ, "DISCOUNT_ID": config.discount_id,
               "DISCOUNT_PASSWORD": config.discount_password, "DISCOUNT_NUM": config.discount_num}
        with open(lock_path, "w") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("a finance sync is already running")
            proc = subprocess.run([config.finance_node_bin, script], capture_output=True,
                                  text=True, env=env, timeout=180, shell=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(f"collector failed (rc={proc.returncode})")  # stderr NOT surfaced
        return json.loads(proc.stdout)
    return _fetch
