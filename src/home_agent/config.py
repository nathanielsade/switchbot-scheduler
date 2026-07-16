import os
from dataclasses import dataclass, field

from switchbot_scheduler.config import load_env

DEFAULT_MODEL = "gpt-4o"
DEFAULT_DB_PATH = "home_agent.db"
DEFAULT_OPENAI_TIMEOUT = 60.0  # seconds; caps a hung request instead of the SDK's 600s default
DEFAULT_DEVICES_PATH = "devices.yaml"
DEFAULT_ROOMS_PATH = "roborock_rooms.yaml"
DEFAULT_ROBOROCK_USERDATA_PATH = "roborock_userdata.json"
DEFAULT_COLLECTOR_SCRIPT = "collector/scrape_discount.js"


@dataclass
class Config:
    openai_api_key: str
    telegram_bot_token: str
    allowed_chat_ids: set[int]
    model: str = DEFAULT_MODEL
    db_path: str = DEFAULT_DB_PATH
    openai_timeout: float = DEFAULT_OPENAI_TIMEOUT
    devices_path: str = DEFAULT_DEVICES_PATH
    google_sa_keyfile: str = ""
    calendar_ids: list[str] = field(default_factory=list)
    calendar_write_id: str = ""
    roborock_username: str = ""
    roborock_password: str = ""
    roborock_rooms_path: str = DEFAULT_ROOMS_PATH
    roborock_userdata_path: str = DEFAULT_ROBOROCK_USERDATA_PATH
    discount_id: str = ""
    discount_password: str = ""
    discount_num: str = ""
    finance_node_bin: str = "node"
    finance_collector_script: str = DEFAULT_COLLECTOR_SCRIPT
    switchbot_token: str = ""
    switchbot_secret: str = ""
    home_tz: str = "Asia/Jerusalem"


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
    cal_ids = [x for x in os.environ.get("CALENDAR_IDS", "").replace(",", " ").split() if x.strip()]
    return Config(
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        allowed_chat_ids=_parse_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", "")),
        model=os.environ.get("HOME_AGENT_MODEL", DEFAULT_MODEL),
        db_path=os.environ.get("HOME_AGENT_DB", DEFAULT_DB_PATH),
        openai_timeout=float(os.environ.get("HOME_AGENT_OPENAI_TIMEOUT", DEFAULT_OPENAI_TIMEOUT)),
        devices_path=os.environ.get("SWITCHBOT_DEVICES", DEFAULT_DEVICES_PATH),
        google_sa_keyfile=os.environ.get("GOOGLE_SA_KEYFILE", ""),
        calendar_ids=cal_ids,
        calendar_write_id=os.environ.get("CALENDAR_WRITE_ID", "") or (cal_ids[0] if cal_ids else ""),
        roborock_username=os.environ.get("ROBOROCK_USERNAME", ""),
        roborock_password=os.environ.get("ROBOROCK_PASSWORD", ""),
        roborock_rooms_path=os.environ.get("ROBOROCK_ROOMS", DEFAULT_ROOMS_PATH),
        roborock_userdata_path=os.environ.get("ROBOROCK_USERDATA", DEFAULT_ROBOROCK_USERDATA_PATH),
        discount_id=os.environ.get("DISCOUNT_ID", ""),
        discount_password=os.environ.get("DISCOUNT_PASSWORD", ""),
        discount_num=os.environ.get("DISCOUNT_NUM", ""),
        finance_node_bin=os.environ.get("FINANCE_NODE_BIN", "node"),
        finance_collector_script=os.environ.get("FINANCE_COLLECTOR_SCRIPT", DEFAULT_COLLECTOR_SCRIPT),
        switchbot_token=os.environ.get("SWITCHBOT_TOKEN", ""),
        switchbot_secret=os.environ.get("SWITCHBOT_SECRET", ""),
        home_tz=os.environ.get("HOME_TZ", "Asia/Jerusalem"),
    )
