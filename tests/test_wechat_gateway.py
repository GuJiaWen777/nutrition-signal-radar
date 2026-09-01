import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nutrition_signal_radar import cli
from nutrition_signal_radar.cli import main
from nutrition_signal_radar.news_compose import build_news_article
from nutrition_signal_radar.wechat_gateway import (
    WechatGatewayError,
    push_news_draft,
)
from tests.test_news_compose import (
    FakeComposeGateway,
    _seed_four_selected_items,
    _valid_payload,
)
from tests.test_site_fetch_gateway import NOW


class FakeWechatResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict:
        return self._payload


class FakeWechatSession:
    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict, dict | None, dict | None]] = []
        self.responses: list[FakeWechatResponse] = []

    def queue(self, payload: dict) -> None:
        self.responses.append(FakeWechatResponse(payload))

    def get(self, url: str, *, params: dict, timeout: float) -> FakeWechatResponse:
        self.get_calls.append((url, dict(params)))
        return self.responses.pop(0)

    def post(
        self,
        url: str,
        *,
        params: dict,
        json: dict | None = None,
        files: dict | None = None,
        timeout: float,
    ) -> FakeWechatResponse:
        self.post_calls.append((url, dict(params), json, dict(files) if files else None))
        return self.responses.pop(0)


def _cover(tmp_path: Path) -> Path:
    from PIL import Image

    cover = tmp_path / "cover.png"
    Image.new("RGB", (1800, 766), (240, 244, 248)).save(cover)
    return cover


def test_push_news_draft_calls_token_material_and_draft_in_order(tmp_path: Path) -> None:
    session = FakeWechatSession()
    session.queue({"access_token": "TOKEN-1"})
    session.queue({"media_id": "COVER-MEDIA"})
    session.queue({"media_id": "DRAFT-MEDIA"})

    result = push_news_draft(
        session=session,
        app_id="wx-app",
        secret="super-secret",
        title="营养健康行业资讯｜测试",
        html_content="<section>正文</section>",
        cover_path=_cover(tmp_path),
        digest="导语",
    )

    assert result.media_id == "DRAFT-MEDIA"
    assert result.cover_media_id == "COVER-MEDIA"
    token_url, token_params = session.get_calls[0]
    assert token_url == "https://api.weixin.qq.com/cgi-bin/token"
    assert token_params["appid"] == "wx-app"
    assert token_params["secret"] == "super-secret"

    material_url, material_params, _, material_files = session.post_calls[0]
    assert material_url == "https://api.weixin.qq.com/cgi-bin/material/add_material"
    assert material_params["access_token"] == "TOKEN-1"
    upload_name, upload_bytes, upload_type = material_files["media"]
    assert upload_name == "cover.png"
    assert upload_bytes.startswith(b"\x89PNG")
    assert upload_type == "image/png"

    draft_url, draft_params, draft_json, _ = session.post_calls[1]
    assert draft_url == "https://api.weixin.qq.com/cgi-bin/draft/add"
    assert draft_params["access_token"] == "TOKEN-1"
    article = draft_json["articles"][0]
    assert article["title"] == "营养健康行业资讯｜测试"
    assert article["thumb_media_id"] == "COVER-MEDIA"
    assert article["content"] == "<section>正文</section>"


def test_push_news_draft_normalizes_errors_and_redacts_secret(tmp_path: Path) -> None:
    session = FakeWechatSession()
    session.queue({"errcode": 40013, "errmsg": "invalid appid secret=super-secret"})

    with pytest.raises(WechatGatewayError, match="redacted"):
        push_news_draft(
            session=session,
            app_id="wx-app",
            secret="super-secret",
            title="t",
            html_content="<p>body</p>",
            cover_path=_cover(tmp_path),
        )

    with pytest.raises(WechatGatewayError, match="WECHAT_APP_ID"):
        push_news_draft(
            session=session,
            app_id="",
            secret="",
            title="t",
            html_content="<p>body</p>",
            cover_path=_cover(tmp_path),
        )


def test_news_publish_cli_gates_and_records_media_id(tmp_path: Path, capsys, monkeypatch) -> None:
    database_path = tmp_path / "var" / "radar.sqlite3"
    _seed_four_selected_items(database_path)
    # Build with a fake gateway through the compose API (CLI LLM wiring is
    # covered in test_news_compose); only the publish path is under test here.
    result = build_news_article(
        database_path,
        FakeComposeGateway(lambda _prompt: _valid_payload()),
        exports_dir=tmp_path / "var" / "exports" / "news-articles",
        llm_model="deepseek-chat",
        now=NOW,
    )
    assert result.status == "built"

    protected_code = main(
        ["--project-root", str(tmp_path), "news", "publish", "--slug", result.slug, "--version", "1", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert protected_code == 2
    assert payload["status"] == "protected"

    # --live alone is not enough: publishing always needs the explicit human
    # confirmation flag, so no script or scheduled chain can push a draft.
    unconfirmed_code = main(
        [
            "--project-root",
            str(tmp_path),
            "news",
            "publish",
            "--live",
            "--slug",
            result.slug,
            "--version",
            "1",
            "--json",
        ]
    )
    unconfirmed = json.loads(capsys.readouterr().out)
    assert unconfirmed_code == 2
    assert "human decision" in unconfirmed["message"]

    no_creds_code = main(
        [
            "--project-root",
            str(tmp_path),
            "news",
            "publish",
            "--live",
            "--confirm",
            "--slug",
            result.slug,
            "--version",
            "1",
            "--json",
        ]
    )
    assert no_creds_code == 1  # credentials not configured in the temp root

    monkeypatch.setenv("WECHAT_APP_ID", "wx-app")
    monkeypatch.setenv("WECHAT_APP_SECRET", "super-secret")

    def fake_push(**kwargs):
        assert kwargs["app_id"] == "wx-app"
        assert Path(kwargs["cover_path"]).is_file()
        return SimpleNamespace(
            media_id="DRAFT-MEDIA-1",
            cover_media_id="COVER-MEDIA-1",
            as_dict=lambda: {"media_id": "DRAFT-MEDIA-1", "cover_media_id": "COVER-MEDIA-1"},
        )

    monkeypatch.setattr(cli, "push_news_draft", fake_push)
    published_code = main(
        [
            "--project-root",
            str(tmp_path),
            "news",
            "publish",
            "--live",
            "--confirm",
            "--slug",
            result.slug,
            "--version",
            "1",
            "--json",
        ]
    )
    published = json.loads(capsys.readouterr().out)
    assert published_code == 0
    assert published["media_id"] == "DRAFT-MEDIA-1"

    # A second publish attempt refuses to duplicate the draft.
    again_code = main(
        [
            "--project-root",
            str(tmp_path),
            "news",
            "publish",
            "--live",
            "--confirm",
            "--slug",
            result.slug,
            "--version",
            "1",
            "--json",
        ]
    )
    assert again_code == 1

    from nutrition_signal_radar.database import connect_database

    connection = connect_database(database_path, readonly=True)
    try:
        row = connection.execute(
            "SELECT wechat_media_id, wechat_pushed_at FROM news_articles WHERE slug = ? AND version = 1",
            (result.slug,),
        ).fetchone()
        assert row["wechat_media_id"] == "DRAFT-MEDIA-1"
        assert row["wechat_pushed_at"]
    finally:
        connection.close()
