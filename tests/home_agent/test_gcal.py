from datetime import datetime, timezone
from home_agent.gcal import build_calendar_tools
from home_agent.calendar_pending import CalendarPending


class _Exec:
    def __init__(self, r): self._r = r
    def execute(self): return self._r


class _Events:
    def __init__(self, by_cal): self.by_cal = by_cal; self.calls = []
    def list(self, **kw): self.calls.append(("list", kw)); return _Exec({"items": self.by_cal.get(kw["calendarId"], [])})
    def insert(self, **kw): self.calls.append(("insert", kw)); return _Exec({"id": "newid"})
    def patch(self, **kw): self.calls.append(("patch", kw)); return _Exec({})
    def delete(self, **kw): self.calls.append(("delete", kw)); return _Exec({})


class _Service:
    def __init__(self, by_cal): self._e = _Events(by_cal)
    def events(self): return self._e


def _now():
    return datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)


def _ev(uid, start, summary, eid):
    return {"iCalUID": uid, "id": eid, "summary": summary,
            "start": {"dateTime": start}, "end": {"dateTime": start}}


def _tools(by_cal, tmp_path, cal_ids=("fam", "me"), write="fam"):
    svc = _Service(by_cal)
    store = CalendarPending(str(tmp_path / "c.db"))
    return {t.name: t for t in build_calendar_tools(svc, store, 1, None, calendar_ids=list(cal_ids),
                                                     write_id=write, now_fn=_now)}, svc, store


def test_find_events_dedups_same_instance_prefers_write_calendar(tmp_path):
    # same event (uid A, same start) on both calendars → one result, from the write calendar
    ev_fam = _ev("A", "2026-07-11T10:00:00+03:00", "Dentist", "efam")
    ev_me = _ev("A", "2026-07-11T10:00:00+03:00", "Dentist", "eme")
    tools, _, _ = _tools({"fam": [ev_fam], "me": [ev_me]}, tmp_path)
    out = tools["find_events"].impl({})
    assert out.count("Dentist") == 1
    assert "ref:fam|efam" in out            # preferred the write-calendar copy


def test_find_events_keeps_distinct_recurring_instances(tmp_path):
    i1 = _ev("W", "2026-07-11T18:00:00+03:00", "Class", "w1")
    i2 = _ev("W", "2026-07-18T18:00:00+03:00", "Class", "w2")   # same uid, different start
    tools, _, _ = _tools({"fam": [i1, i2], "me": []}, tmp_path)
    out = tools["find_events"].impl({})
    assert out.count("Class") == 2          # both instances kept


def test_find_events_query_uses_wide_range(tmp_path):
    tools, svc, _ = _tools({"fam": [], "me": []}, tmp_path)
    tools["find_events"].impl({"query": "dentist"})
    kw = svc.events().calls[0][1]
    # query → now-30d .. now+180d
    assert kw["timeMin"].startswith("2026-06-10") and kw["timeMax"].startswith("2027-01-06")
    assert kw["q"] == "dentist"


def test_find_events_default_range_one_week(tmp_path):
    tools, svc, _ = _tools({"fam": [], "me": []}, tmp_path)
    tools["find_events"].impl({})
    kw = svc.events().calls[0][1]
    assert kw["timeMin"].startswith("2026-07-10") and kw["timeMax"].startswith("2026-07-17")


def test_find_events_dedups_same_instant_across_offsets(tmp_path):
    # same event (uid A), same instant expressed in different offsets on the two calendars
    fam = _ev("A", "2026-07-11T10:00:00+03:00", "Dentist", "efam")
    me = _ev("A", "2026-07-11T07:00:00+00:00", "Dentist", "eme")
    tools, _, _ = _tools({"fam": [fam], "me": [me]}, tmp_path)
    out = tools["find_events"].impl({})
    assert out.count("Dentist") == 1          # same instant -> deduped despite different offset strings
    assert "ref:fam|efam" in out               # preferred the write-calendar copy


def test_prepare_create_stages_and_no_google_call(tmp_path):
    tools, svc, store = _tools({"fam": [], "me": []}, tmp_path)
    out = tools["prepare_calendar_change"].impl(
        {"action": "create", "title": "רופא שיניים", "start": "2026-07-14T15:00:00+03:00"})
    assert "כן" in out or "confirm" in out.lower()
    assert store.current(1)["payload"]["title"] == "רופא שיניים"
    assert svc.events().calls == []          # nothing sent to Google


