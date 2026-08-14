"""Acceptance tests for Task F-A: nightly auto-sync + coverage-grace fix.

1. Coverage grace: a card whose sync ended a couple of days ago is still treated as covered
   (itemized detail retained); a genuinely stale card (well past the grace window) falls back
   to bank-level + partial flag.
2. Idempotent sync: running the same sync twice inserts 0 new rows the second time.
3. Nightly job: build_application (finance configured) registers a daily job at
   finance_sync_hour whose callback invokes run_finance_sync (frozen clock, fake fetch).
4. No nightly job when finance is unconfigured.
"""
import os
import tempfile
from datetime import datetime

from home_agent.finance import build_finance_tools, run_finance_sync
from home_agent.finance_store import FinanceStore
from finance_fakes import contract, make_fetch


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _frozen():
    return datetime(2026, 7, 12, 12, 0, 0)


def _row(source, account, amount_agorot, description, txn_date="2026-07-10", identifier=None):
    return {
        "source": source, "account": account, "identifier": identifier,
        "fingerprint": f"h:{source}:{account}:{description}:{amount_agorot}:{txn_date}",
        "txn_date": txn_date, "processed_date": None,
        "amount_agorot": amount_agorot, "currency": "ILS",
        "description": description, "status": "completed", "raw_json": "{}",
    }


def test_coverage_grace_retains_categories_when_recently_synced():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743"),
        _row("max", "1743", -300000, "רמי לוי"),
    ])
    # Last sync ended 2 days before "to_date" — within the 3-day grace window.
    store.record_coverage("max", "1743", "2026-07-01", "2026-07-10", "2026-07-10T00:00:00")

    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns={})
    breakdown = _tool(tools, "spending_by_category").impl(
        {"from_date": "2026-07-01", "to_date": "2026-07-12"})
    # Itemized detail kept: the card is still "covered" thanks to grace.
    assert "4,717.00" not in breakdown  # bank bill was NOT reintroduced
    summary = _tool(tools, "financial_summary").impl(
        {"from_date": "2026-07-01", "to_date": "2026-07-12"})
    assert "3,000.00" in summary  # itemized Max total, single-counted
    assert "פירוט הכרטיס אינו זמין" not in summary  # no partial-fallback flag


def test_coverage_grace_does_not_over_extend_genuinely_stale_range():
    store = _store()
    store.upsert_transactions([
        _row("discount", "checking", -471700, "חיוב לכרטיס ויזה 1743"),
        _row("max", "1743", -300000, "רמי לוי"),
    ])
    # Last sync ended 30 days before "to_date" — well past the 3-day grace window.
    store.record_coverage("max", "1743", "2026-06-01", "2026-06-12", "2026-06-12T00:00:00")

    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns={})
    summary = _tool(tools, "financial_summary").impl(
        {"from_date": "2026-06-01", "to_date": "2026-07-12"})
    # Falls back to bank-level card-bill figure + the honest partial flag.
    assert "4,717.00" in summary
    assert "פירוט הכרטיס אינו זמין" in summary


def test_run_finance_sync_is_idempotent():
    store = _store()
    fetch_fns = {"discount": make_fetch(contract())}
    first = run_finance_sync(store=store, fetch_fns=fetch_fns, now_fn=_frozen)
    assert "1 חדשות" in first or "2 חדשות" in first  # sanity: something was imported

    inserted_before, updated_before = store.upsert_transactions([])  # no-op, just to read shape
    assert (inserted_before, updated_before) == (0, 0)

    balance_after_first = store.current_balance_agorot()
    second = run_finance_sync(store=store, fetch_fns=fetch_fns, now_fn=_frozen)
    assert "0 חדשות" in second  # 2nd run over the same fixture inserts nothing new
    assert store.current_balance_agorot() == balance_after_first  # unchanged


def _finance_config(tmp_path, **over):
    from home_agent.config import Config
    kw = dict(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
              allowed_chat_ids={1}, db_path=str(tmp_path / "m.db"),
              devices_path=str(tmp_path / "none.yaml"))
    kw.update(over)
    return Config(**kw)


def test_nightly_finance_job_registered_and_invokes_run_finance_sync(tmp_path, monkeypatch, make_fake_client):
    import home_agent.telegram_app as ta
    from home_agent.memory import Conversation

    cfg = _finance_config(tmp_path, discount_id="1", discount_password="p", discount_num="9")
    monkeypatch.setattr(ta, "make_collector_fetch",
                        lambda cfg, source="discount": (lambda: contract()))

    calls = {"n": 0}
    real_run = ta.run_finance_sync

    def spy(*a, **kw):
        calls["n"] += 1
        return real_run(*a, **kw)

    monkeypatch.setattr(ta, "run_finance_sync", spy)

    app = ta.build_application(cfg, client=make_fake_client([]),
                               conversation=Conversation(str(tmp_path / "m.db")))
    jobs = app.job_queue.get_jobs_by_name("finance-sync")
    assert len(jobs) == 1
    job = jobs[0]
    hour_field = next(f for f in job.job.trigger.fields if f.name == "hour")
    assert str(hour_field) == str(cfg.finance_sync_hour)

    import asyncio
    asyncio.run(job.callback(None))
    assert calls["n"] == 1


def test_no_nightly_finance_job_when_unconfigured(tmp_path, make_fake_client):
    import home_agent.telegram_app as ta
    from home_agent.memory import Conversation

    cfg = _finance_config(tmp_path)  # no discount_* creds
    app = ta.build_application(cfg, client=make_fake_client([]),
                               conversation=Conversation(str(tmp_path / "m.db")))
    assert not app.job_queue.get_jobs_by_name("finance-sync")
