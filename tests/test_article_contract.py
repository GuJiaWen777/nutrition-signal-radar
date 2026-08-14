from nutrition_signal_radar.article_contract import (
    canonicalize_article_url,
    external_id_for_article,
)


def test_wechat_tracking_variants_share_one_canonical_identity() -> None:
    first = (
        "http://mp.weixin.qq.com/s?__biz=abc==&mid=100&idx=1&sn=deadbeef"
        "&chksm=first&scene=1"
    )
    second = (
        "https://mp.weixin.qq.com/s?scene=2&mid=100&__biz=abc=="
        "&sn=deadbeef&idx=1&chksm=second"
    )

    first_canonical = canonicalize_article_url(first)
    second_canonical = canonicalize_article_url(second)

    assert first_canonical == second_canonical
    assert first_canonical.startswith("https://mp.weixin.qq.com/s?")
    assert "chksm" not in first_canonical
    assert external_id_for_article(
        url=first,
        account_name="测试号",
        title="测试",
        published_at=None,
        content_hash=None,
    ) == external_id_for_article(
        url=second,
        account_name="测试号",
        title="测试",
        published_at=None,
        content_hash=None,
    )


def test_path_style_wechat_share_link_drops_tracking_query() -> None:
    raw = "https://mp.weixin.qq.com/s/QIC-i874ALW48HnJyvx5Yg?scene=1#wechat_redirect"

    assert canonicalize_article_url(raw) == "https://mp.weixin.qq.com/s/QIC-i874ALW48HnJyvx5Yg"


def test_synthetic_werss_path_keeps_signed_query_for_source_traceability() -> None:
    raw = (
        "https://mp.weixin.qq.com/s/sg_4406608e27dca7b8f1a4add1"
        "?src=11&timestamp=1786643973&ver=6902&signature=signed-value&new=1"
        "#wechat_redirect"
    )

    assert canonicalize_article_url(raw) == raw.removesuffix("#wechat_redirect")
