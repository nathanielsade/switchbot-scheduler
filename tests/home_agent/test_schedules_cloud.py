import yaml, pytest
from switchbot_scheduler.registry import Registry
from home_agent.schedule_store import ScheduleStore
from home_agent.schedules import build_schedule_tools
from datetime import datetime, timezone

def _reg(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump({"devices": {
        "kitchen": {"ble_id": "AA:BB"},
        "garden": {"cloud_id": "EECE111B5B1C", "aliases": ["גינה"]}}}, allow_unicode=True), encoding="utf-8")
    return Registry.load(str(p))

class FakeScheduler:
    def __init__(self, fail=False): self.scheduled = []; self.removed = []; self.fail = fail
    def schedule_row(self, row):
        if self.fail: raise RuntimeError("jobqueue down")
        self.scheduled.append(row["id"])
    def unschedule(self, row_id): self.removed.append(row_id)

def _now(): return datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)

def test_cloud_schedule_registers_job_not_ble(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler()
    tools = {t.name: t for t in build_schedule_tools(
        _reg(tmp_path), store,
        write_fn=lambda *a: (_ for _ in ()).throw(AssertionError("no BLE for cloud")),
        now_fn=_now, scheduler=sch)}
    out = tools["schedule_device"].impl({"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"]})
    assert sch.scheduled and "✅" in out
    assert store.list("garden")

def test_cloud_schedule_rolls_back_on_scheduler_failure(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler(fail=True)
    tools = {t.name: t for t in build_schedule_tools(_reg(tmp_path), store, now_fn=_now, scheduler=sch)}
    out = tools["schedule_device"].impl({"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"]})
    assert store.list("garden") == []      # rolled back
    assert "✅" not in out

def test_cloud_cancel_unschedules(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler()
    tools = {t.name: t for t in build_schedule_tools(_reg(tmp_path), store, now_fn=_now, scheduler=sch)}
    tools["schedule_device"].impl({"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"]})
    tools["cancel_schedule"].impl({"device": "גינה"})
    assert sch.removed and store.list("garden") == []
