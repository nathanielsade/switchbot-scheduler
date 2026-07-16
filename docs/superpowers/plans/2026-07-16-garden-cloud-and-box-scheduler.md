# Garden Cloud Control + Box-Side Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Menashe control and schedule the out-of-BLE-range garden SwitchBot via the SwitchBot Cloud API (through the user's hub), with scheduling fired by the 24/7 box.

**Architecture:** Hybrid routing keyed on `devices.yaml`: a device with `cloud_id` → SwitchBot Cloud API; a device with `ble_id` → direct BLE (unchanged). A box-side scheduler (python-telegram-bot JobQueue) fires cloud schedules; BLE bots keep their on-device timers.

**Tech Stack:** Python 3.11, `python-telegram-bot[job-queue]` (APScheduler), stdlib `urllib`/`hmac`/`hashlib`, `zoneinfo`. Offline tests (inject HTTP + JobQueue seams).

## Global Constraints

- `requires-python = ">=3.11"`.
- `pytest` is the only CI gate; **no network/BLE/OpenAI in the automated suite** — every side effect behind an injectable seam filled with a fake.
- Money/`Decimal` rules N/A here.
- `FAMILY_SYSTEM_PROMPT` stays digit-free + byte-stable — **do not touch it** (tool *schema* descriptions may change).
- Stores: connection-per-op (`with closing(sqlite3.connect(...))`), append-only history.
- Never log `SWITCHBOT_TOKEN`, `SWITCHBOT_SECRET`, or the request `sign`.
- SwitchBot cloud IDs (verified): garden `EECE111B5B1C`, hub `FAEE46B6877F`.
- Run tests: `.venv/bin/pytest -q --ignore=integration_tests`.

---

### Task 1: SwitchBot Cloud client

**Files:**
- Create: `src/home_agent/switchbot_cloud.py`
- Test: `tests/home_agent/test_switchbot_cloud.py`

**Interfaces:**
- Produces:
  - `class SwitchBotCloudError(Exception)`
  - `send_command(device_id: str, command: str, *, token: str, secret: str, http_fn=None, sleep_fn=None) -> None` — `command ∈ {"turnOn","turnOff","press"}`; raises `SwitchBotCloudError` on non-100 or exhausted retries.
  - `get_status(device_id: str, *, token: str, secret: str, http_fn=None) -> dict` — returns the API `body` dict (has `"battery"`).
  - `to_command(action: str) -> str` — `{"on":"turnOn","off":"turnOff","press":"press"}[action]`.
  - `_sign(token, secret, t, nonce) -> str` (helper; base64 HMAC-SHA256 of `token+t+nonce`).
  - `http_fn(method, url, headers, body) -> tuple[int, dict]` seam — default performs a real `urllib` request returning `(http_status, parsed_json)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/home_agent/test_switchbot_cloud.py
import pytest
from home_agent import switchbot_cloud as sc

def _fake_http(calls, responses):
    def http_fn(method, url, headers, body):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return responses[len(calls) - 1]
    return http_fn

def test_send_command_success_posts_signed_request():
    calls = []
    http = _fake_http(calls, [(200, {"statusCode": 100, "message": "success", "body": {}})])
    sc.send_command("EECE111B5B1C", "turnOn", token="TOK", secret="SEC", http_fn=http)
    c = calls[0]
    assert c["method"] == "POST"
    assert c["url"].endswith("/v1.1/devices/EECE111B5B1C/commands")
    assert c["body"] == {"command": "turnOn", "parameter": "default", "commandType": "command"}
    # signed headers present; secret never appears in headers values
    assert set(["Authorization", "sign", "t", "nonce", "Content-Type"]) <= set(c["headers"])
    assert "SEC" not in " ".join(map(str, c["headers"].values()))

def test_send_command_raises_on_non_100_statuscode():
    http = _fake_http([], [(200, {"statusCode": 161, "message": "device offline"})])
    with pytest.raises(sc.SwitchBotCloudError):
        sc.send_command("X", "turnOn", token="T", secret="S", http_fn=http)

def test_send_command_retries_transient_then_succeeds():
    calls = []
    def http_fn(method, url, headers, body):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("boom")
        return (200, {"statusCode": 100, "message": "success"})
    sc.send_command("X", "turnOff", token="T", secret="S", http_fn=http_fn, sleep_fn=lambda s: None)
    assert len(calls) == 2

def test_to_command_maps_actions():
    assert sc.to_command("on") == "turnOn"
    assert sc.to_command("off") == "turnOff"
    assert sc.to_command("press") == "press"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_switchbot_cloud.py -q`
