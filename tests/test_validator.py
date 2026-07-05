import pytest
from switchbot_scheduler.model import Event, DeviceSchedule, Schedule
from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.validator import validate, ScheduleError


def _reg():
    return Registry([Device(name="living_room", aliases=[], ble_id="U1")])


def _sched(events, device="living_room"):
    return Schedule(schedules=[DeviceSchedule(device=device, events=events)])


def test_valid_schedule_passes():
    validate(_sched([Event("06:00", "on", ["sun"])]), _reg())  # no raise


def test_unknown_device_raises():
    with pytest.raises(ScheduleError, match="Unknown device"):
        validate(_sched([Event("06:00", "on", ["sun"])], device="bedroom"), _reg())


def test_more_than_five_alarms_raises():
    events = [Event(f"0{h}:00", "on", ["sun"]) for h in range(6)]  # 6 alarms
    with pytest.raises(ScheduleError, match="max is 5"):
        validate(_sched(events), _reg())


def test_bad_time_raises():
    with pytest.raises(ScheduleError, match="time"):
        validate(_sched([Event("25:00", "on", ["sun"])]), _reg())


def test_bad_day_raises():
    with pytest.raises(ScheduleError, match="day"):
        validate(_sched([Event("06:00", "on", ["funday"])]), _reg())
