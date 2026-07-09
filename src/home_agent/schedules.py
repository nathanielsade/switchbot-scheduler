from datetime import timedelta

from switchbot_scheduler.model import DAYS

_DAY_WORDS = {
    "daily": list(DAYS),
    "weekdays": ["mon", "tue", "wed", "thu", "fri"],
    "weekends": ["sat", "sun"],
}
# python's datetime.weekday(): Mon=0..Sun=6
_PY_WEEKDAY = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


def _normalize_days(days):
    """Expand convenience words, validate, and return a DAYS-ordered, deduped subset."""
    seen = set()
    for d in days:
        key = str(d).strip().lower()
        if key in _DAY_WORDS:
            seen.update(_DAY_WORDS[key])
        elif key in DAYS:
            seen.add(key)
        else:
            raise ValueError(f"unknown day '{d}'")
    return [d for d in DAYS if d in seen]


def _one_time_target(time_str, now):
    """(weekday_name, fire_at_iso) of the next occurrence of HH:MM from `now`
    (today if still ahead, else tomorrow)."""
    hh, mm = (int(x) for x in time_str.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return _PY_WEEKDAY[target.weekday()], target.isoformat()
