from home_agent.config import Config
from home_agent.telegram_app import build_application


def _cfg(tmp_path):
    dev = tmp_path / "d.yaml"
    dev.write_text('devices:\n  garden:\n    aliases: ["גינה"]\n    cloud_id: "EECE111B5B1C"\n',
                   encoding="utf-8")
    return Config(openai_api_key="k", telegram_bot_token="123:abc", allowed_chat_ids={1},
                  db_path=str(tmp_path / "a.db"), devices_path=str(dev),
                  switchbot_token="TOK", switchbot_secret="SEC")


def test_build_application_has_jobqueue_and_no_crash(tmp_path):
    # job-queue extra installed + Application built before scheduler wiring => job_queue present
    app = build_application(_cfg(tmp_path), client=object())
    assert app.job_queue is not None
