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
