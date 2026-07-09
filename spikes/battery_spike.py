"""One-off: read a SwitchBot Bot's 'basic info' reply and print the raw bytes so we can
locate the battery percentage. Usage: python spikes/battery_spike.py <BLE_ID>"""
import asyncio
import sys
from switchbot_scheduler.actuator import WRITE_CHAR, NOTIFY_CHAR, MAGIC

CMD_INFO = 0x02


async def main(ble_id: str) -> None:
    from bleak import BleakClient
    responses: list[bytes] = []
    async with BleakClient(ble_id) as client:
        await client.start_notify(NOTIFY_CHAR, lambda _, d: responses.append(bytes(d)))
        await client.write_gatt_char(WRITE_CHAR, bytes([MAGIC, CMD_INFO]), response=True)
        await asyncio.sleep(1.5)
        await client.stop_notify(NOTIFY_CHAR)
    if not responses:
        print("NO RESPONSE from device (basic-info command returned nothing)")
        return
    for i, r in enumerate(responses):
        print(f"response[{i}] = {list(r)}  (hex: {r.hex()})")
        print(f"  byte[1] (our battery assumption) = {r[1] if len(r) > 1 else 'N/A'}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
