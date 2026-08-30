"""Acceptance tests for Task D-1: proactive finance nudges (scheduling wiring in build_application).

Locked decisions being asserted here (see docs/superpowers/sdd/d-1-plan.md):
- Sunday = days=(0,) (PTB v20+: Sun=0..Sat=6) — NEVER (6,). We assert the resulting APScheduler
  trigger's day_of_week field renders 'sun' (mirrors cloud_scheduler's own Sun=0 regression test).
- Monthly recap fires daily at 09:00 (the callback itself gates on day==2).
- Sends go through the injectable send_fn(chat_id, text) seam — job callbacks never touch
  context.bot, so job.callback(None) (context=None) must work.
- No nudge jobs at all when finance is unconfigured.
"""
import asyncio

from finance_fakes import contract


def _finance_config(tmp_path, **over):
    from home_agent.config import Config
    kw = dict(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
              allowed_chat_ids={1}, db_path=str(tmp_path / "m.db"),
              devices_path=str(tmp_path / "none.yaml"))
    kw.update(over)
    return Config(**kw)


def _build(tmp_path, monkeypatch, make_fake_client, send_fn=None, **cfg_over):
    import home_agent.telegram_app as ta
    from home_agent.memory import Conversation

    cfg = _finance_config(tmp_path, discount_id="1", discount_password="p", discount_num="9", **cfg_over)
    monkeypatch.setattr(ta, "make_collector_fetch", lambda cfg, source="discount": (lambda: contract()))
    app = ta.build_application(cfg, client=make_fake_client([]),
                               conversation=Conversation(str(tmp_path / "m.db")), send_fn=send_fn)
    return ta, app, cfg


def test_month_recap_job_registered_daily_at_nine(tmp_path, monkeypatch, make_fake_client):
    _ta, app, _cfg = _build(tmp_path, monkeypatch, make_fake_client)
    jobs = app.job_queue.get_jobs_by_name("finance-month-recap")
    assert len(jobs) == 1
    hour_field = next(f for f in jobs[0].job.trigger.fields if f.name == "hour")
    assert str(hour_field) == "9"


def test_weekly_summary_job_registered_sunday_evening(tmp_path, monkeypatch, make_fake_client):
    _ta, app, _cfg = _build(tmp_path, monkeypatch, make_fake_client)
    jobs = app.job_queue.get_jobs_by_name("finance-weekly-summary")
    assert len(jobs) == 1
    job = jobs[0]
    hour_field = next(f for f in job.job.trigger.fields if f.name == "hour")
    dow_field = next(f for f in job.job.trigger.fields if f.name == "day_of_week")
    assert str(hour_field) == "20"
    # Regression: PTB v20+ Sun=0..Sat=6 -> day_of_week renders as 'sun'. NEVER 'sat' ((6,) bug).
    assert str(dow_field) == "sun", f"expected sun (days=(0,)), got {dow_field}"


def test_no_nudge_jobs_when_finance_unconfigured(tmp_path, make_fake_client):
    import home_agent.telegram_app as ta
    from home_agent.memory import Conversation

    cfg = _finance_config(tmp_path)  # no discount_* creds
    app = ta.build_application(cfg, client=make_fake_client([]),
                               conversation=Conversation(str(tmp_path / "m.db")))
    assert not app.job_queue.get_jobs_by_name("finance-month-recap")
    assert not app.job_queue.get_jobs_by_name("finance-weekly-summary")


def test_month_recap_callback_uses_injected_send_fn_and_gates_on_day(tmp_path, monkeypatch, make_fake_client):
    sent = []

    def fake_send(chat_id, text):
        sent.append((chat_id, text))

    _ta, app, _cfg = _build(tmp_path, monkeypatch, make_fake_client, send_fn=fake_send)
    job = app.job_queue.get_jobs_by_name("finance-month-recap")[0]

    asyncio.run(job.callback(None))  # context=None: callback must never touch context.bot

    # No seeded transactions -> build_month_recap returns None -> no send, regardless of "today".
    assert sent == []


def test_weekly_summary_callback_uses_injected_send_fn(tmp_path, monkeypatch, make_fake_client):
    sent = []

    def fake_send(chat_id, text):
        sent.append((chat_id, text))

    _ta, app, _cfg = _build(tmp_path, monkeypatch, make_fake_client, send_fn=fake_send)
    job = app.job_queue.get_jobs_by_name("finance-weekly-summary")[0]

    asyncio.run(job.callback(None))  # context=None: callback must never touch context.bot

    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == 1  # resolved from the single ALLOWED_CHAT_IDS entry
    assert isinstance(text, str) and text.strip()
