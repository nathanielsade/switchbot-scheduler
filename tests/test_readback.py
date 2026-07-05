from switchbot_scheduler.model import Event, DeviceSchedule, Schedule
from switchbot_scheduler.readback import readback, describe_days


def test_describe_days_every_day():
    assert describe_days(["sun", "mon", "tue", "wed", "thu", "fri", "sat"]) == "every day"


def test_describe_days_partial_in_week_order():
    assert describe_days(["mon", "sun", "wed"]) == "sun, mon, wed"


def test_readback_lists_each_event():
    sched = Schedule(schedules=[
        DeviceSchedule(device="living_room", events=[
            Event("06:00", "on", ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]),
            Event("17:00", "off", ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]),
        ]),
    ])
    text = readback(sched)
    assert "living_room: on 06:00 — every day" in text
    assert "living_room: off 17:00 — every day" in text
