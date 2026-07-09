from dataclasses import dataclass
from datetime import datetime
from typing import Callable


@dataclass
class Tool:
    name: str
    schema: dict
    impl: Callable[[dict], str]


def _now_string(args: dict) -> str:
    # Include the weekday and timezone so the model never has to infer the day of week
    # (it guessed wrong when only "YYYY-MM-DD HH:MM:SS" was returned).
    now = datetime.now().astimezone()
    return now.strftime("%A, %Y-%m-%d %H:%M:%S %Z")


get_current_time = Tool(
    name="get_current_time",
    schema={"type": "function", "function": {
        "name": "get_current_time",
        "description": "Return the current local date and time, including the day of the week.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    impl=_now_string,
)

DEFAULT_TOOLS = [get_current_time]
