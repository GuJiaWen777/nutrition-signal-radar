from pathlib import Path

import pytest

from nutrition_signal_radar.config import ConfigError, load_settings


def test_load_settings_uses_extension_env_without_overriding_process_values(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "REDFOX_API_KEY=file-redfox\n"
        "RADAR_LLM_API_KEY=file-llm\n"
        "RADAR_LLM_MODEL=file-model\n",
        encoding="utf-8",
    )

    settings = load_settings(
        tmp_path,
        environ={
            "REDFOX_API_KEY": "process-redfox",
            "RADAR_LLM_API_KEY": "process-llm",
            "RADAR_LLM_MODEL": "process-model",
        },
    )

    assert settings.redfox_api_key == "process-redfox"
    assert settings.llm_api_key == "process-llm"
    assert settings.llm_model == "process-model"
    assert settings.data_dir == tmp_path / "var"
    assert settings.redfox_base_url == "https://redfox.hk"
    assert settings.redfox_timeout_seconds == 20.0
    assert settings.redfox_max_retries == 1
    assert settings.redfox_raw_directory == tmp_path / "var" / "raw" / "redfox"


def test_load_settings_rejects_data_directory_outside_extension(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="RADAR_DATA_DIR must resolve inside"):
        load_settings(tmp_path, environ={"RADAR_DATA_DIR": "../outside"})


def test_deepseek_defaults_are_non_secret_and_model_is_explicit(tmp_path: Path) -> None:
    settings = load_settings(tmp_path, environ={})

    assert settings.llm_provider == "deepseek"
    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_model == ""
    assert not settings.llm_ready


def test_redfox_http_settings_are_validated_and_normalized(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path,
        environ={
            "REDFOX_BASE_URL": "https://redfox.example/api/",
            "REDFOX_TIMEOUT_SECONDS": "4.5",
            "REDFOX_MAX_RETRIES": "2",
        },
    )

    assert settings.redfox_base_url == "https://redfox.example/api"
    assert settings.redfox_timeout_seconds == 4.5
    assert settings.redfox_max_retries == 2

    with pytest.raises(ConfigError, match="REDFOX_TIMEOUT_SECONDS"):
        load_settings(tmp_path, environ={"REDFOX_TIMEOUT_SECONDS": "0"})

    with pytest.raises(ConfigError, match="REDFOX_BASE_URL"):
        load_settings(tmp_path, environ={"REDFOX_BASE_URL": "not-a-url"})

    with pytest.raises(ConfigError, match="REDFOX_MAX_RETRIES"):
        load_settings(tmp_path, environ={"REDFOX_MAX_RETRIES": "-1"})
