from .model import Schedule, DAYS

_ORDER = {d: i for i, d in enumerate(DAYS)}


def describe_days(days: list[str]) -> str:
    s = set(days)
    if s == set(DAYS):
        return "every day"
    if s == {"mon", "tue", "wed", "thu", "fri"}:
        return "weekdays"
    if s == {"sat", "sun"}:
        return "weekends"
    return ", ".join(sorted(s, key=lambda d: _ORDER[d]))


def readback(schedule: Schedule) -> str:
    lines = []
    for ds in schedule.schedules:
        for e in ds.events:
            when = f"once ({describe_days(e.days)})" if e.once else describe_days(e.days)
            lines.append(f"{ds.device}: {e.action} {e.time} — {when}")
    return "\n".join(lines)
