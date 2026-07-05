from dataclasses import dataclass
from typing import Literal

DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
Action = Literal["on", "off", "press"]


@dataclass
class Event:
    time: str          # "HH:MM"
    action: Action     # "on" | "off" | "press"
    days: list[str]    # subset of DAYS


@dataclass
class DeviceSchedule:
    device: str            # canonical device name
    events: list[Event]


@dataclass
class Schedule:
    schedules: list[DeviceSchedule]
