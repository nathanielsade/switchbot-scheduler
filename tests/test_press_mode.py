import json
from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.core import build_schedule
from switchbot_scheduler.readback import readback


def test_registry_mode_defaults_and_press():
    r = Registry([
        Device(name="ac", aliases=[], ble_id="U1", mode="press"),
        Device(name="light", aliases=[], ble_id="U2"),   # mode defaults to "switch"
    ])
    assert r.is_press_mode("ac") is True
    assert r.is_press_mode("light") is False


def test_press_mode_normalizes_on_off_to_press():
    reg = Registry([Device(name="ac", aliases=[], ble_id="U1", mode="press")])
    canned = json.dumps({"schedules": [{"device": "ac", "events": [
        {"time": "20:00", "action": "on", "days": ["mon"]},
        {"time": "22:00", "action": "off", "days": ["mon"]},
    ]}]})
    sched = build_schedule("ac on at 8pm off at 10pm", reg, completion_fn=lambda s, u: canned)
    # on/off intent collapses to a single press on a press-mode Bot
    assert [e.action for e in sched.schedules[0].events] == ["press", "press"]
    # read-back is faithful — it shows press, not on/off
    assert "press" in readback(sched)
    assert " on " not in readback(sched) and "off" not in readback(sched)


def test_switch_mode_actions_unchanged():
    reg = Registry([Device(name="light", aliases=[], ble_id="U2")])
    canned = json.dumps({"schedules": [{"device": "light", "events": [
        {"time": "06:00", "action": "on", "days": ["mon"]},
    ]}]})
    sched = build_schedule("light on 6am", reg, completion_fn=lambda s, u: canned)
    assert sched.schedules[0].events[0].action == "on"
