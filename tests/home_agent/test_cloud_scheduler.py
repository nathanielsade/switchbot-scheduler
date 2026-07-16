from datetime import datetime
from zoneinfo import ZoneInfo
import yaml
from switchbot_scheduler.registry import Registry
from home_agent.schedule_store import ScheduleStore
from home_agent.cloud_scheduler import CloudScheduler

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=TZ)

class FakeJob:
    def __init__(self, name): self.name = name; self.removed = False
    def schedule_removal(self): self.removed = True

class FakeJobQueue:
    def __init__(self): self.jobs = []
    def run_daily(self, cb, time, days, name): self.jobs.append(FakeJob(name)); return self.jobs[-1]
    def run_once(self, cb, when, name): self.jobs.append(FakeJob(name)); return self.jobs[-1]
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
