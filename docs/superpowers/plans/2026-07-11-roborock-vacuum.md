# Roborock Q Revo Vacuum Control — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Natural-language control of the Roborock Q Revo (clean/room-clean, plan, dock, status, consumables, native schedules) from the Hebrew Telegram family agent.

**Architecture:** Same in-process `Tool` pattern as `home.py`. A deterministic `RoomRegistry` (segment-id ↔ Hebrew room names, hand-authored YAML) resolves rooms; a **domain-level `RoborockClient` wrapper** is the single injectable seam (real one lazily imports `python-roborock` and talks cloud/MQTT; tests inject a fake). Tools translate NL intent → segment ids + enums and call the client. All tools are chat-agnostic (built once at startup, wired in `telegram_app.build_application`).

**Tech Stack:** Python 3.11+ (venv is 3.14), `python-roborock` (lazy import), `PyYAML`, `pytest`. No network in the test suite.

**Spec:** `docs/superpowers/specs/2026-07-11-roborock-vacuum-design.md`.

## Global Constraints

- **Test command (the whole CI gate):** `PYTHONPATH=src .venv/bin/pytest -q --ignore=integration_tests`. No `ruff`/`mypy`.
- **The venv lives in the MAIN repo**, not this worktree. Run tests with the absolute path: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest …`.
- **No network / BLE / roborock-cloud in the automated suite.** Every side effect is behind an injectable seam filled with a fake in tests (here: a `FakeRoborockClient`).
- **`python-roborock` is imported LAZILY inside the real client bootstrap / methods** — importing `roborock.py` must never touch the network; tests must not need the lib installed.
- **Tools are `home_agent.tools.Tool(name, schema, impl)`.** `impl(args: dict) -> str`. The schema `description` IS the model's instruction; each must say "report back in the user's language".
- **Deterministic in Python; language/judgment in the model.** Room→segment mapping is fuzzy-matched by the MODEL against `list_rooms`; the registry does only exact alias lookup.
- **`FAMILY_SYSTEM_PROMPT` must stay digit-free and byte-stable** (tests enforce). Any prompt additions must contain no digits.
- **Graceful-if-unconfigured:** unset `ROBOROCK_USERNAME`/`ROBOROCK_PASSWORD` → vacuum tools don't load, bot still runs (warning). Rooms YAML absent → whole-home clean still works, room ops refuse politely.
- **Cloud transport (MQTT) for v1.** Local LAN transport is out of scope.
- **`clean` applies ONE plan per run** (per-room differing plans are out of scope).

---

### Task 1: Config keys + dependency + graceful client loader

**Files:**
- Modify: `src/home_agent/config.py`
- Create: `src/home_agent/roborock.py` (loader stub only this task)
- Modify: `pyproject.toml` (add `python-roborock` dep)
- Modify: `.env.example`
- Test: `tests/home_agent/test_home_agent_config.py` (add cases), `tests/home_agent/test_roborock_loader.py` (create)

**Interfaces:**
- Consumes: `Config` dataclass, `load_config()`.
- Produces:
  - `Config` gains `roborock_username: str = ""`, `roborock_password: str = ""`, `roborock_rooms_path: str = DEFAULT_ROOMS_PATH`.
  - `roborock.load_roborock_client(config) -> RoborockClient | None` — returns `None` (logs a warning) when username or password is empty. (Real cloud build lands in Task 3; this task ships only the graceful-None path.)
  - `DEFAULT_ROOMS_PATH = "roborock_rooms.yaml"` (module constant in `config.py`).

- [ ] **Step 1: Write the failing config test**

In `tests/home_agent/test_home_agent_config.py` add:
```python
def test_load_config_reads_roborock_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "")
    monkeypatch.setenv("ROBOROCK_USERNAME", "me@example.com")
    monkeypatch.setenv("ROBOROCK_PASSWORD", "secret")
    monkeypatch.setenv("ROBOROCK_ROOMS", "custom_rooms.yaml")
    from home_agent.config import load_config
    cfg = load_config()
    assert cfg.roborock_username == "me@example.com"
    assert cfg.roborock_password == "secret"
    assert cfg.roborock_rooms_path == "custom_rooms.yaml"


