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
