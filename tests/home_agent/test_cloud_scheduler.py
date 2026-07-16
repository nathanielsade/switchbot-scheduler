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
    def __init__(self, name, cb=None): self.name = name; self.removed = False; self.cb = cb
    def schedule_removal(self): self.removed = True

class FakeJobQueue:
    def __init__(self): self.jobs = []
    def run_daily(self, cb, time, days, name): self.jobs.append(FakeJob(name, cb)); return self.jobs[-1]
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
    store.add("garden", "on", "09:00", ["thu"], True, "2026-07-16T09:00:00+03:00")  # past
    cs.reconcile()
    assert jq.jobs == []                    # not fired late
    assert store.list("garden") == []       # dropped

def test_reconcile_registers_future_one_time(tmp_path):
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    rid = store.add("garden", "on", "18:00", ["thu"], True, "2026-07-16T18:00:00+03:00")  # future
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

    rid = store.add("garden", "on", "18:00", ["thu"], True, "2026-07-16T18:00:00+03:00")
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

    rid = store.add("garden", "on", "18:00", ["thu"], True, "2026-07-16T18:00:00+03:00")
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
