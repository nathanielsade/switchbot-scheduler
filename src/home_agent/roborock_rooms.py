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