def test_load_config_roborock_defaults(monkeypatch):
    for k in ("ROBOROCK_USERNAME", "ROBOROCK_PASSWORD", "ROBOROCK_ROOMS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "")
    from home_agent.config import load_config
    cfg = load_config()
    assert cfg.roborock_username == "" and cfg.roborock_password == ""
    assert cfg.roborock_rooms_path == "roborock_rooms.yaml"
```

- [ ] **Step 2: Run and verify it fails**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_home_agent_config.py -k roborock -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'roborock_username'`.

- [ ] **Step 3: Add the fields + parsing to `config.py`**

Add constant near the others:
```python
DEFAULT_ROOMS_PATH = "roborock_rooms.yaml"
```
Add to the `Config` dataclass (after `calendar_write_id`):
```python
    roborock_username: str = ""
    roborock_password: str = ""
    roborock_rooms_path: str = DEFAULT_ROOMS_PATH
```
Add to the `Config(...)` construction in `load_config`:
```python
        roborock_username=os.environ.get("ROBOROCK_USERNAME", ""),
        roborock_password=os.environ.get("ROBOROCK_PASSWORD", ""),
        roborock_rooms_path=os.environ.get("ROBOROCK_ROOMS", DEFAULT_ROOMS_PATH),
```

- [ ] **Step 4: Run config test to verify it passes**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_home_agent_config.py -k roborock -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Write the failing loader test**

Create `tests/home_agent/test_roborock_loader.py`:
```python
from home_agent.config import Config
from home_agent.roborock import load_roborock_client


def _cfg(**kw):
    base = dict(openai_api_key="k", telegram_bot_token="t", allowed_chat_ids=set())
    base.update(kw)
    return Config(**base)


def test_loader_returns_none_when_unconfigured():
    assert load_roborock_client(_cfg()) is None


def test_loader_returns_none_when_password_missing():
    assert load_roborock_client(_cfg(roborock_username="me@example.com")) is None
```

- [ ] **Step 6: Run and verify it fails**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'home_agent.roborock'`.

- [ ] **Step 7: Create `roborock.py` with the graceful loader**

Create `src/home_agent/roborock.py`:
```python
import logging

log = logging.getLogger("home_agent")


def load_roborock_client(config):
    """Build the real cloud RoborockClient from config, or return None (with a warning) when
    credentials are unset. python-roborock is imported LAZILY inside the real build path (Task 3),
    so importing this module never touches the network."""
    if not config.roborock_username or not config.roborock_password:
        log.warning("ROBOROCK_USERNAME/PASSWORD unset — Roborock control disabled")
        return None
    # Real cloud client build lands in Task 3.
    return _build_cloud_client(config)


def _build_cloud_client(config):  # replaced with the real lazy-import build in Task 3
    raise NotImplementedError
```

- [ ] **Step 8: Run loader test to verify it passes**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_loader.py -v`
Expected: PASS (2 passed) — both cases hit the None branch before `_build_cloud_client`.

- [ ] **Step 9: Add the dependency + `.env.example` keys**

In `pyproject.toml`, append to `dependencies`: `"python-roborock>=2.0"`.
In `.env.example`, add under a new section:
```
# --- Roborock Q Revo (vacuum) ---
# Roborock account; unset -> vacuum tools don't load (bot still runs).
ROBOROCK_USERNAME=you@example.com
ROBOROCK_PASSWORD=your-roborock-password
# Optional: path to the room registry (segment-id <-> Hebrew room names).
# ROBOROCK_ROOMS=roborock_rooms.yaml
```

- [ ] **Step 10: Run the full suite + commit**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (all prior tests + 4 new).
```bash
git add src/home_agent/config.py src/home_agent/roborock.py pyproject.toml .env.example \
        tests/home_agent/test_home_agent_config.py tests/home_agent/test_roborock_loader.py
git commit -m "feat(roborock): config keys + dependency + graceful client loader"
```

---

### Task 2: Room registry (YAML) + discovery script

**Files:**
- Create: `src/home_agent/roborock_rooms.py`
- Create: `roborock_rooms.yaml.example`
- Create: `scripts/roborock_discover.py`
- Test: `tests/home_agent/test_roborock_rooms.py`

**Interfaces:**
- Produces:
  - `Room` dataclass: `name: str`, `segment_id: int`, `aliases: list[str]`.
  - `RoomRegistry`:
    - `RoomRegistry(rooms: list[Room])`
    - `RoomRegistry.load(path: str) -> RoomRegistry` (reads `rooms:` mapping)
    - `resolve(spoken: str) -> Room | None` (exact, case-insensitive alias/name match)
    - `known_names() -> list[str]`
    - `name_for_segment(segment_id: int) -> str | None`
    - attribute `rooms: list[Room]`
  - `roborock_rooms.load_room_registry(config) -> RoomRegistry | None` (None + warning if the YAML file is absent).

- [ ] **Step 1: Write the failing registry test**

Create `tests/home_agent/test_roborock_rooms.py`:
```python
from home_agent.roborock_rooms import Room, RoomRegistry


def _reg():
    return RoomRegistry([
        Room(name="living_room", segment_id=16, aliases=["סלון", "salon", "living room"]),
        Room(name="kitchen", segment_id=17, aliases=["מטבח", "kitchen"]),
    ])


def test_resolve_by_hebrew_alias():
    room = _reg().resolve("סלון")
    assert room is not None and room.segment_id == 16 and room.name == "living_room"


def test_resolve_is_case_insensitive_and_trims():
    assert _reg().resolve("  Living Room ").segment_id == 16


def test_resolve_unknown_returns_none():
    assert _reg().resolve("garage") is None


def test_known_names_and_name_for_segment():
    reg = _reg()
    assert reg.known_names() == ["living_room", "kitchen"]
    assert reg.name_for_segment(17) == "kitchen"
    assert reg.name_for_segment(999) is None


def test_load_from_yaml(tmp_path):
    p = tmp_path / "rooms.yaml"
    p.write_text(
        "rooms:\n"
        "  living_room:\n"
        "    segment_id: 16\n"
        "    aliases: [\"סלון\", \"salon\"]\n"
        "  kitchen:\n"
        "    segment_id: 17\n"
        "    aliases: [\"מטבח\"]\n",
        encoding="utf-8",
    )
    reg = RoomRegistry.load(str(p))
    assert reg.resolve("salon").segment_id == 16
    assert reg.name_for_segment(17) == "kitchen"
```

- [ ] **Step 2: Run and verify it fails**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_rooms.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'home_agent.roborock_rooms'`.

- [ ] **Step 3: Implement `roborock_rooms.py`**

Create `src/home_agent/roborock_rooms.py` (mirrors `switchbot_scheduler.registry`):
```python
import logging
import os
from dataclasses import dataclass

import yaml

log = logging.getLogger("home_agent")


@dataclass
class Room:
    name: str
    segment_id: int
    aliases: list[str]


class RoomRegistry:
    def __init__(self, rooms: list[Room]):
        self.rooms = rooms
        self._by_segment = {r.segment_id: r.name for r in rooms}
        self._alias_map: dict[str, Room] = {}
        for r in rooms:
            self._alias_map[r.name.lower()] = r
            for a in r.aliases:
                self._alias_map[a.strip().lower()] = r

    @classmethod
    def load(cls, path: str) -> "RoomRegistry":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        rooms = [
            Room(name=name, segment_id=int(cfg["segment_id"]), aliases=cfg.get("aliases", []))
            for name, cfg in data["rooms"].items()
        ]
        return cls(rooms)

    def resolve(self, spoken: str) -> Room | None:
        return self._alias_map.get(spoken.strip().lower())

    def known_names(self) -> list[str]:
        return [r.name for r in self.rooms]

    def name_for_segment(self, segment_id) -> str | None:
        return self._by_segment.get(segment_id)


def load_room_registry(config):
    """Return the RoomRegistry, or None (with a warning) if the rooms YAML is absent."""
    path = config.roborock_rooms_path
    if not os.path.exists(path):
        log.warning("roborock rooms file not found at %s — room-scoped cleaning disabled", path)
        return None
    return RoomRegistry.load(path)
```

- [ ] **Step 4: Run registry test to verify it passes**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_rooms.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Create the example YAML + discovery script (no test — manual tooling)**

Create `roborock_rooms.yaml.example`:
```yaml
# Seed with `python scripts/roborock_discover.py` (prints current segment ids + names),
# then add Hebrew aliases. Copy to roborock_rooms.yaml (git-ignored if it holds real ids).
rooms:
  living_room:
    segment_id: 16
    aliases: ["סלון", "salon", "living room"]
  kitchen:
    segment_id: 17
    aliases: ["מטבח", "kitchen"]
  bedroom:
    segment_id: 18
    aliases: ["חדר שינה", "bedroom"]
```

Create `scripts/roborock_discover.py`:
```python
"""One-time helper: log in to Roborock (cloud) and print the current segment ids + room names,
so you can seed roborock_rooms.yaml. Run:  python scripts/roborock_discover.py
Reads ROBOROCK_USERNAME / ROBOROCK_PASSWORD from the environment / .env."""
import sys

from home_agent.config import load_config
from home_agent.roborock import load_roborock_client


def main() -> int:
    config = load_config()
    client = load_roborock_client(config)
    if client is None:
        print("Set ROBOROCK_USERNAME and ROBOROCK_PASSWORD (in .env) first.", file=sys.stderr)
        return 1
    print("segment_id\tname")
    for segment_id, name in client.room_mapping():
        print(f"{segment_id}\t{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run the full suite + commit**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS.
```bash
git add src/home_agent/roborock_rooms.py roborock_rooms.yaml.example scripts/roborock_discover.py \
        tests/home_agent/test_roborock_rooms.py
git commit -m "feat(roborock): RoomRegistry (YAML) + discovery script"
```

---

### Task 3: RoborockClient seam + FakeRoborockClient + `build_roborock_tools` + `list_rooms`

**Files:**
- Modify: `src/home_agent/roborock.py` (add `RoborockClient`, real build, `build_roborock_tools`, `list_rooms`)
- Create: `tests/home_agent/roborock_fakes.py` (shared `FakeRoborockClient`)
- Test: `tests/home_agent/test_roborock_tools.py`

**Interfaces:**
- Produces — the **domain-level client interface** every tool uses (duck-typed; real `RoborockClient` and `FakeRoborockClient` both implement it):
  - `room_mapping() -> list[tuple[int, str]]`
  - `clean(segment_ids: list[int] | None, *, mode: str | None, suction: str | None, water_flow: str | None, repeat: int) -> None`
  - `pause() / resume() / stop() / return_to_dock() / locate() -> None`
  - `empty_bin() / wash_mop() / dry_mop() -> None`
  - `status() -> dict` with keys `state:str, battery:int, cleaned_area:float, clean_time:int(sec), segment_id:int|None, error:str|None`
  - `consumables() -> dict` with keys `main_brush:int, side_brush:int, filter:int, sensor:int` (percent remaining)
  - `get_timers() -> list[dict]` each `{id:str, time:str"HH:MM", days:list[str], enabled:bool, target:str, mode:str|None}`
  - `set_timer(*, time:str, days:list[str], segment_ids:list[int]|None, mode, suction, water_flow) -> str` (returns timer id)
  - `del_timer(timer_id: str) -> bool`
  - `build_roborock_tools(client, registry, *, now_fn=None) -> list[Tool]` (`registry` may be None).

- [ ] **Step 1: Write the shared fake + the failing `list_rooms` test**

Create `tests/home_agent/roborock_fakes.py`:
```python
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
```

Create `tests/home_agent/test_roborock_tools.py`:
```python
from home_agent.roborock import build_roborock_tools
from home_agent.roborock_rooms import Room, RoomRegistry
from roborock_fakes import FakeRoborockClient   # sibling helper; tests/ has no __init__.py (prepend import mode)


def _reg():
    return RoomRegistry([
        Room(name="living_room", segment_id=16, aliases=["סלון", "living room"]),
        Room(name="kitchen", segment_id=17, aliases=["מטבח"]),
    ])


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_list_rooms_lists_names_and_aliases():
    tools = build_roborock_tools(FakeRoborockClient(), _reg())
    out = _tool(tools, "list_rooms").impl({})
    assert "living_room" in out and "סלון" in out
    assert "kitchen" in out and "מטבח" in out


def test_list_rooms_without_registry_is_friendly():
    tools = build_roborock_tools(FakeRoborockClient(), None)
    out = _tool(tools, "list_rooms").impl({})
    assert "no rooms" in out.lower()
```

- [ ] **Step 2: Run and verify it fails**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_roborock_tools'`.

- [ ] **Step 3: Implement the client seam + `build_roborock_tools` + `list_rooms`**

Replace the `_build_cloud_client` stub in `src/home_agent/roborock.py` and add the tools. Full module now:
```python
import logging

from .tools import Tool

log = logging.getLogger("home_agent")

MODES = ("vacuum", "mop", "vac_and_mop")
SUCTIONS = ("quiet", "balanced", "turbo", "max")
WATER_FLOWS = ("low", "medium", "high")


def load_roborock_client(config):
    if not config.roborock_username or not config.roborock_password:
        log.warning("ROBOROCK_USERNAME/PASSWORD unset — Roborock control disabled")
        return None
    try:
        return _build_cloud_client(config)
    except Exception as e:
        log.warning("Roborock login failed (%s) — vacuum control disabled", e)
        return None


def _build_cloud_client(config):
    """Real cloud client. python-roborock is imported HERE (lazy) so importing this module
    never touches the network and the test suite never needs the library.
    NOTE: the exact python-roborock login + command call shapes are CONFIRMED AT BUILD TIME
    during the live smoke (Task 8); RoborockClient below is where that mapping lives."""
    return RoborockClient(config.roborock_username, config.roborock_password)


class RoborockClient:
    """Domain-level wrapper over python-roborock. Translates domain terms (mode/suction/
    water_flow enums, segment ids) into library commands. The single injectable seam; tests
    inject a fake with the same method surface."""

    def __init__(self, username, password):
        # Lazy import + cloud login. Filled in at build time against python-roborock's current API
        # (RoborockApiClient login -> home data -> device -> MQTT/local client). Kept out of the
        # test path entirely (tests use FakeRoborockClient).
        from roborock import RoborockApiClient  # noqa: F401  (lazy; confirm exact symbols at build)
        raise NotImplementedError("wire python-roborock login here at build time (Task 8)")

    # The method surface below is what the tools call; the real bodies are wired at build time.
    def room_mapping(self): raise NotImplementedError
    def clean(self, segment_ids, *, mode=None, suction=None, water_flow=None, repeat=1): raise NotImplementedError
    def pause(self): raise NotImplementedError
    def resume(self): raise NotImplementedError
    def stop(self): raise NotImplementedError
    def return_to_dock(self): raise NotImplementedError
    def locate(self): raise NotImplementedError
    def empty_bin(self): raise NotImplementedError
    def wash_mop(self): raise NotImplementedError
    def dry_mop(self): raise NotImplementedError
    def status(self): raise NotImplementedError
    def consumables(self): raise NotImplementedError
    def get_timers(self): raise NotImplementedError
    def set_timer(self, *, time, days, segment_ids, mode, suction, water_flow): raise NotImplementedError
    def del_timer(self, timer_id): raise NotImplementedError


_LIST_ROOMS_SCHEMA = {"type": "function", "function": {
    "name": "list_rooms",
    "description": (
        "List the rooms the vacuum can clean — names and Hebrew/English aliases. Use when the user "
        "asks what rooms you can clean, or when you need the exact room name before a room-scoped "
        "clean. Does NOT report the vacuum's current state."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}


def _list_rooms_impl(args, *, registry) -> str:
    if registry is None:
        return "no rooms are configured — I can only clean the whole home."
    lines = []
    for r in registry.rooms:
        aliases = ", ".join(r.aliases) if r.aliases else "(no aliases)"
        lines.append(f"{r.name} — aliases: {aliases}")
    return "\n".join(lines)


def build_roborock_tools(client, registry, *, now_fn=None) -> list[Tool]:
    return [
        Tool(name="list_rooms", schema=_LIST_ROOMS_SCHEMA,
             impl=lambda args: _list_rooms_impl(args, registry=registry)),
    ]
```

- [ ] **Step 4: Run tool test to verify it passes**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the loader test (still green) + full suite + commit**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (loader test still green — its None path never reaches `_build_cloud_client`).
```bash
git add src/home_agent/roborock.py tests/home_agent/roborock_fakes.py tests/home_agent/test_roborock_tools.py
git commit -m "feat(roborock): client seam + fake + build_roborock_tools + list_rooms"
```

---

### Task 4: `clean` tool (whole-home + room + per-run plan)

**Files:**
- Modify: `src/home_agent/roborock.py`
- Test: `tests/home_agent/test_roborock_tools.py`

**Interfaces:**
- Consumes: `client.clean(...)`, `RoomRegistry.resolve/known_names`, `MODES/SUCTIONS/WATER_FLOWS`.
- Produces: a `clean` tool. Behavior: no `rooms` → `client.clean(None, …)` (whole home); `rooms` present → resolve each to a segment id → `client.clean([ids], …)`. Unknown room or bad enum → friendly message, **no** client call. Reports the requested target + plan.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_roborock_tools.py`:
```python
def test_clean_whole_home_when_no_rooms():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "clean").impl({})
    assert client.calls == [("clean", dict(segment_ids=None, mode=None, suction=None,
                                           water_flow=None, repeat=1))]
    assert "whole home" in out and "✅" in out


def test_clean_resolves_rooms_to_segments_with_plan():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "clean").impl(
        {"rooms": ["סלון", "מטבח"], "mode": "vac_and_mop", "suction": "turbo"})
    assert client.calls == [("clean", dict(segment_ids=[16, 17], mode="vac_and_mop",
                                           suction="turbo", water_flow=None, repeat=1))]
    assert "living_room" in out and "kitchen" in out


def test_clean_unknown_room_refuses_without_calling():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "clean").impl({"rooms": ["garage"]})
    assert client.calls == []
    assert "garage" in out and "living_room" in out


def test_clean_bad_mode_refuses_without_calling():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "clean").impl({"mode": "polish"})
    assert client.calls == []
    assert "polish" in out.lower()


def test_clean_rooms_without_registry_is_friendly():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, None)
    out = _tool(tools, "clean").impl({"rooms": ["סלון"]})
    assert client.calls == []
    assert "whole home" in out.lower()


def test_clean_reports_error_friendly():
    from roborock_fakes import ExplodingRoborockClient
    tools = build_roborock_tools(ExplodingRoborockClient(), _reg())
    out = _tool(tools, "clean").impl({})
    assert "offline" in out
```

- [ ] **Step 2: Run and verify it fails**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -k clean -v`
Expected: FAIL — `StopIteration` (no tool named `clean`).

- [ ] **Step 3: Implement `clean`**

Add to `roborock.py` (schema + impl), and register in `build_roborock_tools`:
```python
_CLEAN_SCHEMA = {"type": "function", "function": {
    "name": "clean",
    "description": (
        "Start the vacuum cleaning. Omit `rooms` to clean the WHOLE home; give one or more room "
        "names/aliases (Hebrew or English) to clean just those rooms. `mode` sets vacuum / mop / "
        "vac_and_mop (vacuum and mop); `suction` sets fan power; `water_flow` sets mop wetness. Call "
        "list_rooms first if unsure of a room name. One plan applies to the whole run. Report what "
        "you started, in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "rooms": {"type": "array", "items": {"type": "string"},
                  "description": "Room names/aliases to clean; omit for the whole home."},
        "mode": {"type": "string", "enum": list(MODES),
                 "description": "vacuum, mop, or vac_and_mop (vacuum then mop / both)."},
        "suction": {"type": "string", "enum": list(SUCTIONS), "description": "fan power."},
        "water_flow": {"type": "string", "enum": list(WATER_FLOWS), "description": "mop water level."},
        "repeat": {"type": "integer", "description": "times to repeat a room clean (default once)."},
    }, "additionalProperties": False},
}}

_MODE_WORDS = {"vacuum": "vacuum", "mop": "mop", "vac_and_mop": "vacuum + mop"}


def _describe_plan(mode, suction, water_flow) -> str:
    bits = []
    if mode: bits.append(_MODE_WORDS[mode])
    if suction: bits.append(f"suction {suction}")
    if water_flow: bits.append(f"water {water_flow}")
    return f" ({', '.join(bits)})" if bits else ""


def _clean_impl(args, *, client, registry) -> str:
    rooms_spoken = args.get("rooms") or []
    mode = args.get("mode")
    suction = args.get("suction")
    water_flow = args.get("water_flow")
    repeat = args.get("repeat") or 1
    if mode is not None and mode not in MODES:
        return f"unknown mode '{mode}'. Use one of: {', '.join(MODES)}."
    if suction is not None and suction not in SUCTIONS:
        return f"unknown suction '{suction}'. Use one of: {', '.join(SUCTIONS)}."
    if water_flow is not None and water_flow not in WATER_FLOWS:
        return f"unknown water_flow '{water_flow}'. Use one of: {', '.join(WATER_FLOWS)}."
    if rooms_spoken:
        if registry is None:
            return "no rooms are configured, so I can only clean the whole home. Say 'clean everything'."
        segs, names, unknown = [], [], []
        for spoken in rooms_spoken:
            room = registry.resolve(spoken)
            if room is None:
                unknown.append(spoken)
            else:
                segs.append(room.segment_id); names.append(room.name)
        if unknown:
            return (f"unknown room(s): {', '.join(unknown)}. "
                    f"I can clean: {', '.join(registry.known_names())}")
        target, segment_ids = ", ".join(names), segs
    else:
        target, segment_ids = "the whole home", None
    try:
        client.clean(segment_ids, mode=mode, suction=suction, water_flow=water_flow, repeat=repeat)
    except Exception as e:
        return f"couldn't start cleaning — {e}"
    return f"cleaning {target}{_describe_plan(mode, suction, water_flow)} ✅"
```
Register in `build_roborock_tools` list:
```python
        Tool(name="clean", schema=_CLEAN_SCHEMA,
             impl=lambda args: _clean_impl(args, client=client, registry=registry)),
```

- [ ] **Step 4: Run clean tests to verify they pass**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -k clean -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Full suite + commit**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
```bash
git add src/home_agent/roborock.py tests/home_agent/test_roborock_tools.py
git commit -m "feat(roborock): clean tool (whole-home + rooms + per-run plan)"
```

---

### Task 5: `control_vacuum` + `dock_action`

**Files:**
- Modify: `src/home_agent/roborock.py`
- Test: `tests/home_agent/test_roborock_tools.py`

**Interfaces:**
- Consumes: `client.pause/resume/stop/return_to_dock/locate`, `client.empty_bin/wash_mop/dry_mop`.
- Produces: `control_vacuum(action)` and `dock_action(action)` tools; unknown action → friendly message, no client call; client error → friendly message.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_roborock_tools.py`:
```python
import pytest


@pytest.mark.parametrize("action,method", [
    ("pause", "pause"), ("resume", "resume"), ("stop", "stop"),
    ("return_to_dock", "return_to_dock"), ("locate", "locate"),
])
def test_control_vacuum_dispatches(action, method):
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "control_vacuum").impl({"action": action})
    assert client.calls == [(method, {})]
    assert "✅" in out


def test_control_vacuum_unknown_action():
    client = FakeRoborockClient()
    out = _tool(build_roborock_tools(client, _reg()), "control_vacuum").impl({"action": "fly"})
    assert client.calls == []
    assert "fly" in out.lower()


