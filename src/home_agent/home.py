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


def build_home_tools(registry, *, actuate_fn=None, battery_fn=None) -> list[Tool]:
    return [
        Tool(name="list_devices", schema=_LIST_SCHEMA,
             impl=lambda args: _list_impl(args, registry=registry)),
    ]
