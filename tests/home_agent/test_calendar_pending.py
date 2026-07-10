from home_agent.calendar_pending import CalendarPending


def test_stage_current_clear(tmp_path):
    p = CalendarPending(str(tmp_path / "c.db"))
    assert p.current(1) is None
    i1 = p.stage(1, {"action": "create", "title": "x"}, "2026-07-10T08:00:00+03:00")
    cur = p.current(1)
    assert cur["id"] == i1 and cur["payload"]["title"] == "x"
    assert cur["created_at"] == "2026-07-10T08:00:00+03:00"
    p.clear(1)
    assert p.current(1) is None


def test_stage_replaces_and_new_id(tmp_path):
    p = CalendarPending(str(tmp_path / "c.db"))
    i1 = p.stage(1, {"action": "create"}, "2026-07-10T08:00:00+03:00")
    i2 = p.stage(1, {"action": "delete"}, "2026-07-10T08:05:00+03:00")
    assert i2 != i1                       # fresh id per staging (the same-turn guard depends on this)
    assert p.current(1)["payload"]["action"] == "delete"   # only the latest survives


def test_chats_isolated(tmp_path):
    p = CalendarPending(str(tmp_path / "c.db"))
    p.stage(1, {"a": 1}, "t")
    p.stage(2, {"a": 2}, "t")
    assert p.current(1)["payload"] == {"a": 1}
    assert p.current(2)["payload"] == {"a": 2}


def test_usable_from_a_different_thread(tmp_path):
    import threading
    p = CalendarPending(str(tmp_path / "c.db"))
    errs = []

    def worker():
        try:
            p.stage(7, {"a": 1}, "t")
            assert p.current(7)["payload"] == {"a": 1}
        except Exception as e:
            errs.append(repr(e))

    t = threading.Thread(target=worker); t.start(); t.join()
    assert errs == []
