from datetime import datetime, timezone
from home_agent.schedules import _normalize_days, _one_time_target


def _thu_1824():
    # Thursday 2026-07-09 18:24 (fixed clock for deterministic tests)
    return datetime(2026, 7, 9, 18, 24, tzinfo=timezone.utc)


def test_normalize_days_words_and_explicit():
    assert _normalize_days(["daily"]) == ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    assert _normalize_days(["weekdays"]) == ["mon", "tue", "wed", "thu", "fri"]
    assert _normalize_days(["weekends"]) == ["sun", "sat"]      # DAYS order
    assert _normalize_days(["tue", "sun"]) == ["sun", "tue"]      # DAYS order
    assert _normalize_days(["mon", "mon"]) == ["mon"]            # dedupe


def test_normalize_days_bad_day_raises():
    import pytest
    with pytest.raises(ValueError):
        _normalize_days(["funday"])


def test_one_time_target_today_when_time_ahead():
    day, fire_at = _one_time_target("18:29", _thu_1824())
    assert day == "thu"
    assert fire_at.startswith("2026-07-09T18:29")


def test_one_time_target_rolls_to_next_day_when_past():
    day, fire_at = _one_time_target("18:00", _thu_1824())   # already past 18:24
    assert day == "fri"
    assert fire_at.startswith("2026-07-10T18:00")


from switchbot_scheduler.registry import Registry, Device
from home_agent.schedule_store import ScheduleStore
from home_agent.schedules import build_schedule_tools


def _registry():
    return Registry([
        Device(name="living_room", aliases=["סלון"], ble_id="ID1", inverted=True),
        Device(name="ac", aliases=["מזגן"], ble_id="ID2", mode="press"),
        Device(name="dining", aliases=["פינת אוכל"], ble_id="ID3"),
    ])


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _tools(tmp_path, writes, now=None):
    store = ScheduleStore(str(tmp_path / "s.db"))
    return build_schedule_tools(
        _registry(), store,
        write_fn=lambda ble_id, alarms: writes.append((ble_id, alarms)),
        now_fn=(now or _thu_1824)), store


def test_schedule_one_time_sets_once_bit_and_correct_time(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    out = _tool(tools, "schedule_device").impl({"device": "פינת אוכל", "action": "on", "time": "18:29"})
    assert "dining" in out and "✅" in out
    ble_id, alarms = writes[-1]
    assert ble_id == "ID3" and len(alarms) == 1
    assert alarms[0]["hour"] == 18 and alarms[0]["minute"] == 29
    assert alarms[0]["repeat_byte"] & 0x80          # one-time bit set
    row = store.list("dining")[0]
    assert row["once"] is True and row["days"] == ["thu"]


def test_schedule_recurring_expands_days_no_once_bit(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["weekdays"]})
    _, alarms = writes[-1]
    assert not (alarms[0]["repeat_byte"] & 0x80)    # not one-time


def test_schedule_applies_inversion_and_press(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    _tool(tools, "schedule_device").impl({"device": "סלון", "action": "on", "time": "18:00"})
    assert writes[-1][1][0]["action"] == 2          # inverted on -> off code 2
    _tool(tools, "schedule_device").impl({"device": "מזגן", "action": "on", "time": "18:00"})
    assert writes[-1][1][0]["action"] == 0          # press-mode -> press code 0


def test_schedule_rewrites_full_set_for_device(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    st = _tool(tools, "schedule_device")
    st.impl({"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]})
    st.impl({"device": "פינת אוכל", "action": "off", "time": "23:00", "days": ["mon"]})
    assert len(writes[-1][1]) == 2                  # 2nd write carries BOTH timers


def test_schedule_rejects_over_five_cap_and_rolls_back(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    st = _tool(tools, "schedule_device")
    for i in range(5):
        st.impl({"device": "פינת אוכל", "action": "on", "time": f"0{i}:00", "days": ["mon"]})
    out = st.impl({"device": "פינת אוכל", "action": "on", "time": "06:00", "days": ["mon"]})
    assert "5" in out or "max" in out.lower()
    assert len(store.list("dining")) == 5           # 6th rolled back
    assert len(writes) == 5                         # no write for the rejected 6th


def test_schedule_write_failure_rolls_back(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db"))

    def boom(ble_id, alarms):
        raise RuntimeError("out of range")

    tools = build_schedule_tools(_registry(), store, write_fn=boom, now_fn=_thu_1824)
    out = _tool(tools, "schedule_device").impl({"device": "פינת אוכל", "action": "on", "time": "18:00"})
    assert "dining" in out and ("range" in out or "couldn't" in out.lower())
    assert store.list("dining") == []               # nothing persisted


def test_schedule_unknown_device(tmp_path):
    tools, _ = _tools(tmp_path, [])
    out = _tool(tools, "schedule_device").impl({"device": "garage", "action": "on", "time": "18:00"})
    assert "unknown device" in out.lower()


def test_get_schedule_lists_and_reports_device(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]})
    out = _tool(tools, "get_schedule").impl({"device": "פינת אוכל"})
    assert "dining" in out and "18:00" in out


def test_get_schedule_empty(tmp_path):
    tools, _ = _tools(tmp_path, [])
    assert "nothing" in _tool(tools, "get_schedule").impl({}).lower()


def test_get_schedule_expires_past_one_time(tmp_path):
    writes = []
    store = ScheduleStore(str(tmp_path / "s.db"))
    # a one-time that already fired (fire_at before our frozen now)
    store.add("dining", "on", "08:00", ["thu"], True, fire_at="2026-07-09T08:00:00+00:00")
    tools = build_schedule_tools(_registry(), store,
                                 write_fn=lambda b, a: writes.append((b, a)), now_fn=_thu_1824)
    out = _tool(tools, "get_schedule").impl({})
    assert "nothing" in out.lower()                 # expired, not shown
    assert store.list("dining") == []               # and removed from the record


def test_cancel_all_clears_the_bot(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]})
    out = _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל"})
    assert "dining" in out
    assert store.list("dining") == []
    assert writes[-1] == ("ID3", [])                # empty write clears the Bot


def test_cancel_one_by_time_keeps_the_rest(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    st = _tool(tools, "schedule_device")
    st.impl({"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]})
    st.impl({"device": "פינת אוכל", "action": "off", "time": "23:00", "days": ["mon"]})
    _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל", "time": "18:00"})
    assert [r["time"] for r in store.list("dining")] == ["23:00"]
    assert len(writes[-1][1]) == 1                  # rewrote the remaining one


def test_cancel_nothing_matched(tmp_path):
    tools, _ = _tools(tmp_path, [])
    out = _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל", "time": "09:00"})
    assert "nothing" in out.lower()


def test_cancel_write_failure_rolls_back(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db"))
    n = {"c": 0}

    def flaky(ble_id, alarms):
        n["c"] += 1
        if n["c"] == 2:            # 1st write (the schedule) ok; 2nd (the cancel) fails
            raise RuntimeError("out of range")

    tools = build_schedule_tools(_registry(), store, write_fn=flaky, now_fn=_thu_1824)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]})
    out = _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל"})
    assert "try again" in out.lower() or "not cancelled" in out.lower()
    assert len(store.list("dining")) == 1     # rolled back — record intact so a retry can re-try


def test_cancel_names_the_action_and_time_it_cancelled(tmp_path):
    # Regression: the confirmation used to omit the action, so the model called an "off" timer "on".
    writes = []
    tools, _ = _tools(tmp_path, writes)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "off", "time": "20:00", "days": ["mon"]})
    out = _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל", "time": "20:00"})
    assert "off" in out and "20:00" in out    # names the real action + time, not a guess
