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


def test_control_device_resolves_alias_and_fires():
    calls = []
    tools = build_home_tools(_registry(), actuate_fn=lambda ble_id, code: calls.append((ble_id, code)))
    out = _tool(tools, "control_device").impl({"device": "מטבח", "action": "on"})
    assert calls == [("ID3", 1)]        # kitchen on → code 1
    assert "kitchen" in out and "✅" in out


def test_control_device_applies_inversion():
    calls = []
    tools = build_home_tools(_registry(), actuate_fn=lambda b, c: calls.append((b, c)))
    _tool(tools, "control_device").impl({"device": "סלון", "action": "on"})
    assert calls == [("ID1", 2)]        # inverted: on → off → code 2


def test_control_device_ac_is_press_mode():
    calls = []
    tools = build_home_tools(_registry(), actuate_fn=lambda b, c: calls.append((b, c)))
    _tool(tools, "control_device").impl({"device": "מזגן", "action": "on"})
    assert calls == [("ID2", 0)]        # press-mode: on → press → code 0


def test_control_device_unknown_device_is_friendly():
    out = _tool(build_home_tools(_registry()), "control_device").impl({"device": "garage", "action": "on"})
    assert "unknown device" in out.lower()
    assert "kitchen" in out             # lists known devices
