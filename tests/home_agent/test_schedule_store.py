from home_agent.schedule_store import ScheduleStore


def test_add_list_and_remove(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    s.add("kitchen", "on", "18:00", ["mon", "tue"], False)
    s.add("kitchen", "off", "23:00", ["thu"], True, fire_at="2026-07-09T23:00:00")
    rows = s.list("kitchen")
    assert [r["time"] for r in rows] == ["18:00", "23:00"]
    assert rows[0]["days"] == ["mon", "tue"] and rows[0]["once"] is False
    assert rows[1]["once"] is True and rows[1]["fire_at"] == "2026-07-09T23:00:00"


def test_list_all_and_isolation(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    s.add("kitchen", "on", "18:00", ["mon"], False)
    s.add("garden", "on", "19:00", ["mon"], False)
    assert {r["device"] for r in s.list()} == {"kitchen", "garden"}
    assert [r["device"] for r in s.list("garden")] == ["garden"]


def test_remove_all_and_by_time(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    s.add("kitchen", "on", "18:00", ["mon"], False)
    s.add("kitchen", "off", "23:00", ["mon"], False)
    assert s.remove("kitchen", "18:00") == 1
    assert [r["time"] for r in s.list("kitchen")] == ["23:00"]
    assert s.remove("kitchen") == 1
    assert s.list("kitchen") == []


def test_remove_id_and_expired(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    rid = s.add("kitchen", "on", "18:00", ["mon"], False)
    s.remove_id(rid)
    assert s.list() == []
    s.add("kitchen", "on", "08:00", ["thu"], True, fire_at="2026-07-09T08:00:00+00:00")
    s.add("kitchen", "on", "20:00", ["thu"], True, fire_at="2026-07-09T20:00:00+00:00")
    assert len(s.remove_expired("2026-07-09T12:00:00+00:00")) == 1   # only the 08:00 one is past
    assert [r["time"] for r in s.list("kitchen")] == ["20:00"]


def test_remove_expired_returns_deleted_rows(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    s.add("kitchen", "on", "08:00", ["thu"], True, fire_at="2026-07-09T05:00:00+00:00")
    s.add("kitchen", "on", "20:00", ["thu"], True, fire_at="2026-07-09T17:00:00+00:00")
    removed = s.remove_expired("2026-07-09T09:00:00+00:00")
    assert [r["time"] for r in removed] == ["08:00"]
    assert removed[0]["device"] == "kitchen"
    assert [r["time"] for r in s.list("kitchen")] == ["20:00"]


def test_remove_expired_orders_by_instant_not_wall_clock(tmp_path):
    # A UTC row that fires at 01:00 local on the 16th must NOT be expired by a
    # "now" that is 30 minutes earlier in real time. With local-tz strings this
    # comparison silently deleted live timers.
    s = ScheduleStore(str(tmp_path / "s.db"))
    s.add("kitchen", "on", "01:00", ["sun"], True, fire_at="2026-08-15T22:00:00+00:00")
    removed = s.remove_expired("2026-08-15T21:30:00+00:00")
    assert removed == []
    assert len(s.list("kitchen")) == 1


def test_usable_from_a_different_thread(tmp_path):
    import threading
    s = ScheduleStore(str(tmp_path / "s.db"))
    errors = []

    def worker():
        try:
            s.add("kitchen", "on", "18:00", ["mon"], False)
            assert len(s.list("kitchen")) == 1
        except Exception as e:
            errors.append(repr(e))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors == []
