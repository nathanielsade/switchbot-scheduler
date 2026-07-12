from home_agent.config import Config
from home_agent.finance import finance_configured


def _cfg(**kw):
    base = dict(openai_api_key="k", telegram_bot_token="t", allowed_chat_ids=set())
    base.update(kw)
    return Config(**base)


def test_unconfigured_is_false():
    assert finance_configured(_cfg()) is False


def test_all_three_set_is_true():
    assert finance_configured(_cfg(discount_id="1", discount_password="p", discount_num="9")) is True


def test_partial_config_is_false_and_warns(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="home_agent"):
        assert finance_configured(_cfg(discount_id="1")) is False
    assert any("partial" in r.message.lower() or "finance" in r.message.lower() for r in caplog.records)
