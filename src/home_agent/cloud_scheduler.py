import asyncio, logging
from datetime import datetime, time as dtime, timezone as _dt_timezone
from switchbot_scheduler.actuator import resolve_action
from . import switchbot_cloud

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
        self.store.remove_expired(self.now_fn().astimezone(_dt_timezone.utc).isoformat())
        for row in self.store.list():
            if self.registry.is_cloud(row["device"]):
                try:
                    self.schedule_row(row)
                except ValueError:
                    continue          # already-past rows are expected on restart

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
