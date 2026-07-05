from .model import Event

# SwitchBot repeat byte: bit0=Mon .. bit6=Sun (verified via switchbotpy protocol);
# bit 7 stays 0 => recurring weekly.
DAY_BIT = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
ACTION_CODE = {"press": 0, "on": 1, "off": 2}


def encode_alarm(event: Event, inverted: bool = False) -> dict:
    day_mask = 0
    for d in event.days:
        day_mask |= (1 << DAY_BIT[d])
    if event.once:
        day_mask |= 0x80   # bit 7 = execute once
    hour, minute = (int(x) for x in event.time.split(":"))
    action = event.action
    if inverted and action in ("on", "off"):
        action = "off" if action == "on" else "on"
    return {
        "repeat_byte": day_mask,
        "hour": hour,
        "minute": minute,
        "action": ACTION_CODE[action],
    }
