import logging
from datetime import datetime, time as _dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from switchbot_scheduler.model import DAYS, Event, DeviceSchedule, Schedule
from switchbot_scheduler.encoder import encode_alarm
from switchbot_scheduler.validator import validate, ScheduleError, MAX_ALARMS
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


# Fixed day offsets. "soonest" is conditional, so it is handled separately.
_WHEN_OFFSETS = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2, "in_a_week": 7}
_WHEN_TOKENS = ["soonest", *_WHEN_OFFSETS, *DAYS]


def _resolve_fire_at(when, time_str, now):
    """Resolve a `when` token to a tz-aware datetime in `now`'s zone.

    All date arithmetic lives here, never in the model: the model maps language to a
    token ("מחר" -> "tomorrow") and nothing else. Built from date+time+tzinfo rather
    than timedelta on an aware value, so a target across a DST boundary keeps its
    wall-clock time.
    """
    key = str(when or "").strip()
    if key not in _WHEN_TOKENS:
        raise ValueError(f"unknown when '{when}'. Use one of: {', '.join(_WHEN_TOKENS)}")
    hh, mm = (int(x) for x in time_str.split(":"))
    tz = now.tzinfo

    def at(d):
        return datetime.combine(d, _dtime(hh, mm), tzinfo=tz)

    if key == "soonest":
        target = at(now.date())
        return target if target > now else at(now.date() + timedelta(days=1))

    if key in _WHEN_OFFSETS:
        target = at(now.date() + timedelta(days=_WHEN_OFFSETS[key]))
        if key == "today" and target <= now:
            raise ValueError(
                f"{time_str} already passed today. If the user did not explicitly say "
                f"today, retry with when=soonest or when=tomorrow")
        return target

    # a weekday name: nearest occurrence, today counting only if the time is still ahead
    target_idx = DAYS.index(key)
    now_idx = (now.weekday() + 1) % 7          # python Mon=0 -> our sun=0 indexing
    delta = (target_idx - now_idx) % 7
    target = at(now.date() + timedelta(days=delta))
    if target <= now:
        target = at(now.date() + timedelta(days=delta + 7))
    return target


_SCHEDULE_SCHEMA = {"type": "function", "function": {
    "name": "schedule_device",
    "description": (
        "Schedule a SwitchBot device to turn on/off (or press) ONCE, at a clock time on a "
        "specific day. Bluetooth devices are programmed into the device's own timer (they fire "
        "even if this computer is off); cloud devices (e.g. the garden) are fired by the "
        "home-agent, so it must be running. `time` is 24-hour \"HH:MM\". `when` says which day "
        "and is required — use \"soonest\" when the user named no day at all. Never guess a "
        "weekday: if the user's wording does not map cleanly onto a `when` value, or asks for "
        "more than a week ahead, ask them which day instead of choosing one. For relative "
        "requests like 'in 5 minutes', first call get_current_time and compute the HH:MM. Each "
        "device holds at most 5 timers. Report what you scheduled, including the date, in the "
        "user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "Room/device name or alias, Hebrew or English."},
        "action": {"type": "string", "enum": ["on", "off", "press"],
                   "description": "on, off, or press; the AC only honors press."},
        "time": {"type": "string", "description": "24-hour clock time, \"HH:MM\"."},
        "when": {"type": "string", "enum": _WHEN_TOKENS,
                 "description": "Which day: soonest (no day named), today, tomorrow, "
                                "day_after_tomorrow, in_a_week, or a weekday sun..sat."},
    }, "required": ["device", "action", "time", "when"], "additionalProperties": False},
}}

_RECURRING_SCHEMA = {"type": "function", "function": {
    "name": "schedule_recurring_device",
    "description": (
        "Schedule a SwitchBot device to repeat WEEKLY on the given days. Use ONLY when the user "
        "explicitly asked for repetition (כל יום / כל שישי / every week) — this is rare. For a "
        "single day, even a named weekday like 'ביום שישי', use schedule_device instead. Pass "
        "the user's own repetition words in `repetition_phrase`. Each device holds at most 5 "
        "timers. Report what you scheduled, in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "Room/device name or alias, Hebrew or English."},
        "action": {"type": "string", "enum": ["on", "off", "press"],
                   "description": "on, off, or press; the AC only honors press."},
        "time": {"type": "string", "description": "24-hour clock time, \"HH:MM\"."},
        "days": {"type": "array", "items": {"type": "string"},
                 "description": "Any of sun mon tue wed thu fri sat, or daily/weekdays/weekends."},
        "repetition_phrase": {"type": "string",
                              "description": "The user's own words that mean repetition."},
    }, "required": ["device", "action", "time", "days", "repetition_phrase"],
       "additionalProperties": False},
}}


def _program_bot(ble_id, alarms):
    import asyncio
    from switchbot_scheduler.ble_writer import write_alarms
    asyncio.run(write_alarms(ble_id, alarms))


