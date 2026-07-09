def test_load_config_parses_allowlist(tmp_path, monkeypatch):
    for k in ("ALLOWED_CHAT_IDS", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "HOME_AGENT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text('OPENAI_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN=tok\nALLOWED_CHAT_IDS=111, 222\n')
    from home_agent.config import load_config
    cfg = load_config(str(env))
    assert cfg.openai_api_key == "sk-x"
    assert cfg.telegram_bot_token == "tok"
    assert cfg.allowed_chat_ids == {111, 222}
    assert cfg.model == "gpt-4o"  # default
