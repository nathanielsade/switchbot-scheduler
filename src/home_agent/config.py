import os
from dataclasses import dataclass

from switchbot_scheduler.config import load_env

DEFAULT_MODEL = "gpt-4o"
DEFAULT_DB_PATH = "home_agent.db"
DEFAULT_OPENAI_TIMEOUT = 60.0  # seconds; caps a hung request instead of the SDK's 600s default


@dataclass
class Config:
    openai_api_key: str
    telegram_bot_token: str
    allowed_chat_ids: set[int]
    model: str = DEFAULT_MODEL
    db_path: str = DEFAULT_DB_PATH
    openai_timeout: float = DEFAULT_OPENAI_TIMEOUT


def _parse_chat_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for tok in raw.replace(",", " ").split():
        try:
            ids.add(int(tok))
        except ValueError:
            raise SystemExit(
                f"ALLOWED_CHAT_IDS contains a non-integer value: {tok!r}. "
                "Use space/comma-separated numeric Telegram chat IDs (e.g. '111 -100222')."
            )
    return ids


def load_config(path: str | None = None) -> Config:
    load_env(path)  # reuse the sibling package's loader (override=False: shell exports win)
    return Config(
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        allowed_chat_ids=_parse_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", "")),
        model=os.environ.get("HOME_AGENT_MODEL", DEFAULT_MODEL),
        db_path=os.environ.get("HOME_AGENT_DB", DEFAULT_DB_PATH),
        openai_timeout=float(os.environ.get("HOME_AGENT_OPENAI_TIMEOUT", DEFAULT_OPENAI_TIMEOUT)),
    )