def _program_device(device, store, registry, write_fn, now_fn):
    """Rebuild `device`'s full alarm set from the store and write it to the Bot (empty list clears it).

    A BLE one-time row encodes only (weekday, HH:MM, once-bit) — the wire format carries no date.
    Whether the firmware fires it "on that weekday" or "at the next HH:MM, then deletes" is
    unverified, so under the unfavourable reading a row set for more than a day out would fire
    tonight instead — the original bug. Refuse those here until the firmware behaviour is
    confirmed (a hardware spike). Cloud-routed devices never reach this function (callers route
    them through the scheduler instead), so they are unaffected."""
    rows = store.list(device)
    now = now_fn()
    for r in rows:
        if r["once"] and r["fire_at"]:
            fire_at = datetime.fromisoformat(r["fire_at"]).astimezone(now.tzinfo)
            if (fire_at.date() - now.date()).days > 1:
                raise ScheduleError(
                    f"{device}: Bluetooth devices can only hold a one-time timer up to a day "
                    f"ahead — the firmware's once-bit behaviour for anything further out isn't "
                    f"verified yet, and it could fire tonight instead of on "
                    f"{fire_at.date().isoformat()}. Use a recurring timer, or pick a nearer day."
                )
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


def _expire_and_reprogram(store, registry, write_fn, now_fn):
    """Drop fired one-time rows, and rewrite the alarm set of every BLE device that lost
    one — clearing the store alone leaves the Bot still holding the alarm."""
    for device in {r["device"] for r in store.remove_expired(_utc_iso(now_fn()))}:
        if registry.resolve(device) is None or registry.is_cloud(device):
            continue          # cloud one-time jobs self-remove in CloudScheduler._make_cb
        try:
            _program_device(device, store, registry, write_fn, now_fn)
        except Exception as e:
            # An unrelated Bot's dead battery must not block the call the user made.
            log.warning("could not reprogram %s after expiry: %s", device, type(e).__name__)


def _describe_row(action, time_str, days, once, fire_at, now):
    """One line per timer, always carrying the resolved calendar date for one-time rows —
    a weekday name alone is what made the original off-by-one invisible."""
    if not once or not fire_at:
        return f"{action} at {time_str} — RECURRING, every {describe_days(days)}"
    local = datetime.fromisoformat(fire_at).astimezone(now.tzinfo)
    line = f"{action} at {time_str} on {local.date().isoformat()} ({local.strftime('%a')}) — ONE-TIME"
    if (local.date() - now.date()).days > 6:
        line += " — NEXT WEEK"
    return line


def _schedule_impl(args, *, recurring, registry, store, write_fn, now_fn, scheduler=None):
    spoken = (args.get("device") or "").strip()
    action = (args.get("action") or "").strip().lower()
    time_str = (args.get("time") or "").strip()
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    if action not in ("on", "off", "press"):
        return f"unknown action '{action}'. Use on, off, or press."

    _expire_and_reprogram(store, registry, write_fn, now_fn)
    if len(store.list(name)) >= MAX_ALARMS:
        return f"{name} already has {MAX_ALARMS} timers, which is the maximum. Cancel one first."

    try:
        if recurring:
            if not (args.get("repetition_phrase") or "").strip():
                return ("repetition_phrase is required — if the user did not actually ask for a "
                        "repeating timer, use schedule_device instead.")
            days, once, fire_at, target = _normalize_days(args.get("days") or []), False, None, None
            if not days:
                return "give at least one day for a recurring timer."
        else:
            target = _resolve_fire_at(args.get("when"), time_str, now_fn())
            days, once, fire_at = [_PY_WEEKDAY[target.weekday()]], True, _utc_iso(target)
    except (ValueError, AttributeError) as e:
        return f"couldn't set the timer: {e}"

    row_id = store.add(name, action, time_str, days, once, fire_at)
    if registry.is_cloud(name):
        try:
            scheduler.schedule_row({"id": row_id, "device": name, "action": action,
                                    "time": time_str, "days": days, "once": once,
                                    "fire_at": fire_at})
        except Exception as e:
            store.remove_id(row_id)
            return f"couldn't schedule {name} ({e}) — timer not set"
    else:
        try:
            _program_device(name, store, registry, write_fn, now_fn)
        except ScheduleError as e:
            store.remove_id(row_id); return f"can't schedule that: {e}"
        except Exception as e:
            store.remove_id(row_id); return f"couldn't reach {name} — timer not set ({e})"
    return f"{name}: {_describe_row(action, time_str, days, once, fire_at, now_fn())} ✅"


_GET_SCHEDULE_SCHEMA = {"type": "function", "function": {
    "name": "get_schedule",
    "description": (
        "List the timers currently programmed (from what I have set). Use when the user asks what's "
        "scheduled — for one device or all. Reflects what I programmed; timers set outside me (e.g. the "
        "SwitchBot app) won't appear."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "One device name/alias; omit for all."},
    }, "additionalProperties": False},
}}


