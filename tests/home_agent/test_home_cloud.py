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
