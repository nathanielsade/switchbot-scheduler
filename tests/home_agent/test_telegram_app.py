from telegram.ext import Application
from home_agent.config import Config
from home_agent.memory import Conversation
from home_agent.telegram_app import build_application


def _cfg(tmp_path):
    # token must be BotFather-shaped ("<digits>:<rest>") for python-telegram-bot to accept it
    return Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                  allowed_chat_ids={1}, model="gpt-4o", db_path=str(tmp_path / "m.db"),
                  devices_path=str(tmp_path / "no-devices.yaml"))


def test_build_application_registers_one_text_handler(tmp_path, make_fake_client):
    app = build_application(_cfg(tmp_path), client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    assert sum(len(hs) for hs in app.handlers.values()) == 1  # exactly one message handler, no network
    assert app.error_handlers  # a last-resort error handler is registered


def test_split_for_telegram_chunks_oversized_text():
    from home_agent.telegram_app import _split_for_telegram, _TELEGRAM_MAX_CHARS
    text = "x" * (_TELEGRAM_MAX_CHARS * 2 + 5)
    chunks = _split_for_telegram(text)
    assert len(chunks) == 3
    assert all(len(c) <= _TELEGRAM_MAX_CHARS for c in chunks)
    assert "".join(chunks) == text  # no bytes lost when there is nothing to split on


def test_split_for_telegram_prefers_newline_boundary():
    from home_agent.telegram_app import _split_for_telegram
    text = "a" * 4000 + "\n" + "b" * 200
    chunks = _split_for_telegram(text)
    assert chunks == ["a" * 4000, "b" * 200]  # split at the newline, not mid-line


def test_split_for_telegram_short_text_is_single_chunk():
    from home_agent.telegram_app import _split_for_telegram
    assert _split_for_telegram("שלום") == ["שלום"]


def test_build_application_composes_schedule_tools(tmp_path, monkeypatch, make_fake_client):
    import home_agent.telegram_app as ta
    from home_agent.config import Config
    from home_agent.schedule_store import ScheduleStore
    dev = tmp_path / "devices.yaml"
    dev.write_text("devices:\n  dining:\n    aliases: [פינת אוכל]\n    ble_id: ID3\n")
    cfg = Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                 allowed_chat_ids={1}, db_path=str(tmp_path / "m.db"), devices_path=str(dev))
    seen = {}
    real = ta.build_schedule_tools

    def spy(registry, store, **kw):
        seen["registry"] = registry
        seen["store"] = store
        return real(registry, store, **kw)

    monkeypatch.setattr(ta, "build_schedule_tools", spy)
    app = build_application(cfg, client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    assert seen.get("registry") is not None            # composed with the loaded registry
    assert isinstance(seen["store"], ScheduleStore)     # and a ScheduleStore


def test_build_application_composes_shopping_tools(tmp_path, monkeypatch, make_fake_client):
    import home_agent.telegram_app as ta
    from home_agent.config import Config
    from home_agent.shopping_store import ShoppingStore
    cfg = Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                 allowed_chat_ids={1}, db_path=str(tmp_path / "m.db"),
                 devices_path=str(tmp_path / "none.yaml"))
    seen = {}
    real = ta.build_shopping_tools

    def spy(store, **kw):
        seen["store"] = store
        return real(store, **kw)

    monkeypatch.setattr(ta, "build_shopping_tools", spy)
    app = build_application(cfg, client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    assert isinstance(seen.get("store"), ShoppingStore)   # shopping tools were composed
