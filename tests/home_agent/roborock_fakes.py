class FakeRoborockClient:
    """Records domain calls and returns canned data. No network."""
    def __init__(self, *, status=None, consumables=None, timers=None, mapping=None):
        self.calls = []                      # list[(method_name, kwargs_or_args)]
        self._status = status or {}
        self._consumables = consumables or {}
        self._timers = list(timers or [])
        self._mapping = list(mapping or [])
        self._next_id = 1

    def room_mapping(self):
        self.calls.append(("room_mapping", {}))
        return self._mapping

    def clean(self, segment_ids, *, mode=None, suction=None, water_flow=None, repeat=1):
        self.calls.append(("clean", dict(segment_ids=segment_ids, mode=mode,
                                         suction=suction, water_flow=water_flow, repeat=repeat)))

    def _simple(self, name):
        self.calls.append((name, {}))

    def pause(self): self._simple("pause")
    def resume(self): self._simple("resume")
    def stop(self): self._simple("stop")
    def return_to_dock(self): self._simple("return_to_dock")
    def locate(self): self._simple("locate")
    def empty_bin(self): self._simple("empty_bin")
    def wash_mop(self): self._simple("wash_mop")
    def dry_mop(self): self._simple("dry_mop")

    def status(self):
        self.calls.append(("status", {}))
        return self._status

    def consumables(self):
        self.calls.append(("consumables", {}))
        return self._consumables

    def get_timers(self):
        self.calls.append(("get_timers", {}))
        return self._timers

    def set_timer(self, *, time, days, segment_ids, mode, suction, water_flow):
        self.calls.append(("set_timer", dict(time=time, days=days, segment_ids=segment_ids,
                                              mode=mode, suction=suction, water_flow=water_flow)))
        tid = str(self._next_id); self._next_id += 1
        self._timers.append({"id": tid, "time": time, "days": days, "enabled": True,
                             "target": "whole home" if not segment_ids else ",".join(map(str, segment_ids)),
                             "mode": mode})
        return tid

    def del_timer(self, timer_id):
        self.calls.append(("del_timer", {"timer_id": timer_id}))
        before = len(self._timers)
        self._timers = [t for t in self._timers if t["id"] != timer_id]
        return len(self._timers) < before


class ExplodingRoborockClient(FakeRoborockClient):
    """Every action raises — for exercising the friendly-error branches."""
    def clean(self, *a, **k): raise RuntimeError("offline")
    def status(self): raise RuntimeError("offline")
