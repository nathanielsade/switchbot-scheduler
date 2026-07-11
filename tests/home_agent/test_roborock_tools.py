from home_agent.roborock import build_roborock_tools
from home_agent.roborock_rooms import Room, RoomRegistry
from roborock_fakes import FakeRoborockClient   # sibling helper; tests/ has no __init__.py (prepend import mode)


def _reg():
    return RoomRegistry([
        Room(name="living_room", segment_id=16, aliases=["סלון", "living room"]),
        Room(name="kitchen", segment_id=17, aliases=["מטבח"]),
    ])


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_list_rooms_lists_names_and_aliases():
    tools = build_roborock_tools(FakeRoborockClient(), _reg())
    out = _tool(tools, "list_rooms").impl({})
    assert "living_room" in out and "סלון" in out
    assert "kitchen" in out and "מטבח" in out


def test_list_rooms_without_registry_is_friendly():
    tools = build_roborock_tools(FakeRoborockClient(), None)
    out = _tool(tools, "list_rooms").impl({})
    assert "no rooms" in out.lower()


def test_clean_whole_home_when_no_rooms():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "clean").impl({})
    assert client.calls == [("clean", dict(segment_ids=None, mode=None, suction=None,
                                           water_flow=None, repeat=1))]
    assert "whole home" in out and "✅" in out


def test_clean_resolves_rooms_to_segments_with_plan():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "clean").impl(
        {"rooms": ["סלון", "מטבח"], "mode": "vac_and_mop", "suction": "turbo"})
    assert client.calls == [("clean", dict(segment_ids=[16, 17], mode="vac_and_mop",
                                           suction="turbo", water_flow=None, repeat=1))]
    assert "living_room" in out and "kitchen" in out


def test_clean_unknown_room_refuses_without_calling():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "clean").impl({"rooms": ["garage"]})
    assert client.calls == []
    assert "garage" in out and "living_room" in out


def test_clean_bad_mode_refuses_without_calling():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "clean").impl({"mode": "polish"})
    assert client.calls == []
    assert "polish" in out.lower()


def test_clean_rooms_without_registry_is_friendly():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, None)
    out = _tool(tools, "clean").impl({"rooms": ["סלון"]})
    assert client.calls == []
    assert "whole home" in out.lower()


def test_clean_reports_error_friendly():
    from roborock_fakes import ExplodingRoborockClient
    tools = build_roborock_tools(ExplodingRoborockClient(), _reg())
    out = _tool(tools, "clean").impl({})
    assert "offline" in out


import pytest


@pytest.mark.parametrize("action,method", [
    ("pause", "pause"), ("resume", "resume"), ("stop", "stop"),
    ("return_to_dock", "return_to_dock"), ("locate", "locate"),
])
def test_control_vacuum_dispatches(action, method):
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "control_vacuum").impl({"action": action})
    assert client.calls == [(method, {})]
    assert "✅" in out


def test_control_vacuum_unknown_action():
    client = FakeRoborockClient()
    out = _tool(build_roborock_tools(client, _reg()), "control_vacuum").impl({"action": "fly"})
    assert client.calls == []
    assert "fly" in out.lower()


@pytest.mark.parametrize("action,method", [
    ("empty_bin", "empty_bin"), ("wash_mop", "wash_mop"), ("dry_mop", "dry_mop"),
])
def test_dock_action_dispatches(action, method):
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "dock_action").impl({"action": action})
    assert client.calls == [(method, {})]
    assert "✅" in out


def test_dock_action_unknown():
    client = FakeRoborockClient()
    out = _tool(build_roborock_tools(client, _reg()), "dock_action").impl({"action": "polish"})
    assert client.calls == []
    assert "polish" in out.lower()


def test_vacuum_status_summarizes_and_names_room():
    client = FakeRoborockClient(status={
        "state": "cleaning", "battery": 82, "cleaned_area": 12.5,
        "clean_time": 600, "segment_id": 16, "error": None})
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "vacuum_status").impl({})
    assert ("status", {}) in client.calls
    assert "cleaning" in out and "82" in out and "living_room" in out


