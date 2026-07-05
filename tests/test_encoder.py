from switchbot_scheduler.model import Event
from switchbot_scheduler.encoder import encode_alarm
from switchbot_scheduler.ble_writer import build_alarm_frames


def test_encode_action_and_time():
    a = encode_alarm(Event("06:30", "on", ["sun"]))
    assert a["hour"] == 6 and a["minute"] == 30
    assert a["action"] == 1  # on=1


def test_encode_day_mask_sunday_is_bit0():
    a = encode_alarm(Event("06:00", "off", ["sun"]))
    assert a["repeat_byte"] == 0b0000001
    assert a["action"] == 2  # off=2


def test_encode_all_days_mask():
    a = encode_alarm(Event("06:00", "press", ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]))
    assert a["repeat_byte"] == 0b1111111
    assert a["action"] == 0  # press=0


def test_build_alarm_frames_one_per_alarm_with_index_and_count():
    alarms = [
        {"repeat_byte": 0b1111111, "hour": 6, "minute": 0, "action": 1},
        {"repeat_byte": 0b1111111, "hour": 17, "minute": 0, "action": 2},
    ]
    frames = build_alarm_frames(alarms)
    assert len(frames) == 2
    # each frame carries total count (2) and its own index (0, 1)
    assert frames[0][2] == 2 and frames[0][3] == 0
    assert frames[1][2] == 2 and frames[1][3] == 1
    # hour/minute/action land in the documented positions
    assert frames[0][5] == 6 and frames[0][6] == 0 and frames[0][7] == 1
