from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.model import ImmediateAction
from switchbot_scheduler.actuator import run_immediate, resolve_action


def _reg():
    return Registry([
        Device(name="living_room", aliases=[], ble_id="U1", inverted=True),
        Device(name="kitchen", aliases=[], ble_id="U2"),
        Device(name="ac", aliases=[], ble_id="U3", mode="press"),
        Device(name="no_ble", aliases=[], ble_id=""),
    ])


def test_resolve_action_plain():
    assert resolve_action("kitchen", "on", _reg()) == "on"


def test_resolve_action_inverted_swaps_on_off():
    assert resolve_action("living_room", "on", _reg()) == "off"
    assert resolve_action("living_room", "off", _reg()) == "on"


def test_resolve_action_press_mode_forces_press():
    assert resolve_action("ac", "on", _reg()) == "press"


def test_run_immediate_sends_correct_bytes():
    calls = []
    fake = lambda ble_id, code: calls.append((ble_id, code)) or b"\x01"
    results = run_immediate([ImmediateAction("kitchen", "on")], _reg(), actuate_fn=fake)
    assert calls == [("U2", 1)]                       # ACTION_CODE["on"] == 1
    assert results[0].ok is True and results[0].action == "on"


def test_run_immediate_inverted_and_press():
    calls = []
    fake = lambda ble_id, code: calls.append((ble_id, code)) or b""
    run_immediate([ImmediateAction("living_room", "on"), ImmediateAction("ac", "off")], _reg(), actuate_fn=fake)
    assert calls == [("U1", 2), ("U3", 0)]            # inverted on->off (2); press-mode ->press (0)


def test_run_immediate_unknown_device_is_reported_not_raised():
    fake = lambda ble_id, code: b""
    results = run_immediate([ImmediateAction("bedroom", "on")], _reg(), actuate_fn=fake)
    assert results[0].ok is False and "unknown" in results[0].error.lower()


def test_run_immediate_missing_ble_id_reported():
    fake = lambda ble_id, code: b""
    results = run_immediate([ImmediateAction("no_ble", "on")], _reg(), actuate_fn=fake)
    assert results[0].ok is False and "ble_id" in results[0].error


def test_run_immediate_ble_error_does_not_abort_others():
    def fake(ble_id, code):
        if ble_id == "U1":
            raise RuntimeError("out of range")
        return b""
    results = run_immediate([ImmediateAction("living_room", "on"), ImmediateAction("kitchen", "on")], _reg(), actuate_fn=fake)
    assert results[0].ok is False and "out of range" in results[0].error
    assert results[1].ok is True                       # second device still ran
