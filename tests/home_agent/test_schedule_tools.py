from datetime import datetime, timezone
from home_agent.schedules import _normalize_days
from home_agent.agent import run_turn
from home_agent.prompts import FAMILY_SYSTEM_PROMPT


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


from zoneinfo import ZoneInfo
from home_agent.schedules import _resolve_fire_at

_TZ = ZoneInfo("Asia/Jerusalem")


def _fri_1721():
    # Friday 2026-08-14 17:21 local — the exact moment of the reported bug
    return datetime(2026, 8, 14, 17, 21, tzinfo=_TZ)


def test_tomorrow_never_resolves_to_today():
    got = _resolve_fire_at("tomorrow", "18:30", _fri_1721())
    assert got.date().isoformat() == "2026-08-15"      # Saturday, not Friday


def test_soonest_is_today_when_ahead_and_tomorrow_when_passed():
    assert _resolve_fire_at("soonest", "18:30", _fri_1721()).date().isoformat() == "2026-08-14"
    late = datetime(2026, 8, 14, 19, 0, tzinfo=_TZ)
    assert _resolve_fire_at("soonest", "18:30", late).date().isoformat() == "2026-08-15"


def test_today_errors_when_the_time_has_passed():
    import pytest
    late = datetime(2026, 8, 14, 19, 0, tzinfo=_TZ)
    with pytest.raises(ValueError):
        _resolve_fire_at("today", "18:30", late)


def test_weekday_counts_today_only_when_the_time_is_ahead():
    # today IS Friday
    assert _resolve_fire_at("fri", "18:30", _fri_1721()).date().isoformat() == "2026-08-14"
    late = datetime(2026, 8, 14, 19, 0, tzinfo=_TZ)
    assert _resolve_fire_at("fri", "18:30", late).date().isoformat() == "2026-08-21"


def test_day_after_tomorrow_and_in_a_week():
    assert _resolve_fire_at("day_after_tomorrow", "18:30", _fri_1721()).date().isoformat() == "2026-08-16"
    assert _resolve_fire_at("in_a_week", "18:30", _fri_1721()).date().isoformat() == "2026-08-21"


def test_unknown_token_raises():
    import pytest
    with pytest.raises(ValueError):
        _resolve_fire_at("friday", "18:30", _fri_1721())


def test_resolution_survives_the_dst_boundary():
    # Israel ends DST overnight 2026-10-24/25. "tomorrow 18:30" must be 18:30 WALL CLOCK
    # on the 25th, not 17:30 — which is what fixed-offset arithmetic would produce.
    from datetime import timedelta
    before = datetime(2026, 10, 24, 12, 0, tzinfo=_TZ)
    got = _resolve_fire_at("tomorrow", "18:30", before)
    assert got.date().isoformat() == "2026-10-25"
    assert (got.hour, got.minute) == (18, 30)
    # Assert the INSTANT, not just the wall clock: the wall-clock assertion above holds for
    # any plausible implementation (ZoneInfo recomputes the offset even for timedelta
    # arithmetic), so only the offset actually distinguishes correct from wrong here.
    assert got.utcoffset() == timedelta(hours=2)      # DST has ended by the 25th
    assert before.utcoffset() == timedelta(hours=3)   # it had not on the 24th


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


