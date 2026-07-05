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


async def settimer(ble_id: str, minutes_ahead: int = 2):
    """Program ONE on-device alarm to fire `minutes_ahead` from now, every day, action=press.
    Sequence per SwitchBot BLE 0x09: set clock -> set count -> set alarm."""
    import time
    from bleak import BleakClient

    now = time.time()
    gmtoff = time.localtime(now).tm_gmtoff or 0
    clock_ts = int(now) + gmtoff                      # device clock = local-time-as-unix
    fire = time.localtime(now + minutes_ahead * 60)
    hh, mm = fire.tm_hour, fire.tm_min

    set_clock = b"\x57\x09\x01" + clock_ts.to_bytes(8, "big")
    set_count = b"\x57\x09\x02\x01"                    # 1 alarm
    # subcmd 0x03 (idx0), num=1, filler 0x00, repeat 0x7F (bit7=0 repeat, all 7 days),
    # HH, MM, mode 0x00 (at HH:MM), job 0x00 (press), sum 0, interval 0,0,0
    set_alarm = bytes([0x57, 0x09, 0x03, 0x01, 0x00, 0x7F, hh, mm, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

    responses = []
    async with BleakClient(ble_id) as client:
        await client.start_notify(NOTIFY_CHAR, lambda _, d: responses.append(bytes(d)))
        for label, cmd in [("set_clock", set_clock), ("set_count", set_count), ("set_alarm", set_alarm)]:
            await client.write_gatt_char(WRITE_CHAR, cmd, response=True)
            await asyncio.sleep(1.0)
            reply = responses[-1].hex() if responses else "(none)"
            print(f"{label}: sent {cmd.hex()}  <-- reply {reply}")
        await asyncio.sleep(1.0)
        await client.stop_notify(NOTIFY_CHAR)
    print(f"\nAlarm set for {hh:02d}:{mm:02d} (in ~{minutes_ahead} min), every day, action=press.")
    print("Watch the Bot at that time — the arm should move.")


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
    elif cmd == "settimer":
        mins = int(sys.argv[3]) if len(sys.argv) > 3 else 2
        asyncio.run(settimer(sys.argv[2], mins))
    elif cmd == "press":
        asyncio.run(press(sys.argv[2]))
    else:
        print(__doc__)
