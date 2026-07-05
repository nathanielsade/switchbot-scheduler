from switchbot_scheduler.model import Event, DeviceSchedule, Schedule, DAYS


def test_days_are_seven_lowercase_codes():
    assert DAYS == ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def test_schedule_nests_device_schedules_and_events():
    e = Event(time="06:00", action="on", days=["sun", "mon"])
    ds = DeviceSchedule(device="living_room", events=[e])
    sched = Schedule(schedules=[ds])
    assert sched.schedules[0].events[0].time == "06:00"
    assert sched.schedules[0].device == "living_room"


def test_event_once_defaults_false():
    assert Event("06:00", "on", ["mon"]).once is False
