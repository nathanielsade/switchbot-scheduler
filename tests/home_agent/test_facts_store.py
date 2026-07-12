from home_agent.facts import FactStore


def _store(tmp_path):
    return FactStore(str(tmp_path / "facts.db"))


def test_add_and_active_newest_first(tmp_path):
    s = _store(tmp_path)
    s.add("gate code", "1234", "נתנאל", "2026-07-12T10:00:00")
    s.add("passports", "in the safe", "שרי", "2026-07-12T11:00:00")
    rows = s.active()
    assert [r["subject"] for r in rows] == ["passports", "gate code"]  # newest first
    assert rows[0] == {"id": 2, "subject": "passports", "fact": "in the safe",
                       "author": "שרי", "created_at": "2026-07-12T11:00:00"}


def test_find_active_matches_subject_and_fact_case_insensitive(tmp_path):
    s = _store(tmp_path)
    s.add("gate code", "1234", "נתנאל", "t1")
    s.add("wifi", "the PassWord is abc", "נתנאל", "t2")
    assert [r["subject"] for r in s.find_active("GATE")] == ["gate code"]   # subject, case-insensitive
    assert [r["subject"] for r in s.find_active("password")] == ["wifi"]    # fact text, case-insensitive
    assert s.find_active("nonexistent") == []


def test_forget_flips_status_and_is_idempotent(tmp_path):
    s = _store(tmp_path)
    fid = s.add("gate code", "1234", "נתנאל", "t1")
    s.forget(fid)
    assert s.active() == []              # gone from active
    assert s.find_active("gate") == []   # and from matches
    s.forget(fid)                        # idempotent — no error


def test_connection_per_op_persists_across_instances(tmp_path):
    path = str(tmp_path / "facts.db")
    FactStore(path).add("gate code", "1234", "נתנאל", "t1")
    assert FactStore(path).active()[0]["fact"] == "1234"   # a fresh instance sees prior rows
