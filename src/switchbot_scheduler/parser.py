import json
from dataclasses import dataclass
from datetime import datetime
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


@dataclass
class ParseResult:
    schedule: Schedule | None
    clarification: str | None


def build_conversation_system_prompt(registry: Registry, now: datetime) -> str:
    device_lines = []
    for d in registry.devices:
        if d.aliases:
            device_lines.append(f"{d.name} (aliases: {', '.join(d.aliases)})")
        else:
            device_lines.append(d.name)
    names = "\n".join(device_lines)
    today = now.strftime("%A, %Y-%m-%d")
    return f"""You convert a conversation about device schedules (Hebrew or English) into strict JSON.
Today is {today}.

Output EXACTLY ONE of:
  {{"schedules": [{{"device": <name>, "events": [
     {{"time": "HH:MM", "action": "on"|"off"|"press", "days": [<weekdays>], "once": <bool>}} ]}} ]}}
  {{"clarification": "<a short question or explanation>"}}

Known device names (map spoken names/aliases to exactly one of these):
{names}

Rules:
- weekdays are lowercase 3-letter codes: sun mon tue wed thu fri sat. Use 24-hour zero-padded time.
- Every turn-ON and every turn-OFF is its OWN event. Each device supports at most {MAX_ALARMS} events.
- Recurring: "every day"/no day -> all 7 days, once=false. Named weekdays repeating -> those days, once=false.
- One-time: "today"/"tomorrow"/"this <weekday>" -> set days to that single weekday and once=true.
  Resolve relative days using today's date above.
- NEVER output "every day" when the user implied a specific or one-time day.
- If the request is ambiguous, unparseable, or asks for a specific calendar date more than 7 days away
  (which the device cannot do), return a "clarification" explaining/asking — do NOT guess a schedule.
- The conversation is a list of user turns, newest reflecting corrections. Output the CURRENT complete
  intended schedule reflecting the WHOLE conversation.
"""


def parse_conversation(messages, registry, now, completion_fn=_default_completion) -> ParseResult:
    system = build_conversation_system_prompt(registry, now)
    convo = "\n".join(f"[{i+1}] {m}" for i, m in enumerate(messages))
    raw = completion_fn(system, convo)
    data = json.loads(raw)
    if "clarification" in data and "schedules" not in data:
        return ParseResult(schedule=None, clarification=str(data["clarification"]))
    schedules = []
    for s in data["schedules"]:
        raw_device = s["device"]
        canonical = registry.resolve(raw_device)
        device = canonical if canonical is not None else raw_device
        events = [Event(time=e["time"], action=e["action"], days=e["days"], once=bool(e.get("once", False)))
                  for e in s["events"]]
        schedules.append(DeviceSchedule(device=device, events=events))
    return ParseResult(schedule=Schedule(schedules=schedules), clarification=None)
