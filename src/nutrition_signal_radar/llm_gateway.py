"""OpenAI-compatible analysis gateway for the configurable DeepSeek backend.

The workbench treats the LLM strictly as a local analysis service: it only
turns provided discovery leads into structured interpretation and never adds
facts, browses the web, or changes editorial state.  Credentials come from the
project ``.env`` (RADAR_LLM_*) and are never logged or persisted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Protocol

import requests


DEFAULT_LLM_TIMEOUT_SECONDS = 120.0
DEFAULT_LLM_MAX_RETRIES = 1
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


class LlmGatewayError(RuntimeError):
    """A safe, normalized failure from the LLM HTTP boundary."""


class LlmHttpSession(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
        timeout: float,
    ) -> object: ...


@dataclass(frozen=True)
class LlmAnalysisResult:
    status: str
    content: str
    prompt_tokens: int
    completion_tokens: int
    error: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "content": self.content,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "error": self.error,
        }


class LlmGateway:
    """Talk to one OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_LLM_MAX_RETRIES,
        session: LlmHttpSession | None = None,
    ) -> None:
        if not str(api_key).strip():
            raise LlmGatewayError("LLM API key is not configured")
        if not str(base_url).strip().startswith(("https://", "http://")):
            raise LlmGatewayError("LLM base URL must be an absolute HTTP(S) URL")
        if not str(model).strip():
            raise LlmGatewayError("LLM model must be configured explicitly")
        if timeout_seconds <= 0:
            raise LlmGatewayError("LLM timeout must be positive")
        if not isinstance(max_retries, int) or max_retries < 0:
            raise LlmGatewayError("LLM max_retries must be a non-negative integer")

        self._api_key = str(api_key).strip()
        self._base_url = str(base_url).strip().rstrip("/")
        self._model = str(model).strip()
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = max_retries
        self._session = session or requests.Session()

    def analyze(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_object: bool = True,
    ) -> LlmAnalysisResult:
        """Run one bounded analysis call and return normalized text output."""

        url = f"{self._base_url}/chat/completions"
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.post(
                    url,
                    headers=dict(headers),
                    json=payload,
                    timeout=self._timeout_seconds,
                )
            except Exception as exc:
                error = f"LLM request failed: {type(exc).__name__}"
                if attempt < self._max_retries:
                    continue
                raise LlmGatewayError(error) from exc

            status_code = self._optional_int(getattr(response, "status_code", None))
            try:
                raw = response.json()
            except Exception as exc:
                preview = str(getattr(response, "text", ""))[:500]
                error = f"LLM returned a non-JSON response: {preview}"
                if attempt < self._max_retries and status_code in _RETRYABLE_HTTP_STATUSES:
                    continue
                raise LlmGatewayError(error) from exc

            if status_code is None or not 200 <= status_code < 300:
                message = self._provider_message(raw)
                error = f"LLM request failed with HTTP {status_code}: {message}"
                if attempt < self._max_retries and status_code in _RETRYABLE_HTTP_STATUSES:
                    continue
                raise LlmGatewayError(error)

            content = self._message_content(raw)
            usage = raw.get("usage") if isinstance(raw, dict) else {}
            if not content.strip():
                raise LlmGatewayError("LLM returned an empty response")
            return LlmAnalysisResult(
                status="completed",
                content=content,
                prompt_tokens=self._optional_int(usage.get("prompt_tokens")) or 0,
                completion_tokens=self._optional_int(usage.get("completion_tokens")) or 0,
                error="",
            )

        raise AssertionError("LLM retry loop exited without a response")

    def _message_content(self, raw: object) -> str:
        if not isinstance(raw, dict):
            return ""
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            return "".join(parts).strip()
        return ""

    def _provider_message(self, raw: object) -> str:
        if not isinstance(raw, dict):
            return "provider returned no error message"
        error = raw.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "unknown error")
        else:
            message = str(raw.get("message") or "unknown error")
        return message.replace(self._api_key, "[redacted]")[:300]

    @staticmethod
    def _optional_int(value: object) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
