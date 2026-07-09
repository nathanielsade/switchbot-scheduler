import logging
import os

from switchbot_scheduler.actuator import run_immediate
from switchbot_scheduler.model import ImmediateAction
from switchbot_scheduler.registry import Registry

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


def _device_type(device) -> str:
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


def _control_impl(args, *, registry, actuate_fn) -> str:
    spoken = (args.get("device") or "").strip()
    action = (args.get("action") or "").strip().lower()
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    if action not in ("on", "off", "press"):
        return f"unknown action '{action}'. Use on, off, or press."
    result = run_immediate([ImmediateAction(name, action)], registry, actuate_fn=actuate_fn)[0]
    if result.ok:
        return f"{result.device}: {result.action} ✅"
    return f"{result.device}: failed — {result.error}"


def build_home_tools(registry, *, actuate_fn=None, battery_fn=None) -> list[Tool]:
    return [
        Tool(name="control_device", schema=_CONTROL_SCHEMA,
             impl=lambda args: _control_impl(args, registry=registry, actuate_fn=actuate_fn)),
        Tool(name="list_devices", schema=_LIST_SCHEMA,
             impl=lambda args: _list_impl(args, registry=registry)),
    ]
