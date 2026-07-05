#!/usr/bin/env bash
# Start the SwitchBot Scheduler web app.
# Run it, then open the URL below. Ctrl+C to stop.
cd "$(dirname "$0")" || exit 1

ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"
echo "SwitchBot Scheduler — starting…"
echo "  On this Mac:  http://localhost:8000"
[ -n "$ip" ] && echo "  From a phone: http://${ip}:8000   (same Wi-Fi; macOS firewall must allow incoming)"
echo "  Press Ctrl+C to stop."
echo

exec .venv/bin/switchbot-ui
