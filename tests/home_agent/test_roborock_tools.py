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
