import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Config:
    openai_api_key: str
    telegram_bot_token: str
    allowed_chat_ids: set[int]
    model: str = "gpt-4o"
    db_path: str = "home_agent.db"


def load_config(path: str | None = None) -> Config:
    load_dotenv(dotenv_path=path, override=False)
    raw = os.environ.get("ALLOWED_CHAT_IDS", "")
    allowed = {int(x) for x in raw.replace(",", " ").split() if x.strip()}
    return Config(
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        allowed_chat_ids=allowed,
        model=os.environ.get("HOME_AGENT_MODEL", "gpt-4o"),
        db_path=os.environ.get("HOME_AGENT_DB", "home_agent.db"),
    )
