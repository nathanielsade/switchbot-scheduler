from .model import Event

# sun=bit0 .. sat=bit6 (repeat-byte bit 7 stays 0 => recurring weekly)
DAY_BIT = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
ACTION_CODE = {"press": 0, "on": 1, "off": 2}


def encode_alarm(event: Event, inverted: bool = False) -> dict:
    day_mask = 0
    for d in event.days:
        day_mask |= (1 << DAY_BIT[d])
    hour, minute = (int(x) for x in event.time.split(":"))
    action = event.action
    if inverted and action in ("on", "off"):
        action = "off" if action == "on" else "on"
    return {
        "repeat_byte": day_mask,   # bit 7 = 0 => recurring weekly
        "hour": hour,
        "minute": minute,
        "action": ACTION_CODE[action],
    }
