from datetime import datetime
from pathlib import Path

import pytest

from nutrition_signal_radar.xlsx_gateway import XlsxImportError, normalize_xlsx_rows


HEADERS = (
    "序号",
    "类别",
    "标题",
    "媒体名称",
    "公众号",
    "作者",
    "日期",
    "话题",
    "一级分类",
    "二级分类",
    "三级分类",
    "链接",
    "阅读量",
    "点赞量",
    "分享",
    "推荐",
    "文章内容",
)


def _row(*, sequence: int, url: str, reads: int, likes: int) -> tuple[object, ...]:
    return (
        sequence,
        "P&B",
        "同一篇文章",
        None,
        "示例公众号",
        "作者",
        datetime(2026, 3, 8, 9, 30),
        None,
        None,
        None,
        None,
        url,
        reads,
        likes,
        3,
        1,
        "用于测试的完整正文",
    )


def test_rows_are_normalized_and_tracking_duplicates_are_collapsed(tmp_path: Path) -> None:
    common = "__biz=abc==&mid=100&idx=1&sn=deadbeef"
    rows = (
        (2, _row(sequence=1, url=f"http://mp.weixin.qq.com/s?{common}&chksm=a", reads=10, likes=2)),
        (3, _row(sequence=2, url=f"https://mp.weixin.qq.com/s?{common}&chksm=b", reads=25, likes=4)),
    )

    batch = normalize_xlsx_rows(
        HEADERS,
        rows,
        source_path=tmp_path / "source.xlsx",
        source_fingerprint="fingerprint",
    )

    assert batch.source_rows == 2
    assert batch.skipped_rows == 0
    assert batch.collapsed_rows == 1
    assert len(batch.articles) == 1
    article = batch.articles[0]
    assert article.published_at == "2026-03-08T09:30:00+08:00"
    assert article.read_count == 25
    assert article.like_count == 4
    assert article.metadata["source_rows"] == [2, 3]
    assert len(article.metadata["original_urls"]) == 2
    assert "chksm" not in article.url


def test_missing_required_header_is_rejected(tmp_path: Path) -> None:
    headers = tuple(header for header in HEADERS if header != "文章内容")

    with pytest.raises(XlsxImportError, match="文章内容"):
        normalize_xlsx_rows(
            headers,
            (),
            source_path=tmp_path / "source.xlsx",
            source_fingerprint="fingerprint",
        )


def test_invalid_rows_are_skipped_without_echoing_article_content(tmp_path: Path) -> None:
    values = list(_row(sequence=1, url="", reads=0, likes=0))
    values[2] = None

    batch = normalize_xlsx_rows(
        HEADERS,
        ((12, tuple(values)),),
        source_path=tmp_path / "source.xlsx",
        source_fingerprint="fingerprint",
    )

    assert batch.articles == ()
    assert batch.skipped_rows == 1
    assert batch.issues[0].row_number == 12
    assert batch.issues[0].reason == "missing title"
