import re
from pathlib import Path


DEMO_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "demos"
    / "earlydata-topic-board-manager-demo.html"
)


def test_manager_demo_is_one_portable_offline_html_file() -> None:
    html = DEMO_PATH.read_text(encoding="utf-8")

    assert html.startswith("<!doctype html>")
    assert DEMO_PATH.stat().st_size < 1_000_000
    assert "<style>" in html
    assert "<script>" in html
    assert not re.search(r"<script\s+[^>]*src=", html, flags=re.IGNORECASE)
    assert not re.search(r"<link\s+[^>]*stylesheet", html, flags=re.IGNORECASE)
    assert not re.search(r"\b(fetch|XMLHttpRequest|WebSocket)\b", html)
    assert "http://" not in html
    assert "https://" not in html


def test_manager_demo_contains_the_clickable_topic_to_research_story() -> None:
    html = DEMO_PATH.read_text(encoding="utf-8")

    for expected in (
        "选题看板",
        "研究任务",
        "点击查看判断依据",
        "文章结构",
        "研究任务书",
        "人工确认并进入待启动",
        "不会执行研究",
        "不联网、不保存",
        "重置演示",
    ):
        assert expected in html

    ids = re.findall(r'\sid="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    close_targets = set(re.findall(r'data-close="([^"]+)"', html))
    assert close_targets <= set(ids)
