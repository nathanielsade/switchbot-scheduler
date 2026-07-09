from switchbot_scheduler.registry import Registry, Device
from home_agent.home import build_home_tools


def _registry():
    return Registry([
        Device(name="living_room", aliases=["סלון", "living room"], ble_id="ID1", inverted=True),
        Device(name="ac", aliases=["מזגן", "ac"], ble_id="ID2", mode="press"),
        Device(name="kitchen", aliases=["מטבח"], ble_id="ID3"),
    ])


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_list_devices_lists_names_aliases_and_type():
    tools = build_home_tools(_registry())
    out = _tool(tools, "list_devices").impl({})
    assert "living_room" in out and "סלון" in out
    assert "inverted" in out            # living_room type note
    assert "ac" in out and "toggle" in out
    assert "kitchen" in out and "מטבח" in out
