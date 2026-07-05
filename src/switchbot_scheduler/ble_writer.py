import asyncio
import time
from .encoder import encode_alarm
from .model import Schedule
from .registry import Registry

# SwitchBot BLE GATT characteristics (confirmed via spike against real Bots 2026-07-05).
WRITE_CHAR = "cba20002-224d-11e6-9fb8-0002a5d5c51b"
NOTIFY_CHAR = "cba20003-224d-11e6-9fb8-0002a5d5c51b"

# 0x09 = "Set Device Time Management Info" (unencrypted base 0x57 0x09).
MAGIC = 0x57
CMD_TIME_MGMT = 0x09
SUB_SET_CLOCK = 0x01
SUB_SET_COUNT = 0x02
MODE_AT_TIME = 0x00  # execute at HH:MM, repeating by the weekday bits


def build_clock_frame(now_epoch: float, gmtoff_seconds: int) -> bytes:
    """Set the Bot's clock to local time (device has no timezone, so send local-as-unix)."""
    clock_ts = int(now_epoch) + gmtoff_seconds
    return bytes([MAGIC, CMD_TIME_MGMT, SUB_SET_CLOCK]) + clock_ts.to_bytes(8, "big")


def build_count_frame(num_alarms: int) -> bytes:
    return bytes([MAGIC, CMD_TIME_MGMT, SUB_SET_COUNT, num_alarms])


def build_alarm_frames(alarms: list[dict]) -> list[bytes]:
    """One 14-byte frame per alarm:
    57 09 [idx*16+3] [total] 00 [repeat] HH MM [mode] [job] [sum] [int_h int_m int_s]"""
    total = len(alarms)
    frames = []
    for index, a in enumerate(alarms):
        subcmd = index * 16 + 3
        frames.append(bytes([
            MAGIC, CMD_TIME_MGMT, subcmd, total, 0x00,
            a["repeat_byte"], a["hour"], a["minute"],
            MODE_AT_TIME, a["action"], 0x00, 0x00, 0x00, 0x00,
        ]))
    return frames


async def write_alarms(ble_id: str, alarms: list[dict]) -> None:
    """Program a Bot's complete alarm set in one connection: clock -> count -> alarms."""
    from bleak import BleakClient
    now = time.time()
    gmtoff = time.localtime(now).tm_gmtoff or 0
    frames = [build_clock_frame(now, gmtoff), build_count_frame(len(alarms))]
    frames += build_alarm_frames(alarms)

    responses: list[bytes] = []
    async with BleakClient(ble_id) as client:
        await client.start_notify(NOTIFY_CHAR, lambda _, d: responses.append(bytes(d)))
        for frame in frames:
            await client.write_gatt_char(WRITE_CHAR, frame, response=True)
            await asyncio.sleep(0.5)
        await asyncio.sleep(0.5)
        await client.stop_notify(NOTIFY_CHAR)


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
