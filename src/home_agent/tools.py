from dataclasses import dataclass
from datetime import datetime
from typing import Callable


@dataclass
class Tool:
    name: str
    schema: dict
    impl: Callable[[dict], str]


get_current_time = Tool(
    name="get_current_time",
    schema={"type": "function", "function": {
        "name": "get_current_time",
        "description": "Return the current local date and time.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    impl=lambda args: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
)

DEFAULT_TOOLS = [get_current_time]
