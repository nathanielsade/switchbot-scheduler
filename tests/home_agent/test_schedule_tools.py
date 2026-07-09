from datetime import datetime, timezone
from home_agent.schedules import _normalize_days, _one_time_target


def _thu_1824():
    # Thursday 2026-07-09 18:24 (fixed clock for deterministic tests)
    return datetime(2026, 7, 9, 18, 24, tzinfo=timezone.utc)


def test_normalize_days_words_and_explicit():
    assert _normalize_days(["daily"]) == ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    assert _normalize_days(["weekdays"]) == ["mon", "tue", "wed", "thu", "fri"]
    assert _normalize_days(["weekends"]) == ["sun", "sat"]      # DAYS order
    assert _normalize_days(["tue", "sun"]) == ["sun", "tue"]      # DAYS order
    assert _normalize_days(["mon", "mon"]) == ["mon"]            # dedupe


def test_normalize_days_bad_day_raises():
    import pytest
    with pytest.raises(ValueError):
        _normalize_days(["funday"])


def test_one_time_target_today_when_time_ahead():
    day, fire_at = _one_time_target("18:29", _thu_1824())
    assert day == "thu"
    assert fire_at.startswith("2026-07-09T18:29")


def test_one_time_target_rolls_to_next_day_when_past():
    day, fire_at = _one_time_target("18:00", _thu_1824())   # already past 18:24
    assert day == "fri"
    assert fire_at.startswith("2026-07-10T18:00")