def test_schedule_device_has_no_days_property(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    props = _tool(tools, "schedule_device").schema["function"]["parameters"]["properties"]
    assert "days" not in props
    assert props["when"]["enum"][0] == "soonest"
    assert "when" in _tool(tools, "schedule_device").schema["function"]["parameters"]["required"]


def test_one_time_row_contract(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    row = store.list("dining")[0]
    assert row["once"] is True
    assert row["days"] == ["sat"]                     # weekday OF the resolved date
    assert row["fire_at"].endswith("+00:00")          # stored UTC


def test_recurring_row_contract_and_required_phrase(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    out = _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "days": ["fri"],
         "repetition_phrase": "כל שישי"})
    assert "RECURRING" in out
    row = store.list("dining")[0]
    assert row["once"] is False and row["fire_at"] is None

    # _registry() has only living_room/ac/dining — no kitchen, no garden.
    empty_phrase = _tool(tools, "schedule_recurring_device").impl(
        {"device": "סלון", "action": "on", "time": "18:30", "days": ["fri"],
         "repetition_phrase": "  "})
    assert "schedule_device" in empty_phrase
    assert store.list("living_room") == []


def test_schedule_one_time_sets_once_bit_and_correct_time(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    out = _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:29", "when": "soonest"})
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
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["weekdays"],
         "repetition_phrase": "כל יום חול"})
    _, alarms = writes[-1]
    assert not (alarms[0]["repeat_byte"] & 0x80)    # not one-time


def test_schedule_applies_inversion_and_press(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    _tool(tools, "schedule_device").impl(
        {"device": "סלון", "action": "on", "time": "18:00", "when": "soonest"})
    assert writes[-1][1][0]["action"] == 2          # inverted on -> off code 2
    _tool(tools, "schedule_device").impl(
        {"device": "מזגן", "action": "on", "time": "18:00", "when": "soonest"})
    assert writes[-1][1][0]["action"] == 0          # press-mode -> press code 0


def test_schedule_rewrites_full_set_for_device(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    st = _tool(tools, "schedule_recurring_device")
    st.impl({"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"],
             "repetition_phrase": "כל שני"})
    st.impl({"device": "פינת אוכל", "action": "off", "time": "23:00", "days": ["mon"],
             "repetition_phrase": "כל שני"})
    assert len(writes[-1][1]) == 2                  # 2nd write carries BOTH timers


def test_schedule_rejects_over_five_cap_and_rolls_back(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    st = _tool(tools, "schedule_recurring_device")
    for i in range(5):
        st.impl({"device": "פינת אוכל", "action": "on", "time": f"0{i}:00", "days": ["mon"],
                 "repetition_phrase": "כל שני"})
    out = st.impl({"device": "פינת אוכל", "action": "on", "time": "06:00", "days": ["mon"],
                   "repetition_phrase": "כל שני"})
    assert "5" in out or "max" in out.lower()
    assert len(store.list("dining")) == 5           # 6th rolled back
    assert len(writes) == 5                         # no write for the rejected 6th


def test_schedule_write_failure_rolls_back(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db"))

    def boom(ble_id, alarms):
        raise RuntimeError("out of range")

    tools = build_schedule_tools(_registry(), store, write_fn=boom, now_fn=_thu_1824)
    out = _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "when": "soonest"})
    assert "dining" in out and ("range" in out or "couldn't" in out.lower())
    assert store.list("dining") == []               # nothing persisted


def test_schedule_unknown_device(tmp_path):
    tools, _ = _tools(tmp_path, [])
    out = _tool(tools, "schedule_device").impl({"device": "garage", "action": "on", "time": "18:00"})
    assert "unknown device" in out.lower()


def test_get_schedule_lists_and_reports_device(tmp_path):
    writes = []
    tools, _ = _tools(tmp_path, writes)
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
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
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    out = _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל"})
    assert "dining" in out
    assert store.list("dining") == []
    assert writes[-1] == ("ID3", [])                # empty write clears the Bot


def test_cancel_one_by_time_keeps_the_rest(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    st = _tool(tools, "schedule_recurring_device")
    st.impl({"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"],
             "repetition_phrase": "כל שני"})
    st.impl({"device": "פינת אוכל", "action": "off", "time": "23:00", "days": ["mon"],
             "repetition_phrase": "כל שני"})
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
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    out = _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל"})
    assert "try again" in out.lower() or "not cancelled" in out.lower()
    assert len(store.list("dining")) == 1     # rolled back — record intact so a retry can re-try


def test_cancel_can_disambiguate_two_timers_at_the_same_clock_time(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)     # 2026-08-14 17:21, before 18:30
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "today"})
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    out = _tool(tools, "cancel_schedule").impl(
        {"device": "פינת אוכל", "time": "18:30", "when": "tomorrow"})
    assert "2026-08-15" in out
    remaining = store.list("dining")
    assert len(remaining) == 1 and remaining[0]["fire_at"].startswith("2026-08-14")


def test_cancel_with_a_date_leaves_recurring_rows_alone(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "days": ["mon"],
         "repetition_phrase": "כל יום שני"})
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    _tool(tools, "cancel_schedule").impl(
        {"device": "פינת אוכל", "time": "18:30", "when": "tomorrow"})
    remaining = store.list("dining")
    assert len(remaining) == 1 and remaining[0]["once"] is False


def test_fired_one_time_row_is_not_rearmed_by_a_later_write(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes)
    # a one-time row that has already fired (before the frozen clock _thu_1824)
    store.add("dining", "on", "08:00", ["thu"], True, "2026-07-09T05:00:00+00:00")
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "off", "time": "23:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    assert [r["time"] for r in store.list("dining")] == ["23:00"]
    # only the new alarm was written to the Bot, not the stale 08:00 one
    assert len(writes[-1][1]) == 1


def test_cancel_names_the_action_and_time_it_cancelled(tmp_path):
    # Regression: the confirmation used to omit the action, so the model called an "off" timer "on".
    writes = []
    tools, _ = _tools(tmp_path, writes)
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "פינת אוכל", "action": "off", "time": "20:00", "days": ["mon"],
         "repetition_phrase": "כל שני"})
    out = _tool(tools, "cancel_schedule").impl({"device": "פינת אוכל", "time": "20:00"})
    assert "off" in out and "20:00" in out    # names the real action + time, not a guess


def test_one_time_confirmation_states_the_date(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    out = _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    assert "2026-08-15" in out and "Sat" in out and "ONE-TIME" in out


def test_a_week_out_is_refused_for_ble(tmp_path):
    # I1 regression: a BLE one-time row has no date on the wire, so a row more than a day
    # out risks firing tonight under the unfavourable once-bit reading — and in_a_week
    # resolves to today's own weekday, which is exactly the case that must be refused.
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    out = _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "in_a_week"})
    assert "✅" not in out and "day" in out.lower()
    assert store.list("dining") == []          # nothing left behind
    assert writes == []


def test_two_days_out_is_refused_for_ble_and_nothing_persists(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    out = _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "day_after_tomorrow"})
    assert "✅" not in out
    assert store.list("dining") == []
    assert writes == []


def test_tomorrow_still_works_for_ble(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    out = _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    assert "✅" in out
    assert store.list("dining")[0]["fire_at"].startswith("2026-08-15")
    assert writes and len(writes[-1][1]) == 1


def test_get_schedule_lists_dates_and_marks_recurring(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30", "when": "tomorrow"})
    _tool(tools, "schedule_recurring_device").impl(
        {"device": "סלון", "action": "on", "time": "07:00", "days": ["mon"],
         "repetition_phrase": "כל יום שני"})
    out = _tool(tools, "get_schedule").impl({})
    assert "2026-08-15" in out and "ONE-TIME" in out
    assert "RECURRING" in out


def test_both_schedule_tools_are_offered_and_steering_text_is_present(tmp_path, make_fake_client):
    """Tool SELECTION cannot be exercised offline: the fake client's script hardcodes which
    tool "the model" calls, so no assertion on that call can prove real steering behaviour.
    What we CAN verify offline is the steering SURFACE the model would actually read: that
    both tools are offered (so there is a real choice to make) and that the specific clauses
    telling it to prefer schedule_device for a single named weekday are present verbatim, in
    both the schedule_recurring_device schema and the system prompt."""
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "schedule_device",
                         "arguments": {"device": "סלון", "action": "on", "time": "18:30",
                                       "when": "sat"}}]},
        {"content": "קבעתי להדלקה בשבת בשעה 18:30, פעם אחת."},
    ])
    writes = []
    tools, _store = _tools(tmp_path, writes, now=_fri_1721)
    run_turn("תדליק את האור בשבת ב-18:30", [], client=client, model="gpt-4o",
             system=FAMILY_SYSTEM_PROMPT, tools=tools)
    sent_tools = {t["function"]["name"] for t in client._calls[0]["tools"]}
    assert "schedule_device" in sent_tools
    assert "schedule_recurring_device" in sent_tools               # a real choice existed
    recurring_desc = _tool(tools, "schedule_recurring_device").schema["function"]["description"]
    assert "use schedule_device instead" in recurring_desc
    assert "one-time by default" in FAMILY_SYSTEM_PROMPT


