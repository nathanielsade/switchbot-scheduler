from pathlib import Path
from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.parser import parse_schedule, build_system_prompt

FIXTURE = Path(__file__).parent / "fixtures" / "parser_living_room.json"


def _reg():
    return Registry([Device(name="living_room", aliases=["salon"], ble_id="U1")])


def test_system_prompt_lists_known_devices():
    prompt = build_system_prompt(_reg())
    assert "living_room" in prompt


def test_system_prompt_lists_aliases():
    prompt = build_system_prompt(_reg())
    assert "salon" in prompt


def test_parse_schedule_builds_objects_from_json():
    canned = FIXTURE.read_text()
    sched = parse_schedule(
        "turn the living room on 6 to 5 every day",
        _reg(),
        completion_fn=lambda system, user: canned,
    )
    assert sched.schedules[0].device == "living_room"
    assert [e.action for e in sched.schedules[0].events] == ["on", "off"]
    assert sched.schedules[0].events[0].time == "06:00"


def test_parse_canonicalizes_alias_device():
    registry = Registry([Device(name="living_room", aliases=["living room", "סלון", "salon"], ble_id="U1")])
    canned = '{"schedules":[{"device":"סלון","events":[{"time":"06:00","action":"on","days":["sun"]}]}]}'
    sched = parse_schedule(
        "turn on סלון at 6",
        registry,
        completion_fn=lambda system, user: canned,
    )
    assert sched.schedules[0].device == "living_room"