@pytest.mark.parametrize("action,method", [
    ("empty_bin", "empty_bin"), ("wash_mop", "wash_mop"), ("dry_mop", "dry_mop"),
])
def test_dock_action_dispatches(action, method):
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "dock_action").impl({"action": action})
    assert client.calls == [(method, {})]
    assert "✅" in out


def test_dock_action_unknown():
    client = FakeRoborockClient()
    out = _tool(build_roborock_tools(client, _reg()), "dock_action").impl({"action": "polish"})
    assert client.calls == []
    assert "polish" in out.lower()
```

- [ ] **Step 2: Run and verify it fails**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -k "control_vacuum or dock_action" -v`
Expected: FAIL — no such tools.

- [ ] **Step 3: Implement both tools**

Add to `roborock.py`:
```python
_CONTROL_SCHEMA = {"type": "function", "function": {
    "name": "control_vacuum",
    "description": (
        "Control the running vacuum: pause, resume, stop, return_to_dock (send it back to charge), "
        "or locate (make it beep so you can find it). Report what you did, in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string",
                   "enum": ["pause", "resume", "stop", "return_to_dock", "locate"]},
    }, "required": ["action"], "additionalProperties": False},
}}

_DOCK_SCHEMA = {"type": "function", "function": {
    "name": "dock_action",
    "description": (
        "Run a dock maintenance action while the vacuum is docked: empty_bin (empty the dust bin), "
        "wash_mop (wash the mop pads), or dry_mop (dry them). Report back in the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["empty_bin", "wash_mop", "dry_mop"]},
    }, "required": ["action"], "additionalProperties": False},
}}

_CONTROL_WORDS = {"pause": "paused", "resume": "resumed", "stop": "stopped",
                  "return_to_dock": "returning to dock", "locate": "locating (beeping)"}
_DOCK_WORDS = {"empty_bin": "emptying the bin", "wash_mop": "washing the mop", "dry_mop": "drying the mop"}


def _control_impl(args, *, client) -> str:
    action = (args.get("action") or "").strip().lower()
    method = {"pause": client.pause, "resume": client.resume, "stop": client.stop,
              "return_to_dock": client.return_to_dock, "locate": client.locate}.get(action)
    if method is None:
        return f"unknown action '{action}'. Use pause, resume, stop, return_to_dock, or locate."
    try:
        method()
    except Exception as e:
        return f"couldn't {action} — {e}"
    return f"{_CONTROL_WORDS[action]} ✅"


def _dock_impl(args, *, client) -> str:
    action = (args.get("action") or "").strip().lower()
    method = {"empty_bin": client.empty_bin, "wash_mop": client.wash_mop,
              "dry_mop": client.dry_mop}.get(action)
    if method is None:
        return f"unknown action '{action}'. Use empty_bin, wash_mop, or dry_mop."
    try:
        method()
    except Exception as e:
        return f"couldn't {action} — {e}"
    return f"{_DOCK_WORDS[action]} ✅"
```
Register both in `build_roborock_tools`:
```python
        Tool(name="control_vacuum", schema=_CONTROL_SCHEMA,
             impl=lambda args: _control_impl(args, client=client)),
        Tool(name="dock_action", schema=_DOCK_SCHEMA,
             impl=lambda args: _dock_impl(args, client=client)),
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -k "control_vacuum or dock_action" -v`
Expected: PASS (10 passed).

- [ ] **Step 5: Full suite + commit**

```bash
git add src/home_agent/roborock.py tests/home_agent/test_roborock_tools.py
git commit -m "feat(roborock): control_vacuum + dock_action tools"
```

---

### Task 6: `vacuum_status` + `consumables`

**Files:**
- Modify: `src/home_agent/roborock.py`
- Test: `tests/home_agent/test_roborock_tools.py`

**Interfaces:**
- Consumes: `client.status() -> dict`, `client.consumables() -> dict`, `RoomRegistry.name_for_segment`.
- Produces: `vacuum_status()` (human summary; names current room via registry) and `consumables()` tools.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_roborock_tools.py`:
```python
def test_vacuum_status_summarizes_and_names_room():
    client = FakeRoborockClient(status={
        "state": "cleaning", "battery": 82, "cleaned_area": 12.5,
        "clean_time": 600, "segment_id": 16, "error": None})
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "vacuum_status").impl({})
    assert ("status", {}) in client.calls
    assert "cleaning" in out and "82" in out and "living_room" in out


def test_vacuum_status_reports_error_field():
    client = FakeRoborockClient(status={
        "state": "error", "battery": 40, "cleaned_area": 0, "clean_time": 0,
        "segment_id": None, "error": "stuck"})
    out = _tool(build_roborock_tools(client, _reg()), "vacuum_status").impl({})
    assert "stuck" in out


def test_consumables_summarizes():
    client = FakeRoborockClient(consumables={
        "main_brush": 80, "side_brush": 65, "filter": 40, "sensor": 90})
    out = _tool(build_roborock_tools(client, _reg()), "consumables").impl({})
    assert "main brush" in out.lower() and "80" in out
    assert "filter" in out.lower() and "40" in out
