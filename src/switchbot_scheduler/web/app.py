import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..registry import Registry
from ..core import build_schedule, preview_conversation
from ..readback import readback
from ..ble_writer import write_schedule
from ..validator import validate
from ..model import Schedule, DeviceSchedule, Event

STATIC = Path(__file__).parent / "static"

# Test seam: None => parser uses the real OpenAI call; tests set a canned fn.
_completion_fn = None

app = FastAPI()


def _registry() -> Registry:
    return Registry.load(os.environ.get("SWITCHBOT_DEVICES", "devices.yaml"))


def schedule_to_json(s: Schedule) -> dict:
    return {"schedules": [
        {"device": ds.device,
         "events": [{"time": e.time, "action": e.action, "days": e.days, "once": e.once} for e in ds.events]}
        for ds in s.schedules
    ]}


def schedule_from_json(data: dict) -> Schedule:
    return Schedule(schedules=[
        DeviceSchedule(device=s["device"], events=[
            Event(time=e["time"], action=e["action"], days=e["days"], once=bool(e.get("once", False)))
            for e in s["events"]
        ]) for s in data["schedules"]
    ])


class PreviewReq(BaseModel):
    messages: list[str]


class ApplyReq(BaseModel):
    schedule: dict


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/preview")
def preview(req: PreviewReq):
    try:
        registry = _registry()
        kind, payload, schedule = preview_conversation(
            req.messages, registry, datetime.now(), completion_fn=_completion_fn)
        if kind == "clarification":
            return {"ok": True, "kind": "clarification", "message": payload}
        return {"ok": True, "kind": "schedule", "readback": payload, "schedule": schedule_to_json(schedule)}
    except Exception as err:
        return {"ok": False, "error": str(err)}


@app.post("/apply")
def apply(req: ApplyReq):
    try:
        registry = _registry()
        schedule = schedule_from_json(req.schedule)
        validate(schedule, registry)
        write_schedule(schedule, registry)
        seen, written = set(), []
        for ds in schedule.schedules:
            if ds.device not in seen:
                seen.add(ds.device)
                written.append(ds.device)
        return {"ok": True, "written": written}
    except Exception as err:
        return {"ok": False, "error": str(err)}


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
