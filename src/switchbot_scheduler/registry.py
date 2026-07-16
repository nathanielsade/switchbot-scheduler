from dataclasses import dataclass
import yaml


@dataclass
class Device:
    name: str
    aliases: list[str]
    ble_id: str
    inverted: bool = False
    mode: str = "switch"   # "switch" (on/off) or "press" (momentary toggle)
    cloud_id: str = ""


class Registry:
    def __init__(self, devices: list[Device]):
        self.devices = devices
        self._by_name = {d.name: d for d in devices}
        self._alias_map: dict[str, str] = {}
        for d in devices:
            self._alias_map[d.name.lower()] = d.name
            for a in d.aliases:
                self._alias_map[a.strip().lower()] = d.name

    @classmethod
    def load(cls, path: str) -> "Registry":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        devices = [
            Device(
                name=name,
                aliases=cfg.get("aliases", []),
                ble_id=cfg.get("ble_id", ""),
                inverted=cfg.get("inverted", False),
                mode=cfg.get("mode", "switch"),
                cloud_id=cfg.get("cloud_id", ""),
            )
            for name, cfg in data["devices"].items()
        ]
        return cls(devices)

    def resolve(self, spoken: str) -> str | None:
        return self._alias_map.get(spoken.strip().lower())

    def known_names(self) -> list[str]:
        return [d.name for d in self.devices]

    def ble_id(self, name: str) -> str:
        return self._by_name[name].ble_id

    def is_cloud(self, name: str) -> bool:
        return bool(self._by_name[name].cloud_id)

    def cloud_id(self, name: str) -> str:
        return self._by_name[name].cloud_id

    def is_inverted(self, name: str) -> bool:
        return self._by_name[name].inverted

    def is_press_mode(self, name: str) -> bool:
        return self._by_name[name].mode == "press"
