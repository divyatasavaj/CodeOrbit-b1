"""Adapter for any OpenAI-compatible ``/chat/completions`` endpoint.

Officially documented providers used by CodeOracle (Groq, OpenAI) subclass this,
and a deployment can point at any other compatible gateway through
``*_BASE_URL`` without code changes. Uses raw ``httpx`` so retries, timeouts and
backoff stay under this adapter's control instead of an SDK's hidden policy.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Sequence

import httpx

from app.services.ai.provider_base import (
    BaseProvider,
    ChatMessage,
    LLMResponse,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    coerce_status_error,
    sanitize_error_text,
    split_system_messages,
)


class OpenAICompatibleProvider(BaseProvider):
    """Chat-completions provider for OpenAI-compatible HTTP APIs."""

    name = "openai_compatible"
    default_base_url = ""

    def _endpoint(self) -> str:
        base = (self.settings.base_url or self.default_base_url or "").rstrip("/")
        if not base:
            raise ProviderConfigError(
                f"{self.name}: a base URL is required",
                provider=self.name,
            )
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
    ) -> Dict[str, Any]:
        return {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

    async def _generate_once(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> LLMResponse:
        endpoint = self._endpoint()
        payload = self._payload(messages, max_tokens=max_tokens, temperature=temperature)
        started = time.time()

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    timeout,
                    connect=min(10.0, timeout),
                    read=timeout,
                    write=min(10.0, timeout),
                )
            ) as client:
                response = await client.post(endpoint, headers=self._headers(), json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc), provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(str(exc), provider=self.name, cause=exc) from exc

        latency_ms = int((time.time() - started) * 1000)

        if response.status_code != 200:
            raise coerce_status_error(
                self.name,
                response.status_code,
                response.text,
                retry_after=response.headers.get("retry-after"),
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderResponseError(
                f"{self.name}: non-JSON response: {response.text[:200]}", provider=self.name
            ) from exc

        text = self._extract_text(data)
        if not text.strip():
            raise ProviderResponseError(f"{self.name}: empty completion", provider=self.name)

        usage = data.get("usage") or {}
        choices = data.get("choices") or [{}]
        return LLMResponse(
            text=text.strip(),
            provider=self.name,
            model=data.get("model") or self.model,
            latency_ms=latency_ms,
            prompt_tokens=_as_int(usage.get("prompt_tokens")),
            completion_tokens=_as_int(usage.get("completion_tokens")),
            finish_reason=(choices[0] or {}).get("finish_reason"),
        )

    @staticmethod
    def _extract_text(data: Dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # Some gateways return content parts instead of a plain string.
            return "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return content or ""


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
