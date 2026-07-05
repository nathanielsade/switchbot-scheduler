import argparse
import sys
from .registry import Registry
from .core import apply_schedule
from .ble_writer import write_schedule

_completion_fn = None  # tests override; None => parser uses its OpenAI default


def _confirm(text: str) -> bool:
    print("\nGoing to write:\n" + text)
    return input("\nProceed? [y/N] ").strip().lower() == "y"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="switchbot-schedule")
    ap.add_argument("prompt", help="schedule in plain Hebrew/English")
    ap.add_argument("--devices", default="devices.yaml")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--write", dest="dry_run", action="store_false")
    args = ap.parse_args(argv)

    registry = Registry.load(args.devices)
    try:
        outcome, text, _ = apply_schedule(
            args.prompt, registry,
            dry_run=args.dry_run, confirm=_confirm,
            writer=write_schedule, completion_fn=_completion_fn,
        )
    except Exception as err:  # ScheduleError and friends -> friendly message
        print(f"⚠️  {err}", file=sys.stderr)
        return 1

    if outcome == "dry_run":
        print("[DRY RUN] would write:\n" + text)
    elif outcome == "cancelled":
        print("Cancelled — nothing written.")
    else:
        print("✅ Written to the Bots:\n" + text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
