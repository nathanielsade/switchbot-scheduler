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


def test_conversation_emits_immediate_now():
    canned = lambda s, u: json.dumps({"immediate": [{"device": "living_room", "action": "on"}]})
    res = parse_conversation(["turn on the salon now"], _reg(), NOW, completion_fn=canned)
    assert res.schedule is None
    assert res.clarification is None
    assert len(res.immediate) == 1
    assert res.immediate[0].device == "living_room" and res.immediate[0].action == "on"


def test_conversation_resolves_immediate_alias():
    canned = lambda s, u: json.dumps({"immediate": [{"device": "סלון", "action": "off"}]})
    res = parse_conversation(["כבה את הסלון עכשיו"], _reg(), NOW, completion_fn=canned)
    assert res.immediate[0].device == "living_room"  # alias resolved to canonical


def test_conversation_mixed_immediate_and_schedule():
    canned = lambda s, u: json.dumps({
        "immediate": [{"device": "living_room", "action": "on"}],
        "schedules": [{"device": "living_room",
            "events": [{"time": "22:00", "action": "off", "days": ["mon"], "once": False}]}],
    })
    res = parse_conversation(["salon on now and off at 22:00 mondays"], _reg(), NOW, completion_fn=canned)
    assert len(res.immediate) == 1 and res.immediate[0].action == "on"
    assert res.schedule is not None and res.schedule.schedules[0].events[0].time == "22:00"


def test_immediate_invalid_action_returns_clarification():
    canned = lambda s, u: json.dumps({"immediate": [{"device": "living_room", "action": "toggle"}]})
    res = parse_conversation(["toggle the salon"], _reg(), NOW, completion_fn=canned)
    assert res.immediate == []
    assert res.clarification and "toggle" in res.clarification


def test_clarification_wins_over_immediate():
    canned = lambda s, u: json.dumps({
        "clarification": "Which room?",
        "immediate": [{"device": "living_room", "action": "on"}],
    })
    res = parse_conversation(["turn it on"], _reg(), NOW, completion_fn=canned)
    assert res.clarification == "Which room?"
    assert res.immediate == []
    assert res.schedule is None


def test_immediate_prompt_forbids_fabricated_time():
    seen = {}
    def cap(system, user):
        seen["system"] = system
        return json.dumps({"immediate": [{"device": "living_room", "action": "on"}]})
    parse_conversation(["now"], _reg(), NOW, completion_fn=cap)
    assert "immediate" in seen["system"]
    assert "never" in seen["system"].lower()  # the "never invent a time" rule is present
