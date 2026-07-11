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
