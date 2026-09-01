"""WeChat Official Account draft-box gateway (Phase 4 P4-06, ADR-0014).

Implements the approved minimal path only: fetch an access token, upload the
cover as permanent material, and write one article into the draft box
(``draft/add``).  There is deliberately no mass-send endpoint here — 群发 is
forbidden in every phase.  Publishing to followers is handled by the operator
inside WeChat (manually or via the platform's own scheduler).  Credentials
come from ``WECHAT_APP_ID`` / ``WECHAT_APP_SECRET`` in the project ``.env`` and
are never logged; errors are normalized with secrets redacted.  Callers inject
an HTTP session in tests; real requests happen only behind ``news publish
--live``.

The endpoint flow (token → material/add_material → draft/add) mirrors the
proven reference implementation in the historical Weekly Publisher codebase
that ADR-0014 approved as a read-only reference; it is re-implemented here
for this repository's gateway/error conventions.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

import requests


WECHAT_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
WECHAT_ADD_MATERIAL_URL = "https://api.weixin.qq.com/cgi-bin/material/add_material"
WECHAT_DRAFT_ADD_URL = "https://api.weixin.qq.com/cgi-bin/draft/add"
WECHAT_TIMEOUT_SECONDS = 30.0
_MAX_DRAFT_CONTENT_CHARS = 20000


class WechatGatewayError(RuntimeError):
    """A safe, normalized failure from the WeChat HTTP boundary."""


class WechatHttpSession(Protocol):
    def get(self, url: str, *, params: Mapping[str, str], timeout: float) -> object: ...

    def post(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        json: Mapping[str, object] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        timeout: float,
    ) -> object: ...


@dataclass(frozen=True)
class WechatDraftResult:
    media_id: str
    cover_media_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "media_id": self.media_id,
            "cover_media_id": self.cover_media_id,
        }


def push_news_draft(
    *,
    session: requests.Session | None = None,
    app_id: str,
    secret: str,
    title: str,
    html_content: str,
    cover_path: Path,
    digest: str = "",
    author: str = "EarlyData",
    timeout: float = WECHAT_TIMEOUT_SECONDS,
) -> WechatDraftResult:
    """Push one article into the WeChat draft box and return its media_id."""

    cleaned_app_id = str(app_id or "").strip()
    cleaned_secret = str(secret or "").strip()
    if not cleaned_app_id or not cleaned_secret:
        raise WechatGatewayError("WECHAT_APP_ID 和 WECHAT_APP_SECRET 必须先在 .env 中配置")
    cleaned_title = str(title or "").strip()
    if not cleaned_title:
        raise WechatGatewayError("草稿标题不能为空")
    content = str(html_content or "").strip()
    if not content:
        raise WechatGatewayError("草稿正文不能为空")
    if len(content) > _MAX_DRAFT_CONTENT_CHARS:
        raise WechatGatewayError(
            f"草稿正文超过微信限制（{len(content)} > {_MAX_DRAFT_CONTENT_CHARS} 字符）"
        )
    cover = Path(cover_path)
    if not cover.is_file():
        raise WechatGatewayError(f"封面文件不存在：{cover}（请先重新 build 生成 cover.png）")

    http = session if session is not None else requests.Session()
    token = _get_access_token(http, cleaned_app_id, cleaned_secret, timeout)
    cover_media_id = _upload_cover(http, cover, token, timeout)
    media_id = _add_draft(
        http,
        token,
        title=cleaned_title,
        author=str(author or "EarlyData").strip() or "EarlyData",
        digest=str(digest or "").strip(),
        content=content,
        cover_media_id=cover_media_id,
        timeout=timeout,
    )
    return WechatDraftResult(media_id=media_id, cover_media_id=cover_media_id)


def _get_access_token(http, app_id: str, secret: str, timeout: float) -> str:
    try:
        response = http.get(
            WECHAT_TOKEN_URL,
            params={"grant_type": "client_credential", "appid": app_id, "secret": secret},
            timeout=timeout,
        )
    except Exception as exc:
        raise WechatGatewayError(f"获取 access_token 失败：{type(exc).__name__}") from exc
    payload = _as_json(response, "获取 access_token")
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise WechatGatewayError(
            f"获取 access_token 失败：{_errmsg(payload, secret)}"
        )
    return token


def _upload_cover(http, cover_path: Path, token: str, timeout: float) -> str:
    content = cover_path.read_bytes()
    filename = cover_path.name
    content_type = mimetypes.guess_type(filename)[0] or "image/png"
    try:
        response = http.post(
            WECHAT_ADD_MATERIAL_URL,
            params={"access_token": token, "type": "image"},
            files={"media": (filename, content, content_type)},
            timeout=timeout,
        )
    except Exception as exc:
        raise WechatGatewayError(f"封面上传失败：{type(exc).__name__}") from exc
    payload = _as_json(response, "封面上传")
    media_id = str(payload.get("media_id") or "").strip()
    if not media_id:
        raise WechatGatewayError(f"封面上传失败：{_errmsg(payload)}")
    return media_id


def _add_draft(
    http,
    token: str,
    *,
    title: str,
    author: str,
    digest: str,
    content: str,
    cover_media_id: str,
    timeout: float,
) -> str:
    payload_articles = {
        "articles": [
            {
                "title": title,
                "author": author,
                "digest": digest,
                "content": content,
                "thumb_media_id": cover_media_id,
                "need_open_comment": 0,
                "only_fans_can_comment": 0,
            }
        ]
    }
    try:
        response = http.post(
            WECHAT_DRAFT_ADD_URL,
            params={"access_token": token},
            json=payload_articles,
            timeout=timeout,
        )
    except Exception as exc:
        raise WechatGatewayError(f"写入草稿箱失败：{type(exc).__name__}") from exc
    payload = _as_json(response, "写入草稿箱")
    media_id = str(payload.get("media_id") or "").strip()
    if not media_id:
        raise WechatGatewayError(f"写入草稿箱失败：{_errmsg(payload)}")
    return media_id


def _as_json(response: object, action: str) -> dict:
    try:
        payload = response.json()
    except Exception as exc:
        preview = str(getattr(response, "text", ""))[:300]
        raise WechatGatewayError(f"{action}返回非 JSON 内容：{preview}") from exc
    if not isinstance(payload, dict):
        raise WechatGatewayError(f"{action}返回结构异常")
    return payload


def _errmsg(payload: dict, secret: str | None = None) -> str:
    message = str(payload.get("errmsg") or f"errcode={payload.get('errcode')}")
    if secret:
        message = message.replace(secret, "[redacted]")
    return message[:300]


__all__ = [
    "WechatDraftResult",
    "WechatGatewayError",
    "push_news_draft",
]
