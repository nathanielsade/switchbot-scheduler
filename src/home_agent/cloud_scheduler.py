import asyncio, logging
from datetime import datetime, time as dtime
from switchbot_scheduler.actuator import resolve_action
from . import switchbot_cloud

log = logging.getLogger("home_agent")
_PREFIX = "switchbot-cloud:"
_DAY_NUM = {"sun": 6, "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5}  # PTB: Mon=0..Sun=6


def _job_name(row_id): return f"{_PREFIX}{row_id}"


class CloudScheduler:
    def __init__(self, job_queue, store, registry, *, send_command_fn, tz, now_fn=None):
        self.jq = job_queue; self.store = store; self.registry = registry
        self.send = send_command_fn; self.tz = tz
        self.now_fn = now_fn or (lambda: datetime.now(tz))

    def reconcile(self):
        self.store.remove_expired(self.now_fn().isoformat())
        for row in self.store.list():
            if self.registry.is_cloud(row["device"]):
                self.schedule_row(row)

    def schedule_row(self, row):
        name = _job_name(row["id"])
        if row["once"]:
            when = datetime.fromisoformat(row["fire_at"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=self.tz)
            if when <= self.now_fn():
                return
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
