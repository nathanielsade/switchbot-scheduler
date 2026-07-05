from switchbot_scheduler.model import Event
from switchbot_scheduler.encoder import encode_alarm
from switchbot_scheduler.ble_writer import (
    build_alarm_frames,
    build_clock_frame,
    build_count_frame,
)


def test_encode_action_and_time():
    a = encode_alarm(Event("06:30", "on", ["sun"]))
    assert a["hour"] == 6 and a["minute"] == 30
    assert a["action"] == 1  # on=1


def test_encode_day_mask_monday_is_bit0():
    a = encode_alarm(Event("06:00", "off", ["mon"]))
    assert a["repeat_byte"] == 0b0000001
    assert a["action"] == 2  # off=2


def test_encode_day_mask_sunday_is_bit6():
    a = encode_alarm(Event("06:00", "off", ["sun"]))
    assert a["repeat_byte"] == 0b1000000  # Sun = bit6


def test_encode_all_days_mask():
    a = encode_alarm(Event("06:00", "press", ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]))
    assert a["repeat_byte"] == 0b1111111
    assert a["action"] == 0  # press=0


def test_encode_inverted_on_becomes_off():
    a = encode_alarm(Event("06:00", "on", ["sun"]), inverted=True)
    assert a["action"] == 2  # on -> off when inverted


def test_encode_inverted_off_becomes_on():
    a = encode_alarm(Event("06:00", "off", ["sun"]), inverted=True)
    assert a["action"] == 1  # off -> on when inverted


def test_encode_inverted_press_unaffected():
    a = encode_alarm(Event("06:00", "press", ["sun"]), inverted=True)
    assert a["action"] == 0  # press stays press


def test_encode_not_inverted_on_stays_on():
    a = encode_alarm(Event("06:00", "on", ["sun"]), inverted=False)
    assert a["action"] == 1


def test_build_alarm_frames_verified_layout():
    alarms = [
        {"repeat_byte": 0b1111111, "hour": 6, "minute": 0, "action": 1},
        {"repeat_byte": 0b1111111, "hour": 17, "minute": 0, "action": 2},
    ]
    frames = build_alarm_frames(alarms)
    assert len(frames) == 2
    assert len(frames[0]) == 14
    # 57 09 [idx*16+3] [total] 00 [repeat] HH MM [mode] [job] 00 00 00 00
    assert frames[0][0] == 0x57 and frames[0][1] == 0x09
    assert frames[0][2] == 0x03 and frames[1][2] == 0x13  # subcmd = idx*16+3
    assert frames[0][3] == 2                               # total count
    assert frames[0][4] == 0x00                            # filler/rev
    assert frames[0][5] == 0b1111111                       # repeat byte
    assert frames[0][6] == 6 and frames[0][7] == 0         # HH MM
    assert frames[0][8] == 0x00                            # mode = at HH:MM
    assert frames[0][9] == 1                               # job = on
    assert frames[1][9] == 2                               # second alarm job = off


def test_build_count_frame():
    assert build_count_frame(3) == bytes([0x57, 0x09, 0x02, 3])


def test_build_clock_frame_local_time_big_endian():
    frame = build_clock_frame(1000, 3600)  # epoch 1000 + 1h offset = 4600
    assert frame[:3] == bytes([0x57, 0x09, 0x01])
    assert frame[3:] == (4600).to_bytes(8, "big")
