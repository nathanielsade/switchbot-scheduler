from switchbot_scheduler.model import Event
from switchbot_scheduler.encoder import encode_alarm


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
