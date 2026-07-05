"""Manual Bluetooth spike. Run near the Bots. NOT shipped logic.

Usage:
    python spikes/ble_spike.py scan            # list nearby BLE devices, flag SwitchBots
    python spikes/ble_spike.py press <UUID>    # send one press to that device (arm moves)
"""
import asyncio
import sys

# SwitchBot BLE GATT characteristics (from OpenWonderLabs/SwitchBotAPI-BLE)
WRITE_CHAR = "cba20002-224d-11e6-9fb8-0002a5d5c51b"
NOTIFY_CHAR = "cba20003-224d-11e6-9fb8-0002a5d5c51b"

# SwitchBot advertises service data under this 16-bit UUID (0xFD3D)
SWITCHBOT_SVC = "0000fd3d-0000-1000-8000-00805f9b34fb"


async def scan():
    from bleak import BleakScanner
    print("Scanning 12s (make sure Bluetooth is ON and the terminal has Bluetooth permission)...\n")
    found = await BleakScanner.discover(timeout=12.0, return_adv=True)
    rows = []
    for address, (dev, adv) in found.items():
        svc = adv.service_data or {}
        is_sb = SWITCHBOT_SVC in svc or (dev.name or "").lower() in ("wohand", "bot")
        model = ""
        if SWITCHBOT_SVC in svc and svc[SWITCHBOT_SVC]:
            model = f"model=0x{svc[SWITCHBOT_SVC][0]:02x}"  # Bot = 0x48 ('H')
        rows.append((is_sb, adv.rssi or -999, address, dev.name, model))
    rows.sort(key=lambda r: (not r[0], -r[1]))  # SwitchBots first, then strongest signal
    for is_sb, rssi, address, name, model in rows:
        tag = "  <== SWITCHBOT" if is_sb else ""
        print(f"{address}  rssi={rssi:>4}  name={name!r:<20} {model}{tag}")
    print(f"\n{sum(1 for r in rows if r[0])} likely SwitchBot device(s) found, {len(rows)} total.")


async def services(ble_id: str):
    from bleak import BleakClient
    async with BleakClient(ble_id) as client:
        for svc in client.services:
            print(f"service {svc.uuid}")
            for ch in svc.characteristics:
                print(f"    char {ch.uuid}  props={','.join(ch.properties)}")


async def press(ble_id: str):
    from bleak import BleakClient
    responses = []
    async with BleakClient(ble_id) as client:
        await client.start_notify(NOTIFY_CHAR, lambda _, data: responses.append(bytes(data)))
        await client.write_gatt_char(WRITE_CHAR, b"\x57\x01\x00", response=True)
        await asyncio.sleep(2.0)
        await client.stop_notify(NOTIFY_CHAR)
    if responses:
        for r in responses:
            code = r[0]
            meaning = {0x01: "OK/success", 0x02: "battery low", 0x06: "device busy/unsupported"}.get(code, "?")
            print(f"Bot replied: {r.hex()}  (status byte 0x{code:02x} = {meaning})")
    else:
        print("no reply from Bot (command may have been silently ignored)")
    print(f"press sent to {ble_id} — did the arm move?")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "scan":
        asyncio.run(scan())
    elif cmd == "services":
        asyncio.run(services(sys.argv[2]))
    elif cmd == "press":
        asyncio.run(press(sys.argv[2]))
    else:
        print(__doc__)
