from home_agent.schedule_store import ScheduleStore

def test_list_includes_row_id(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    rid = s.add("garden", "on", "18:00", ["mon"], False, None)
    rows = s.list("garden")
    assert rows[0]["id"] == rid
