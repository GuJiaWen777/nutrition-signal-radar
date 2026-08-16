import json
from pathlib import Path

from nutrition_signal_radar.cli import main


def test_sources_cli_seeds_disabled_priority_sites_and_lists_them(tmp_path: Path, capsys) -> None:
    root = Path(tmp_path)

    first = main(["--project-root", str(root), "sources", "seed-priority-sites", "--json"])
    seeded = json.loads(capsys.readouterr().out)
    listed_result = main(["--project-root", str(root), "sources", "list", "--json"])
    listed = json.loads(capsys.readouterr().out)

    assert first == 0
    assert seeded["inserted"] == 31
    assert listed_result == 0
    assert listed["priority_site_count"] == 31
    assert all(not site["enabled"] for site in listed["priority_sites"])


def test_sync_cli_requires_explicit_live_acknowledgement_before_creating_runtime_state(
    tmp_path: Path, capsys
) -> None:
    result = main(["--project-root", str(tmp_path), "sync", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 2
    assert payload["status"] == "protected"
    assert not (tmp_path / "var").exists()
