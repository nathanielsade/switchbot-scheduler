from datetime import datetime
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


def test_build_application_composes_roborock_tools_when_configured(tmp_path, monkeypatch, make_fake_client):
    import home_agent.telegram_app as ta
    from roborock_fakes import FakeRoborockClient   # sibling helper; tests/ has no __init__.py
    from home_agent.roborock_rooms import Room, RoomRegistry
    cfg = Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                 allowed_chat_ids={1}, db_path=str(tmp_path / "m.db"),
                 devices_path=str(tmp_path / "none.yaml"),
                 roborock_username="u", roborock_password="p")
    monkeypatch.setattr(ta, "load_roborock_client", lambda cfg: FakeRoborockClient())
    monkeypatch.setattr(ta, "load_room_registry",
                        lambda cfg: RoomRegistry([Room("kitchen", 17, ["מטבח"])]))
    seen = {}
    real = ta.build_roborock_tools

    def spy(client, registry, **kw):
        seen["client"] = client
        seen["registry"] = registry
        seen["tools"] = real(client, registry, **kw)
        return seen["tools"]

    monkeypatch.setattr(ta, "build_roborock_tools", spy)
    app = build_application(cfg, client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    assert isinstance(seen.get("client"), FakeRoborockClient)
    assert isinstance(seen.get("registry"), RoomRegistry)
    # Verify the composed tools include all expected vacuum capabilities
    composed_tools = seen.get("tools", [])
    tool_names = {t.name for t in composed_tools}
    expected_names = {"clean", "list_rooms", "vacuum_status", "control_vacuum", "dock_action",
                      "consumables", "schedule_clean", "get_cleaning_schedule", "cancel_cleaning_schedule"}
    assert expected_names <= tool_names, f"Missing tools: {expected_names - tool_names}"


def test_build_application_omits_roborock_tools_when_unconfigured(tmp_path, monkeypatch, make_fake_client):
    import home_agent.telegram_app as ta
    cfg = _cfg(tmp_path)  # no roborock_username/password set
    called = {"count": 0}
    real = ta.build_roborock_tools

    def spy(*a, **kw):
        called["count"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(ta, "build_roborock_tools", spy)
    app = build_application(cfg, client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    assert called["count"] == 0   # tools not composed when no Roborock credentials configured


def test_build_application_composes_finance_tools_when_configured(tmp_path, monkeypatch, make_fake_client):
    import home_agent.telegram_app as ta
    cfg = Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                 allowed_chat_ids={1}, db_path=str(tmp_path / "m.db"),
                 devices_path=str(tmp_path / "none.yaml"),
                 discount_id="1", discount_password="p", discount_num="9")
    monkeypatch.setattr(ta, "make_collector_fetch",
                        lambda cfg, source="discount": (lambda: {"source": source,
                                              "scraped_at": "2026-07-12T00:00:00Z", "accounts": []}))
    seen = {}
    real = ta.build_finance_tools

    def spy(store, **kw):
        seen["store"] = store
        seen["tools"] = real(store, **kw)
        return seen["tools"]

    monkeypatch.setattr(ta, "build_finance_tools", spy)
    app = build_application(cfg, client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    from home_agent.finance_store import FinanceStore
    assert isinstance(seen.get("store"), FinanceStore)
    tool_names = {t.name for t in seen.get("tools", [])}
    expected_names = {"sync_finances", "financial_summary", "find_transactions",
                      "spending_by_category", "cash_flow_forecast"}
    assert expected_names <= tool_names, f"Missing tools: {expected_names - tool_names}"


def test_build_application_wires_model_clock_and_scheduler_clock_to_the_same_tz(
        tmp_path, monkeypatch, make_fake_client):
    # F2: get_current_time (what the MODEL reads to pick a weekday) and the scheduler's
    # now_fn (what does the date math) MUST resolve to the same timezone — that agreement is
    # this branch's core claim. Pin a non-default HOME_TZ so a future edit that diverges the
    # two wirings (e.g. one left on UTC) fails this test instead of only failing silently.
    import home_agent.telegram_app as ta
    dev = tmp_path / "devices.yaml"
    dev.write_text("devices:\n  dining:\n    aliases: [פינת אוכל]\n    ble_id: ID3\n")
    cfg = Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                 allowed_chat_ids={1}, db_path=str(tmp_path / "m.db"), devices_path=str(dev),
                 home_tz="America/New_York")

    seen = {}
    real_time_tools = ta.build_time_tools
    real_schedule_tools = ta.build_schedule_tools

    def time_spy(tz):
        seen["time_tz"] = tz
        return real_time_tools(tz)

    def schedule_spy(registry, store, **kw):
        seen["now_fn"] = kw.get("now_fn")
        return real_schedule_tools(registry, store, **kw)

    monkeypatch.setattr(ta, "build_time_tools", time_spy)
    monkeypatch.setattr(ta, "build_schedule_tools", schedule_spy)
    app = build_application(cfg, client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)

    assert seen.get("time_tz") is not None
    assert seen.get("now_fn") is not None
    model_offset = datetime.now(seen["time_tz"]).utcoffset()
    scheduler_offset = seen["now_fn"]().utcoffset()
    assert model_offset == scheduler_offset  # would fail if either wiring diverged to a different zone


def test_build_application_omits_finance_tools_when_unconfigured(tmp_path, monkeypatch, make_fake_client):
    import home_agent.telegram_app as ta
    cfg = _cfg(tmp_path)  # no discount_* creds set
    called = {"count": 0}
    real = ta.build_finance_tools

    def spy(*a, **kw):
        called["count"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(ta, "build_finance_tools", spy)
    app = build_application(cfg, client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    assert called["count"] == 0   # tools not composed when no Discount credentials configured