def test_model_states_the_date_in_its_reply(tmp_path, make_fake_client):
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "schedule_device",
                         "arguments": {"device": "פינת אוכל", "action": "on",
                                       "time": "18:30", "when": "tomorrow"}}]},
        {"content": "תזמנתי למחר, 2026-08-15, בשעה 18:30."},
    ])
    writes = []
    tools, _store = _tools(tmp_path, writes, now=_fri_1721)
    reply = run_turn("תזמן את האור למחר ב-18:30", [], client=client, model="gpt-4o",
                     system=FAMILY_SYSTEM_PROMPT, tools=tools)
    assert "2026-08-15" in reply
    # and the tool result the model was given carried that date in the first place
    tool_msg = [m for m in client._calls[-1]["messages"] if m.get("role") == "tool"][0]
    assert "2026-08-15" in tool_msg["content"]


def test_several_devices_in_one_turn(tmp_path):
    """The real request was four devices at once (this registry has three); a partial
    failure must not half-apply."""
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    for dev in ["סלון", "פינת אוכל", "מזגן"]:
        out = _tool(tools, "schedule_device").impl(
            {"device": dev, "action": "on", "time": "18:30", "when": "tomorrow"})
        assert "✅" in out and "2026-08-15" in out
    assert all(r["fire_at"].startswith("2026-08-15") for r in store.list())


def test_tomorrow_is_stable_across_midnight(tmp_path):
    from datetime import datetime
    late = lambda: datetime(2026, 8, 14, 23, 0, tzinfo=_TZ)
    early = lambda: datetime(2026, 8, 15, 1, 0, tzinfo=_TZ)
    assert _resolve_fire_at("tomorrow", "18:30", late()).date().isoformat() == "2026-08-15"
    assert _resolve_fire_at("tomorrow", "18:30", early()).date().isoformat() == "2026-08-16"


def test_missing_when_errors_and_stores_nothing(tmp_path):
    writes = []
    tools, store = _tools(tmp_path, writes, now=_fri_1721)
    out = _tool(tools, "schedule_device").impl(
        {"device": "פינת אוכל", "action": "on", "time": "18:30"})
    assert "when" in out.lower()
    assert store.list() == []


def test_tool_error_is_fed_back_to_the_model(tmp_path, make_fake_client):
    """A rejected call must come back as a tool message the model can recover from."""
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "schedule_device",
                         "arguments": {"device": "פינת אוכל", "action": "on",
                                       "time": "18:30", "when": "friday"}}]},
        {"content": "לא הצלחתי, אנסה שוב"},
    ])
    writes = []
    tools, _store = _tools(tmp_path, writes, now=_fri_1721)
    run_turn("תזמן משהו", [], client=client, model="gpt-4o",
             system=FAMILY_SYSTEM_PROMPT, tools=tools)
    tool_msg = [m for m in client._calls[-1]["messages"] if m.get("role") == "tool"][0]
    assert "unknown when" in tool_msg["content"]      # strict enum: "friday" is rejected
