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
