import logging
from datetime import datetime, timedelta

from .tools import Tool

log = logging.getLogger("home_agent")
_TZ = "Asia/Jerusalem"


def _now():
    return datetime.now().astimezone()


def _start_of(e):
    s = e.get("start") or {}
    return s.get("dateTime") or s.get("date") or ""


def _end_of(e):
    en = e.get("end") or {}
    return en.get("dateTime") or en.get("date") or ""


_FIND_SCHEMA = {"type": "function", "function": {
    "name": "find_events",
    "description": "Look up calendar events across the family's calendars. Use for 'what do we have this "
                   "week?', 'are we free Saturday?', 'when's the dentist?'. Pass ISO datetimes time_min/"
                   "time_max to bound the range, and/or a text query. Returns matching events, each ending "
                   "with a [ref:…] handle you pass to prepare_calendar_change for update/delete.",
    "parameters": {"type": "object", "properties": {
        "time_min": {"type": "string", "description": "ISO datetime lower bound (optional)."},
        "time_max": {"type": "string", "description": "ISO datetime upper bound (optional)."},
        "query": {"type": "string", "description": "Free-text search (optional)."},
    }, "additionalProperties": False},
}}


def _find_impl(args, *, service, calendar_ids, write_id, now_fn):
    query = (args.get("query") or "").strip() or None
    now = now_fn()
    time_min = args.get("time_min") or ((now - timedelta(days=30)) if query else now).isoformat()
    time_max = args.get("time_max") or ((now + timedelta(days=180)) if query else now + timedelta(days=7)).isoformat()
    chosen = {}
    for cal_id in calendar_ids:
        resp = service.events().list(calendarId=cal_id, timeMin=time_min, timeMax=time_max,
                                     singleEvents=True, orderBy="startTime", q=query).execute()
        for e in resp.get("items", []):
            key = (e.get("iCalUID"), _start_of(e))
            if key not in chosen or cal_id == write_id:
                chosen[key] = (cal_id, e)
    items = sorted(chosen.values(), key=lambda ce: _start_of(ce[1]))
    if not items:
        return "no events found"
    return "\n".join(
        f"{_start_of(e)} – {_end_of(e)}: {e.get('summary', '(no title)')} [ref:{cal_id}|{e['id']}]"
        for cal_id, e in items)


def build_calendar_tools(service, pending_store, chat_id, committable_id, *,
                         calendar_ids, write_id, now_fn=None):
    now_fn = now_fn or _now
    return [
        Tool(name="find_events", schema=_FIND_SCHEMA,
             impl=lambda a: _find_impl(a, service=service, calendar_ids=calendar_ids,
                                       write_id=write_id, now_fn=now_fn)),
    ]


def load_calendar_service(config):
    """Build the real Google Calendar client from the service-account key, or None if unconfigured."""
    import os
    if not config.google_sa_keyfile or not os.path.exists(config.google_sa_keyfile):
        log.warning("GOOGLE_SA_KEYFILE not set/found — calendar tools disabled")
        return None
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        config.google_sa_keyfile, scopes=["https://www.googleapis.com/auth/calendar.events"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
