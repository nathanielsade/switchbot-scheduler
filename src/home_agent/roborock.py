import logging
import re

from switchbot_scheduler.model import DAYS

from .tools import Tool

log = logging.getLogger("home_agent")

MODES = ("vacuum", "mop", "vac_and_mop")
SUCTIONS = ("quiet", "balanced", "turbo", "max")
WATER_FLOWS = ("low", "medium", "high")
# Default cleaning program when the user doesn't specify one — vacuum-and-mop ("שאיבה ואז שטיפה").
DEFAULT_CLEAN_MODE = "vac_and_mop"


def load_roborock_client(config):
    """Build the real cloud RoborockClient from config, or return None (with a warning) when the
    account isn't configured. python-roborock is imported LAZILY inside the build path, so importing
    this module never touches the network and the test suite never needs the library."""
    if not config.roborock_username:
        log.warning("ROBOROCK_USERNAME unset — Roborock control disabled")
        return None
    try:
        return _build_cloud_client(config)
    except Exception as e:
        log.warning("Roborock connect failed (%s) — vacuum control disabled", e)
        return None


def _build_cloud_client(config):
    """Obtain UserData (from a saved token file if present, else a password login) and build the
    connected RoborockClient. python-roborock is imported HERE (lazy). Token-file auth is preferred
    because accounts created via the app's email-code / Google sign-in have no usable password."""
    import json
    import os

    from roborock.data.containers import UserData

    if os.path.exists(config.roborock_userdata_path):
        with open(config.roborock_userdata_path) as f:
            user_data = UserData.from_dict(json.load(f))
    elif config.roborock_password:
        import asyncio

        from roborock.web_api import RoborockApiClient
        user_data = asyncio.run(RoborockApiClient(config.roborock_username).pass_login(config.roborock_password))
    else:
        raise RuntimeError(
            f"no Roborock auth: neither a token file at {config.roborock_userdata_path!r} nor ROBOROCK_PASSWORD"
        )
    return RoborockClient(config.roborock_username, user_data)


# Roborock v1 wire codes (confirmed live on the Qrevo a187: status showed fan_power 104, water_box_mode 202).
_SUCTION_CODE = {"quiet": 101, "balanced": 102, "turbo": 103, "max": 104}
_WATER_CODE = {"low": 201, "medium": 202, "high": 203}
_STATE_NAMES = {
    1: "starting", 2: "charger disconnected", 3: "idle", 4: "remote control", 5: "cleaning",
    6: "returning to dock", 7: "manual mode", 8: "charging", 9: "charging error", 10: "paused",
    11: "spot cleaning", 12: "error", 13: "shutting down", 14: "updating", 15: "docking",
    16: "going to target", 17: "zone cleaning", 18: "room cleaning", 22: "emptying dust",
    23: "washing mop", 26: "going to wash the mop",
}


