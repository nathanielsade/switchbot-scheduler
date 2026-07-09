from pathlib import Path
import pytest
from switchbot_scheduler.registry import Registry, Device
from switchbot_scheduler.core import apply_schedule

CANNED = (Path(__file__).parent / "fixtures" / "parser_living_room.json").read_text()


def _reg():
    return Registry([Device(name="living_room", aliases=[], ble_id="U1")])


def _fn(system, user):
    return CANNED


def test_dry_run_returns_readback_and_does_not_write():
    wrote = []
    outcome, text, sched = apply_schedule(
        "living room 6 to 5", _reg(), dry_run=True,
        writer=lambda s, r: wrote.append(s), completion_fn=_fn,
    )
    assert outcome == "dry_run"
    assert "living_room: on 06:00 — every day" in text
    assert wrote == []


def test_decline_at_confirm_does_not_write():
    wrote = []
    outcome, _, _ = apply_schedule(
        "living room 6 to 5", _reg(), dry_run=False,
        confirm=lambda text: False,
        writer=lambda s, r: wrote.append(s), completion_fn=_fn,
    )
    assert outcome == "cancelled"
    assert wrote == []


def test_confirm_yes_writes():
    wrote = []
    outcome, _, _ = apply_schedule(
        "living room 6 to 5", _reg(), dry_run=False,
        confirm=lambda text: True,
        writer=lambda s, r: wrote.append(s), completion_fn=_fn,
    )
    assert outcome == "written"
    assert len(wrote) == 1


def test_dry_run_false_without_writer_raises():
    with pytest.raises(ValueError):
        apply_schedule("living room 6 to 5", _reg(), dry_run=False,
                       confirm=lambda text: False, writer=None, completion_fn=_fn)


import json as _json
from datetime import datetime as _dt
from switchbot_scheduler.core import preview_conversation

def test_preview_conversation_schedule():
    canned = lambda s, u: _json.dumps({"schedules": [{"device": "living_room",
        "events": [{"time": "09:00", "action": "on", "days": ["mon"], "once": True}]}]})
    res = preview_conversation(["living room on tomorrow, once"], _reg(),
                               _dt(2026, 7, 5, 12, 0), completion_fn=canned)
    assert res.clarification is None
    assert res.schedule is not None and "once (mon)" in res.readback
    assert res.immediate == []


def test_preview_conversation_clarification():
    canned = lambda s, u: _json.dumps({"clarification": "Which device?"})
    res = preview_conversation(["do it"], _reg(), _dt(2026, 7, 5, 12, 0), completion_fn=canned)
    assert res.clarification == "Which device?" and res.schedule is None and res.immediate == []


def test_preview_conversation_immediate_only():
    canned = lambda s, u: _json.dumps({"immediate": [{"device": "living_room", "action": "on"}]})
    res = preview_conversation(["salon on now"], _reg(), _dt(2026, 7, 5, 12, 0), completion_fn=canned)
    assert res.schedule is None and res.readback is None
    assert len(res.immediate) == 1 and res.immediate[0].action == "on"


def test_preview_conversation_mixed():
    canned = lambda s, u: _json.dumps({
        "immediate": [{"device": "living_room", "action": "on"}],
        "schedules": [{"device": "living_room",
            "events": [{"time": "22:00", "action": "off", "days": ["mon"], "once": False}]}]})
    res = preview_conversation(["salon on now, off 22:00 mon"], _reg(), _dt(2026, 7, 5, 12, 0), completion_fn=canned)
    assert len(res.immediate) == 1
    assert res.schedule is not None and "living_room: off 22:00" in res.readback
