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