class RoborockClient:
    """Domain-level wrapper over python-roborock (cloud/MQTT). Translates domain terms (mode/suction/
    water_flow enums, segment ids) into device commands via the v1 `command.send` primitive.

    The tools call this synchronously (python-telegram-bot runs handlers in a worker thread), but the
    library is async, so we own a background asyncio loop thread and marshal each call onto it. The
    MQTT connection is established once and reused. This is the single injectable seam; tests inject a
    fake with the same method surface, so none of this async/library code runs in the suite."""

    def __init__(self, username, user_data, *, connect_timeout=30.0, call_timeout=60.0):
        import asyncio
        import threading

        from roborock import RoborockCommand
        self._RoborockCommand = RoborockCommand
        self._call_timeout = call_timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="roborock-loop")
        self._thread.start()
        self._username = username
        self._user_data = user_data
        self._mgr = None
        self._dev = None
        self._run(self._connect(), timeout=connect_timeout)

    # ---- async plumbing ------------------------------------------------------
    def _run(self, coro, timeout=None):
        import asyncio
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout or self._call_timeout)

    async def _connect(self):
        from roborock.devices.device_manager import UserParams, create_device_manager
        self._mgr = await create_device_manager(
            UserParams(username=self._username, user_data=self._user_data))
        devices = await self._mgr.discover_devices()
        if not devices:
            raise RuntimeError("no Roborock devices found on the account")
        self._dev = devices[0]
        if not self._dev.is_connected:
            await self._dev.connect()

    async def _send(self, command, params=None):
        return await self._dev.v1_properties.command.send(command, params)

    def _cmd(self, command, params=None):
        return self._run(self._send(command, params))

    # ---- reads ---------------------------------------------------------------
    def room_mapping(self):
        raw = self._cmd(self._RoborockCommand.GET_ROOM_MAPPING) or []
        # [[segment_id, iot_room_id, ...], ...]; name lookup is the caller's job (web rooms).
        return [(row[0], str(row[1])) for row in raw if isinstance(row, (list, tuple)) and len(row) >= 2]

    def status(self):
        raw = self._cmd(self._RoborockCommand.GET_STATUS)
        s = raw[0] if isinstance(raw, list) else raw
        code = s.get("error_code") or 0
        return {
            "state": _STATE_NAMES.get(s.get("state"), f"state {s.get('state')}"),
            "battery": s.get("battery"),
            "cleaned_area": round((s.get("clean_area") or 0) / 1_000_000, 1),  # mm² → m²
            "clean_time": s.get("clean_time") or 0,                            # seconds
            "segment_id": None,  # not exposed by GET_STATUS
            "error": None if code == 0 else f"error code {code}",
        }

    def consumables(self):
        raw = self._cmd(self._RoborockCommand.GET_CONSUMABLE)
        c = raw[0] if isinstance(raw, list) else raw
        # Report % remaining against the manufacturer replace intervals (seconds).
        HOUR = 3600
        lifetimes = {"main_brush": 300 * HOUR, "side_brush": 200 * HOUR,
                     "filter": 150 * HOUR, "sensor": 30 * HOUR}
        used = {"main_brush": c.get("main_brush_work_time", 0), "side_brush": c.get("side_brush_work_time", 0),
                "filter": c.get("filter_work_time", 0), "sensor": c.get("sensor_dirty_time", 0)}
        return {k: max(0, round(100 * (1 - used[k] / lifetimes[k]))) for k in lifetimes}

    # ---- cleaning ------------------------------------------------------------
    def _apply_plan(self, mode, suction, water_flow):
        R = self._RoborockCommand
        # High-level program (vacuum / mop / vac_and_mop) via the device's own cleaning-mode setting —
        # the enum values match our MODES verbatim. "שאיבה ואז שטיפה" maps to vac_and_mop.
        if mode in MODES:
            self._run(self._dev.v1_properties.status.set_cleaning_mode(mode))
        if suction in _SUCTION_CODE:
            self._cmd(R.SET_CUSTOM_MODE, [_SUCTION_CODE[suction]])
        if water_flow in _WATER_CODE:
            self._cmd(R.SET_WATER_BOX_CUSTOM_MODE, [_WATER_CODE[water_flow]])

    def clean(self, segment_ids, *, mode=None, suction=None, water_flow=None, repeat=1):
        if mode or suction or water_flow:
            self._apply_plan(mode, suction, water_flow)
        R = self._RoborockCommand
        if segment_ids:
            self._cmd(R.APP_SEGMENT_CLEAN, [{"segments": list(segment_ids), "repeat": repeat}])
        else:
            self._cmd(R.START_CLEAN)

    def pause(self):
        self._cmd(self._RoborockCommand.APP_PAUSE)

    def resume(self):
        self._cmd(self._RoborockCommand.RESUME_SEGMENT_CLEAN)

    def stop(self):
        self._cmd(self._RoborockCommand.APP_STOP)

    def return_to_dock(self):
        self._cmd(self._RoborockCommand.APP_CHARGE)

    def locate(self):
        self._cmd(self._RoborockCommand.FIND_ME)

    # ---- dock ----------------------------------------------------------------
    def empty_bin(self):
        self._cmd(self._RoborockCommand.APP_START_COLLECT_DUST)

    def wash_mop(self):
        self._cmd(self._RoborockCommand.APP_START_WASH)

    def dry_mop(self):
        self._cmd(self._RoborockCommand.APP_SET_DRYER_STATUS, {"status": 1})

    # ---- schedules (robot-native server timers) ------------------------------
    def get_timers(self):
        raw = self._cmd(self._RoborockCommand.GET_SERVER_TIMER) or []
        timers = []
        for row in raw:
            # [id, "on"/"off", [cron_min, cron_hour, ...], ...]
            tid = str(row[0]) if isinstance(row, (list, tuple)) else str(row)
            enabled = (row[1] == "on") if isinstance(row, (list, tuple)) and len(row) > 1 else True
            timers.append({"id": tid, "time": "", "days": [], "enabled": enabled, "target": "", "mode": None})
        return timers

    def set_timer(self, *, time, days, segment_ids, mode, suction, water_flow):
        # SET_SERVER_TIMER's payload (a cron plus an embedded clean command) is firmware-specific and
        # not yet verified live; surface a friendly failure so schedule_clean degrades per the spec's
        # scheduling fallback rather than sending a malformed command to the robot.
        raise RuntimeError("recurring cleaning schedules aren't enabled yet on this robot")

    def del_timer(self, timer_id):
        self._cmd(self._RoborockCommand.DEL_SERVER_TIMER, [str(timer_id)])
        return True

    def close(self):
        try:
            self._run(self._mgr.close(), timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)


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
        "names/aliases (Hebrew or English) to clean just those rooms. `suction` sets fan power; "
        "`water_flow` sets mop wetness. Call list_rooms first if unsure of a room name. One plan "
        "applies to the whole run. Report what you started, in the user's language. "
        "IMPORTANT: leave `mode` unset by default — the default program is vacuum-and-mop "
        "(שאיבה ואז שטיפה). Only set `mode` when the user explicitly asks for vacuum-only "
        "(שאיבה) or mop-only (שטיפה)."
    ),
    "parameters": {"type": "object", "properties": {
        "rooms": {"type": "array", "items": {"type": "string"},
                  "description": "Room names/aliases to clean; omit for the whole home."},
        "mode": {"type": "string", "enum": list(MODES),
                 "description": "vacuum (only) / mop (only) / vac_and_mop. OMIT for the default vac_and_mop."},
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
    mode = mode or DEFAULT_CLEAN_MODE   # default program: vacuum-and-mop unless the user asked otherwise
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
    mode = mode or DEFAULT_CLEAN_MODE   # default program: vacuum-and-mop unless the user asked otherwise
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
