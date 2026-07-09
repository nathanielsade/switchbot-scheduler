import json
from fastapi.testclient import TestClient
from switchbot_scheduler.web import app as webapp

CANNED = json.dumps({"schedules": [{"device": "living_room",
    "events": [{"time": "06:00", "action": "on", "days": ["mon"]}]}]})


def _client(tmp_path, monkeypatch, completion=None):
    reg = tmp_path / "devices.yaml"
    reg.write_text('devices:\n  living_room:\n    aliases: ["salon"]\n    ble_id: "U1"\n')
    monkeypatch.setenv("SWITCHBOT_DEVICES", str(reg))
    monkeypatch.setattr(webapp, "_completion_fn", completion or (lambda s, u: CANNED))
    return TestClient(webapp.app)


def test_preview_thread_returns_schedule_kind(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)  # canned parser returns living_room on 06:00
    body = client.post("/preview", json={"messages": ["salon on 6am"]}).json()
    assert body["ok"] is True and body["kind"] == "schedule"
    assert "living_room: on 06:00" in body["readback"]
    assert body["immediate"] == []


def test_preview_thread_returns_clarification_kind(tmp_path, monkeypatch):
    import json
    clar = lambda s, u: json.dumps({"clarification": "Which room?"})
    client = _client(tmp_path, monkeypatch, clar)
    body = client.post("/preview", json={"messages": ["do the thing"]}).json()
    assert body["ok"] is True and body["kind"] == "clarification" and body["message"] == "Which room?"


def test_preview_unknown_device_returns_error(tmp_path, monkeypatch):
    bad = lambda s, u: json.dumps({"schedules": [{"device": "bedroom",
        "events": [{"time": "06:00", "action": "on", "days": ["mon"]}]}]})
    body = _client(tmp_path, monkeypatch, bad).post("/preview", json={"messages": ["bedroom on"]}).json()
    assert body["ok"] is False and "Unknown device" in body["error"]


def test_preview_immediate_only_kind_none(tmp_path, monkeypatch):
    imm = lambda s, u: json.dumps({"immediate": [{"device": "living_room", "action": "on"}]})
    body = _client(tmp_path, monkeypatch, imm).post("/preview", json={"messages": ["salon on now"]}).json()
    assert body["ok"] is True and body["kind"] == "none"
    assert body["immediate"] == [{"device": "living_room", "action": "on"}]


def test_preview_mixed_has_schedule_and_immediate(tmp_path, monkeypatch):
    mixed = lambda s, u: json.dumps({
        "immediate": [{"device": "living_room", "action": "on"}],
        "schedules": [{"device": "living_room",
            "events": [{"time": "22:00", "action": "off", "days": ["mon"]}]}]})
    body = _client(tmp_path, monkeypatch, mixed).post("/preview", json={"messages": ["salon now + 22:00"]}).json()
    assert body["kind"] == "schedule"
    assert body["immediate"] == [{"device": "living_room", "action": "on"}]
    assert "living_room: off 22:00" in body["readback"]


def test_execute_runs_immediate_actions(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = []
    from switchbot_scheduler.web import app as wa
    monkeypatch.setattr(wa, "run_immediate", lambda actions, reg: [
        wa.ActionResult(a.device, a.action, True, None) for a in actions])
    body = client.post("/execute", json={"actions": [{"device": "living_room", "action": "on"}]}).json()
    assert body["ok"] is True
    assert body["results"] == [{"device": "living_room", "action": "on", "ok": True, "error": None}]


def test_execute_reports_per_device_failure(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from switchbot_scheduler.web import app as wa
    monkeypatch.setattr(wa, "run_immediate", lambda actions, reg: [
        wa.ActionResult("living_room", "on", False, "out of range")])
    body = client.post("/execute", json={"actions": [{"device": "living_room", "action": "on"}]}).json()
    assert body["ok"] is True
    assert body["results"][0]["ok"] is False and "out of range" in body["results"][0]["error"]


def test_apply_writes_posted_schedule(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(webapp, "write_schedule", lambda s, r: calls.append(s))
    sched = {"schedules": [{"device": "living_room",
        "events": [{"time": "06:00", "action": "on", "days": ["mon"]}]}]}
    body = client.post("/apply", json={"schedule": sched}).json()
    assert body["ok"] is True and body["written"] == ["living_room"]
    assert len(calls) == 1 and calls[0].schedules[0].events[0].time == "06:00"


def test_apply_ble_failure_returns_error(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    def boom(s, r):
        raise RuntimeError("couldn't reach living_room")
    monkeypatch.setattr(webapp, "write_schedule", boom)
    sched = {"schedules": [{"device": "living_room",
        "events": [{"time": "06:00", "action": "on", "days": ["mon"]}]}]}
    body = client.post("/apply", json={"schedule": sched}).json()
    assert body["ok"] is False and "reach" in body["error"]


def test_apply_reads_once(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(webapp, "write_schedule", lambda s, r: calls.append(s))
    sched = {"schedules": [{"device": "living_room",
        "events": [{"time": "09:00", "action": "on", "days": ["mon"], "once": True}]}]}
    body = client.post("/apply", json={"schedule": sched}).json()
    assert body["ok"] is True and calls[0].schedules[0].events[0].once is True


def test_once_round_trips_preview_to_apply(tmp_path, monkeypatch):
    once_parser = lambda s, u: json.dumps({"schedules": [{"device": "living_room",
        "events": [{"time": "09:00", "action": "on", "days": ["mon"], "once": True}]}]})
    client = _client(tmp_path, monkeypatch, once_parser)
    prev = client.post("/preview", json={"messages": ["living room on tomorrow, once"]}).json()
    assert prev["kind"] == "schedule"
    assert prev["immediate"] == []
    # once must survive serialization back to the client, else Approve loses it
    assert prev["schedule"]["schedules"][0]["events"][0]["once"] is True
    calls = []
    monkeypatch.setattr(webapp, "write_schedule", lambda sc, r: calls.append(sc))
    ap = client.post("/apply", json={"schedule": prev["schedule"]}).json()
    assert ap["ok"] is True and calls[0].schedules[0].events[0].once is True
