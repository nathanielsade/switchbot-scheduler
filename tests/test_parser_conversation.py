import json
from datetime import datetime
from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.parser import parse_conversation, ParseResult

NOW = datetime(2026, 7, 5, 12, 0)  # a Sunday


def _reg():
    return Registry([Device(name="living_room", aliases=["סלון"], ble_id="U1")])


def test_conversation_passes_all_turns_to_model():
    seen = {}
    def cap(system, user):
        seen["user"] = user
        return json.dumps({"schedules": [{"device": "living_room",
            "events": [{"time": "09:00", "action": "on", "days": ["mon"], "once": True}]}]})
    parse_conversation(["turn on living room tomorrow 9am", "make it one-time"], _reg(), NOW, completion_fn=cap)
    assert "turn on living room tomorrow 9am" in seen["user"]
    assert "make it one-time" in seen["user"]


def test_conversation_builds_schedule_with_once():
    canned = lambda s, u: json.dumps({"schedules": [{"device": "living_room",
        "events": [{"time": "09:00", "action": "on", "days": ["mon"], "once": True}]}]})
    res = parse_conversation(["living room on tomorrow 9am, once"], _reg(), NOW, completion_fn=canned)
    assert res.clarification is None
    ev = res.schedule.schedules[0].events[0]
    assert ev.once is True and ev.time == "09:00"


def test_clarification_path():
    canned = lambda s, u: json.dumps({"clarification": "Which device did you mean?"})
    res = parse_conversation(["do the thing"], _reg(), NOW, completion_fn=canned)
    assert res.schedule is None
    assert res.clarification == "Which device did you mean?"


def test_system_prompt_includes_today():
    seen = {}
    def cap(system, user):
        seen["system"] = system
        return json.dumps({"clarification": "?"})
    parse_conversation(["hi"], _reg(), NOW, completion_fn=cap)
    assert "2026-07-05" in seen["system"] and "Sunday" in seen["system"]