```

- [ ] **Step 2: Run and verify it fails**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -k "status or consumables" -v`
Expected: FAIL — no such tools.

- [ ] **Step 3: Implement both tools**

Add to `roborock.py`:
```python
_STATUS_SCHEMA = {"type": "function", "function": {
    "name": "vacuum_status",
    "description": (
        "Report the vacuum's current state: what it's doing, battery %, area and time cleaned, "
        "current room, and any error. Use when the user asks how the vacuum is doing or where it is. "
        "Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_CONSUMABLES_SCHEMA = {"type": "function", "function": {
    "name": "consumables",
    "description": (
        "Report remaining life of the vacuum's consumables (main brush, side brush, filter, "
        "sensors) as a percentage. Use when the user asks about maintenance or whether parts need "
        "replacing. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_CONSUMABLE_LABELS = {"main_brush": "main brush", "side_brush": "side brush",
                      "filter": "filter", "sensor": "sensor"}


def _status_impl(args, *, client, registry) -> str:
    try:
        s = client.status()
    except Exception as e:
        return f"couldn't read the vacuum status — {e}"
    lines = [f"state: {s.get('state', 'unknown')}", f"battery: {s.get('battery', '?')}%"]
    if s.get("cleaned_area"):
        lines.append(f"cleaned area: {s['cleaned_area']} m²")
    if s.get("clean_time"):
        lines.append(f"clean time: {s['clean_time'] // 60} min")
    seg = s.get("segment_id")
    room = registry.name_for_segment(seg) if (registry is not None and seg is not None) else None
    if room:
        lines.append(f"current room: {room}")
    if s.get("error"):
        lines.append(f"error: {s['error']}")
    return "\n".join(lines)


def _consumables_impl(args, *, client) -> str:
    try:
        c = client.consumables()
    except Exception as e:
        return f"couldn't read consumables — {e}"
    return "\n".join(f"{_CONSUMABLE_LABELS.get(k, k)}: {v}% remaining" for k, v in c.items())
```
Register in `build_roborock_tools`:
```python
        Tool(name="vacuum_status", schema=_STATUS_SCHEMA,
             impl=lambda args: _status_impl(args, client=client, registry=registry)),
        Tool(name="consumables", schema=_CONSUMABLES_SCHEMA,
             impl=lambda args: _consumables_impl(args, client=client)),
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -k "status or consumables" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Full suite + commit**

```bash
git add src/home_agent/roborock.py tests/home_agent/test_roborock_tools.py
git commit -m "feat(roborock): vacuum_status + consumables tools"
```

---

### Task 7: Scheduling trio (robot-native timers)

**Files:**
- Modify: `src/home_agent/roborock.py`
- Test: `tests/home_agent/test_roborock_tools.py`

**Interfaces:**
- Consumes: `client.set_timer/get_timers/del_timer`, `RoomRegistry.resolve/known_names`.
- Produces: `schedule_clean(time, days?, rooms?, mode?, suction?, water_flow?)`, `get_cleaning_schedule()`, `cancel_cleaning_schedule(id)`.
- **Recurring day-of-week schedules only** (what `server_timer` supports). `days` accepts `sun..sat` or the words `daily`/`weekdays`/`weekends`; **default `daily`**. Genuine one-off "clean once at T" is deferred to the future cron/box path (per the spec's scheduling fallback clause) — the tool description must NOT promise one-off.
- `_normalize_days` reuses the day vocabulary: import `DAYS` from `switchbot_scheduler.model`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/home_agent/test_roborock_tools.py`:
```python
def test_schedule_clean_daily_whole_home():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "schedule_clean").impl({"time": "08:00"})
    assert client.calls == [("set_timer", dict(
        time="08:00", days=["sun", "mon", "tue", "wed", "thu", "fri", "sat"],
        segment_ids=None, mode=None, suction=None, water_flow=None))]
    assert "08:00" in out and "✅" in out


def test_schedule_clean_rooms_and_days():
    client = FakeRoborockClient()
    tools = build_roborock_tools(client, _reg())
    out = _tool(tools, "schedule_clean").impl(
        {"time": "20:30", "days": ["weekends"], "rooms": ["מטבח"], "mode": "mop"})
    assert client.calls == [("set_timer", dict(
        time="20:30", days=["sat", "sun"], segment_ids=[17],
        mode="mop", suction=None, water_flow=None))]


def test_schedule_clean_bad_time_refuses():
    client = FakeRoborockClient()
    out = _tool(build_roborock_tools(client, _reg()), "schedule_clean").impl({"time": "8pm"})
    assert client.calls == []
    assert "time" in out.lower()


def test_schedule_clean_unknown_room_refuses():
    client = FakeRoborockClient()
    out = _tool(build_roborock_tools(client, _reg()), "schedule_clean").impl(
        {"time": "08:00", "rooms": ["garage"]})
    assert client.calls == []
    assert "garage" in out


def test_get_cleaning_schedule_lists_timers():
    client = FakeRoborockClient(timers=[
        {"id": "7", "time": "08:00", "days": ["mon", "tue"], "enabled": True,
         "target": "whole home", "mode": None}])
    out = _tool(build_roborock_tools(client, _reg()), "get_cleaning_schedule").impl({})
    assert "08:00" in out and "7" in out


def test_get_cleaning_schedule_empty():
    out = _tool(build_roborock_tools(FakeRoborockClient(), _reg()), "get_cleaning_schedule").impl({})
    assert "nothing" in out.lower()


def test_cancel_cleaning_schedule_deletes():
    client = FakeRoborockClient(timers=[
        {"id": "7", "time": "08:00", "days": ["mon"], "enabled": True,
         "target": "whole home", "mode": None}])
    out = _tool(build_roborock_tools(client, _reg()), "cancel_cleaning_schedule").impl({"id": "7"})
    assert ("del_timer", {"timer_id": "7"}) in client.calls
    assert "✅" in out


def test_cancel_cleaning_schedule_unknown_id():
    client = FakeRoborockClient()
    out = _tool(build_roborock_tools(client, _reg()), "cancel_cleaning_schedule").impl({"id": "99"})
    assert "99" in out and "no" in out.lower()
```

- [ ] **Step 2: Run and verify it fails**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -k "schedule or cancel_cleaning" -v`
Expected: FAIL — no such tools.

- [ ] **Step 3: Implement the trio**

Add to `roborock.py` (import at top: `from switchbot_scheduler.model import DAYS`):
```python
import re

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_DAY_WORDS = {"daily": list(DAYS), "weekdays": ["mon", "tue", "wed", "thu", "fri"],
              "weekends": ["sat", "sun"]}