Expected: FAIL (module `switchbot_cloud` not found).

- [ ] **Step 3: Implement the client**

```python
# src/home_agent/switchbot_cloud.py
import base64, hashlib, hmac, json, logging, time, uuid
import urllib.request

log = logging.getLogger("home_agent")
_BASE = "https://api.switch-bot.com/v1.1"
_TIMEOUT = 10
_RETRIES = 2
_COMMANDS = {"on": "turnOn", "off": "turnOff", "press": "press"}


class SwitchBotCloudError(Exception):
    pass


def to_command(action: str) -> str:
    try:
        return _COMMANDS[action]
    except KeyError:
        raise SwitchBotCloudError(f"unknown action {action!r}")


def _sign(token: str, secret: str, t: str, nonce: str) -> str:
    mac = hmac.new(secret.encode(), (token + t + nonce).encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def _headers(token: str, secret: str) -> dict:
    t = str(int(time.time() * 1000)); nonce = str(uuid.uuid4())
    return {"Authorization": token, "sign": _sign(token, secret, t, nonce),
            "t": t, "nonce": nonce, "Content-Type": "application/json"}


def _real_http(method, url, headers, body):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode())


def _call(method, url, *, token, secret, body=None, http_fn=None, sleep_fn=None):
    http_fn = http_fn or _real_http
    sleep_fn = sleep_fn or time.sleep
    last = None
    for attempt in range(_RETRIES + 1):
        try:
            status, payload = http_fn(method, url, _headers(token, secret), body)
        except Exception as e:  # transient (timeout/conn) — retry
            last = SwitchBotCloudError(f"request failed: {type(e).__name__}")
            sleep_fn(1 + attempt); continue
        if status // 100 == 5:  # server error — retry
            last = SwitchBotCloudError(f"HTTP {status}"); sleep_fn(1 + attempt); continue
        code = payload.get("statusCode")
        if status // 100 != 2 or code != 100:
            raise SwitchBotCloudError(payload.get("message") or f"HTTP {status} statusCode {code}")
        return payload
    raise last


def send_command(device_id, command, *, token, secret, http_fn=None, sleep_fn=None):
    _call("POST", f"{_BASE}/devices/{device_id}/commands", token=token, secret=secret,
          body={"command": command, "parameter": "default", "commandType": "command"},
          http_fn=http_fn, sleep_fn=sleep_fn)
    log.info("cloud command %s -> %s ok", command, device_id)


def get_status(device_id, *, token, secret, http_fn=None):
    return _call("GET", f"{_BASE}/devices/{device_id}/status",
                 token=token, secret=secret, http_fn=http_fn).get("body", {})
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/home_agent/test_switchbot_cloud.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/switchbot_cloud.py tests/home_agent/test_switchbot_cloud.py
git commit -m "feat(cloud): SwitchBot Cloud client (signed, validated, retrying)"
```

---

### Task 2: Registry cloud routing

**Files:**
- Modify: `src/switchbot_scheduler/registry.py`
- Test: `tests/home_agent/test_registry_cloud.py`

**Interfaces:**
- Consumes: `Registry.load`, `Device`.
- Produces: `Device.cloud_id: str = ""`; `Registry.is_cloud(name) -> bool`; `Registry.cloud_id(name) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/home_agent/test_registry_cloud.py
from switchbot_scheduler.registry import Registry
import yaml

def _reg(tmp_path):
    p = tmp_path / "devices.yaml"
    p.write_text(yaml.safe_dump({"devices": {
        "kitchen": {"aliases": ["מטבח"], "ble_id": "D8:66:D3:5D:B2:96"},
        "garden": {"aliases": ["גינה"], "cloud_id": "EECE111B5B1C"}}}), encoding="utf-8")
    return Registry.load(str(p))

def test_is_cloud_and_cloud_id(tmp_path):
    r = _reg(tmp_path)
    assert r.is_cloud("garden") is True
    assert r.is_cloud("kitchen") is False
    assert r.cloud_id("garden") == "EECE111B5B1C"
    assert r.ble_id("kitchen") == "D8:66:D3:5D:B2:96"
```

