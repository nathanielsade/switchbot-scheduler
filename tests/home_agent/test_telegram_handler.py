import pytest
from home_agent.config import Config
from home_agent.memory import Conversation
from home_agent.telegram_app import handle_message


def _cfg(tmp_path, allowed):
    return Config(openai_api_key="x", telegram_bot_token="t:t", allowed_chat_ids=set(allowed),
                  model="gpt-4o", db_path=str(tmp_path / "m.db"))


def test_allowed_chat_runs_agent_persists_and_replies(tmp_path, make_fake_client):
    client = make_fake_client([{"content": "שלום"}])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "היי", config=_cfg(tmp_path, {1}), conversation=conv, client=client)
    assert reply == "שלום"
    assert conv.load(1) == [{"role": "user", "content": "היי"},
                            {"role": "assistant", "content": "שלום"}]


def test_unauthorized_chat_ignored_no_side_effects(tmp_path, make_fake_client):
    client = make_fake_client([{"content": "should not happen"}])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(999, "hi", config=_cfg(tmp_path, {1}), conversation=conv, client=client)
    assert reply is None
    assert conv.load(999) == []
    assert client._calls == []  # agent never invoked


def test_discovery_mode_reveals_chat_id_without_running_agent(tmp_path, make_fake_client):
    client = make_fake_client([{"content": "nope"}])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(-100123, "anything", config=_cfg(tmp_path, set()), conversation=conv, client=client)
    assert "-100123" in reply
    assert client._calls == []
    assert conv.load(-100123) == []


def test_history_loaded_before_appending_current_message(tmp_path, make_fake_client):
    client = make_fake_client([{"content": "ok"}])
    conv = Conversation(str(tmp_path / "m.db"))
    conv.append(1, "user", "old")
    conv.append(1, "assistant", "prev")
    handle_message(1, "new", config=_cfg(tmp_path, {1}), conversation=conv, client=client)
    sent = client._calls[0]["messages"]
    assert [m["content"] for m in sent if m["content"] == "new"] == ["new"]  # current appears once
    assert any(m["content"] == "old" for m in sent)  # prior history present


def test_agent_error_returns_friendly_message_and_does_not_persist(tmp_path):
    class Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("openai down")
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "hi", config=_cfg(tmp_path, {1}), conversation=conv, client=Boom())
    assert isinstance(reply, str) and reply.strip()
    assert reply != "hi"
    assert conv.load(1) == []  # nothing persisted on failure


def test_empty_agent_reply_returns_fallback_and_is_not_persisted(tmp_path, make_fake_client):
    # Regression: an empty completion used to make the bot go silent while still writing an
    # empty assistant row that polluted the next turn's context.
    client = make_fake_client([{"content": ""}])  # model yields empty text, no tool calls
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "hi", config=_cfg(tmp_path, {1}), conversation=conv, client=client)
    assert reply and reply.strip()  # user gets a real message, not silence
    assert conv.load(1) == []       # empty turn not persisted


def test_handle_message_runs_control_device_through_composed_tools(tmp_path, make_fake_client):
    from home_agent.home import build_home_tools
    from home_agent.tools import DEFAULT_TOOLS
    from switchbot_scheduler.registry import Registry, Device
    reg = Registry([Device(name="kitchen", aliases=["מטבח"], ble_id="ID3")])
    calls = []
    tools = list(DEFAULT_TOOLS) + build_home_tools(reg, actuate_fn=lambda b, c: calls.append((b, c)))
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "control_device",
                         "arguments": {"device": "מטבח", "action": "on"}}]},
        {"content": "הדלקתי את המטבח"},
    ])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "תדליק את המטבח", config=_cfg(tmp_path, {1}),
                           conversation=conv, client=client, tools=tools)
    assert calls == [("ID3", 1)]
    assert reply == "הדלקתי את המטבח"


def test_handle_message_runs_schedule_device_through_composed_tools(tmp_path, make_fake_client):
    from home_agent.schedules import build_schedule_tools
    from home_agent.schedule_store import ScheduleStore
    from home_agent.tools import DEFAULT_TOOLS
    from switchbot_scheduler.registry import Registry, Device
    reg = Registry([Device(name="dining", aliases=["פינת אוכל"], ble_id="ID3")])
    store = ScheduleStore(str(tmp_path / "s.db"))
    writes = []
    sched = build_schedule_tools(reg, store, write_fn=lambda b, a: writes.append((b, a)))
    tools = list(DEFAULT_TOOLS) + sched
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "schedule_device",
                         "arguments": {"device": "פינת אוכל", "action": "on", "time": "18:00", "days": ["mon"]}}]},
        {"content": "קבעתי"},
    ])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "תזמן", config=_cfg(tmp_path, {1}),
                           conversation=conv, client=client, tools=tools)
    assert writes and writes[-1][0] == "ID3"
    assert reply == "קבעתי"


def test_handle_message_runs_add_to_list_through_composed_tools(tmp_path, make_fake_client):
    from home_agent.shopping import build_shopping_tools
    from home_agent.shopping_store import ShoppingStore
    from home_agent.tools import DEFAULT_TOOLS
    store = ShoppingStore(str(tmp_path / "sh.db"))
    tools = list(DEFAULT_TOOLS) + build_shopping_tools(store)
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "add_to_list", "arguments": {"item": "חלב"}}]},
        {"content": "הוספתי חלב"},
    ])
    conv = Conversation(str(tmp_path / "m.db"))
    reply = handle_message(1, "תוסיף חלב", config=_cfg(tmp_path, {1}),
                           conversation=conv, client=client, tools=tools)
    assert reply == "הוספתי חלב"
    assert store.pending()[0]["item"] == "חלב"


def test_calendar_same_turn_prepare_then_commit_is_not_applied(tmp_path, make_fake_client):
    from home_agent.calendar_pending import CalendarPending

    class _Exec:
        def __init__(self, r): self._r = r
        def execute(self): return self._r

    class _Events:
        def __init__(self): self.calls = []
        def list(self, **k): self.calls.append(("list", k)); return _Exec({"items": []})
        def insert(self, **k): self.calls.append(("insert", k)); return _Exec({"id": "x"})
        def patch(self, **k): self.calls.append(("patch", k)); return _Exec({})
        def delete(self, **k): self.calls.append(("delete", k)); return _Exec({})

    class _Svc:
        def __init__(self): self._e = _Events()
        def events(self): return self._e

    svc, pend = _Svc(), CalendarPending(str(tmp_path / "c.db"))
    # model tries prepare THEN commit in one turn
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "prepare_calendar_change",
                         "arguments": {"action": "create", "title": "רופא", "start": "2026-07-14T15:00:00+03:00"}}]},
        {"tool_calls": [{"id": "c2", "name": "commit_calendar_change", "arguments": {}}]},
        {"content": "רשמתי, תאשרו"},
    ])
    conv = Conversation(str(tmp_path / "m.db"))
    cfg = _cfg(tmp_path, {1})
    cfg.calendar_ids = ["fam"]; cfg.calendar_write_id = "fam"
    reply = handle_message(1, "תוסיף רופא שיניים", config=cfg, conversation=conv, client=client,
                           calendar_service=svc, calendar_pending=pend)
    assert not any(c[0] == "insert" for c in svc.events().calls)   # same-turn commit did NOT apply
    assert pend.current(1) is not None                             # still staged for a later confirm
