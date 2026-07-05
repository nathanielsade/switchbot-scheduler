# Chat UI — Design

**Date:** 2026-07-05
**Status:** Approved, going to implementation plan

## Goal
A clean, minimal web chat where the user types a natural-language schedule, sees
exactly what the tool understood (the read-back), and either **Approves** (which
writes the alarms to the Bots over Bluetooth) or **refines** by typing again.
Purpose: let the user drive the whole pipeline end-to-end by hand.

## Scope (v1)
Core loop only: prompt → read-back → approve/refine → real BLE write. No viewing/
clearing existing on-device alarms, no cloud device-status (deferred).

## Architecture
A local web app served on the user's Mac (where BLE + OpenAI access live). Thin
front door over the existing core — no core logic changes.

```
Browser (chat page)  ──HTTP──►  FastAPI (app.py)  ──►  existing core
 static/index.html               GET /   POST /preview   POST /apply
 (clean, auto light/dark)                                (parse→validate→press-mode
                                                          →readback→ble_writer)
```

New code: `src/switchbot_scheduler/web/app.py` + `src/switchbot_scheduler/web/static/index.html`.
New deps: `fastapi`, `uvicorn`. New console script `switchbot-ui` launches uvicorn.
Server binds host `0.0.0.0` (default port 8000) so the same-WiFi phone can reach it later.
Reads `devices.yaml` (path via env `SWITCHBOT_DEVICES`, default `devices.yaml`) and
`OPENAI_API_KEY` from the environment.

## Endpoints
- `GET /` → serves `static/index.html`.
- `POST /preview` body `{"prompt": str}` → runs `build_schedule` (parse → validate →
  press-mode normalize) + `readback`. Returns `{"ok": true, "readback": str,
  "schedule": <Schedule as JSON>}` or `{"ok": false, "error": str}`. **No Bot touched.**
- `POST /apply` body `{"schedule": <Schedule JSON>}` → rebuild `Schedule`, re-`validate`
  against the registry, then `write_schedule`. Returns `{"ok": true, "written": [device,…]}`
  or `{"ok": false, "error": str}`. Writes the EXACT approved schedule (no re-parse), so
  what was shown is what is written.

## Data flow
`/preview` returns the structured `Schedule`; the browser holds it; **Approve** posts
that same `Schedule` to `/apply`. Guarantees preview == write. Press-mode normalization
and inversion are already handled by the core (`build_schedule` / `encode_alarm`), so the
read-back is faithful and the write matches it.

## Chat UX
- Layout: message list + input box; user prompts as right-aligned bubbles, tool replies
  as left-aligned. Clean/minimal, whitespace, subtle accent, chat bubbles; auto light/dark
  via `prefers-color-scheme`.
- A tool reply renders the read-back as a small structured card (one line per event, e.g.
  `🛋️ living_room: on 06:00 — every day`) with an **Approve** button; refining = type again.
- Approve → `/apply` → success reply (`✅ Written to N Bot(s)`), buttons disabled after.

## Error handling (all rendered as friendly chat replies, never a stack trace)
| Where | Shown |
|-------|-------|
| Missing `OPENAI_API_KEY` | "Set OPENAI_API_KEY and restart the server." |
| Parser junk / unparseable | "Couldn't understand that — try rephrasing." |
| Unknown device / >5 alarms / bad time | the validator's exact message |
| Bot unreachable over BLE (in /apply) | "Couldn't reach <device> — are you home / is it on?" + Approve again to retry |

## Testing
- `/preview` and `/apply` tested with FastAPI `TestClient`, injecting a canned parser
  via the existing `completion_fn` seam and a fake writer — network-free, hardware-free.
  Cover: happy path, validation error surfaced as `{ok:false}`, `/apply` writes the posted
  schedule, `/apply` BLE failure surfaced as `{ok:false}`.
- The UI itself is verified **manually, end-to-end** by the user (`switchbot-ui` → real
  prompt → Approve → Bot fires).

## Out of scope
Viewing/clearing current on-device alarms; cloud device status; auth (local/home use);
remote-from-anywhere access (needs a tunnel; separate concern).
