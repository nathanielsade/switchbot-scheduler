from home_agent.config import load_config, max_configured


def test_max_config(monkeypatch, tmp_path):
    for k, v in {
        "OPENAI_API_KEY": "k",
        "TELEGRAM_BOT_TOKEN": "t",
        "ALLOWED_CHAT_IDS": "1",
        "MAX_USERNAME": "u",
        "MAX_PASSWORD": "p",
    }.items():
        monkeypatch.setenv(k, v)
    c = load_config(str(tmp_path / "no.env"))
    assert c.max_username == "u" and c.max_password == "p" and max_configured(c)
    assert c.max_collector_script.endswith("scrape_max.js") and c.finance_start_days >= 365
