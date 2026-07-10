import logging
from datetime import datetime, timedelta, timezone

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


def _start_key(e):
    """A comparable start value: timed events normalized to UTC (so the same instant across calendars/
    offsets collapses and sorts chronologically); all-day events keep their date string."""
    s = e.get("start") or {}
    dt = s.get("dateTime")
    if dt:
        try:
            return datetime.fromisoformat(dt).astimezone(timezone.utc).isoformat()
        except ValueError:
            return dt
    return s.get("date") or ""


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


_PREPARE_SCHEMA = {"type": "function", "function": {
    "name": "prepare_calendar_change",
    "description": "STAGE a calendar change for the user to confirm (does not apply it yet). action is "
                   "create/update/delete. For create: give title + start (ISO). For update/delete: give a "
                   "ref from find_events (only Family-calendar events can be changed). After staging, tell "
                   "the user the exact change and wait; it is applied only when they confirm and you call "
                   "commit_calendar_change.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["create", "update", "delete"]},
        "title": {"type": "string"}, "start": {"type": "string", "description": "ISO datetime."},
        "end": {"type": "string", "description": "ISO datetime (optional)."},
        "all_day": {"type": "boolean"},
        "notes": {"type": "string"},
        "ref": {"type": "string", "description": "Event ref from find_events (for update/delete)."},
    }, "required": ["action"], "additionalProperties": False},
}}


def _find_impl(args, *, service, calendar_ids, write_id, now_fn):
    query = (args.get("query") or "").strip() or None
    now = now_fn()
    time_min = args.get("time_min") or ((now - timedelta(days=30)) if query else now).isoformat()
    time_max = args.get("time_max") or ((now + timedelta(days=180)) if query else now + timedelta(days=7)).isoformat()
    chosen = {}
    for cal_id in calendar_ids:
        params = {"calendarId": cal_id, "timeMin": time_min, "timeMax": time_max,
                  "singleEvents": True, "orderBy": "startTime"}
        if query:
            params["q"] = query
        resp = service.events().list(**params).execute()
        for e in resp.get("items", []):
            key = (e.get("iCalUID"), _start_key(e))
            if key not in chosen or cal_id == write_id:
                chosen[key] = (cal_id, e)
    items = sorted(chosen.values(), key=lambda ce: _start_key(ce[1]))
    if not items:
        return "no events found"
    return "\n".join(
        f"{_start_of(e)} – {_end_of(e)}: {e.get('summary', '(no title)')} [ref:{cal_id}|{e['id']}]"
        for cal_id, e in items)


def _prepare_impl(args, *, pending_store, chat_id, write_id, now_fn):
    action = (args.get("action") or "").strip().lower()
    if action == "create":
        title = (args.get("title") or "").strip()
        start = (args.get("start") or "").strip()
        if not title or not start:
            return "to create an event I need both a title and a start time"
        payload = {"action": "create", "title": title, "start": start, "end": args.get("end"),
                   "all_day": bool(args.get("all_day")), "notes": args.get("notes")}
        when = start + (" (all day)" if payload["all_day"] else "")
        summary = f"create '{title}' at {when}"
    elif action in ("update", "delete"):
        ref = (args.get("ref") or "").strip()
        if "|" not in ref:
            return "which event? use find_events first and pass its ref"
        if ref.rsplit("|", 1)[0] != write_id:
            return "I can only change events on the Family calendar; that one is on a personal calendar."
        if action == "delete":
            payload = {"action": "delete", "ref": ref}
            summary = f"delete event {ref}"
        else:
            payload = {"action": "update", "ref": ref, "title": args.get("title"),
                       "start": args.get("start"), "end": args.get("end"), "notes": args.get("notes")}
            summary = f"update event {ref}"
    else:
        return "unknown action; use create, update, or delete"
    pending_store.stage(chat_id, payload, now_fn().isoformat())
    return f"Ready to {summary}. Reply כן to confirm."


def build_calendar_tools(service, pending_store, chat_id, committable_id, *,
                         calendar_ids, write_id, now_fn=None):
    now_fn = now_fn or _now
    return [
        Tool(name="find_events", schema=_FIND_SCHEMA,
             impl=lambda a: _find_impl(a, service=service, calendar_ids=calendar_ids,
                                       write_id=write_id, now_fn=now_fn)),
        Tool(name="prepare_calendar_change", schema=_PREPARE_SCHEMA,
             impl=lambda a: _prepare_impl(a, pending_store=pending_store, chat_id=chat_id,
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
