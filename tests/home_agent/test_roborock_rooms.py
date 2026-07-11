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
