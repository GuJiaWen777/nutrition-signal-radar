import json
from pathlib import Path

from nutrition_signal_radar.cli import main


def test_database_cli_initializes_and_reports_status(tmp_path: Path, capsys) -> None:
    result = main(["--project-root", str(tmp_path), "db", "init", "--json"])
    init_payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert init_payload["starting_version"] == 0
    assert init_payload["ending_version"] == 8
    assert init_payload["applied_versions"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert (tmp_path / "var" / "radar.sqlite3").is_file()

    result = main(["--project-root", str(tmp_path), "db", "status", "--json"])
    status_payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert status_payload["state"] == "up_to_date"
    assert status_payload["current_version"] == 8
    assert status_payload["latest_version"] == 8
