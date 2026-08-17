import asyncio, logging
from datetime import datetime, time as dtime
from switchbot_scheduler.actuator import resolve_action
from . import switchbot_cloud
from .schedules import _utc_iso  # canonical UTC-ISO helper (no cycle: schedules.py doesn't import this module)

log = logging.getLogger("home_agent")
_PREFIX = "switchbot-cloud:"
_DAY_NUM = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}  # PTB v20+: Sun=0..Sat=6


def _job_name(row_id): return f"{_PREFIX}{row_id}"


class CloudScheduler:
    def __init__(self, job_queue, store, registry, *, send_command_fn, tz, now_fn=None):
        self.jq = job_queue; self.store = store; self.registry = registry
        self.send = send_command_fn; self.tz = tz
        self.now_fn = now_fn or (lambda: datetime.now(tz))

    def reconcile(self):
        # Sweep expired rows without reprogramming anything: unlike a BLE Bot (see
        # schedules._expire_and_reprogram), nothing here writes a cloud device at startup —
        # the next schedule/cancel call rebuilds jobs from the store, so a stale store row is
        # never left armed anywhere.
        self.store.remove_expired(_utc_iso(self.now_fn()))
        for row in self.store.list():
            if self.registry.is_cloud(row["device"]):
                try:
                    self.schedule_row(row)
                except ValueError as e:
                    # schedule_row raises ValueError for THREE distinct reasons: (1) the
                    # deliberate "already passed" raise below — expected on every restart, stay
                    # quiet; (2) datetime.fromisoformat on a corrupt fire_at; (3) int(x) on a
                    # corrupt time string. (2)/(3) mean a bad row is silently skipped forever
                    # while get_schedule keeps showing it as active — log loudly, naming the row,
                    # so it's actually found. Never abort the reconcile sweep either way.
                    if str(e) == "that moment has already passed":
                        log.debug("reconcile: skipping already-past row id=%s", row.get("id"))
                    else:
                        log.warning("reconcile: skipping corrupt schedule row id=%s (%s): %s",
                                    row.get("id"), row, e)
                    continue
                except Exception:
                    # Anything else (KeyError from a row missing `time`/`days`/`fire_at`,
                    # whatever job_queue.run_once/run_daily raises for a bad value, ...) must
                    # not abort the sweep either: this loop runs UNGUARDED at startup, and every
                    # cloud device on the live box is reconciled by this SAME call, so one
                    # corrupt row must never disable scheduling for the rest of them.
                    log.warning("reconcile: skipping corrupt schedule row id=%s (%s)",
                                row.get("id"), row, exc_info=True)
                    continue

    def schedule_row(self, row):
        name = _job_name(row["id"])
        if row["once"]:
            when = datetime.fromisoformat(row["fire_at"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=self.tz)
            if when <= self.now_fn():
                # Must raise, not return: _schedule_impl rolls the store row back on
                # failure, and a silent return reports ✅ for a timer that never fires.
                raise ValueError("that moment has already passed")
            self.jq.run_once(self._make_cb(row), when=when, name=name)
        else:
            hh, mm = (int(x) for x in row["time"].split(":"))
            days = tuple(_DAY_NUM[d] for d in row["days"])
            self.jq.run_daily(self._make_cb(row), time=dtime(hh, mm, tzinfo=self.tz),
                              days=days, name=name)

    def unschedule(self, row_id):
        for job in self.jq.get_jobs_by_name(_job_name(row_id)):
            job.schedule_removal()

    def _make_cb(self, row):
        device, action, once, rid = row["device"], row["action"], row["once"], row["id"]
        cloud_id = self.registry.cloud_id(device)
        async def _cb(context=None):
            cmd = switchbot_cloud.to_command(resolve_action(device, action, self.registry))
            try:
                await asyncio.to_thread(self.send, cloud_id, cmd)
                log.info("scheduled cloud %s %s ok", device, cmd)
            except Exception as e:
                log.warning("scheduled cloud %s failed: %s", device, type(e).__name__)
            finally:
                if once:
                    self.store.remove_id(rid)
        return _cb