- [ ] **Step 2: Run test, verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_registry_cloud.py -q`
Expected: FAIL (`is_cloud` missing / `cloud_id` not loaded).

- [ ] **Step 3: Implement**

In `src/switchbot_scheduler/registry.py`:
- Add field to `Device`: `cloud_id: str = ""`.
- In `Registry.load`, add to the `Device(...)` kwargs: `cloud_id=cfg.get("cloud_id", "")`.
- Add methods:

```python
    def is_cloud(self, name: str) -> bool:
        return bool(self._by_name[name].cloud_id)

    def cloud_id(self, name: str) -> str:
        return self._by_name[name].cloud_id
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/home_agent/test_registry_cloud.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/switchbot_scheduler/registry.py tests/home_agent/test_registry_cloud.py
git commit -m "feat(registry): cloud_id routing helpers (is_cloud/cloud_id)"
```

---

### Task 3: Config — SwitchBot cloud creds + home timezone

**Files:**
- Modify: `src/home_agent/config.py`
- Test: `tests/home_agent/test_config_cloud.py`

**Interfaces:**
- Produces on `Config`: `switchbot_token: str = ""`, `switchbot_secret: str = ""`, `home_tz: str = "Asia/Jerusalem"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/home_agent/test_config_cloud.py
from home_agent.config import load_config

def test_cloud_and_tz_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "k"); monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("SWITCHBOT_TOKEN", "TOK"); monkeypatch.setenv("SWITCHBOT_SECRET", "SEC")
    cfg = load_config(str(tmp_path / "nope.env"))
    assert cfg.switchbot_token == "TOK" and cfg.switchbot_secret == "SEC"
    assert cfg.home_tz == "Asia/Jerusalem"
```

- [ ] **Step 2: Run test, verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_config_cloud.py -q`
Expected: FAIL (`Config` has no `switchbot_token`).

- [ ] **Step 3: Implement**

In `src/home_agent/config.py`:
- Add fields to `Config` dataclass: `switchbot_token: str = ""`, `switchbot_secret: str = ""`, `home_tz: str = "Asia/Jerusalem"`.
- Add to the `Config(...)` construction in `load_config`:

```python
        switchbot_token=os.environ.get("SWITCHBOT_TOKEN", ""),
        switchbot_secret=os.environ.get("SWITCHBOT_SECRET", ""),
        home_tz=os.environ.get("HOME_TZ", "Asia/Jerusalem"),
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/home_agent/test_config_cloud.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/config.py tests/home_agent/test_config_cloud.py
git commit -m "feat(config): SWITCHBOT_TOKEN/SECRET + HOME_TZ"
```

---

### Task 4: control_device + list_devices + battery_status cloud routing

**Files:**
- Modify: `src/home_agent/home.py`
- Test: `tests/home_agent/test_home_cloud.py`

**Interfaces:**
- Consumes: `switchbot_cloud.to_command`; `switchbot_scheduler.actuator.resolve_action`; `Registry.is_cloud/cloud_id`.
- Produces: `build_home_tools(registry, *, actuate_fn=None, battery_fn=None, cloud_send_fn=None, cloud_battery_fn=None)`.
  - `cloud_send_fn(cloud_id: str, command: str) -> None`
  - `cloud_battery_fn(cloud_id: str) -> int`

- [ ] **Step 1: Write the failing tests**

