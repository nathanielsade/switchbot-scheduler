from switchbot_scheduler.config import load_env


def test_load_env_sets_vars_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SB_TEST_VAR", raising=False)
    env = tmp_path / ".env"
    env.write_text("SB_TEST_VAR=hello\n")
    load_env(str(env))
    import os
    assert os.environ["SB_TEST_VAR"] == "hello"


def test_load_env_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_TEST_VAR2", "from_shell")
    env = tmp_path / ".env"
    env.write_text("SB_TEST_VAR2=from_file\n")
    load_env(str(env))
    import os
    assert os.environ["SB_TEST_VAR2"] == "from_shell"  # explicit export wins
