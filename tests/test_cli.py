from pathlib import Path
from switchbot_scheduler import cli

CANNED = (Path(__file__).parent / "fixtures" / "parser_living_room.json").read_text()


def test_cli_dry_run_prints_readback(monkeypatch, capsys, tmp_path):
    reg = tmp_path / "devices.yaml"
    reg.write_text("devices:\n  living_room:\n    aliases: []\n    ble_id: \"U1\"\n")
    monkeypatch.setattr(cli, "_completion_fn", lambda system, user: CANNED)
    code = cli.main(["--devices", str(reg), "--dry-run", "living room 6 to 5"])
    out = capsys.readouterr().out
    assert code == 0
    assert "living_room: on 06:00 — every day" in out


def test_cli_missing_devices_file_returns_1(capsys, tmp_path):
    missing = tmp_path / "does_not_exist.yaml"
    code = cli.main(["--devices", str(missing), "--dry-run", "living room 6 to 5"])
    assert code == 1
    err = capsys.readouterr().err
    assert err.strip() != ""   # a friendly message went to stderr