def _normalize_days(days):
    seen = set()
    for d in days:
        key = str(d).strip().lower()
        if key in _DAY_WORDS:
            seen.update(_DAY_WORDS[key])
        elif key in DAYS:
            seen.add(key)
        else:
            raise ValueError(f"unknown day '{d}'")
    return [d for d in DAYS if d in seen]


_SCHEDULE_CLEAN_SCHEMA = {"type": "function", "function": {
    "name": "schedule_clean",
    "description": (
        "Program a RECURRING cleaning schedule into the vacuum itself (runs even if this computer is "
        "off). `time` is 24-hour \"HH:MM\". `days` are the days it repeats (sun mon tue wed thu fri "
        "sat, or the words daily/weekdays/weekends) — omit for every day. Omit `rooms` for the whole "
        "home. `mode`/`suction`/`water_flow` set the cleaning plan. Report what you scheduled, in the "
        "user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "time": {"type": "string", "description": "24-hour clock time, \"HH:MM\"."},
        "days": {"type": "array", "items": {"type": "string"},
                 "description": "Days to repeat; omit for daily."},
        "rooms": {"type": "array", "items": {"type": "string"},
                  "description": "Rooms to clean; omit for the whole home."},
        "mode": {"type": "string", "enum": list(MODES)},
        "suction": {"type": "string", "enum": list(SUCTIONS)},
        "water_flow": {"type": "string", "enum": list(WATER_FLOWS)},
    }, "required": ["time"], "additionalProperties": False},
}}