_CANCEL_SCHEMA = {"type": "function", "function": {
    "name": "cancel_schedule",
    "description": (
        "Cancel timers. Device alone clears all of them; add `time`, and `when` if two timers share "
        "a clock time on different dates. Reports the date of everything cancelled."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "Device name or alias."},
        "time": {"type": "string", "description": "24-hour \"HH:MM\" to cancel one timer; omit to clear all for the device."},
        "when": {"type": "string", "enum": _WHEN_TOKENS,
                 "description": "Which day, to disambiguate two timers at the same clock time on "
                                "different dates: soonest, today, tomorrow, day_after_tomorrow, "
                                "in_a_week, or a weekday sun..sat. Requires `time`."},
    }, "required": ["device"], "additionalProperties": False},
}}


def _get_schedule_impl(args, *, registry, store, now_fn):
    spoken = (args.get("device") or "").strip()
    device = None
    if spoken:
        device = registry.resolve(spoken)
        if device is None:
            return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    store.remove_expired(_utc_iso(now_fn()))      # drop fired one-time timers from the record
    rows = store.list(device)
    if not rows:
        return "nothing scheduled" if device is None else f"{device}: nothing scheduled"
    now = now_fn()
    return "\n".join(
        f"{r['device']}: {_describe_row(r['action'], r['time'], r['days'], r['once'], r['fire_at'], now)}"
        for r in rows)


def _cancel_impl(args, *, registry, store, write_fn, now_fn, scheduler=None):
    spoken = (args.get("device") or "").strip()
    time_str = (args.get("time") or "").strip() or None
    when = (args.get("when") or "").strip() or None
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"

    _expire_and_reprogram(store, registry, write_fn, now_fn)
    now = now_fn()
    target_date = None
    if when:
        if not time_str:
            return "give the time too when you name a day."
        try:
            target_date = _resolve_fire_at(when, time_str, now).date()
        except ValueError as e:
            return f"couldn't cancel: {e}"

    removed_rows = []
    for r in store.list(name):
        if time_str is not None and r["time"] != time_str:
            continue
        if target_date is not None:
            # Recurring rows have no date and must never be swept up by a dated cancel.
            if not r["once"] or not r["fire_at"]:
                continue
            # fire_at is UTC; compare in HOME_TZ or a 01:00-local timer lands a day early.
            if datetime.fromisoformat(r["fire_at"]).astimezone(now.tzinfo).date() != target_date:
                continue
        removed_rows.append(r)

    if not removed_rows:
        return f"nothing scheduled matched for {name}."
    for r in removed_rows:
        store.remove_id(r["id"])
    if registry.is_cloud(name):
        if scheduler is not None:
            try:
                for r in removed_rows:
                    scheduler.unschedule(r["id"])
            except Exception as e:
                for r in removed_rows:   # symmetric with the BLE branch below: keep the record
                    store.add(r["device"], r["action"], r["time"], r["days"], r["once"], r["fire_at"])
                return f"couldn't unschedule {name} ({e}) — timer(s) not cancelled, try again."
    else:
        try:
            _program_device(name, store, registry, write_fn, now_fn)
        except Exception as e:
            for r in removed_rows:   # roll back so the record matches the Bot and a retry re-tries
                store.add(r["device"], r["action"], r["time"], r["days"], r["once"], r["fire_at"])
            return f"couldn't reprogram {name} ({e}) — timer(s) not cancelled, try again."
    what = "; ".join(
        _describe_row(r["action"], r["time"], r["days"], r["once"], r["fire_at"], now)
        for r in removed_rows)
    return f"{name}: cancelled {len(removed_rows)} timer(s) — {what} ✅"


def _utc_iso(dt):
    """UTC ISO for storage and for every remove_expired comparison."""
    return dt.astimezone(timezone.utc).isoformat()


def _now():
    # ZoneInfo, not .astimezone(): the latter yields a FIXED-OFFSET tzinfo, which is exactly
    # what _resolve_fire_at must not build dates from. Production injects now_fn from config.
    return datetime.now(ZoneInfo("Asia/Jerusalem"))


def build_schedule_tools(registry, store, *, write_fn=None, now_fn=None, scheduler=None):
    write_fn = write_fn or _program_bot
    now_fn = now_fn or _now
    return [
        Tool(name="schedule_device", schema=_SCHEDULE_SCHEMA,
             impl=lambda args: _schedule_impl(
                 args, recurring=False, registry=registry, store=store,
                 write_fn=write_fn, now_fn=now_fn, scheduler=scheduler)),
        Tool(name="schedule_recurring_device", schema=_RECURRING_SCHEMA,
             impl=lambda args: _schedule_impl(
                 args, recurring=True, registry=registry, store=store,
                 write_fn=write_fn, now_fn=now_fn, scheduler=scheduler)),
        Tool(name="get_schedule", schema=_GET_SCHEDULE_SCHEMA,
             impl=lambda args: _get_schedule_impl(
                 args, registry=registry, store=store, now_fn=now_fn)),
        Tool(name="cancel_schedule", schema=_CANCEL_SCHEMA,
             impl=lambda args: _cancel_impl(
                 args, registry=registry, store=store, write_fn=write_fn, now_fn=now_fn, scheduler=scheduler)),
    ]