```python
# tests/home_agent/test_home_cloud.py
import yaml
from switchbot_scheduler.registry import Registry
from home_agent.home import build_home_tools

def _reg(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump({"devices": {
        "kitchen": {"aliases": ["מטבח"], "ble_id": "AA:BB"},
        "garden": {"aliases": ["גינה"], "cloud_id": "EECE111B5B1C"}}}), encoding="utf-8")
    return Registry.load(str(p))

def test_control_garden_routes_to_cloud(tmp_path):
    sent = []
    tools = {t.name: t for t in build_home_tools(
        _reg(tmp_path),
        actuate_fn=lambda *a: (_ for _ in ()).throw(AssertionError("BLE must not be used")),
        cloud_send_fn=lambda cid, cmd: sent.append((cid, cmd)))}
    out = tools["control_device"].impl({"device": "גינה", "action": "on"})
    assert sent == [("EECE111B5B1C", "turnOn")]
    assert "✅" in out

def test_control_kitchen_still_uses_ble(tmp_path):
    from switchbot_scheduler.actuator import ActionResult
    calls = []
    def fake_actuate(ble_id, code): calls.append((ble_id, code))
    tools = {t.name: t for t in build_home_tools(_reg(tmp_path), actuate_fn=fake_actuate)}
    tools["control_device"].impl({"device": "מטבח", "action": "on"})
    assert calls  # BLE path used

def test_battery_garden_uses_cloud(tmp_path):
    tools = {t.name: t for t in build_home_tools(
        _reg(tmp_path), cloud_battery_fn=lambda cid: 97)}
    out = tools["battery_status"].impl({"device": "גינה"})
    assert "97%" in out

def test_list_devices_labels_cloud(tmp_path):
    tools = {t.name: t for t in build_home_tools(_reg(tmp_path))}
    out = tools["list_devices"].impl({})
    assert "garden" in out and "cloud" in out.lower()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_home_cloud.py -q`
Expected: FAIL (cloud params unknown / garden routed to BLE).

- [ ] **Step 3: Implement**

In `src/home_agent/home.py`:
- Add imports: `from switchbot_scheduler.actuator import resolve_action` and `from . import switchbot_cloud`.
- In `_device_type`, prepend: `if device.cloud_id: return "cloud-controlled"` (so `list_devices` labels it).
- Rewrite `_control_impl` signature and body:

```python
def _control_impl(args, *, registry, actuate_fn, cloud_send_fn) -> str:
    spoken = (args.get("device") or "").strip()
    action = (args.get("action") or "").strip().lower()
    name = registry.resolve(spoken)
    if name is None:
        return f"unknown device '{spoken}'. I can control: {', '.join(registry.known_names())}"
    if action not in ("on", "off", "press"):
        return f"unknown action '{action}'. Use on, off, or press."
    if registry.is_cloud(name):
        eff = resolve_action(name, action, registry)
        try:
            cloud_send_fn(registry.cloud_id(name), switchbot_cloud.to_command(eff))
        except Exception as e:
            return f"{name}: failed — {e}"
        reported = "press" if registry.is_press_mode(name) else action
        return f"{name}: {reported} ✅"
    result = run_immediate([ImmediateAction(name, action)], registry, actuate_fn=actuate_fn)[0]
    if result.ok:
        reported = "press" if registry.is_press_mode(name) else action
        return f"{result.device}: {reported} ✅"
    return f"{result.device}: failed — {result.error}"
```

- In `_battery_impl`, add a cloud branch (before the `ble_id` lookup) and pass `cloud_battery_fn`:

```python
def _battery_impl(args, *, registry, battery_fn, cloud_battery_fn) -> str:
    ...  # resolve targets as today
    for name in targets:
        if registry.is_cloud(name):
            try:
                lines.append(f"{name}: {cloud_battery_fn(registry.cloud_id(name))}%")
            except Exception as e:
                lines.append(f"{name}: unavailable — {e}")
            continue
        ble_id = registry.ble_id(name)
        ...  # unchanged BLE path
```

- Update `build_home_tools`:

```python
def build_home_tools(registry, *, actuate_fn=None, battery_fn=None,
                     cloud_send_fn=None, cloud_battery_fn=None) -> list[Tool]:
    battery_fn = battery_fn or _run_battery
    return [
        Tool(name="control_device", schema=_CONTROL_SCHEMA,
             impl=lambda args: _control_impl(args, registry=registry,
                 actuate_fn=actuate_fn, cloud_send_fn=cloud_send_fn)),
        Tool(name="list_devices", schema=_LIST_SCHEMA,
             impl=lambda args: _list_impl(args, registry=registry)),
        Tool(name="battery_status", schema=_BATTERY_SCHEMA,
             impl=lambda args: _battery_impl(args, registry=registry,
                 battery_fn=battery_fn, cloud_battery_fn=cloud_battery_fn)),
    ]
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/home_agent/test_home_cloud.py tests/home_agent/test_home.py -q`
Expected: PASS (new + existing home tests).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/home.py tests/home_agent/test_home_cloud.py
git commit -m "feat(home): route cloud devices for control/battery, label in list"
```

---

### Task 5: Expose schedule row id in ScheduleStore.list()

**Files:**
- Modify: `src/home_agent/schedule_store.py`
- Test: `tests/home_agent/test_schedule_store_id.py`

**Interfaces:**
- Produces: each dict from `ScheduleStore.list()` includes `"id": int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/home_agent/test_schedule_store_id.py
from home_agent.schedule_store import ScheduleStore

