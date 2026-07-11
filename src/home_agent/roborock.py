import logging
import re

from switchbot_scheduler.model import DAYS

from .tools import Tool

log = logging.getLogger("home_agent")

MODES = ("vacuum", "mop", "vac_and_mop")
SUCTIONS = ("quiet", "balanced", "turbo", "max")
WATER_FLOWS = ("low", "medium", "high")


def load_roborock_client(config):
    """Build the real cloud RoborockClient from config, or return None (with a warning) when
    credentials are unset. python-roborock is imported LAZILY inside the real build path (Task 3),
    so importing this module never touches the network."""
    if not config.roborock_username or not config.roborock_password:
        log.warning("ROBOROCK_USERNAME/PASSWORD unset — Roborock control disabled")
        return None
    try:
        return _build_cloud_client(config)
    except Exception as e:
        log.warning("Roborock login failed (%s) — vacuum control disabled", e)
        return None


def _build_cloud_client(config):
    """Real cloud client. python-roborock is imported HERE (lazy) so importing this module
    never touches the network and the test suite never needs the library.
    NOTE: the exact python-roborock login + command call shapes are CONFIRMED AT BUILD TIME
    during the live smoke (Task 8); RoborockClient below is where that mapping lives."""
    return RoborockClient(config.roborock_username, config.roborock_password)


class RoborockClient:
    """Domain-level wrapper over python-roborock. Translates domain terms (mode/suction/
    water_flow enums, segment ids) into library commands. The single injectable seam; tests
    inject a fake with the same method surface."""

    def __init__(self, username, password):
        # Lazy import + cloud login. Filled in at build time against python-roborock's current API
        # (RoborockApiClient login -> home data -> device -> MQTT/local client). Kept out of the
        # test path entirely (tests use FakeRoborockClient).
        from roborock import RoborockApiClient  # noqa: F401  (lazy; confirm exact symbols at build)
        raise NotImplementedError("wire python-roborock login here at build time (Task 8)")

    # The method surface below is what the tools call; the real bodies are wired at build time.
    def room_mapping(self): raise NotImplementedError
    def clean(self, segment_ids, *, mode=None, suction=None, water_flow=None, repeat=1): raise NotImplementedError
    def pause(self): raise NotImplementedError
    def resume(self): raise NotImplementedError
    def stop(self): raise NotImplementedError
    def return_to_dock(self): raise NotImplementedError
    def locate(self): raise NotImplementedError
    def empty_bin(self): raise NotImplementedError
    def wash_mop(self): raise NotImplementedError
    def dry_mop(self): raise NotImplementedError
    def status(self): raise NotImplementedError
    def consumables(self): raise NotImplementedError
    def get_timers(self): raise NotImplementedError
    def set_timer(self, *, time, days, segment_ids, mode, suction, water_flow): raise NotImplementedError
    def del_timer(self, timer_id): raise NotImplementedError


_LIST_ROOMS_SCHEMA = {"type": "function", "function": {
    "name": "list_rooms",
    "description": (
        "List the rooms the vacuum can clean — names and Hebrew/English aliases. Use when the user "
        "asks what rooms you can clean, or when you need the exact room name before a room-scoped "
        "clean. Does NOT report the vacuum's current state."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}


def _list_rooms_impl(args, *, registry) -> str:
    if registry is None:
        return "no rooms are configured — I can only clean the whole home."
    lines = []
    for r in registry.rooms:
        aliases = ", ".join(r.aliases) if r.aliases else "(no aliases)"
        lines.append(f"{r.name} — aliases: {aliases}")
    return "\n".join(lines)


_CLEAN_SCHEMA = {"type": "function", "function": {
    "name": "clean",
    "description": (
        "Start the vacuum cleaning. Omit `rooms` to clean the WHOLE home; give one or more room "
        "names/aliases (Hebrew or English) to clean just those rooms. `mode` sets vacuum / mop / "
        "vac_and_mop (vacuum and mop); `suction` sets fan power; `water_flow` sets mop wetness. Call "
        "list_rooms first if unsure of a room name. One plan applies to the whole run. Report what "
        "you started, in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "rooms": {"type": "array", "items": {"type": "string"},
                  "description": "Room names/aliases to clean; omit for the whole home."},
        "mode": {"type": "string", "enum": list(MODES),
                 "description": "vacuum, mop, or vac_and_mop (vacuum then mop / both)."},
        "suction": {"type": "string", "enum": list(SUCTIONS), "description": "fan power."},
        "water_flow": {"type": "string", "enum": list(WATER_FLOWS), "description": "mop water level."},
        "repeat": {"type": "integer", "description": "times to repeat a room clean (default once)."},
    }, "additionalProperties": False},
}}

