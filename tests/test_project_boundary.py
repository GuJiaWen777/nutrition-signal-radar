from pathlib import Path


def test_extension_source_has_no_weekly_publisher_or_wechat_dependency() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "nutrition_signal_radar"
    forbidden = ("earlydata_weekly_publisher", "api.weixin.qq.com")

    for path in package_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in source, f"{path.name} contains forbidden dependency {marker}"
