import asyncio
from .encoder import encode_alarm
from .model import Schedule
from .registry import Registry

# SwitchBot BLE GATT characteristics (confirmed via spike against real Bots 2026-07-05).
WRITE_CHAR = "cba20002-224d-11e6-9fb8-0002a5d5c51b"

# Command frame layout (confirm bytes against spikes/FINDINGS.md):
#   [0] 0x57 magic  [1] 0x09 set-time-management  [2] total count  [3] index
#   [4] repeat_byte [5] hour  [6] minute  [7] action(job type)
MAGIC = 0x57
CMD_SET_TIME_MGMT = 0x09


def build_alarm_frames(alarms: list[dict]) -> list[bytes]:
    total = len(alarms)
    frames = []
    for index, a in enumerate(alarms):
        frames.append(bytes([
            MAGIC, CMD_SET_TIME_MGMT, total, index,
            a["repeat_byte"], a["hour"], a["minute"], a["action"],
        ]))
    return frames


async def write_alarms(ble_id: str, alarms: list[dict]) -> None:
    from bleak import BleakClient
    frames = build_alarm_frames(alarms)
    async with BleakClient(ble_id) as client:
        for frame in frames:
            await client.write_gatt_char(WRITE_CHAR, frame, response=True)


def group_events_by_device(schedule: Schedule) -> dict:
    grouped: dict = {}
    for ds in schedule.schedules:
        grouped.setdefault(ds.device, []).extend(ds.events)
    return grouped


def write_schedule(schedule: Schedule, registry: Registry) -> None:
    for device, events in group_events_by_device(schedule).items():
        ble_id = registry.ble_id(device)
        if not ble_id:
            raise ValueError(f"No ble_id for '{device}'. Fill devices.yaml from the spike.")
        alarms = [encode_alarm(e, inverted=registry.is_inverted(device)) for e in events]
        asyncio.run(write_alarms(ble_id, alarms))
