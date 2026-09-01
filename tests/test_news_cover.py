from pathlib import Path

import pytest
from PIL import Image

from nutrition_signal_radar.news_cover import (
    COVER_HEIGHT,
    COVER_WIDTH,
    find_headless_browser,
    render_cover_html,
    render_news_cover,
)


def test_html_template_fills_fields_and_escapes_title(tmp_path: Path) -> None:
    html_path = render_cover_html(
        tmp_path / "cover.html",
        article_title="特医食品临床使用<规范>征求意见",
        date_text="2026.09.01",
    )
    content = html_path.read_text(encoding="utf-8")

    assert "{{ARTICLE_TITLE}}" not in content
    assert "{{DATE_TEXT}}" not in content
    assert "特医食品临床使用&lt;规范&gt;征求意见" in content
    assert "2026.09.01" in content
    assert "#173B66" in content and "#72BF44" in content
    # No logo configured: the placeholder is replaced with an empty string.
    assert "{{LOGO_IMG}}" not in content
    assert "<img " not in content.split("<head>")[1].split("</head>")[0]


def test_cover_renders_exact_dimensions_with_logo_or_without(tmp_path: Path) -> None:
    logo_path = tmp_path / "logo.png"
    Image.new("RGBA", (300, 120), (23, 59, 102, 255)).save(logo_path)
    for name, logo in (("no-logo.png", None), ("with-logo.png", logo_path)):
        result = render_news_cover(
            tmp_path / name,
            article_title="特医食品临床使用迎来明确规范，行业关注执行细则与落地节奏",
            date_text="2026.09.01",
            logo_path=logo,
        )
        assert result.path.is_file()
        assert result.engine in ("html_browser", "pil_fallback")
        with Image.open(result.path) as cover:
            assert cover.format == "PNG"
            assert cover.size == (COVER_WIDTH, COVER_HEIGHT) == (1800, 766)


def test_cover_falls_back_to_pil_when_no_browser_is_available(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("nutrition_signal_radar.news_cover.find_headless_browser", lambda: None)

    result = render_news_cover(
        tmp_path / "cover.png",
        article_title="标题测试",
        date_text="2026.09.01",
    )

    assert result.engine == "pil_fallback"
    with Image.open(result.path) as cover:
        assert cover.size == (1800, 766)


def test_browser_detection_prefers_override_and_reports_none_gracefully(
    tmp_path: Path, monkeypatch
) -> None:
    fake_browser = tmp_path / "fake-browser.exe"
    fake_browser.write_bytes(b"")
    monkeypatch.setenv("RADAR_BROWSER_PATH", str(fake_browser))
    assert find_headless_browser() == fake_browser

    monkeypatch.setenv("RADAR_BROWSER_PATH", str(tmp_path / "missing.exe"))
    detected = find_headless_browser()
    if detected is None:
        pytest.skip("no local Edge/Chrome installed on this machine")
    assert detected.is_file()
