import asyncio
from dataclasses import dataclass
from .encoder import ACTION_CODE
from .registry import Registry
from .model import ImmediateAction

# Same GATT characteristics as the scheduled writer.
WRITE_CHAR = "cba20002-224d-11e6-9fb8-0002a5d5c51b"
NOTIFY_CHAR = "cba20003-224d-11e6-9fb8-0002a5d5c51b"
MAGIC = 0x57
CMD_CONTROL = 0x01   # 0x57 0x01 <code>: press=0, on=1, off=2


@dataclass
class ActionResult:
    device: str
    action: str          # the RESOLVED action actually sent (after inverted/press mapping)
    ok: bool
    error: str | None = None


def resolve_action(device: str, action: str, registry: Registry) -> str:
    """Mirror the scheduled path: press-mode forces press; inverted swaps on/off."""
    if registry.is_press_mode(device):
        return "press"
    if registry.is_inverted(device) and action in ("on", "off"):
        return "off" if action == "on" else "on"
    return action


async def actuate(ble_id: str, action_code: int) -> bytes:
    """Send one live control command and return the Bot's reply (empty if none)."""
    from bleak import BleakClient
    responses: list[bytes] = []
    async with BleakClient(ble_id) as client:
        await client.start_notify(NOTIFY_CHAR, lambda _, d: responses.append(bytes(d)))
        await client.write_gatt_char(WRITE_CHAR, bytes([MAGIC, CMD_CONTROL, action_code]), response=True)
        await asyncio.sleep(1.0)
        await client.stop_notify(NOTIFY_CHAR)
    return responses[-1] if responses else b""


def _run_actuate(ble_id: str, action_code: int) -> bytes:
    return asyncio.run(actuate(ble_id, action_code))


def run_immediate(actions, registry: Registry, actuate_fn=None) -> list[ActionResult]:
    """Fire each immediate action live, one BLE connection per device. Never raises:
    a per-device failure becomes an ActionResult(ok=False) and does not abort the rest."""
    do = actuate_fn or _run_actuate
    known = registry.known_names()
    results: list[ActionResult] = []
    for a in actions:
        if a.device not in known:
            results.append(ActionResult(a.device, a.action, False, f"unknown device '{a.device}'"))
            continue
        action = resolve_action(a.device, a.action, registry)
        ble_id = registry.ble_id(a.device)
        if not ble_id:
            results.append(ActionResult(a.device, action, False, "no ble_id in devices.yaml"))
            continue
        try:
            do(ble_id, ACTION_CODE[action])
            results.append(ActionResult(a.device, action, True, None))
        except Exception as err:
            results.append(ActionResult(a.device, action, False, str(err)))
    return results
