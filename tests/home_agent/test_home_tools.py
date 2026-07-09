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


def test_battery_status_all_devices():
    tools = build_home_tools(_registry(), battery_fn=lambda b: {"ID1": 88, "ID2": 40, "ID3": 15}[b])
    out = _tool(tools, "battery_status").impl({})
    assert "living_room: 88%" in out
    assert "ac: 40%" in out
    assert "kitchen: 15%" in out


def test_battery_status_single_device():
    tools = build_home_tools(_registry(), battery_fn=lambda b: 55)
    out = _tool(tools, "battery_status").impl({"device": "מטבח"})
    assert out.strip() == "kitchen: 55%"


def test_battery_status_isolates_a_failure():
    def bf(ble_id):
        if ble_id == "ID2":
            raise RuntimeError("timeout")
        return 90
    tools = build_home_tools(_registry(), battery_fn=bf)
    out = _tool(tools, "battery_status").impl({})
    assert "ac: unavailable — timeout" in out
    assert "living_room: 90%" in out    # other devices still reported


def test_load_home_tools_missing_file_returns_empty(tmp_path):
    from home_agent.home import load_home_tools
    from home_agent.config import Config
    cfg = Config(openai_api_key="x", telegram_bot_token="t:t", allowed_chat_ids={1},
                 devices_path=str(tmp_path / "nope.yaml"))
    assert load_home_tools(cfg) == []


def test_load_home_tools_present_file_builds_three(tmp_path):
    from home_agent.home import load_home_tools
    from home_agent.config import Config
    dev = tmp_path / "devices.yaml"
    dev.write_text("devices:\n  kitchen:\n    aliases: [מטבח]\n    ble_id: ID3\n")
    cfg = Config(openai_api_key="x", telegram_bot_token="t:t", allowed_chat_ids={1},
                 devices_path=str(dev))
    assert {t.name for t in load_home_tools(cfg)} == {"control_device", "list_devices", "battery_status"}


def test_control_device_reports_requested_action_for_inverted():
    tools = build_home_tools(_registry(), actuate_fn=lambda b, c: None)
    out = _tool(tools, "control_device").impl({"device": "סלון", "action": "on"})
    assert "living_room: on ✅" in out   # reports intent, not the resolved 'off'


def test_control_device_reports_press_for_ac():
    tools = build_home_tools(_registry(), actuate_fn=lambda b, c: None)
    out = _tool(tools, "control_device").impl({"device": "מזגן", "action": "on"})
    assert "ac: press ✅" in out
