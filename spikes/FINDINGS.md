# BLE Spike Findings (2026-07-05, real Bots on macOS)

## Device map (CoreBluetooth UUIDs — macOS-specific, not the F2B2… cloud IDs)
| Room | UUID | model | inverted |
|------|------|-------|----------|
| סלון (living room) | 40EF82E1-E89F-58C5-930C-58D04473828E | 0x48 (Bot) | yes (upside down) |
| פינת אוכל (dining) | 82433425-0CB9-60A7-199F-219B87D259FC | 0x48 (Bot) | no |
| מזגן (AC)          | 3FD44C5A-BFCE-66AD-9A89-68EFDDC28699 | 0x48 (Bot) | no |
| Hub Mini           | D2CB4E2A-FD6D-0A3E-7C62-73515CEC0C39 | 0x6d       | (not BLE-controlled) |

## GATT (confirmed on device)
- service:  cba20d00-224d-11e6-**9fb8**-0002a5d5c51b
- write:    cba20002-224d-11e6-**9fb8**-0002a5d5c51b   (note: 9fb8, NOT the 9fb9 in old docs)
- notify:   cba20003-224d-11e6-**9fb8**-0002a5d5c51b

## Control protocol (VERIFIED working)
- press command bytes `57 01 00` work.
- MUST use acknowledged writes: `write_gatt_char(..., response=True)`. Plain
  write-without-response is silently dropped (arm does not move).
- Notify replies observed: living/dining `05 48 xx`; AC `01 ff 00`. Command still
  executes regardless (light/AC physically toggled). Don't treat 0x05 as failure.
- ble_writer.py WRITE_CHAR updated to 9fb8; it already uses response=True.

## TIMER PROTOCOL — VERIFIED (2026-07-05)
Sequence per connection (unencrypted base 0x57 0x09), all acknowledged writes + notify:
1. set clock: `57 09 01` + 8-byte big-endian (local unix = utc + tm_gmtoff)
2. set count: `57 09 02 [n]`
3. per alarm: `57 09 [idx*16+3] [n] 00 [repeat] HH MM [mode=00] [job] 00 00 00 00`
   - repeat: bit7=0 repeat weekly; bits0..6 = Mon..Sun (from switchbotpy; verified-by-fire
     for the overall protocol, weekday bit-order inferred, tested only with all-days 0x7F).
   - job: 0=press, 1=on, 2=off. mode 0 = fire at HH:MM (repeating by weekday).
- Ported into src/switchbot_scheduler/ble_writer.py (build_clock_frame/build_count_frame/
  build_alarm_frames + write_alarms) and encoder DAY_BIT fixed to Mon=0..Sun=6.

## FULL END-TO-END VERIFIED (2026-07-05, ~22:55)
Real product path: CLI Hebrew prompt "כבה את פינת אוכל בשעה 22:55 כל יום" -> GPT parse ->
dining off 22:55 every day -> write_schedule -> dining Bot turned OFF on its own at 22:55.

## RESIDUAL (low risk)
- Weekday bit-order inferred (all-days tested); confirm over first week or with a today-only test.
- Living-room inversion is unit-tested but not yet hardware-verified (dining is non-inverted).
- The e2e test left a real recurring "dining off 22:55 daily" alarm — clear/overwrite it.

## SwitchBot Bot battery % (home-mcp battery_status) — confirmed 2026-07-09
Command `0x57 0x02` ("get device basic info") → notify reply. Battery percent is at **reply byte index 1**.
Verified live on the kitchen Bot (ble_id 0F4665AE-...): reply `[1, 87, 66, 100, 0, 0, 0, 186, 1, 16, 0, 0, 0]`
→ byte[1]=87, and battery_status reported kitchen=87% in Telegram. Offset assumption in home.py `_run_battery` is correct; no change needed. (Closes Task 6 of the home-mcp plan.)
