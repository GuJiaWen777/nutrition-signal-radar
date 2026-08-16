from pathlib import Path

import pytest

from nutrition_signal_radar.redfox_gateway import RedFoxListedWork, RedFoxWideSearchPage
from nutrition_signal_radar.redfox_wide_search import (
    render_redfox_wide_search_markdown,
    search_redfox_wide_library,
    write_redfox_wide_search_report,
)


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def search_wide_articles(
        self,
        keyword: str,
        *,
        offset: int = 0,
        sort_type: str = "2",
    ) -> RedFoxWideSearchPage:
        self.calls.append({"keyword": keyword, "offset": offset, "sort_type": sort_type})
        rows = (
            RedFoxListedWork(
                payload={
                    "title": "行业观察",
                    "author": "Bonny营养领航",
                    "publishTime": "2026-08-14",
                    "workUuid": "bonny-1",
                    "workUrl": "https://mp.weixin.qq.com/s/bonny-1",
                    "content": "完整正文",
                    "readCount": "50",
                    "likeCount": "3",
                },
                raw_snapshot_path="C:/runtime/raw.json",
            ),
            RedFoxListedWork(
                payload={
                    "title": "转载提及",
                    "author": "其他账号",
                    "publishTime": "2026-08-13",
                    "workUuid": "other-1",
                },
                raw_snapshot_path="C:/runtime/raw.json",
            ),
        )
        return RedFoxWideSearchPage(
            keyword=keyword,
            sort_type=sort_type,
            works=rows,
            total=2,
            offset=offset,
            has_more=False,
            raw_snapshot_path="C:/runtime/raw.json",
        )


def test_wide_search_report_is_bounded_and_never_contains_article_bodies(tmp_path: Path) -> None:
    gateway = _Gateway()

    report = search_redfox_wide_library(
        gateway,
        keyword="Bonny营养领航",
        exact_author="Bonny营养领航",
        max_pages=1,
    )
    artifacts = write_redfox_wide_search_report(report, tmp_path / "exports")
    markdown = render_redfox_wide_search_markdown(report)

    assert gateway.calls == [
        {"keyword": "Bonny营养领航", "offset": 0, "sort_type": "2"}
    ]
    assert report.exact_author_match_count == 1
    assert report.authors[0].author == "Bonny营养领航"
    assert report.results[0].has_inline_content is True
    assert "完整正文" not in markdown
    assert "不会写入来源配置" in markdown
    assert artifacts.json_path.is_file()
    assert artifacts.markdown_path.is_file()


def test_wide_search_rejects_unbounded_or_invalid_inputs() -> None:
    gateway = _Gateway()

    with pytest.raises(ValueError, match="requires a keyword"):
        search_redfox_wide_library(gateway, keyword=" ")
    with pytest.raises(ValueError, match="max_pages"):
        search_redfox_wide_library(gateway, keyword="Bonny", max_pages=0)
    with pytest.raises(ValueError, match="sort_type"):
        search_redfox_wide_library(gateway, keyword="Bonny", sort_type="999")
