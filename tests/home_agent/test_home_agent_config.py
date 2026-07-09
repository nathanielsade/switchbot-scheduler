import pytest


def _clean_env(monkeypatch):
    for k in ("ALLOWED_CHAT_IDS", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN",
              "HOME_AGENT_MODEL", "HOME_AGENT_DB", "HOME_AGENT_OPENAI_TIMEOUT", "SWITCHBOT_DEVICES"):
        monkeypatch.delenv(k, raising=False)


def test_load_config_parses_allowlist(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text('OPENAI_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN=tok\nALLOWED_CHAT_IDS=111, 222\n')
    from home_agent.config import load_config
    cfg = load_config(str(env))
    assert cfg.openai_api_key == "sk-x"
    assert cfg.telegram_bot_token == "tok"
    assert cfg.allowed_chat_ids == {111, 222}
    assert cfg.model == "gpt-4o"  # default


def test_load_config_rejects_non_integer_chat_id(tmp_path, monkeypatch):
    # Regression: a malformed token used to crash startup with a bare ValueError traceback.
    _clean_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text('OPENAI_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN=tok\nALLOWED_CHAT_IDS=111,me\n')
    from home_agent.config import load_config
    with pytest.raises(SystemExit) as exc:
        load_config(str(env))
    assert "me" in str(exc.value)  # readable error names the offending token


def test_load_config_openai_timeout_default_and_override(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    from home_agent.config import load_config, DEFAULT_OPENAI_TIMEOUT
    env = tmp_path / ".env"
    env.write_text('OPENAI_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN=tok\n')
    assert load_config(str(env)).openai_timeout == DEFAULT_OPENAI_TIMEOUT
    monkeypatch.setenv("HOME_AGENT_OPENAI_TIMEOUT", "12.5")
    assert load_config(str(env)).openai_timeout == 12.5


def test_load_config_devices_path_default_and_override(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    from home_agent.config import load_config, DEFAULT_DEVICES_PATH
    env = tmp_path / ".env"
    env.write_text('OPENAI_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN=tok\n')
    assert load_config(str(env)).devices_path == DEFAULT_DEVICES_PATH
    monkeypatch.setenv("SWITCHBOT_DEVICES", "/tmp/d.yaml")
    assert load_config(str(env)).devices_path == "/tmp/d.yaml"
