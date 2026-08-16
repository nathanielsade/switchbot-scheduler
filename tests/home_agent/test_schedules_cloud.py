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
    def __init__(self, fail=False, fail_unschedule=False):
        self.scheduled = []; self.removed = []
        self.fail = fail; self.fail_unschedule = fail_unschedule
    def schedule_row(self, row):
        if self.fail: raise RuntimeError("jobqueue down")
        self.scheduled.append(row["id"])
    def unschedule(self, row_id):
        if self.fail_unschedule: raise RuntimeError("jobqueue down")
        self.removed.append(row_id)

def _now(): return datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)

def test_cloud_schedule_registers_job_not_ble(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler()
    tools = {t.name: t for t in build_schedule_tools(
        _reg(tmp_path), store,
        write_fn=lambda *a: (_ for _ in ()).throw(AssertionError("no BLE for cloud")),
        now_fn=_now, scheduler=sch)}
    out = tools["schedule_recurring_device"].impl(
        {"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    assert sch.scheduled and "✅" in out
    assert store.list("garden")

def test_cloud_schedule_rolls_back_on_scheduler_failure(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler(fail=True)
    tools = {t.name: t for t in build_schedule_tools(_reg(tmp_path), store, now_fn=_now, scheduler=sch)}
    out = tools["schedule_recurring_device"].impl(
        {"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    assert store.list("garden") == []      # rolled back
    assert "✅" not in out

def test_cloud_cancel_unschedules(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler()
    tools = {t.name: t for t in build_schedule_tools(_reg(tmp_path), store, now_fn=_now, scheduler=sch)}
    tools["schedule_recurring_device"].impl(
        {"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    tools["cancel_schedule"].impl({"device": "גינה"})
    assert sch.removed and store.list("garden") == []

def test_cloud_one_time_device_7_days_out_still_works(tmp_path):
    # I1: the BLE guard (no more than a day out) must not apply to cloud-routed devices —
    # they fire on an exact datetime via the scheduler, not a dateless BLE alarm, and are
    # allowed up to the +7 day cap.
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler()
    tools = {t.name: t for t in build_schedule_tools(
        _reg(tmp_path), store,
        write_fn=lambda *a: (_ for _ in ()).throw(AssertionError("no BLE for cloud")),
        now_fn=_now, scheduler=sch)}
    out = tools["schedule_device"].impl(
        {"device": "גינה", "action": "on", "time": "18:30", "when": "in_a_week"})
    assert "✅" in out
    assert sch.scheduled and store.list("garden")
    # F1: only cloud devices can reach >6 days out, so the "NEXT WEEK" label's only coverage
    # lives here. Both directions must hold — the positive alone would pass if the label were
    # emitted unconditionally.
    assert "NEXT WEEK" in out


def test_cloud_one_time_device_tomorrow_has_no_next_week_label(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler()
    tools = {t.name: t for t in build_schedule_tools(
        _reg(tmp_path), store,
        write_fn=lambda *a: (_ for _ in ()).throw(AssertionError("no BLE for cloud")),
        now_fn=_now, scheduler=sch)}
    out = tools["schedule_device"].impl(
        {"device": "גינה", "action": "on", "time": "18:30", "when": "tomorrow"})
    assert "✅" in out
    assert "NEXT WEEK" not in out


def test_cloud_cancel_rolls_back_on_unschedule_failure(tmp_path):
    # M4: symmetric with the BLE branch — if unschedule() raises, the store rows must not be
    # gone while the jobs stay armed (which would report "error" while the timer still fires).
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler(fail_unschedule=True)
    tools = {t.name: t for t in build_schedule_tools(_reg(tmp_path), store, now_fn=_now, scheduler=sch)}
    tools["schedule_recurring_device"].impl(
        {"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    out = tools["cancel_schedule"].impl({"device": "גינה"})
    assert "✅" not in out
    assert not sch.removed
    assert len(store.list("garden")) == 1     # rolled back, record intact for a retry


def test_cloud_cancel_partial_unschedule_failure_leaves_both_rows_with_original_ids(tmp_path):
    # F7: unschedule must run BEFORE any store deletion. If it raises partway through a
    # multi-row cancel, nothing must be deleted — a delete-then-rollback-via-re-add would
    # hand the restored rows NEW autoincrement ids that no longer match the CloudScheduler
    # jobs keyed by the ORIGINAL id, half-applying the cancellation while reporting failure.
    store = ScheduleStore(str(tmp_path / "s.db"))

    class FlakyOnSecond:
        def __init__(self):
            self.scheduled = []; self.unscheduled = []
        def schedule_row(self, row):
            self.scheduled.append(row["id"])
        def unschedule(self, row_id):
            self.unscheduled.append(row_id)
            if len(self.unscheduled) == 2:
                raise RuntimeError("jobqueue down")

    sch = FlakyOnSecond()
    tools = {t.name: t for t in build_schedule_tools(_reg(tmp_path), store, now_fn=_now, scheduler=sch)}
    tools["schedule_recurring_device"].impl(
        {"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    tools["schedule_recurring_device"].impl(
        {"device": "גינה", "action": "on", "time": "19:00", "days": ["tue"],
         "repetition_phrase": "כל שלישי"})
    before_ids = sorted(r["id"] for r in store.list("garden"))
    assert len(before_ids) == 2

    out = tools["cancel_schedule"].impl({"device": "גינה"})

    assert "✅" not in out
    after = store.list("garden")
    assert len(after) == 2                              # BOTH rows still present
    assert sorted(r["id"] for r in after) == before_ids  # with their ORIGINAL ids, unrenumbered


def test_cloud_device_respects_the_five_timer_cap(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler()
    tools = {t.name: t for t in build_schedule_tools(
        _reg(tmp_path), store, write_fn=lambda *a: None, now_fn=_now, scheduler=sch)}
    for i in range(5):
        tools["schedule_recurring_device"].impl(
            {"device": "גינה", "action": "on", "time": f"0{i+1}:00", "days": ["mon"],
             "repetition_phrase": "כל שני"})
    out = tools["schedule_recurring_device"].impl(
        {"device": "גינה", "action": "on", "time": "07:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    assert "max" in out.lower() or "5" in out
    assert len(store.list("garden")) == 5
