from switchbot_scheduler.registry import Registry, Device


def _reg():
    return Registry([
        Device(name="living_room", aliases=["living room", "סלון"], ble_id="UUID-1"),
        Device(name="ac", aliases=["air conditioner", "מזגן"], ble_id="UUID-2"),
    ])


def test_resolve_by_alias_case_insensitive():
    assert _reg().resolve("Living Room") == "living_room"
    assert _reg().resolve("סלון") == "living_room"


def test_resolve_by_canonical_name():
    assert _reg().resolve("ac") == "ac"


def test_resolve_unknown_returns_none():
    assert _reg().resolve("bedroom") is None


def test_known_names_and_ble_id():
    r = _reg()
    assert r.known_names() == ["living_room", "ac"]
    assert r.ble_id("ac") == "UUID-2"


def test_load_from_yaml(tmp_path):
    p = tmp_path / "devices.yaml"
    p.write_text(
        "devices:\n"
        "  living_room:\n"
        "    aliases: [\"salon\"]\n"
        "    ble_id: \"UUID-X\"\n"
    )
    r = Registry.load(str(p))
    assert r.resolve("salon") == "living_room"
    assert r.ble_id("living_room") == "UUID-X"
