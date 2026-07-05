from .parser import parse_schedule
from .validator import validate
from .readback import readback
from .model import Schedule
from .registry import Registry


def build_schedule(prompt: str, registry: Registry, completion_fn=None) -> Schedule:
    kwargs = {"completion_fn": completion_fn} if completion_fn is not None else {}
    schedule = parse_schedule(prompt, registry, **kwargs)
    validate(schedule, registry)
    return schedule


def apply_schedule(prompt, registry, *, dry_run=True, confirm=lambda text: True,
                   writer=None, completion_fn=None):
    schedule = build_schedule(prompt, registry, completion_fn)
    text = readback(schedule)
    if dry_run:
        return ("dry_run", text, schedule)
    if writer is None:
        raise ValueError("writer is required when dry_run=False")
    if not confirm(text):
        return ("cancelled", text, schedule)
    writer(schedule, registry)
    return ("written", text, schedule)
