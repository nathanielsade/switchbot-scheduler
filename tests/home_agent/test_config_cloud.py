from home_agent.config import load_config


def test_cloud_and_tz_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("SWITCHBOT_TOKEN", "TOK")
    monkeypatch.setenv("SWITCHBOT_SECRET", "SEC")
    cfg = load_config(str(tmp_path / "nope.env"))
    assert cfg.switchbot_token == "TOK" and cfg.switchbot_secret == "SEC"
    assert cfg.home_tz == "Asia/Jerusalem"
