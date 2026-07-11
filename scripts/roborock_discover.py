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