_MODE_WORDS = {"vacuum": "vacuum", "mop": "mop", "vac_and_mop": "vacuum + mop"}


def _describe_plan(mode, suction, water_flow) -> str:
    bits = []
    if mode: bits.append(_MODE_WORDS[mode])
    if suction: bits.append(f"suction {suction}")
    if water_flow: bits.append(f"water {water_flow}")
    return f" ({', '.join(bits)})" if bits else ""


def _resolve_rooms(rooms_spoken, registry):
    """(segment_ids|None, names|None, error_message|None)."""
    if not rooms_spoken:
        return None, None, None
    if registry is None:
        return None, None, "no rooms are configured, so I can only clean the whole home."
    segs, names, unknown = [], [], []
    for spoken in rooms_spoken:
        room = registry.resolve(spoken)
        if room is None:
            unknown.append(spoken)
        else:
            segs.append(room.segment_id); names.append(room.name)
    if unknown:
        return None, None, (f"unknown room(s): {', '.join(unknown)}. "
                            f"I can clean: {', '.join(registry.known_names())}")
    return segs, names, None


def _clean_impl(args, *, client, registry) -> str:
    rooms_spoken = args.get("rooms") or []
    mode = args.get("mode")
    suction = args.get("suction")
    water_flow = args.get("water_flow")
    repeat = args.get("repeat") or 1
    if mode is not None and mode not in MODES:
        return f"unknown mode '{mode}'. Use one of: {', '.join(MODES)}."
    if suction is not None and suction not in SUCTIONS:
        return f"unknown suction '{suction}'. Use one of: {', '.join(SUCTIONS)}."
    if water_flow is not None and water_flow not in WATER_FLOWS:
        return f"unknown water_flow '{water_flow}'. Use one of: {', '.join(WATER_FLOWS)}."
    segment_ids, names, err = _resolve_rooms(rooms_spoken, registry)
    if err:
        return err
    target = ", ".join(names) if names else "the whole home"
    try:
        client.clean(segment_ids, mode=mode, suction=suction, water_flow=water_flow, repeat=repeat)
    except Exception as e:
        return f"couldn't start cleaning — {e}"
    return f"cleaning {target}{_describe_plan(mode, suction, water_flow)} ✅"


_CONTROL_SCHEMA = {"type": "function", "function": {
    "name": "control_vacuum",
    "description": (
        "Control the running vacuum: pause, resume, stop, return_to_dock (send it back to charge), "
        "or locate (make it beep so you can find it). Report what you did, in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string",
                   "enum": ["pause", "resume", "stop", "return_to_dock", "locate"]},
    }, "required": ["action"], "additionalProperties": False},
}}

_DOCK_SCHEMA = {"type": "function", "function": {
    "name": "dock_action",
    "description": (
        "Run a dock maintenance action while the vacuum is docked: empty_bin (empty the dust bin), "
        "wash_mop (wash the mop pads), or dry_mop (dry them). Report back in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["empty_bin", "wash_mop", "dry_mop"]},
    }, "required": ["action"], "additionalProperties": False},
}}

_CONTROL_WORDS = {"pause": "paused", "resume": "resumed", "stop": "stopped",
                  "return_to_dock": "returning to dock", "locate": "locating (beeping)"}
_DOCK_WORDS = {"empty_bin": "emptying the bin", "wash_mop": "washing the mop", "dry_mop": "drying the mop"}


def _control_impl(args, *, client) -> str:
    action = (args.get("action") or "").strip().lower()
    method = {"pause": client.pause, "resume": client.resume, "stop": client.stop,
              "return_to_dock": client.return_to_dock, "locate": client.locate}.get(action)
    if method is None:
        return f"unknown action '{action}'. Use pause, resume, stop, return_to_dock, or locate."
    try:
        method()
    except Exception as e:
        return f"couldn't {action} — {e}"
    return f"{_CONTROL_WORDS[action]} ✅"