def test_list_includes_row_id(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.db"))
    rid = s.add("garden", "on", "18:00", ["mon"], False, None)
    rows = s.list("garden")
    assert rows[0]["id"] == rid
```

- [ ] **Step 2: Run test, verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_store_id.py -q`
Expected: FAIL (KeyError `'id'`).

- [ ] **Step 3: Implement**

In `ScheduleStore.list`, add `id` to both SELECTs and the returned dict:

```python
            rows = conn.execute(
                "SELECT id, device, action, time, days, once, fire_at FROM schedules "
                "ORDER BY device, time").fetchall()   # and the device-filtered variant likewise
        return [{"id": i, "device": d, "action": a, "time": t,
                 "days": [x for x in dd.split(",") if x], "once": bool(o), "fire_at": f}
                for i, d, a, t, dd, o, f in rows]
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/home_agent/test_schedule_store_id.py tests/home_agent/test_schedules.py -q`
Expected: PASS (existing schedule tests unaffected — they index by key).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedule_store.py tests/home_agent/test_schedule_store_id.py
git commit -m "feat(schedule-store): expose row id in list()"
```

---

### Task 6: Cloud scheduler wrapper (box-side JobQueue)

**Files:**
- Create: `src/home_agent/cloud_scheduler.py`
- Test: `tests/home_agent/test_cloud_scheduler.py`

**Interfaces:**
- Consumes: `ScheduleStore`, `Registry`, `switchbot_cloud.to_command`, `switchbot_scheduler.actuator.resolve_action`.
- Produces:
  - `class CloudScheduler(job_queue, store, registry, *, send_command_fn, tz: ZoneInfo, now_fn=None)`
  - `.reconcile()` — `store.remove_expired(now)`, then register every cloud-device schedule.
  - `.schedule_row(row: dict)` — register one job named `switchbot-cloud:{row['id']}`.
  - `.unschedule(row_id: int)` — remove job(s) with that name.
  - Fire callback maps `resolve_action(...) → to_command → await asyncio.to_thread(send_command_fn, cloud_id, cmd)`; one-time rows `store.remove_id` after attempt.
- JobQueue seam: tests pass a fake `job_queue` recording `run_daily`/`run_once`/`get_jobs_by_name`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/home_agent/test_cloud_scheduler.py
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml
from switchbot_scheduler.registry import Registry
from home_agent.schedule_store import ScheduleStore
from home_agent.cloud_scheduler import CloudScheduler

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=TZ)

class FakeJob:
    def __init__(self, name): self.name = name; self.removed = False
    def schedule_removal(self): self.removed = True

class FakeJobQueue:
    def __init__(self): self.jobs = []
    def run_daily(self, cb, time, days, name): self.jobs.append(FakeJob(name)); return self.jobs[-1]
    def run_once(self, cb, when, name): self.jobs.append(FakeJob(name)); return self.jobs[-1]
    def get_jobs_by_name(self, name): return [j for j in self.jobs if j.name == name and not j.removed]

def _reg(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump({"devices": {
        "kitchen": {"ble_id": "AA:BB"}, "garden": {"cloud_id": "EECE111B5B1C"}}}), encoding="utf-8")
    return Registry.load(str(p))

def _sched(tmp_path, jq):
    store = ScheduleStore(str(tmp_path / "s.db"))
    cs = CloudScheduler(jq, store, _reg(tmp_path),
                        send_command_fn=lambda cid, cmd: None, tz=TZ, now_fn=lambda: NOW)
    return store, cs

def test_reconcile_registers_only_cloud_recurring(tmp_path):
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    rid = store.add("garden", "on", "18:00", ["mon", "tue"], False, None)
    store.add("kitchen", "on", "07:00", ["mon"], False, None)  # BLE — must be ignored
    cs.reconcile()
    assert [j.name for j in jq.jobs] == [f"switchbot-cloud:{rid}"]

def test_reconcile_drops_expired_one_time(tmp_path):
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    store.add("garden", "on", "09:00", ["thu"], True, "2026-07-16T09:00:00+03:00")  # past
    cs.reconcile()
    assert jq.jobs == []                    # not fired late
    assert store.list("garden") == []       # dropped

def test_reconcile_registers_future_one_time(tmp_path):
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    rid = store.add("garden", "on", "18:00", ["thu"], True, "2026-07-16T18:00:00+03:00")  # future
    cs.reconcile()
    assert [j.name for j in jq.jobs] == [f"switchbot-cloud:{rid}"]

def test_unschedule_removes_named_job(tmp_path):
    jq = FakeJobQueue(); store, cs = _sched(tmp_path, jq)
    rid = store.add("garden", "on", "18:00", ["mon"], False, None); cs.reconcile()
    cs.unschedule(rid)
    assert jq.get_jobs_by_name(f"switchbot-cloud:{rid}") == []
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_cloud_scheduler.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# src/home_agent/cloud_scheduler.py
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
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/home_agent/test_cloud_scheduler.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/cloud_scheduler.py tests/home_agent/test_cloud_scheduler.py
git commit -m "feat(scheduler): box-side CloudScheduler over JobQueue (tz/misfire-safe)"
```

---

### Task 7: Branch schedule_device/cancel for cloud devices

**Files:**
- Modify: `src/home_agent/schedules.py`
- Test: `tests/home_agent/test_schedules_cloud.py`

**Interfaces:**
- Consumes: `CloudScheduler.schedule_row`, `.unschedule`.
- Produces: `build_schedule_tools(registry, store, *, write_fn=None, now_fn=None, scheduler=None)` — when a device `is_cloud`, use `scheduler` instead of `write_fn`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/home_agent/test_schedules_cloud.py
import yaml, pytest
from switchbot_scheduler.registry import Registry
from home_agent.schedule_store import ScheduleStore
from home_agent.schedules import build_schedule_tools
from datetime import datetime, timezone

def _reg(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump({"devices": {
        "kitchen": {"ble_id": "AA:BB"}, "garden": {"cloud_id": "EECE111B5B1C"}}}), encoding="utf-8")
    return Registry.load(str(p))

class FakeScheduler:
    def __init__(self, fail=False): self.scheduled = []; self.removed = []; self.fail = fail
    def schedule_row(self, row):
        if self.fail: raise RuntimeError("jobqueue down")
        self.scheduled.append(row["id"])
    def unschedule(self, row_id): self.removed.append(row_id)

def _now(): return datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)

