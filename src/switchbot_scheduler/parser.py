import json
from .model import Schedule, DeviceSchedule, Event
from .registry import Registry
from .validator import MAX_ALARMS

MODEL = "gpt-4o-mini"  # small, cheap, supports JSON output; change here to swap models


def build_system_prompt(registry: Registry) -> str:
    device_lines = []
    for d in registry.devices:
        if d.aliases:
            device_lines.append(f"{d.name} (aliases: {', '.join(d.aliases)})")
        else:
            device_lines.append(d.name)
    names = "\n".join(device_lines)
    return f"""You convert natural-language lighting/device schedules (Hebrew or English)
into strict JSON. Output ONLY JSON, no prose.

Schema:
{{"schedules": [{{"device": <name>, "events": [
  {{"time": "HH:MM", "action": "on"|"off"|"press", "days": [<weekdays>]}} ]}} ]}}

Known device names (map spoken names/aliases to exactly one of these):
{names}

Rules:
- weekdays are lowercase 3-letter codes: sun mon tue wed thu fri sat
- Every turn-ON and every turn-OFF is its OWN separate event.
- "every day" or no day mentioned -> all 7 days. Expand ranges (e.g. Sun-Thu).
- Use 24-hour time, zero-padded ("06:00").
- Each device supports at most {MAX_ALARMS} events. If the request needs more,
  still output every event faithfully — do NOT drop any to fit.
"""


def _default_completion(system: str, user: str) -> str:
    from openai import OpenAI
    client = OpenAI()  # reads OPENAI_API_KEY from the environment
    resp = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content


def parse_schedule(prompt: str, registry: Registry, completion_fn=_default_completion) -> Schedule:
    system = build_system_prompt(registry)
    raw = completion_fn(system, prompt)
    data = json.loads(raw)
    schedules = []
    for s in data["schedules"]:
        raw_device = s["device"]
        canonical = registry.resolve(raw_device)
        device = canonical if canonical is not None else raw_device
        events = [Event(time=e["time"], action=e["action"], days=e["days"]) for e in s["events"]]
        schedules.append(DeviceSchedule(device=device, events=events))
    return Schedule(schedules=schedules)