_GET_SCHEDULE_SCHEMA = {"type": "function", "function": {
    "name": "get_cleaning_schedule",
    "description": (
        "List the vacuum's programmed cleaning schedules (each has an id, time, days, and target). "
        "Use when the user asks what cleans are scheduled. Report in the user's language."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

_CANCEL_SCHEDULE_SCHEMA = {"type": "function", "function": {
    "name": "cancel_cleaning_schedule",
    "description": (
        "Cancel one programmed cleaning schedule by its id (from get_cleaning_schedule). Report in "
        "the user's language."
    ),
    "parameters": {"type": "object", "properties": {
        "id": {"type": "string", "description": "The schedule id to cancel."},
    }, "required": ["id"], "additionalProperties": False},
}}


def _resolve_rooms(rooms_spoken, registry):
    """(segment_ids|None, names|None, error_message|None)."""
    if not rooms_spoken:
        return None, None, None
    if registry is None:
        return None, None, "no rooms are configured, so I can only clean the whole home."
    segs, names, unknown = [], [], []
    for spoken in rooms_spoken:
        room = registry.resolve(spoken)
        if room is None:
            unknown.append(spoken)
        else:
            segs.append(room.segment_id); names.append(room.name)
    if unknown:
        return None, None, (f"unknown room(s): {', '.join(unknown)}. "
                            f"I can clean: {', '.join(registry.known_names())}")
    return segs, names, None


def _schedule_clean_impl(args, *, client, registry) -> str:
    time_str = (args.get("time") or "").strip()
    if not _TIME_RE.match(time_str):
        return f"invalid time '{time_str}'. Use 24-hour HH:MM, e.g. 08:00."
    mode, suction, water_flow = args.get("mode"), args.get("suction"), args.get("water_flow")
    for val, allowed, label in ((mode, MODES, "mode"), (suction, SUCTIONS, "suction"),
                                (water_flow, WATER_FLOWS, "water_flow")):
        if val is not None and val not in allowed:
            return f"unknown {label} '{val}'. Use one of: {', '.join(allowed)}."
    try:
        days = _normalize_days(args.get("days") or ["daily"])
    except ValueError as e:
        return f"couldn't set the schedule: {e}"
    segment_ids, names, err = _resolve_rooms(args.get("rooms") or [], registry)
    if err:
        return err
    try:
        client.set_timer(time=time_str, days=days, segment_ids=segment_ids,
                         mode=mode, suction=suction, water_flow=water_flow)
    except Exception as e:
        return f"couldn't set the schedule — {e}"
    target = ", ".join(names) if names else "the whole home"
    return f"scheduled: clean {target} at {time_str} ({', '.join(days)}){_describe_plan(mode, suction, water_flow)} ✅"


def _get_schedule_impl(args, *, client) -> str:
    try:
        timers = client.get_timers()
    except Exception as e:
        return f"couldn't read the schedule — {e}"
    if not timers:
        return "nothing scheduled."
    lines = []
    for t in timers:
        state = "" if t.get("enabled", True) else " (disabled)"
        lines.append(f"[{t['id']}] {t['time']} {', '.join(t.get('days', []))} — {t.get('target', 'whole home')}{state}")
    return "\n".join(lines)


def _cancel_schedule_impl(args, *, client) -> str:
    timer_id = (args.get("id") or "").strip()
    try:
        ok = client.del_timer(timer_id)
    except Exception as e:
        return f"couldn't cancel — {e}"
    if not ok:
        return f"no schedule with id {timer_id} was found."
    return f"cancelled schedule {timer_id} ✅"
```
Register in `build_roborock_tools`:
```python
        Tool(name="schedule_clean", schema=_SCHEDULE_CLEAN_SCHEMA,
             impl=lambda args: _schedule_clean_impl(args, client=client, registry=registry)),
        Tool(name="get_cleaning_schedule", schema=_GET_SCHEDULE_SCHEMA,
             impl=lambda args: _get_schedule_impl(args, client=client)),
        Tool(name="cancel_cleaning_schedule", schema=_CANCEL_SCHEDULE_SCHEMA,
             impl=lambda args: _cancel_schedule_impl(args, client=client)),
```
Also refactor `_clean_impl` to use the shared `_resolve_rooms` helper (replace its inline room-resolution block with a call to `_resolve_rooms`), keeping its existing behavior and tests green.

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -k "schedule or cancel_cleaning" -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Full suite (clean tests still green after refactor) + commit**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_roborock_tools.py -v`
```bash
git add src/home_agent/roborock.py tests/home_agent/test_roborock_tools.py
git commit -m "feat(roborock): scheduling trio (native recurring server timers)"
```

---

### Task 8: Prompt policy + startup wiring + docs

**Files:**
- Modify: `src/home_agent/prompts.py` (append a digit-free vacuum capability line)
- Modify: `src/home_agent/telegram_app.py` (wire the tools when configured)
- Modify: `src/home_agent/CLAUDE.md` (module map row)
- Modify: `docs/ROADMAP.md` (Epic H checkboxes → shipped)
- Test: `tests/home_agent/test_system_prompt.py` (still digit-free/byte-stable), `tests/home_agent/test_telegram_app.py` (wiring)

**Interfaces:**
- Consumes: `load_roborock_client`, `load_room_registry`, `build_roborock_tools`, existing `build_application`.
- Produces: vacuum tools present in the composed tool list iff a client is configured; absent (bot still runs) otherwise.

- [ ] **Step 1: Write the failing wiring test**

Append to `tests/home_agent/test_telegram_app.py` (match the file's existing config/fixture style; adapt names to what's already there):
```python
def test_build_application_includes_roborock_tools_when_configured(monkeypatch, tmp_path):
    import home_agent.telegram_app as ta
    from home_agent.config import Config
    from roborock_fakes import FakeRoborockClient   # sibling helper; tests/ has no __init__.py
    from home_agent.roborock_rooms import Room, RoomRegistry

    monkeypatch.setattr(ta, "load_roborock_client", lambda cfg: FakeRoborockClient())
    monkeypatch.setattr(ta, "load_room_registry",
                        lambda cfg: RoomRegistry([Room("kitchen", 17, ["מטבח"])]))
    monkeypatch.setattr(ta, "load_registry", lambda cfg: None)          # no switchbot devices
    monkeypatch.setattr(ta, "load_calendar_service", lambda cfg: None)  # no calendar

    cfg = Config(openai_api_key="k", telegram_bot_token="t", allowed_chat_ids={1},
                 db_path=str(tmp_path / "t.db"),
                 roborock_username="u", roborock_password="p")
    captured = {}

    class _FakeApp:
        def add_handler(self, *a, **k): pass
        def add_error_handler(self, *a, **k): pass

    class _Builder:
        def token(self, *_): return self
        def build(self): return _FakeApp()

    monkeypatch.setattr(ta.Application, "builder", staticmethod(lambda: _Builder()))
    # Capture the tool list handed to handle_message by wrapping build's local tools:
    orig = ta.build_roborock_tools
    monkeypatch.setattr(ta, "build_roborock_tools",
                        lambda client, reg, **kw: captured.setdefault("tools", orig(client, reg, **kw)))
    ta.build_application(cfg, client=object(), conversation=object())
    names = {t.name for t in captured["tools"]}
    assert {"clean", "list_rooms", "vacuum_status", "schedule_clean"} <= names
```
(If `test_telegram_app.py` already has a helper that builds a `Config` + fake app, reuse it instead of the inline fakes above — keep this test consistent with the file.)

- [ ] **Step 2: Run and verify it fails**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_telegram_app.py -k roborock -v`
Expected: FAIL — `telegram_app` doesn't import/wire the roborock tools yet.

- [ ] **Step 3: Wire in `telegram_app.build_application`**

Add imports:
```python
from .roborock import build_roborock_tools, load_roborock_client
from .roborock_rooms import load_room_registry
```
In `build_application`, after the home/schedule block:
```python
    rr_client = load_roborock_client(config)
    if rr_client is not None:
        tools += build_roborock_tools(rr_client, load_room_registry(config))
```

- [ ] **Step 4: Run wiring test to verify pass**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_telegram_app.py -k roborock -v`
Expected: PASS.

- [ ] **Step 5: Append the vacuum capability line to the system prompt (digit-free)**

In `src/home_agent/prompts.py`, add a sentence to `FAMILY_SYSTEM_PROMPT`'s capability section — **no digits**:
```
You can also control the robot vacuum: clean the whole home or specific rooms, set the cleaning
plan (vacuum, mop, or vacuum-and-mop, plus suction and water level), pause or send it to its dock,
run dock actions, report its status and consumables, and program recurring cleaning schedules.
Map spoken room names to the canonical rooms from list_rooms, just as you do for devices.
```

- [ ] **Step 6: Verify the prompt invariants still hold**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest tests/home_agent/test_system_prompt.py -v`
Expected: PASS (digit-free + byte-stability tests green). If a byte-stability test pins an exact string/length, update its expected value in the same commit.

- [ ] **Step 7: Update docs**

- `src/home_agent/CLAUDE.md`: add a module-map row for `roborock.py` + `roborock_rooms.py` (Roborock control: `list_rooms`/`clean`/`control_vacuum`/`dock_action`/`vacuum_status`/`consumables` + schedule trio; injectable `RoborockClient`).
- `docs/ROADMAP.md`: tick Epic H's checkboxes that are now shipped (auth+discovery, immediate control, cleaning plan, dock actions, status, scheduling, consumables) and note "cloud transport; local + true one-off scheduling deferred; live-verified <date>".

- [ ] **Step 8: Full suite + commit**

Run: `PYTHONPATH=src /Users/netanelsade/smart-home/.venv/bin/pytest -q --ignore=integration_tests`
Expected: PASS (all green).
```bash
git add src/home_agent/telegram_app.py src/home_agent/prompts.py src/home_agent/CLAUDE.md \
        docs/ROADMAP.md tests/home_agent/test_telegram_app.py
git commit -m "feat(roborock): system-prompt policy + startup wiring + docs"
```

---

## Live smoke test (outside CI — needs the real robot + credentials)

Not a code task; the gate before merging + removing the worktree. With `ROBOROCK_USERNAME`/`PASSWORD` in `.env`:
1. `python scripts/roborock_discover.py` → seed `roborock_rooms.yaml` with real segment ids + Hebrew aliases.
2. **Wire the real `RoborockClient`** bodies against the installed `python-roborock` (login → device → MQTT; and the command mapping for each domain method — this is the "confirm at build time" work). Confirm the `set_server_timer` payload format here; if impractical, mark recurring scheduling deferred to the cron path and disable `schedule_clean`.
3. Drive the live robot from Telegram in Hebrew: whole-home clean, room clean (*"תשאב את הסלון"*), vac-then-mop, `return_to_dock`, a dock action, `vacuum_status`, and a `schedule_clean` round-trip (create → `get_cleaning_schedule` → `cancel_cleaning_schedule`).
4. Only after this passes: merge to `main` and remove the worktree.

## Self-Review notes

- **Spec coverage:** auth/discovery → T1/T2; immediate control → T4/T5; per-run plan → T4; dock → T5; status → T6; consumables → T6; scheduling → T7; graceful-unconfigured → T1/T3; wiring + prompt → T8. **Deviation flagged:** true one-off scheduling is deferred (server timers are recurring) — consistent with the spec's scheduling fallback clause; the tool promises recurring only.
- **Placeholders:** the only `NotImplementedError`s are the real `RoborockClient` bodies, intentionally deferred to the live smoke (Task 8 / build-time), never exercised by CI (tests use `FakeRoborockClient`). This is by design, not a gap.
- **Type consistency:** the client method surface in T3 is used verbatim by T4–T7; `Room.segment_id:int`, `status()`/`consumables()` dict keys, and timer dict keys match across tasks and the fake.
