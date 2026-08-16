from dataclasses import dataclass
from datetime import datetime
from typing import Callable


@dataclass
class Tool:
    name: str
    schema: dict
    impl: Callable[[dict], str]


def _now_string(args: dict, tz=None) -> str:
    # Include the weekday and timezone so the model never has to infer the day of week
    # (it guessed wrong when only "YYYY-MM-DD HH:MM:SS" was returned).
    now = datetime.now(tz) if tz is not None else datetime.now().astimezone()
    return now.strftime("%A, %Y-%m-%d %H:%M:%S %Z")


_TIME_SCHEMA = {"type": "function", "function": {
    "name": "get_current_time",
    "description": "Return the current local date and time, including the day of the week.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

get_current_time = Tool(name="get_current_time", schema=_TIME_SCHEMA, impl=_now_string)

DEFAULT_TOOLS = [get_current_time]


def build_time_tools(tz):
    """get_current_time bound to the home timezone. This is the clock the MODEL reads,
    so it must agree with the scheduler's clock — it drives the model's choice of weekday."""
    return [Tool(name="get_current_time", schema=_TIME_SCHEMA,
                 impl=lambda args: _now_string(args, tz=tz))]
