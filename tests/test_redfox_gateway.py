from pathlib import Path

import pytest

from nutrition_signal_radar.redfox_gateway import RedFoxGateway, RedFoxGatewayError


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _NonJsonResponse(_Response):
    def __init__(self, status_code: int, text: str) -> None:
        super().__init__(status_code, None)
        self.text = text

    def json(self) -> object:
        raise ValueError("not JSON")


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> _Response:
        self.calls.append(
            {"url": url, "headers": dict(headers), "json": dict(json), "timeout": timeout}
        )
        return self._responses.pop(0)


def _ok(data: dict[str, object]) -> _Response:
    return _Response(200, {"code": 2000, "msg": "success", "data": data})


def test_gateway_searches_pages_fetches_missing_bodies_and_saves_safe_raw_responses(
    tmp_path: Path,
) -> None:
    api_key = "unit-test-redfox-secret"
    session = _Session(
        [
            _ok(
                {
                    "list": [
                        {
                            "account": "wx_account_a",
                            "accountName": "庶正康讯",
                            "verifyInfo": "企业认证",
                            "redfoxIndex": "88.5",
                            "lastPublishTime": "2026-08-14 08:00:00",
                        }
                    ]
                }
            ),
            _ok(
                {
                    "total": 3,
                    "hasMore": True,
                    "list": [
                        {
                            "workUuid": "work-a",
                            "title": "监管新规解读",
                            "summary": "正文待补全",
                            "workUrl": "https://mp.weixin.qq.com/s/work-a?from=tracking",
                            "publishTime": "2026-08-13 10:00:00",
                            "readCount": "12",
                            "likeCount": "2",
                        },
                        {
                            "workUuid": "work-b",
                            "title": "功能食品新品",
                            "content": "完整正文 B",
                            "workUrl": "https://mp.weixin.qq.com/s/work-b?from=tracking",
                            "publishTime": "2026-08-12 10:00:00",
                            "readCount": "8",
                            "shareCount": "3",
                        },
                    ],
                }
            ),
            _ok(
                {
                    "total": 3,
                    "hasMore": False,
                    "list": [
                        {
                            "workUuid": "work-c",
                            "title": "监管新规解读（转载）",
                            "summary": "转载摘要",
                            "workUrl": "https://mp.weixin.qq.com/s/work-a?from=other",
                            "publishTime": "2026-08-13 11:00:00",
                            "readCount": "40",
                        }
                    ],
                }
            ),
            _ok(
                {
                    "workUuid": "work-a",
                    "content": "完整正文 A",
                    "readCount": "20",
                    "watchCount": "4",
                }
            ),
        ]
    )
    gateway = RedFoxGateway(
        api_key,
        session=session,
        raw_snapshot_directory=tmp_path / "raw" / "redfox",
    )

    search = gateway.search_accounts("庶正康讯")
    batch = gateway.fetch_account_batch(
        account="wx_account_a",
        account_name="庶正康讯",
        publish_time_start="2026-07-16",
        publish_time_end="2026-08-14",
        max_pages=2,
        max_detail_requests=1,
    )

    assert search.candidates[0].account == "wx_account_a"
    assert search.candidates[0].redfox_index == 88.5
    assert [call["json"]["offset"] for call in session.calls[1:3]] == [0, 20]
    assert session.calls[0]["headers"]["REDFOX_API_KEY"] == api_key
    assert batch.total == 3
    assert batch.pages_fetched == 2
    assert batch.pagination_truncated is False
    assert batch.detail_requests == 1
    assert batch.detail_successes == 1
    assert batch.batch.source_rows == 3
    assert batch.batch.collapsed_rows == 1
    assert len(batch.batch.articles) == 2
    assert batch.articles_with_full_body == 2

    first = next(article for article in batch.batch.articles if article.external_id == "redfox:work-a")
    assert first.content_snapshot == "完整正文 A"
    assert first.url == "https://mp.weixin.qq.com/s/work-a"
    assert first.read_count == 40
    assert first.recommend_count == 4
    assert first.metadata["redfox_body_state"] == "detail"

    raw_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "raw").rglob("*.json")
    )
    assert api_key not in raw_text
    assert "完整正文 A" in raw_text


