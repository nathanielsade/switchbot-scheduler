import logging

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


def build_roborock_tools(client, registry, *, now_fn=None) -> list[Tool]:
    return [
        Tool(name="list_rooms", schema=_LIST_ROOMS_SCHEMA,
             impl=lambda args: _list_rooms_impl(args, registry=registry)),
    ]
