import logging
import os

from switchbot_scheduler.actuator import run_immediate, resolve_action
from switchbot_scheduler.model import ImmediateAction
from switchbot_scheduler.registry import Registry

from . import switchbot_cloud
from .tools import Tool

log = logging.getLogger("home_agent")

_LIST_SCHEMA = {"type": "function", "function": {
    "name": "list_devices",
    "description": (
        "List the home devices you can control — names, Hebrew/English aliases, and type. "
        "Use when the user asks what you can control, or when you need the exact device name "
        "before control_device. Does NOT report whether a device is currently on or off."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_CONTROL_SCHEMA = {"type": "function", "function": {
    "name": "control_device",
    "description": (
        "Control a SwitchBot device by sending an on/off/press command. Use whenever the user asks "
        "to turn something on or off, or to toggle it (lights, AC). The air conditioner supports only "
        "'press' (a momentary toggle whose resulting on/off state is unknown). If unsure of the exact "
        "device name, call list_devices first. Report back what happened in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string",
                   "description": "Room/device name or alias, Hebrew or English (e.g. 'סלון', 'living room', 'מזגן', 'kitchen')."},
        "action": {"type": "string", "enum": ["on", "off", "press"],
                   "description": "on, off, or press; the AC only honors press."},
    }, "required": ["device", "action"], "additionalProperties": False},
}}

_BATTERY_SCHEMA = {"type": "function", "function": {
    "name": "battery_status",
    "description": (
        "Report SwitchBot device battery levels. Use when the user asks about battery or whether a "
        "device is running low. Pass one device name/alias to check just that one; omit to check all. "
        "Returns each device's battery percent, or an error if a Bot couldn't be reached."
    ),
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string", "description": "One device name/alias; omit to check all."},
    }, "additionalProperties": False},
}}

_CMD_INFO = 0x02  # 0x57 0x02: "get device basic info"; response carries battery %


def _device_type(device) -> str:
    if device.cloud_id:
        return "cloud-controlled"
    if device.mode == "press":
        return "AC / momentary toggle"
    if device.inverted:
        return "light (mounted inverted)"
    return "light"


def _list_impl(args, *, registry) -> str:
    lines = []
    for d in registry.devices:
        aliases = ", ".join(d.aliases) if d.aliases else "(no aliases)"
        lines.append(f"{d.name} [{_device_type(d)}] — aliases: {aliases}")
    return "\n".join(lines)


def _control_impl(args, *, registry, actuate_fn, cloud_send_fn) -> str:
    spoken = (args.get("device") or "").strip()
    action = (args.get("action") or "").strip().lower()
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    if action not in ("on", "off", "press"):
        return f"unknown action '{action}'. Use on, off, or press."
    if registry.is_cloud(name):
        eff = resolve_action(name, action, registry)
        try:
            cloud_send_fn(registry.cloud_id(name), switchbot_cloud.to_command(eff))
        except Exception as e:
            return f"{name}: failed — {e}"
        reported = "press" if registry.is_press_mode(name) else action
        return f"{name}: {reported} ✅"
    result = run_immediate([ImmediateAction(name, action)], registry, actuate_fn=actuate_fn)[0]
    if result.ok:
        reported = "press" if registry.is_press_mode(name) else action
        return f"{result.device}: {reported} ✅"
    return f"{result.device}: failed — {result.error}"


def _run_battery(ble_id: str) -> int:
    """Real BLE battery read (production). Battery byte offset confirmed by the Task 6 spike."""
    import asyncio
    from switchbot_scheduler.actuator import WRITE_CHAR, NOTIFY_CHAR, MAGIC

    async def _read() -> int:
        from bleak import BleakClient
        responses: list[bytes] = []
        async with BleakClient(ble_id) as client:
            await client.start_notify(NOTIFY_CHAR, lambda _, d: responses.append(bytes(d)))
            await client.write_gatt_char(WRITE_CHAR, bytes([MAGIC, _CMD_INFO]), response=True)
            await asyncio.sleep(1.0)
            await client.stop_notify(NOTIFY_CHAR)
        if not responses:
            raise RuntimeError("no response from device")
        return responses[-1][1]  # battery percent (byte index 1; confirmed on kitchen Bot 2026-07-09)

    return asyncio.run(_read())


def _battery_impl(args, *, registry, battery_fn, cloud_battery_fn) -> str:
    spoken = (args.get("device") or "").strip()
    if spoken:
        name = registry.resolve(spoken)
        if name is None:
            return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
        targets = [name]
    else:
        targets = registry.known_names()
    lines = []
    for name in targets:
        if registry.is_cloud(name):
            try:
                v = cloud_battery_fn(registry.cloud_id(name))
                if v is not None:
                    lines.append(f"{name}: {v}%")
                else:
                    lines.append(f"{name}: battery unavailable")
            except Exception as e:
                lines.append(f"{name}: unavailable — {e}")
            continue
        ble_id = registry.ble_id(name)
        if not ble_id:
            lines.append(f"{name}: unavailable — no ble_id")
            continue
        try:
            lines.append(f"{name}: {battery_fn(ble_id)}%")
        except Exception as e:
            lines.append(f"{name}: unavailable — {e}")
    return "\n".join(lines)


def load_registry(config):
    """Return the device Registry, or None if the devices file is absent."""
    return Registry.load(config.devices_path) if os.path.exists(config.devices_path) else None


def load_home_tools(config) -> list[Tool]:
    """Build the home tools from config.devices_path. If the file is absent, log a warning and
    return [] so the bot still runs (time-only) instead of crashing at startup."""
    path = config.devices_path
    if not os.path.exists(path):
        log.warning("devices file not found at %s — home control disabled", path)
        return []
    return build_home_tools(Registry.load(path))


def build_home_tools(registry, *, actuate_fn=None, battery_fn=None,
                     cloud_send_fn=None, cloud_battery_fn=None) -> list[Tool]:
    battery_fn = battery_fn or _run_battery
    return [
        Tool(name="control_device", schema=_CONTROL_SCHEMA,
             impl=lambda args: _control_impl(args, registry=registry,
                 actuate_fn=actuate_fn, cloud_send_fn=cloud_send_fn)),
        Tool(name="list_devices", schema=_LIST_SCHEMA,
             impl=lambda args: _list_impl(args, registry=registry)),
        Tool(name="battery_status", schema=_BATTERY_SCHEMA,
             impl=lambda args: _battery_impl(args, registry=registry,
                 battery_fn=battery_fn, cloud_battery_fn=cloud_battery_fn)),
    ]
