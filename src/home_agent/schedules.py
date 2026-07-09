import logging
from datetime import datetime, timedelta

from switchbot_scheduler.model import DAYS, Event, DeviceSchedule, Schedule
from switchbot_scheduler.encoder import encode_alarm
from switchbot_scheduler.validator import validate, ScheduleError
from switchbot_scheduler.readback import describe_days

from .tools import Tool

log = logging.getLogger("home_agent")

_DAY_WORDS = {
    "daily": list(DAYS),
    "weekdays": ["mon", "tue", "wed", "thu", "fri"],
    "weekends": ["sat", "sun"],
}
# python's datetime.weekday(): Mon=0..Sun=6
_PY_WEEKDAY = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


def _normalize_days(days):
    """Expand convenience words, validate, and return a DAYS-ordered, deduped subset."""
    seen = set()
    for d in days:
        key = str(d).strip().lower()
        if key in _DAY_WORDS:
            seen.update(_DAY_WORDS[key])
        elif key in DAYS:
            seen.add(key)
        else:
            raise ValueError(f"unknown day '{d}'")
    return [d for d in DAYS if d in seen]


def _one_time_target(time_str, now):
    """(weekday_name, fire_at_iso) of the next occurrence of HH:MM from `now`
    (today if still ahead, else tomorrow)."""
    hh, mm = (int(x) for x in time_str.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return _PY_WEEKDAY[target.weekday()], target.isoformat()


_SCHEDULE_SCHEMA = {"type": "function", "function": {
    "name": "schedule_device",
    "description": (
        "Schedule a SwitchBot device to turn on/off (or press) at a clock time, programmed into the "
        "device's own timer so it fires even if this computer is off. `time` is 24-hour \"HH:MM\". "
        "Omit `days` for a ONE-TIME timer (fires at the next occurrence of that time); give `days` for "
        "a RECURRING timer. For relative requests like 'in 5 minutes', first call get_current_time and "
        "compute the HH:MM. Each device holds at most 5 timers. Report what you scheduled, in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "Room/device name or alias, Hebrew or English."},
        "action": {"type": "string", "enum": ["on", "off", "press"],
                   "description": "on, off, or press; the AC only honors press."},
        "time": {"type": "string", "description": "24-hour clock time, \"HH:MM\"."},
        "days": {"type": "array", "items": {"type": "string"},
                 "description": "Any of sun mon tue wed thu fri sat, or the words daily/weekdays/weekends. Omit for a one-time timer."},
    }, "required": ["device", "action", "time"], "additionalProperties": False},
}}


def _program_bot(ble_id, alarms):
    import asyncio
    from switchbot_scheduler.ble_writer import write_alarms
    asyncio.run(write_alarms(ble_id, alarms))


def _program_device(device, store, registry, write_fn):
    """Rebuild `device`'s full alarm set from the store and write it to the Bot (empty list clears it)."""
    rows = store.list(device)
    events = [Event(r["time"], r["action"], r["days"], r["once"]) for r in rows]
    if events:
        validate(Schedule([DeviceSchedule(device, events)]), registry)
    if registry.is_press_mode(device):
        # A press-mode Bot only toggles, so any on/off intent becomes a single press
        # (mirrors switchbot_scheduler.core._apply_press_mode).
        for e in events:
            e.action = "press"
    alarms = [encode_alarm(e, inverted=registry.is_inverted(device)) for e in events]
    write_fn(registry.ble_id(device), alarms)


def _schedule_impl(args, *, registry, store, write_fn, now_fn):
    spoken = (args.get("device") or "").strip()
    action = (args.get("action") or "").strip().lower()
    time_str = (args.get("time") or "").strip()
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    if action not in ("on", "off", "press"):
        return f"unknown action '{action}'. Use on, off, or press."
    try:
        raw_days = args.get("days") or []
        if raw_days:
            days, once, fire_at = _normalize_days(raw_days), False, None
        else:
            day, fire_at = _one_time_target(time_str, now_fn())
            days, once = [day], True
    except (ValueError, AttributeError) as e:
        return f"couldn't set the timer: {e}"
    row_id = store.add(name, action, time_str, days, once, fire_at)
    try:
        _program_device(name, store, registry, write_fn)
    except ScheduleError as e:
        store.remove_id(row_id)
        return f"can't schedule that: {e}"
    except Exception as e:
        store.remove_id(row_id)
        return f"couldn't reach {name} — timer not set ({e})"
    when = "one-time" if once else describe_days(days)
    return f"{name}: {action} at {time_str} ({when}) ✅"


def _now():
    return datetime.now().astimezone()


def build_schedule_tools(registry, store, *, write_fn=None, now_fn=None):
    write_fn = write_fn or _program_bot
    now_fn = now_fn or _now
    return [
        Tool(name="schedule_device", schema=_SCHEDULE_SCHEMA,
             impl=lambda args: _schedule_impl(
                 args, registry=registry, store=store, write_fn=write_fn, now_fn=now_fn)),
    ]