def test_prepare_create_requires_title_and_start(tmp_path):
    tools, _, store = _tools({"fam": [], "me": []}, tmp_path)
    out = tools["prepare_calendar_change"].impl({"action": "create", "title": "x"})   # no start
    assert "title" in out or "start" in out
    assert store.current(1) is None


def test_prepare_update_delete_require_write_calendar_ref(tmp_path):
    tools, _, store = _tools({"fam": [], "me": []}, tmp_path)
    # ref on a personal calendar → refused
    out = tools["prepare_calendar_change"].impl({"action": "delete", "ref": "me|e1"})
    assert "family" in out.lower()
    assert store.current(1) is None
    # ref on the write calendar → staged
    ok = tools["prepare_calendar_change"].impl({"action": "delete", "ref": "fam|e1"})
    assert store.current(1)["payload"] == {"action": "delete", "ref": "fam|e1"}


def test_commit_refuses_same_turn(tmp_path):
    # committable_id snapshot is None (nothing staged before the turn); staging now → id != None → refuse
    tools, svc, store = _tools({"fam": [], "me": []}, tmp_path)   # committable_id=None
    tools["prepare_calendar_change"].impl(
        {"action": "create", "title": "x", "start": "2026-07-14T15:00:00+03:00"})
    out = tools["commit_calendar_change"].impl({})
    assert "כן" in out or "reply" in out.lower()
    assert not any(c[0] == "insert" for c in svc.events().calls)   # NOT applied
    assert store.current(1) is not None                            # still staged


def test_commit_applies_when_staged_in_prior_turn(tmp_path):
    svc = _Service({"fam": [], "me": []})
    store = CalendarPending(str(tmp_path / "c.db"))
    sid = store.stage(1, {"action": "create", "title": "x", "start": "2026-07-14T15:00:00+03:00",
                          "end": None, "all_day": False, "notes": None}, _now().isoformat())
    # committable_id == the prior-turn staged id
    tools = {t.name: t for t in build_calendar_tools(svc, store, 1, sid, calendar_ids=["fam", "me"],
                                                     write_id="fam", now_fn=_now)}
    out = tools["commit_calendar_change"].impl({})
    assert "✅" in out
    ins = [c for c in svc.events().calls if c[0] == "insert"]
    assert ins and ins[0][1]["calendarId"] == "fam"
    assert ins[0][1]["body"]["summary"] == "x"
    assert store.current(1) is None                                # cleared


def test_commit_all_day_uses_exclusive_end(tmp_path):
    svc = _Service({"fam": [], "me": []})
    store = CalendarPending(str(tmp_path / "c.db"))
    sid = store.stage(1, {"action": "create", "title": "trip", "start": "2026-07-14T00:00:00+03:00",
                          "end": None, "all_day": True, "notes": None}, _now().isoformat())
    tools = {t.name: t for t in build_calendar_tools(svc, store, 1, sid, calendar_ids=["fam"],
                                                     write_id="fam", now_fn=_now)}
    tools["commit_calendar_change"].impl({})
    body = [c for c in svc.events().calls if c[0] == "insert"][0][1]["body"]
    assert body["start"] == {"date": "2026-07-14"}
    assert body["end"] == {"date": "2026-07-15"}                   # exclusive: one-day → +1


def test_commit_expired(tmp_path):
    from datetime import timedelta
    svc = _Service({"fam": [], "me": []})
    store = CalendarPending(str(tmp_path / "c.db"))
    old = (_now() - timedelta(minutes=30)).isoformat()
    sid = store.stage(1, {"action": "delete", "ref": "fam|e1"}, old)
    tools = {t.name: t for t in build_calendar_tools(svc, store, 1, sid, calendar_ids=["fam"],
                                                     write_id="fam", now_fn=_now)}
    out = tools["commit_calendar_change"].impl({})
    assert "expired" in out.lower()
    assert not any(c[0] == "delete" for c in svc.events().calls)


def test_cancel_clears(tmp_path):
    tools, svc, store = _tools({"fam": [], "me": []}, tmp_path)
    tools["prepare_calendar_change"].impl({"action": "create", "title": "x", "start": "2026-07-14T15:00:00+03:00"})
    tools["cancel_calendar_change"].impl({})
    assert store.current(1) is None