def test_vacuum_status_reports_error_field():
    client = FakeRoborockClient(status={
        "state": "error", "battery": 40, "cleaned_area": 0, "clean_time": 0,
        "segment_id": None, "error": "stuck"})
    out = _tool(build_roborock_tools(client, _reg()), "vacuum_status").impl({})
    assert "stuck" in out


def test_consumables_summarizes():
    client = FakeRoborockClient(consumables={
        "main_brush": 80, "side_brush": 65, "filter": 40, "sensor": 90})
    out = _tool(build_roborock_tools(client, _reg()), "consumables").impl({})
    assert "main brush" in out.lower() and "80" in out
    assert "filter" in out.lower() and "40" in out


def test_schedule_clean_daily_whole_home():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "schedule_clean").impl({"time": "08:00"})
    assert client.calls == [("set_timer", dict(
        time="08:00", days=["sun", "mon", "tue", "wed", "thu", "fri", "sat"],
        segment_ids=None, mode=None, suction=None, water_flow=None))]
    assert "08:00" in out and "✅" in out


def test_schedule_clean_rooms_and_days():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "schedule_clean").impl(
        {"time": "20:30", "days": ["weekends"], "rooms": ["מטבח"], "mode": "mop"})
    assert client.calls == [("set_timer", dict(
        time="20:30", days=["sat", "sun"], segment_ids=[17],
        mode="mop", suction=None, water_flow=None))]


def test_schedule_clean_bad_time_refuses():
    client = FakeRoborockClient()
    out = _tool(build_roborock_tools(client, _reg()), "schedule_clean").impl({"time": "8pm"})
    assert client.calls == []
    assert "time" in out.lower()


def test_schedule_clean_unknown_room_refuses():
    client = FakeRoborockClient()
    out = _tool(build_roborock_tools(client, _reg()), "schedule_clean").impl(
        {"time": "08:00", "rooms": ["garage"]})
    assert client.calls == []
    assert "garage" in out


def test_get_cleaning_schedule_lists_timers():
    client = FakeRoborockClient(timers=[
        {"id": "7", "time": "08:00", "days": ["mon", "tue"], "enabled": True,
         "target": "whole home", "mode": None}])
    out = _tool(build_roborock_tools(client, _reg()), "get_cleaning_schedule").impl({})
    assert "08:00" in out and "7" in out


def test_get_cleaning_schedule_empty():
    out = _tool(build_roborock_tools(FakeRoborockClient(), _reg()), "get_cleaning_schedule").impl({})
    assert "nothing" in out.lower()


def test_cancel_cleaning_schedule_deletes():
    client = FakeRoborockClient(timers=[
        {"id": "7", "time": "08:00", "days": ["mon"], "enabled": True,
         "target": "whole home", "mode": None}])
    out = _tool(build_roborock_tools(client, _reg()), "cancel_cleaning_schedule").impl({"id": "7"})
    assert ("del_timer", {"timer_id": "7"}) in client.calls
    assert "✅" in out


def test_cancel_cleaning_schedule_unknown_id():
    client = FakeRoborockClient()
    out = _tool(build_roborock_tools(client, _reg()), "cancel_cleaning_schedule").impl({"id": "99"})
    assert "99" in out and "no" in out.lower()


def test_normalize_days_unknown_raises():
    from home_agent.roborock import _normalize_days
    with pytest.raises(ValueError):
        _normalize_days(["funday"])


def test_normalize_days_dedups_across_tokens_in_first_seen_order():
    from home_agent.roborock import _normalize_days
    # a bare day before its containing word: deduped, first-seen order preserved
    assert _normalize_days(["sat", "weekends"]) == ["sat", "sun"]
    # word then an overlapping bare day: no duplicate
    assert _normalize_days(["weekdays", "mon"]) == ["mon", "tue", "wed", "thu", "fri"]