def test_gateway_redacts_provider_error_messages_and_raw_payloads(tmp_path: Path) -> None:
    api_key = "unit-test-redfox-secret"
    session = _Session(
        [
            _Response(
                401,
                {"code": 4001, "msg": f"invalid key {api_key}", "data": {}},
            )
        ]
    )
    gateway = RedFoxGateway(
        api_key,
        session=session,
        raw_snapshot_directory=tmp_path / "raw" / "redfox",
    )

    with pytest.raises(RedFoxGatewayError) as captured:
        gateway.search_accounts("庶正康讯")

    assert api_key not in str(captured.value)
    assert "[redacted]" in str(captured.value)
    raw_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "raw").rglob("*.json")
    )
    assert api_key not in raw_text
    assert "[redacted]" in raw_text


def test_gateway_retries_one_transient_gateway_failure(tmp_path: Path) -> None:
    session = _Session(
        [
            _NonJsonResponse(502, "upstream temporarily unavailable"),
            _ok({"list": []}),
        ]
    )
    gateway = RedFoxGateway(
        "configured-key",
        session=session,
        raw_snapshot_directory=tmp_path / "raw" / "redfox",
        max_retries=1,
    )

    result = gateway.search_accounts("庶正康讯")

    assert result.candidates == ()
    assert len(session.calls) == 2
    raw_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "raw").rglob("*.json")
    )
    assert "upstream temporarily unavailable" in raw_text


def test_gateway_rejects_non_success_envelopes_without_network_side_effects() -> None:
    gateway = RedFoxGateway("configured-key", session=_Session([]))

    with pytest.raises(RedFoxGatewayError, match="requires a keyword"):
        gateway.search_accounts(" ")


def test_gateway_searches_broad_library_and_filters_exact_author_for_a_batch(
    tmp_path: Path,
) -> None:
    session = _Session(
        [
            _ok(
                {
                    "total": 21,
                    "list": [
                        {
                            "workUuid": "bonny-a",
                            "title": "Bonny 的营养观察",
                            "author": "Bonny营养领航",
                            "content": "完整正文",
                            "workUrl": "https://mp.weixin.qq.com/s/bonny-a?from=tracking",
                            "publishTime": "2026-08-14",
                            "readCount": "123",
                            "likeCount": "4",
                        },
                        {
                            "workUuid": "mention-only",
                            "title": "提及 Bonny营养领航 的转载",
                            "author": "其他来源",
                            "content": "不是该账号的文章",
                            "workUrl": "https://mp.weixin.qq.com/s/mention-only",
                            "publishTime": "2026-08-14",
                        },
                    ],
                }
            )
        ]
    )
    gateway = RedFoxGateway(
        "configured-key",
        session=session,
        raw_snapshot_directory=tmp_path / "raw" / "redfox",
    )

    page = gateway.search_wide_articles("Bonny营养领航", sort_type="2")

    assert page.total == 21
    assert page.has_more is True
    assert session.calls[0]["url"].endswith("/story/api/gzh/data/searchArticle")
    assert session.calls[0]["json"] == {
        "keyword": "Bonny营养领航",
        "offset": 0,
        "sortType": "2",
    }

    session = _Session(
        [
            _ok(
                {
                    "total": 2,
                    "list": [
                        {
                            "workUuid": "bonny-a",
                            "title": "Bonny 的营养观察",
                            "author": "Bonny营养领航",
                            "content": "完整正文",
                            "workUrl": "https://mp.weixin.qq.com/s/bonny-a?from=tracking",
                            "publishTime": "2026-08-14",
                            "readCount": "123",
                        },
                        {
                            "workUuid": "old-bonny",
                            "title": "旧文章",
                            "author": "Bonny营养领航",
                            "content": "旧正文",
                            "workUrl": "https://mp.weixin.qq.com/s/old-bonny",
                            "publishTime": "2026-07-01",
                        },
                    ],
                }
            )
        ]
    )
    gateway = RedFoxGateway("configured-key", session=session)

    batch = gateway.fetch_wide_account_batch(
        account_name="Bonny营养领航",
        publish_time_start="2026-08-01",
        publish_time_end="2026-08-14",
        max_pages=1,
    )

    assert batch.batch.source_kind == "redfox_wide"
    assert batch.batch.source_rows == 1
    assert batch.batch.articles[0].external_id == "redfox:bonny-a"
    assert batch.batch.articles[0].url == "https://mp.weixin.qq.com/s/bonny-a"
    assert batch.batch.articles[0].metadata["redfox_library"] == "wide"
