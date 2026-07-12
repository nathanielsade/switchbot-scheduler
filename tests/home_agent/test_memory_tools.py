from datetime import datetime
from home_agent.facts import FactStore, build_memory_tools


def _frozen(iso="2026-07-12T10:00:00+03:00"):
    dt = datetime.fromisoformat(iso)
    return lambda: dt


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_remember_stores_subject_fact_author_timestamp(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    out = _tool(tools, "remember").impl({"subject": "דרכונים", "fact": "בכספת"})
    assert out  # non-empty confirmation
    rows = store.active()
    assert len(rows) == 1
    assert rows[0]["subject"] == "דרכונים"
    assert rows[0]["fact"] == "בכספת"
    assert rows[0]["author"] == "נתנאל"
    assert rows[0]["created_at"] == "2026-07-12T10:00:00+03:00"


def test_remember_schema_hides_author_and_timestamp(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    props = _tool(tools, "remember").schema["function"]["parameters"]["properties"]
    assert set(props) == {"subject", "fact"}   # author/created_at injected, never model args


def test_recall_empty_store_is_friendly(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    out = _tool(tools, "recall").impl({})
    assert out.strip()                       # a real message, not empty
    assert "remember" in out.lower()         # the friendly "nothing remembered yet" wording


def test_recall_returns_facts_newest_first_with_author_and_date(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    store.add("gate code", "1234", "נתנאל", "2026-07-10T09:00:00+03:00")
    store.add("passports", "in the safe", "שרי", "2026-07-12T09:00:00+03:00")
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    out = _tool(tools, "recall").impl({})
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0].startswith("passports")        # newest first
    assert "in the safe" in lines[0] and "שרי" in lines[0] and "2026-07-12" in lines[0]
    assert any("gate code" in ln and "1234" in ln and "נתנאל" in ln for ln in lines)


def test_recall_takes_no_arguments(tmp_path):
    store = FactStore(str(tmp_path / "facts.db"))
    tools = build_memory_tools(store, sender="נתנאל", now_fn=_frozen())
    props = _tool(tools, "recall").schema["function"]["parameters"]["properties"]
    assert props == {}
