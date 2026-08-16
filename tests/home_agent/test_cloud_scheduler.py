from datetime import datetime
from zoneinfo import ZoneInfo
import asyncio
import yaml
from switchbot_scheduler.registry import Registry
from home_agent.schedule_store import ScheduleStore
from home_agent.cloud_scheduler import CloudScheduler

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=TZ)

class FakeJob:
    def __init__(self, name, cb=None, days=None): self.name = name; self.removed = False; self.cb = cb; self.days = days
    def schedule_removal(self): self.removed = True

class FakeJobQueue:
    def __init__(self): self.jobs = []
    def run_daily(self, cb, time, days, name): job = FakeJob(name, cb, days); self.jobs.append(job); return job
    def run_once(self, cb, when, name): self.jobs.append(FakeJob(name, cb)); return self.jobs[-1]
    def get_jobs_by_name(self, name): return [j for j in self.jobs if j.name == name and not j.removed]

def _reg(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump({"devices": {
        "kitchen": {"ble_id": "AA:BB"}, "garden": {"cloud_id": "EECE111B5B1C"}}}), encoding="utf-8")
    return Registry.load(str(p))

def _sched(tmp_path, jq):
    store = ScheduleStore(str(tmp_path / "s.db"))
    cs = CloudScheduler(jq, store, _reg(tmp_path),
                        send_command_fn=lambda cid, cmd: None, tz=TZ, now_fn=lambda: NOW)
    return store, cs

def test_reconcile_registers_only_cloud_recurring(tmp_path):
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    rid = store.add("garden", "on", "18:00", ["mon", "tue"], False, None)
    store.add("kitchen", "on", "07:00", ["mon"], False, None)  # BLE — must be ignored
    cs.reconcile()
    assert [j.name for j in jq.jobs] == [f"switchbot-cloud:{rid}"]

def test_reconcile_drops_expired_one_time(tmp_path):
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    store.add("garden", "on", "09:00", ["thu"], True, "2026-07-16T06:00:00+00:00")  # past
    cs.reconcile()
    assert jq.jobs == []                    # not fired late
    assert store.list("garden") == []       # dropped

def test_reconcile_registers_future_one_time(tmp_path):
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    rid = store.add("garden", "on", "18:00", ["thu"], True, "2026-07-16T15:00:00+00:00")  # future
    cs.reconcile()
    assert [j.name for j in jq.jobs] == [f"switchbot-cloud:{rid}"]

def test_unschedule_removes_named_job(tmp_path):
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    rid = store.add("garden", "on", "18:00", ["mon"], False, None); cs.reconcile()
    cs.unschedule(rid)
    assert jq.get_jobs_by_name(f"switchbot-cloud:{rid}") == []

def test_one_time_fire_sends_command_and_removes_row(tmp_path):
    recorded = []
    def record_send(cloud_id, cmd): recorded.append((cloud_id, cmd))

    jq = FakeJobQueue()
    store = ScheduleStore(str(tmp_path / "s.db"))
    cs = CloudScheduler(jq, store, _reg(tmp_path),
                        send_command_fn=record_send, tz=TZ, now_fn=lambda: NOW)

    rid = store.add("garden", "on", "18:00", ["thu"], True, "2026-07-16T15:00:00+00:00")
    row = store.list("garden")[0]
    cs.schedule_row(row)

    job = jq.get_jobs_by_name(f"switchbot-cloud:{rid}")[0]
    asyncio.run(job.cb())

    assert recorded == [("EECE111B5B1C", "turnOn")]
    assert store.list("garden") == []

def test_one_time_fire_removes_row_even_when_send_fails(tmp_path):
    def failing_send(cloud_id, cmd): raise RuntimeError("send failed")

    jq = FakeJobQueue()
    store = ScheduleStore(str(tmp_path / "s.db"))
    cs = CloudScheduler(jq, store, _reg(tmp_path),
                        send_command_fn=failing_send, tz=TZ, now_fn=lambda: NOW)

    rid = store.add("garden", "on", "18:00", ["thu"], True, "2026-07-16T15:00:00+00:00")
    row = store.list("garden")[0]
    cs.schedule_row(row)

    job = jq.get_jobs_by_name(f"switchbot-cloud:{rid}")[0]
    asyncio.run(job.cb())  # should not raise; error is caught in callback

    assert store.list("garden") == []  # row removed despite send failure

