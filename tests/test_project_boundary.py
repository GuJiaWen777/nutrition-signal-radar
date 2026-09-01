from pathlib import Path


def test_extension_source_has_no_weekly_publisher_dependency() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "nutrition_signal_radar"

    for path in package_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "earlydata_weekly_publisher" not in source, (
            f"{path.name} contains forbidden Weekly Publisher dependency"
        )


def test_wechat_api_references_live_only_in_the_draft_gateway() -> None:
    """ADR-0014: WeChat API calls are allowed only in the news-line gateway.

    The gateway implements the approved draft-box path exclusively; mass-send
    endpoints are forbidden everywhere in every phase.
    """

    package_root = Path(__file__).resolve().parents[1] / "src" / "nutrition_signal_radar"
    allowed_file = "wechat_gateway.py"
    mass_send_markers = (
        "masssend",
        "mass/send",
        "sendall",
        "freepublish",
        "message/custom",
    )

    for path in package_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "api.weixin.qq.com" in source:
            assert path.name == allowed_file, (
                f"{path.name} references WeChat APIs outside the approved draft gateway"
            )
        for marker in mass_send_markers:
            assert marker not in source, f"{path.name} contains forbidden mass-send marker {marker}"
