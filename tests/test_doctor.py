import json
from pathlib import Path

from nutrition_signal_radar.cli import main
from nutrition_signal_radar.config import load_settings
from nutrition_signal_radar.doctor import build_doctor_report, render_doctor_report


def test_doctor_never_exposes_api_keys(tmp_path: Path) -> None:
    redfox_secret = "redfox-secret-value"
    llm_secret = "deepseek-secret-value"
    settings = load_settings(
        tmp_path,
        environ={
            "REDFOX_API_KEY": redfox_secret,
            "RADAR_LLM_API_KEY": llm_secret,
            "RADAR_LLM_MODEL": "configured-model",
        },
    )

    report = build_doctor_report(settings)
    rendered = render_doctor_report(report)
    serialized = json.dumps(report)

    assert redfox_secret not in rendered
    assert llm_secret not in rendered
    assert redfox_secret not in serialized
    assert llm_secret not in serialized
    assert report["capabilities"]["live_redfox_ready"] is True
    assert report["capabilities"]["llm_analysis_ready"] is True


def test_doctor_json_cli_is_machine_readable(tmp_path: Path, capsys) -> None:
    result = main(["--project-root", str(tmp_path), "doctor", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["project"] == "nutrition-signal-radar"
    assert output["capabilities"]["scaffold_ready"] is True
    assert output["capabilities"]["live_redfox_ready"] is False
    assert output["capabilities"]["llm_analysis_ready"] is False
