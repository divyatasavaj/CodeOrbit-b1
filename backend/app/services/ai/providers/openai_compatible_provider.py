"""Adapter for any OpenAI-compatible ``/chat/completions`` endpoint.

Officially documented providers used by CodeOracle (Groq, OpenAI) subclass this,
and a deployment can point at any other compatible gateway through
``*_BASE_URL`` without code changes. Uses raw ``httpx`` so retries, timeouts and
backoff stay under this adapter's control instead of an SDK's hidden policy.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence

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

logger = logging.getLogger("codeoracle.ai.provider")


def is_daily_quota_text(text: str) -> bool:
    """True when a 429 body is the *daily* budget, which retrying cannot fix."""
    lowered = (text or "").lower()
    return "per day" in lowered or "tpd" in lowered or "per_day" in lowered


def seconds_until_retry(text: str) -> float:
    """Parse a provider's "try again in 6m28.4s" / "18h57m46.7s" wording."""
    match = re.search(r"(?:try again|retry)\s+in\s+([0-9hms.]+)", text or "", re.IGNORECASE)
    if not match:
        return 0.0
    total = 0.0
    for value, unit in re.findall(r"([0-9.]+)([hms])", match.group(1)):
        try:
            total += float(value) * {"h": 3600.0, "m": 60.0, "s": 1.0}[unit]
        except (TypeError, ValueError):
            continue
    return total


class OpenAICompatibleProvider(BaseProvider):
    """Chat-completions provider for OpenAI-compatible HTTP APIs.

    ``settings.api_key`` may hold a *pool*: several comma-separated keys. Each
    Groq key is an independent budget (8k tokens/minute, 200k tokens/day), so a
    pool multiplies throughput. Requests round-robin across the pool, and a key
    that reports its daily budget is spent is parked until its window resets
    while the remaining keys keep serving - one throttled key never fails the
    request or stalls it behind a multi-minute sleep.
    """

    name = "openai_compatible"
    default_base_url = ""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._key_quota_until: List[float] = [0.0] * len(self._api_keys())
        self._key_index = 0

    def _api_keys(self) -> List[str]:
        """Every configured key, in order; blanks dropped."""
        return [k.strip() for k in (self.settings.api_key or "").split(",") if k.strip()]

    def _available_key_indices(self, keys: Sequence[str]) -> List[int]:
        now = time.time()
        return [i for i in range(len(keys)) if now >= self._key_quota_until[i]]

    def key_status(self) -> List[Dict[str, Any]]:
        """Per-key availability for diagnostics; never exposes the key itself."""
        keys = self._api_keys()
        now = time.time()
        if len(self._key_quota_until) != len(keys):
            self._key_quota_until = [0.0] * len(keys)
        return [
            {
                "index": i + 1,
                "available": now >= self._key_quota_until[i],
                "resets_in_seconds": max(0.0, round(self._key_quota_until[i] - now, 1)),
                "fingerprint": keys[i][-4:] if keys[i] else "",
            }
            for i in range(len(keys))
        ]

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

    def _headers(self, api_key: Optional[str] = None) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key or self.settings.api_key}",
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

        keys = self._api_keys()
        if not keys:
            raise ProviderConfigError(
                f"{self.name}: no API key configured for the {self.settings.service} service",
                provider=self.name,
            )
        if len(self._key_quota_until) != len(keys):
            self._key_quota_until = [0.0] * len(keys)

        available = self._available_key_indices(keys)
        if not available:
            raise ProviderRateLimitError(
                f"{self.name}: all {len(keys)} key(s) have spent their daily budget",
                provider=self.name,
                status=429,
            )
        start = self._key_index % len(available)
        self._key_index += 1
        ordered = available[start:] + available[:start]

        last_error: Optional[Exception] = None
        for key_index in ordered:
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
                    response = await client.post(
                        endpoint, headers=self._headers(keys[key_index]), json=payload
                    )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(str(exc), provider=self.name) from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(str(exc), provider=self.name, cause=exc) from exc

            latency_ms = int((time.time() - started) * 1000)

            if response.status_code == 429:
                error = coerce_status_error(
                    self.name,
                    response.status_code,
                    response.text,
                    retry_after=response.headers.get("retry-after"),
                )
                if is_daily_quota_text(response.text):
                    # Park this key until its own window resets and let the rest
                    # of the pool serve; a daily cap is not worth retrying.
                    reset_in = max(seconds_until_retry(response.text), 60.0)
                    self._key_quota_until[key_index] = time.time() + reset_in
                    logger.warning(
                        "%s key #%d daily budget exhausted (resets in %.1f min); rotating on",
                        self.name, key_index + 1, reset_in / 60.0,
                    )
                else:
                    logger.info(
                        "%s key #%d hit its per-minute window; trying another key",
                        self.name, key_index + 1,
                    )
                last_error = error
                continue

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

        raise last_error or ProviderRateLimitError(
            f"{self.name}: every key was throttled", provider=self.name, status=429
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