def test_cloud_schedule_registers_job_not_ble(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler()
    tools = {t.name: t for t in build_schedule_tools(
        _reg(tmp_path), store,
        write_fn=lambda *a: (_ for _ in ()).throw(AssertionError("no BLE for cloud")),
        now_fn=_now, scheduler=sch)}
    out = tools["schedule_device"].impl({"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"]})
    assert sch.scheduled and "✅" in out
    assert store.list("garden")

def test_cloud_schedule_rolls_back_on_scheduler_failure(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler(fail=True)
    tools = {t.name: t for t in build_schedule_tools(_reg(tmp_path), store, now_fn=_now, scheduler=sch)}
    out = tools["schedule_device"].impl({"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"]})
    assert store.list("garden") == []      # rolled back
    assert "✅" not in out

def test_cloud_cancel_unschedules(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.db")); sch = FakeScheduler()
    tools = {t.name: t for t in build_schedule_tools(_reg(tmp_path), store, now_fn=_now, scheduler=sch)}
    tools["schedule_device"].impl({"device": "גינה", "action": "on", "time": "18:00", "days": ["mon"]})
    tools["cancel_schedule"].impl({"device": "גינה"})
    assert sch.removed and store.list("garden") == []
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/pytest tests/home_agent/test_schedules_cloud.py -q`
Expected: FAIL (`scheduler` kwarg unknown; cloud not branched).

- [ ] **Step 3: Implement**

In `src/home_agent/schedules.py`:
- Thread a `scheduler=None` param through `build_schedule_tools` into `_schedule_impl` / `_cancel_impl` (same lambda-injection pattern as `write_fn`).
- In `_schedule_impl`, after `row_id = store.add(...)`, branch:

```python
    if registry.is_cloud(name):
        try:
            scheduler.schedule_row({"id": row_id, "device": name, "action": action,
                                    "time": time_str, "days": days, "once": once, "fire_at": fire_at})
        except Exception as e:
            store.remove_id(row_id)
            return f"couldn't schedule {name} ({e}) — timer not set"
    else:
        try:
            _program_device(name, store, registry, write_fn)
        except ScheduleError as e:
            store.remove_id(row_id); return f"can't schedule that: {e}"
        except Exception as e:
            store.remove_id(row_id); return f"couldn't reach {name} — timer not set ({e})"
    when = "one-time" if once else describe_days(days)
    return f"{name}: {action} at {time_str} ({when}) ✅"
```

- In `_cancel_impl`, after computing `removed_rows` and before `store.remove(...)`, capture ids; branch:

```python
    if registry.is_cloud(name):
        removed = store.remove(name, time_str)
        if removed == 0:
            return f"nothing scheduled matched for {name}."
        for r in removed_rows:
            scheduler.unschedule(r["id"])
    else:
        ...  # existing BLE remove + _program_device rollback path unchanged
```

(Note: `removed_rows` needs each row's `id` — available now from Task 5.)

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/home_agent/test_schedules_cloud.py tests/home_agent/test_schedules.py -q`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/home_agent/test_schedules_cloud.py
git commit -m "feat(schedules): branch cloud devices to CloudScheduler with rollback"
```

---

### Task 8: Update schedule_device tool description (+ its test)

**Files:**
- Modify: `src/home_agent/schedules.py` (`_SCHEDULE_SCHEMA` description)
- Modify: any test asserting the old wording (grep first)

- [ ] **Step 1: Find affected tests**

Run: `grep -rn "even if this computer is off\|device's own timer" src/ tests/`
Note each hit.

- [ ] **Step 2: Update the description**

Replace the "programmed into the device's own timer so it fires even if this computer is off" clause with:
> "BLE devices are programmed into the device's own timer (fires even if this computer is off); cloud devices (e.g. the garden) are fired by this home-agent, so they require it to be running. `time` is 24-hour \"HH:MM\"…"
(keep the rest of the description unchanged.)

- [ ] **Step 3: Update any asserting test** to match the new wording (from Step 1).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/home_agent/ -q -k schedule`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/home_agent/schedules.py tests/
git commit -m "docs(schedules): tool description reflects BLE vs cloud scheduling"
```

---

### Task 9: Wire into build_application (reorder + gating + reconcile)

**Files:**
- Modify: `src/home_agent/telegram_app.py`
- Modify: `pyproject.toml` (job-queue extra)
- Test: `tests/home_agent/test_build_application_cloud.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml` change `"python-telegram-bot>=21.0"` → `"python-telegram-bot[job-queue]>=21.0"`.
Then: `.venv/bin/pip install -e . -q` and verify `.venv/bin/python -c "import apscheduler; print('ok')"` → `ok`.

- [ ] **Step 2: Write the failing test**

```python
# tests/home_agent/test_build_application_cloud.py
from home_agent.config import Config
from home_agent.telegram_app import build_application

def _cfg(tmp_path):
    dev = tmp_path / "d.yaml"; dev.write_text('devices:\n  garden:\n    cloud_id: "EECE111B5B1C"\n')
    return Config(openai_api_key="k", telegram_bot_token="123:abc", allowed_chat_ids={1},
                  db_path=str(tmp_path / "a.db"), devices_path=str(dev),
                  switchbot_token="TOK", switchbot_secret="SEC")

def test_build_application_has_jobqueue_and_no_crash(tmp_path):
    app = build_application(_cfg(tmp_path), client=object())
    assert app.job_queue is not None   # job-queue extra installed + wired
```

- [ ] **Step 3: Run test, verify it fails**

Run: `.venv/bin/pytest tests/home_agent/test_build_application_cloud.py -q`
Expected: FAIL (job_queue None, or cloud tools/scheduler not wired).

- [ ] **Step 4: Implement wiring**

In `build_application`, reorder and add (keep existing tool composition, adjust for the new order):

```python
    app = Application.builder().token(config.telegram_bot_token).build()   # create FIRST
    registry = load_registry(config)
    # cloud seams (None if creds absent -> cloud disabled with a warning)
    cloud_send_fn = cloud_battery_fn = scheduler = None
    if config.switchbot_token and config.switchbot_secret:
        from . import switchbot_cloud
        from .cloud_scheduler import CloudScheduler
        from zoneinfo import ZoneInfo
        tok, sec = config.switchbot_token, config.switchbot_secret
        cloud_send_fn = lambda cid, cmd: switchbot_cloud.send_command(cid, cmd, token=tok, secret=sec)
        cloud_battery_fn = lambda cid: switchbot_cloud.get_status(cid, token=tok, secret=sec).get("battery")
        if registry is not None and app.job_queue is not None:
            scheduler = CloudScheduler(app.job_queue, ScheduleStore(config.db_path), registry,
                                       send_command_fn=cloud_send_fn, tz=ZoneInfo(config.home_tz))
    elif registry is not None and any(registry.is_cloud(n) for n in registry.known_names()):
        log.warning("SWITCHBOT_TOKEN/SECRET unset — cloud devices (e.g. garden) disabled")

    tools = list(DEFAULT_TOOLS)
    tools += build_shopping_tools(ShoppingStore(config.db_path))
    if registry is not None:
        tools += build_home_tools(registry, cloud_send_fn=cloud_send_fn, cloud_battery_fn=cloud_battery_fn)
        tools += build_schedule_tools(registry, ScheduleStore(config.db_path), scheduler=scheduler)
    # ... roborock / finance / calendar / memory unchanged ...
    if scheduler is not None:
        scheduler.reconcile()   # arm existing cloud schedules on startup
```

(Move the `on_message` handler and the rest below, unchanged. Ensure `build_home_tools`/`build_schedule_tools` calls now pass the new kwargs.)

- [ ] **Step 5: Run tests, verify pass**

Run: `.venv/bin/pytest tests/home_agent/test_build_application_cloud.py -q`
Expected: PASS.

- [ ] **Step 6: Full suite**

Run: `.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/home_agent/telegram_app.py tests/home_agent/test_build_application_cloud.py
git commit -m "feat(app): wire cloud control + box-side scheduler; job-queue extra"
```

---

### Task 10: Deploy to the box + live verify

**Files:** none (uses the `deploy-box` skill).

- [ ] **Step 1:** On the box, set the garden to cloud in `devices.yaml` (excluded from rsync, so edit directly):
  ```
  garden:
    aliases: ["garden", "גינה", "yard"]
    cloud_id: "EECE111B5B1C"
  ```
  (remove the empty `ble_id` line).
- [ ] **Step 2:** Run the `deploy-box` skill (rsync → `pip install -e .` for the job-queue extra → restart → verify active/logs/single-instance).
- [ ] **Step 3:** Live-verify immediate: drive `control_device` on garden (via `scripts/agent_smoke.py "תדליק את הגינה"`), user confirms the bot actuates.
- [ ] **Step 4:** Live-verify schedule: schedule garden ~2 min out, confirm it fires from the box (log line `scheduled cloud garden turnOn ok`), user confirms actuation.
- [ ] **Step 5:** Commit nothing new; note completion in `docs/ROADMAP.md` if desired.

---

## Self-Review

- **Spec coverage:** cloud client (T1), Registry routing (T2), config/creds/tz (T3), immediate control + battery + list label (T4), row id (T5), box-side scheduler w/ tz+misfire+one-time policy (T6), schedule branch + atomicity rollback (T7), tool description (T8), wiring/reorder/gating/reconcile + job-queue dep (T9), deploy+verify (T10). All spec sections mapped.
- **Placeholders:** none — every code step has concrete code.
- **Type consistency:** `cloud_send_fn(cloud_id, command)`, `cloud_battery_fn(cloud_id)->int`, `CloudScheduler.schedule_row(row)/.unschedule(id)/.reconcile()`, `to_command(action)->str`, `send_command(device_id, command, *, token, secret, ...)`, `get_status(...)->dict`, `ScheduleStore.list()[i]["id"]` — consistent across T1/T4/T6/T7/T9.
