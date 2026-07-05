from switchbot_scheduler.model import Event, DeviceSchedule, Schedule
from switchbot_scheduler.ble_writer import group_events_by_device


def test_group_events_by_device_merges_duplicate_blocks_in_order():
    e1 = Event("06:00", "on", ["sun"])
    e2 = Event("07:00", "off", ["sun"])
    e3 = Event("18:00", "on", ["mon"])
    schedule = Schedule(schedules=[
        DeviceSchedule(device="living_room", events=[e1, e2]),
        DeviceSchedule(device="ac", events=[e3]),
        DeviceSchedule(device="living_room", events=[e3]),
    ])
    grouped = group_events_by_device(schedule)
    assert list(grouped.keys()) == ["living_room", "ac"]
    assert grouped["living_room"] == [e1, e2, e3]
    assert grouped["ac"] == [e3]
