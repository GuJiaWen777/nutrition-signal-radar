from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import RadarSettings
from .news_registry import news_fetch_freshness
from .schema import inspect_schema


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    summary: str


def build_doctor_report(settings: RadarSettings) -> dict[str, Any]:
    python_ok = sys.version_info >= (3, 10)
    boundary_ok = _is_inside(settings.data_dir, settings.project_root)
    writable_root = _nearest_existing_parent(settings.data_dir)
    writable_ok = writable_root.is_dir() and os.access(writable_root, os.W_OK)
    pi_path = shutil.which("pi") or shutil.which("pi.exe") or ""
    pytest_available = importlib.util.find_spec("pytest") is not None
    schema_status = inspect_schema(settings.database_path)
    freshness = news_fetch_freshness(settings.database_path)

    llm_status = "ok" if settings.llm_ready else "missing"
    if not settings.llm_ready and any((settings.llm_api_key, settings.llm_model)):
        llm_status = "warning"

    checks = [
        DoctorCheck(
            "python",
            "ok" if python_ok else "error",
            f"Python {platform.python_version()} (requires 3.10+)",
        ),
        DoctorCheck(
            "project_boundary",
            "ok" if boundary_ok else "error",
            "runtime data stays inside the extension" if boundary_ok else "runtime data escapes the extension",
        ),
        DoctorCheck(
            "data_directory",
            "ok" if writable_ok else "error",
            f"{settings.data_dir} ({'writable' if writable_ok else 'not writable'})",
        ),
        DoctorCheck(
            "database",
            _database_check_status(schema_status.state),
            f"{schema_status.detail} ({settings.database_path})",
        ),
        DoctorCheck(
            "news_fetch",
            _news_fetch_status(freshness),
            _news_fetch_summary(freshness),
        ),
        DoctorCheck(
            "redfox",
            "ok" if settings.redfox_ready else "missing",
            "API key configured" if settings.redfox_ready else "REDFOX_API_KEY is not configured",
        ),
        DoctorCheck(
            "llm",
            llm_status,
            _llm_summary(settings),
        ),
        DoctorCheck(
            "pi_cli",
            "ok" if pi_path else "missing",
            pi_path or "pi coding-agent CLI is not available on PATH",
        ),
        DoctorCheck(
            "pytest",
            "ok" if pytest_available else "missing",
            "available" if pytest_available else "install the extension dev dependencies",
        ),
    ]

    scaffold_ready = python_ok and boundary_ok and writable_ok
    return {
        "project": "nutrition-signal-radar",
        "project_root": str(settings.project_root),
        "checks": [asdict(check) for check in checks],
        "capabilities": {
            "scaffold_ready": scaffold_ready,
            "live_redfox_ready": scaffold_ready and settings.redfox_ready,
            "llm_analysis_ready": scaffold_ready and settings.llm_ready,
            "pi_available": bool(pi_path),
            "tests_ready": pytest_available,
        },
    }


def render_doctor_report(report: dict[str, Any]) -> str:
    labels = {"ok": "OK", "missing": "MISSING", "warning": "WARNING", "error": "ERROR"}
    lines = ["Nutrition Signal Radar doctor", f"Project root: {report['project_root']}"]
    for check in report["checks"]:
        label = labels.get(str(check["status"]), str(check["status"]).upper())
        lines.append(f"[{label}] {check['name']}: {check['summary']}")
    capabilities = report["capabilities"]
    lines.append(
        "Capabilities: "
        + ", ".join(f"{name}={'yes' if enabled else 'no'}" for name, enabled in capabilities.items())
    )
    return "\n".join(lines)


def _llm_summary(settings: RadarSettings) -> str:
    missing: list[str] = []
    if not settings.llm_api_key:
        missing.append("RADAR_LLM_API_KEY")
    if not settings.llm_model:
        missing.append("RADAR_LLM_MODEL")
    if not settings.llm_base_url:
        missing.append("RADAR_LLM_BASE_URL")
    if missing:
        return f"provider={settings.llm_provider or 'unset'}; missing {', '.join(missing)}"
    return f"provider={settings.llm_provider}; model={settings.llm_model}; base_url={settings.llm_base_url}"


def _news_fetch_status(freshness: dict[str, Any]) -> str:
    state = str(freshness.get("state") or "")
    if state == "not_initialized":
        return "missing"
    if state == "stale":
        return "warning"
    return "ok"


def _news_fetch_summary(freshness: dict[str, Any]) -> str:
    state = str(freshness.get("state") or "")
    if state == "not_initialized":
        return "资讯线尚未初始化（news seed-sources）"
    if state == "no_sources":
        return "没有启用的资讯信源"
    age = freshness.get("age_hours")
    age_text = f"{age}h 前" if age is not None else "从未抓取"
    enabled = int(freshness.get("enabled_count") or 0)
    if state == "stale":
        return (
            f"最近抓取 {age_text}，超过 {freshness.get('stale_hours')}h 阈值；"
            "请检查任务计划程序"
        )
    return f"最近抓取 {age_text}；{enabled} 个启用源"


def _database_check_status(state: str) -> str:
    return {
        "up_to_date": "ok",
        "not_initialized": "missing",
        "outdated": "warning",
        "invalid": "error",
    }.get(state, "error")


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate
