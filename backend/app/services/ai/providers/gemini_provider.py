"""Gemini adapter built on the official ``google-genai`` SDK.

The SDK is already a project dependency (the explanation pipeline uses it), so
the chatbot and test generation can use Gemini without new dependencies. The
call is executed in a worker thread and wrapped in ``asyncio.wait_for`` so a
slow generation cannot block the event loop or outlive its timeout.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, List, Optional, Sequence

from google import genai
from google.genai import types as genai_types

from app.services.ai.provider_base import (
    BaseProvider,
    ChatMessage,
    LLMResponse,
    ProviderAuthError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    sanitize_error_text,
    split_system_messages,
)

_RATE_LIMIT_MARKERS = ("429", "resource_exhausted", "rate limit", "quota")
_AUTH_MARKERS = ("api key", "api_key", "unauthenticated", "permission_denied", "401", "403")
_MODEL_MARKERS = ("not_found", "not found", "404", "unsupported model", "invalid model")


class GeminiProvider(BaseProvider):
    """Google Gemini provider (``generateContent``)."""

    name = "gemini"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._client: Optional[Any] = None

    def _get_client(self) -> Any:
        self._require_configured()
        if self._client is None:
            http_options = None
            if self.settings.base_url:
                http_options = genai_types.HttpOptions(base_url=self.settings.base_url)
            self._client = genai.Client(api_key=self.settings.api_key, http_options=http_options)
        return self._client

    async def _generate_once(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> LLMResponse:
        client = self._get_client()
        system_instruction, conversation = split_system_messages(messages)
        contents: List[Any] = [
            genai_types.Content(
                role="model" if message.role == "assistant" else "user",
                parts=[genai_types.Part(text=message.content)],
            )
            for message in conversation
        ]
        config = genai_types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            system_instruction=system_instruction or None,
        )
        started = time.time()
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.models.generate_content,
                    model=self.model,
                    contents=contents,
                    config=config,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ProviderTimeoutError(
                f"gemini timed out after {timeout:g}s", provider=self.name
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalized below
            raise self._normalize(exc) from exc

        latency_ms = int((time.time() - started) * 1000)
        text = _extract_text(response)
        if not text.strip():
            raise ProviderResponseError("gemini returned an empty response", provider=self.name)

        usage = getattr(response, "usage_metadata", None)
        return LLMResponse(
            text=text.strip(),
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
            prompt_tokens=_count(usage, "prompt_token_count"),
            completion_tokens=_count(usage, "candidates_token_count"),
            finish_reason=_finish_reason(response),
        )

    def _normalize(self, exc: BaseException):
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        detail = sanitize_error_text(exc)
        lowered = detail.lower()
        if isinstance(status, str) and status.isdigit():
            status = int(status)
        if status in (401, 403) or any(m in lowered for m in _AUTH_MARKERS):
            return ProviderAuthError(detail, provider=self.name, status=status)
        if status == 429 or any(m in lowered for m in _RATE_LIMIT_MARKERS):
            return ProviderRateLimitError(detail, provider=self.name, status=status)
        if status == 404 or any(m in lowered for m in _MODEL_MARKERS):
            return ProviderConfigError(detail, provider=self.name, status=status)
        if isinstance(status, int) and status >= 500:
            return ProviderUnavailableError(detail, provider=self.name, status=status)
        return ProviderUnavailableError(detail, provider=self.name, status=status)


def _extract_text(response: Any) -> str:
    """Pull generated text out of a Gemini response without assuming a shape."""
    if response is None:
        return ""
    try:
        text = getattr(response, "text", None)
        if text:
            return str(text)
    except Exception:  # noqa: BLE001 - .text can raise when parts are missing
        pass
    chunks: List[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))
    return "".join(chunks)


def _count(usage: Any, field: str) -> Optional[int]:
    if usage is None:
        return None
    value = getattr(usage, field, None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finish_reason(response: Any) -> Optional[str]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    return str(reason) if reason is not None else None
