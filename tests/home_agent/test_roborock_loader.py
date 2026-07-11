from home_agent.config import Config
from home_agent.roborock import load_roborock_client


def _cfg(**kw):
    base = dict(openai_api_key="k", telegram_bot_token="t", allowed_chat_ids=set())
    base.update(kw)
    return Config(**base)


def test_loader_returns_none_when_unconfigured():
    assert load_roborock_client(_cfg()) is None


def test_loader_returns_none_when_password_missing():
    assert load_roborock_client(_cfg(roborock_username="me@example.com")) is None
