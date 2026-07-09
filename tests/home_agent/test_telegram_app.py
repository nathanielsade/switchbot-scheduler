from telegram.ext import Application
from home_agent.config import Config
from home_agent.memory import Conversation
from home_agent.telegram_app import build_application


def _cfg(tmp_path):
    # token must be BotFather-shaped ("<digits>:<rest>") for python-telegram-bot to accept it
    return Config(openai_api_key="x", telegram_bot_token="123456:ABCdefGHIjklMNOpqrsTUVwxyz012345",
                  allowed_chat_ids={1}, model="gpt-4o", db_path=str(tmp_path / "m.db"))


def test_build_application_registers_one_text_handler(tmp_path, make_fake_client):
    app = build_application(_cfg(tmp_path), client=make_fake_client([]),
                            conversation=Conversation(str(tmp_path / "m.db")))
    assert isinstance(app, Application)
    assert sum(len(hs) for hs in app.handlers.values()) == 1  # exactly one message handler, no network
