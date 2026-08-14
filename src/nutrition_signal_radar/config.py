from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class ConfigError(ValueError):
    """Raised when extension configuration violates its project boundary."""


@dataclass(frozen=True)
class RadarSettings:
    project_root: Path
    data_dir: Path
    database_path: Path
    redfox_api_key: str
    llm_provider: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str

    @property
    def redfox_ready(self) -> bool:
        return bool(self.redfox_api_key)

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_provider and self.llm_api_key and self.llm_base_url and self.llm_model)

    @property
    def demo_database_path(self) -> Path:
        """Keep disposable demo state separate from the future RedFox-backed database."""
        return self.data_dir / "demo" / "radar.sqlite3"

    @property
    def we_mprss_pilot_database_path(self) -> Path:
        """Keep local WeRSS pilot data separate from the Excel demo and formal DB."""
        return self.data_dir / "we-mp-rss-pilot" / "radar.sqlite3"

    @property
    def native_we_mprss_database_path(self) -> Path:
        """Default local database for the isolated native WeRSS runtime."""
        return (
            self.data_dir
            / "native-we-mp-rss-runtime"
            / "source"
            / "data"
            / "we_mp_rss.db"
        )


def load_settings(
    project_root: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> RadarSettings:
    root = (project_root or DEFAULT_PROJECT_ROOT).resolve()
    values = dict(os.environ if environ is None else environ)
    _load_env_file(root / ".env", values)

    data_dir = _resolve_inside(root, values.get("RADAR_DATA_DIR", "var"), name="RADAR_DATA_DIR")
    provider = values.get("RADAR_LLM_PROVIDER", "deepseek").strip().lower()
    base_url = values.get("RADAR_LLM_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).strip().rstrip("/")

    return RadarSettings(
        project_root=root,
        data_dir=data_dir,
        database_path=data_dir / "radar.sqlite3",
        redfox_api_key=values.get("REDFOX_API_KEY", "").strip(),
        llm_provider=provider,
        llm_api_key=values.get("RADAR_LLM_API_KEY", "").strip(),
        llm_base_url=base_url,
        llm_model=values.get("RADAR_LLM_MODEL", "").strip(),
    )


def _load_env_file(path: Path, values: dict[str, str]) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values.setdefault(key, value.strip().strip('"').strip("'"))


def _resolve_inside(root: Path, value: str, *, name: str) -> Path:
    raw_path = Path(str(value).strip() or "var")
    candidate = (raw_path if raw_path.is_absolute() else root / raw_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ConfigError(f"{name} must resolve inside {root}") from exc
    return candidate