def test_recurring_fire_keeps_row(tmp_path):
    recorded = []
    def record_send(cloud_id, cmd): recorded.append((cloud_id, cmd))

    jq = FakeJobQueue()
    store = ScheduleStore(str(tmp_path / "s.db"))
    cs = CloudScheduler(jq, store, _reg(tmp_path),
                        send_command_fn=record_send, tz=TZ, now_fn=lambda: NOW)

    rid = store.add("garden", "on", "18:00", ["mon"], False, None)
    row = store.list("garden")[0]
    cs.schedule_row(row)

    job = jq.get_jobs_by_name(f"switchbot-cloud:{rid}")[0]
    asyncio.run(job.cb())

    assert recorded == [("EECE111B5B1C", "turnOn")]
    rows = store.list("garden")
    assert len(rows) == 1
    assert rows[0]["id"] == rid

def test_weekday_mapping_monday(tmp_path):
    """Regression: _DAY_NUM must map mon→1 for PTB v20+ (Sun=0..Sat=6)."""
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    rid = store.add("garden", "on", "18:00", ["mon"], False, None)
    row = store.list("garden")[0]
    cs.schedule_row(row)
    job = jq.get_jobs_by_name(f"switchbot-cloud:{rid}")[0]
    assert job.days == (1,), f"Expected (1,) for monday, got {job.days}"

def test_weekday_mapping_sunday(tmp_path):
    """Regression: _DAY_NUM must map sun→0 for PTB v20+ (Sun=0..Sat=6)."""
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    rid = store.add("garden", "on", "18:00", ["sun"], False, None)
    row = store.list("garden")[0]
    cs.schedule_row(row)
    job = jq.get_jobs_by_name(f"switchbot-cloud:{rid}")[0]
    assert job.days == (0,), f"Expected (0,) for sunday, got {job.days}"

def test_weekday_mapping_multiple_days(tmp_path):
    """Regression: _DAY_NUM must map multiple days correctly."""
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    rid = store.add("garden", "on", "18:00", ["mon", "tue", "fri"], False, None)
    row = store.list("garden")[0]
    cs.schedule_row(row)
    job = jq.get_jobs_by_name(f"switchbot-cloud:{rid}")[0]
    assert job.days == (1, 2, 5), f"Expected (1, 2, 5) for mon/tue/fri, got {job.days}"

def test_schedule_row_raises_for_past_one_time(tmp_path):
    import pytest
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)   # note: (store, cs) — store first
    row = {"id": 1, "device": "garden", "action": "on", "time": "09:00",
           "days": ["thu"], "once": True, "fire_at": "2026-07-16T06:00:00+00:00"}
    with pytest.raises(ValueError):
        cs.schedule_row(row)                       # NOW is 12:00 Jerusalem = 09:00Z


def test_reconcile_logs_a_warning_for_a_corrupt_row_and_keeps_going(tmp_path, caplog):
    # F8a: schedule_row's ValueError is also raised for a corrupt fire_at/time (not just the
    # deliberate "already passed" case) — that must be distinguishable (a WARNING naming the
    # row id), and must never abort the sweep for the other rows.
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    corrupt_id = store.add("garden", "on", "18:00", ["thu"], True, "not-a-real-timestamp")
    good_id = store.add("garden", "on", "07:00", ["mon"], False, None)

    with caplog.at_level("WARNING"):
        cs.reconcile()

    assert [j.name for j in jq.jobs] == [f"switchbot-cloud:{good_id}"]   # good row still registered
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(str(corrupt_id) in r.getMessage() for r in warnings)


def test_reconcile_stays_quiet_for_the_expected_already_past_case(tmp_path, caplog):
    # The deliberate "already passed" ValueError (a live row whose fire_at ticked by between
    # remove_expired's strict "<" and schedule_row's "<=") is expected on every restart and
    # must not spam a warning.
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    store.add("garden", "on", "12:00", ["thu"], True, "2026-07-16T09:00:00+00:00")  # == NOW (UTC)

    with caplog.at_level("WARNING"):
        cs.reconcile()

    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_reconcile_skips_past_rows_without_raising(tmp_path):
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    # fire_at EXACTLY equal to now: remove_expired's strict "<" keeps the row, so it really
    # reaches schedule_row, whose "<=" raises. A row merely in the past would be deleted by
    # the sweep first, and this test would then pass even without the code change.
    store.add("garden", "on", "12:00", ["thu"], True, "2026-07-16T09:00:00+00:00")
    cs.reconcile()                                 # must not raise
    assert jq.jobs == []
