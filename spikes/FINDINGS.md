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

## STILL TO VERIFY
- The 0x09 "set timer/alarm" frame (build_alarm_frames) — control works, but the
  actual on-device alarm-set command has NOT yet been proven to fire.
