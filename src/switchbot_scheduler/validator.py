from .model import Schedule, DAYS
from .registry import Registry

MAX_ALARMS = 5


class ScheduleError(ValueError):
    pass


def validate(schedule: Schedule, registry: Registry) -> None:
    known = registry.known_names()
    counts: dict[str, int] = {}
    for ds in schedule.schedules:
        if ds.device not in known:
            raise ScheduleError(
                f"Unknown device '{ds.device}'. Known devices: {known}"
            )
        counts[ds.device] = counts.get(ds.device, 0) + len(ds.events)
        for e in ds.events:
            _check_time(e.time, ds.device)
            if e.action not in ("on", "off", "press"):
                raise ScheduleError(f"Bad action '{e.action}' for {ds.device}")
            for d in e.days:
                if d not in DAYS:
                    raise ScheduleError(f"Bad day '{d}' for {ds.device}")
    for device, total in counts.items():
        if total > MAX_ALARMS:
            raise ScheduleError(
                f"{device} needs {total} alarms, but max is {MAX_ALARMS}. "
                f"Simplify the schedule."
            )


def _check_time(t: str, device: str) -> None:
    try:
        hh, mm = (int(x) for x in t.split(":"))
    except (ValueError, AttributeError):
        raise ScheduleError(f"Bad time '{t}' for {device}")
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ScheduleError(f"Bad time '{t}' for {device}")
