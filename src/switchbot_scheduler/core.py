from dataclasses import dataclass
from .parser import parse_schedule, parse_conversation
from .validator import validate
from .readback import readback
from .model import Schedule, ImmediateAction
from .registry import Registry


@dataclass
class PreviewResult:
    clarification: str | None
    schedule: Schedule | None
    readback: str | None
    immediate: list[ImmediateAction]


def build_schedule(prompt: str, registry: Registry, completion_fn=None) -> Schedule:
    kwargs = {"completion_fn": completion_fn} if completion_fn is not None else {}
    schedule = parse_schedule(prompt, registry, **kwargs)
    validate(schedule, registry)
    _apply_press_mode(schedule, registry)
    return schedule


def preview_conversation(messages, registry: Registry, now, completion_fn=None) -> PreviewResult:
    kwargs = {"completion_fn": completion_fn} if completion_fn is not None else {}
    result = parse_conversation(messages, registry, now, **kwargs)
    if result.clarification is not None:
        return PreviewResult(clarification=result.clarification, schedule=None,
                             readback=None, immediate=[])
    schedule = result.schedule
    if schedule is not None and schedule.schedules:
        validate(schedule, registry)
        _apply_press_mode(schedule, registry)
        return PreviewResult(clarification=None, schedule=schedule,
                             readback=readback(schedule), immediate=result.immediate)
    return PreviewResult(clarification=None, schedule=None, readback=None,
                         immediate=result.immediate)


def _apply_press_mode(schedule: Schedule, registry: Registry) -> None:
    """A press-mode Bot only toggles, so any on/off intent becomes a single press.
    Done here (not just at encode time) so the read-back honestly shows 'press'."""
    for ds in schedule.schedules:
        if registry.is_press_mode(ds.device):
            for e in ds.events:
                e.action = "press"


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