def _dock_impl(args, *, client) -> str:
    action = (args.get("action") or "").strip().lower()
    method = {"empty_bin": client.empty_bin, "wash_mop": client.wash_mop,
              "dry_mop": client.dry_mop}.get(action)
    if method is None:
        return f"unknown action '{action}'. Use empty_bin, wash_mop, or dry_mop."
    try:
        method()
    except Exception as e:
        return f"couldn't {action} — {e}"
    return f"{_DOCK_WORDS[action]} ✅"


_STATUS_SCHEMA = {"type": "function", "function": {
    "name": "vacuum_status",
    "description": (
        "Report the vacuum's current state: what it's doing, battery %, area and time cleaned, "
        "current room, and any error. Use when the user asks how the vacuum is doing or where it is. "
        "Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_CONSUMABLES_SCHEMA = {"type": "function", "function": {
    "name": "consumables",
    "description": (
        "Report remaining life of the vacuum's consumables (main brush, side brush, filter, "
        "sensors) as a percentage. Use when the user asks about maintenance or whether parts need "
        "replacing. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_CONSUMABLE_LABELS = {"main_brush": "main brush", "side_brush": "side brush",
                      "filter": "filter", "sensor": "sensor"}


def _status_impl(args, *, client, registry) -> str:
    try:
        s = client.status()
    except Exception as e:
        return f"couldn't read the vacuum status — {e}"
    lines = [f"state: {s.get('state', 'unknown')}", f"battery: {s.get('battery', '?')}%"]
    if s.get("cleaned_area"):
        lines.append(f"cleaned area: {s['cleaned_area']} m²")
    if s.get("clean_time"):
        lines.append(f"clean time: {s['clean_time'] // 60} min")
    seg = s.get("segment_id")
    room = registry.name_for_segment(seg) if (registry is not None and seg is not None) else None
    if room:
        lines.append(f"current room: {room}")
    if s.get("error"):
        lines.append(f"error: {s['error']}")
    return "\n".join(lines)


def _consumables_impl(args, *, client) -> str:
    try:
        c = client.consumables()
    except Exception as e:
        return f"couldn't read consumables — {e}"
    return "\n".join(f"{_CONSUMABLE_LABELS.get(k, k)}: {v}% remaining" for k, v in c.items())


_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_DAY_WORDS = {"daily": list(DAYS), "weekdays": ["mon", "tue", "wed", "thu", "fri"],
              "weekends": ["sat", "sun"]}


def _normalize_days(days):
    result = []
    for d in days:
        key = str(d).strip().lower()
        if key in _DAY_WORDS:
            candidates = _DAY_WORDS[key]
        elif key in DAYS:
            candidates = [key]
        else:
            raise ValueError(f"unknown day '{d}'")
        for c in candidates:
            if c not in result:
                result.append(c)
    return result


_SCHEDULE_CLEAN_SCHEMA = {"type": "function", "function": {
    "name": "schedule_clean",
    "description": (
        "Program a RECURRING cleaning schedule into the vacuum itself (runs even if this computer is "
        "off). `time` is 24-hour \"HH:MM\". `days` are the days it repeats (sun mon tue wed thu fri "
        "sat, or the words daily/weekdays/weekends) — omit for every day. Omit `rooms` for the whole "
        "home. `mode`/`suction`/`water_flow` set the cleaning plan. Report what you scheduled, in the "
        "user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "time": {"type": "string", "description": "24-hour clock time, \"HH:MM\"."},
        "days": {"type": "array", "items": {"type": "string"},
                 "description": "Days to repeat; omit for daily."},
        "rooms": {"type": "array", "items": {"type": "string"},
                  "description": "Rooms to clean; omit for the whole home."},
        "mode": {"type": "string", "enum": list(MODES)},
        "suction": {"type": "string", "enum": list(SUCTIONS)},
        "water_flow": {"type": "string", "enum": list(WATER_FLOWS)},
    }, "required": ["time"], "additionalProperties": False},
}}

_GET_SCHEDULE_SCHEMA = {"type": "function", "function": {
    "name": "get_cleaning_schedule",
    "description": (
        "List the vacuum's programmed cleaning schedules (each has an id, time, days, and target). "
        "Use when the user asks what cleans are scheduled. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_CANCEL_SCHEDULE_SCHEMA = {"type": "function", "function": {
    "name": "cancel_cleaning_schedule",
    "description": (
        "Cancel one programmed cleaning schedule by its id (from get_cleaning_schedule). Report in "
        "the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "id": {"type": "string", "description": "The schedule id to cancel."},
    }, "required": ["id"], "additionalProperties": False},
}}


def _schedule_clean_impl(args, *, client, registry) -> str:
    time_str = (args.get("time") or "").strip()
    if not _TIME_RE.match(time_str):
        return f"invalid time '{time_str}'. Use 24-hour HH:MM, e.g. 08:00."
    mode, suction, water_flow = args.get("mode"), args.get("suction"), args.get("water_flow")
    for val, allowed, label in ((mode, MODES, "mode"), (suction, SUCTIONS, "suction"),
                                (water_flow, WATER_FLOWS, "water_flow")):
        if val is not None and val not in allowed:
            return f"unknown {label} '{val}'. Use one of: {', '.join(allowed)}."
    try:
        days = _normalize_days(args.get("days") or ["daily"])
    except ValueError as e:
        return f"couldn't set the schedule: {e}"
    segment_ids, names, err = _resolve_rooms(args.get("rooms") or [], registry)
    if err:
        return err
    try:
        client.set_timer(time=time_str, days=days, segment_ids=segment_ids,
                         mode=mode, suction=suction, water_flow=water_flow)
    except Exception as e:
        return f"couldn't set the schedule — {e}"
    target = ", ".join(names) if names else "the whole home"
    return f"scheduled: clean {target} at {time_str} ({', '.join(days)}){_describe_plan(mode, suction, water_flow)} ✅"


def _get_schedule_impl(args, *, client) -> str:
    try:
        timers = client.get_timers()
    except Exception as e:
        return f"couldn't read the schedule — {e}"
    if not timers:
        return "nothing scheduled."
    lines = []
    for t in timers:
        state = "" if t.get("enabled", True) else " (disabled)"
        lines.append(f"[{t['id']}] {t['time']} {', '.join(t.get('days', []))} — {t.get('target', 'whole home')}{state}")
    return "\n".join(lines)


def _cancel_schedule_impl(args, *, client) -> str:
    timer_id = (args.get("id") or "").strip()
    try:
        ok = client.del_timer(timer_id)
    except Exception as e:
        return f"couldn't cancel — {e}"
    if not ok:
        return f"no schedule with id {timer_id} was found."
    return f"cancelled schedule {timer_id} ✅"


def build_roborock_tools(client, registry, *, now_fn=None) -> list[Tool]:
    return [
        Tool(name="list_rooms", schema=_LIST_ROOMS_SCHEMA,
             impl=lambda args: _list_rooms_impl(args, registry=registry)),
        Tool(name="clean", schema=_CLEAN_SCHEMA,
             impl=lambda args: _clean_impl(args, client=client, registry=registry)),
        Tool(name="control_vacuum", schema=_CONTROL_SCHEMA,
             impl=lambda args: _control_impl(args, client=client)),
        Tool(name="dock_action", schema=_DOCK_SCHEMA,
             impl=lambda args: _dock_impl(args, client=client)),
        Tool(name="vacuum_status", schema=_STATUS_SCHEMA,
             impl=lambda args: _status_impl(args, client=client, registry=registry)),
        Tool(name="consumables", schema=_CONSUMABLES_SCHEMA,
             impl=lambda args: _consumables_impl(args, client=client)),
        Tool(name="schedule_clean", schema=_SCHEDULE_CLEAN_SCHEMA,
             impl=lambda args: _schedule_clean_impl(args, client=client, registry=registry)),
        Tool(name="get_cleaning_schedule", schema=_GET_SCHEDULE_SCHEMA,
             impl=lambda args: _get_schedule_impl(args, client=client)),
        Tool(name="cancel_cleaning_schedule", schema=_CANCEL_SCHEDULE_SCHEMA,
             impl=lambda args: _cancel_schedule_impl(args, client=client)),
    ]
