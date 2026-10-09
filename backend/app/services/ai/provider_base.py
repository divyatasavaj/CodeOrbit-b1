"""Provider-independent foundation for CodeOracle's AI services.

Every AI feature (repository chatbot, test generation, and any future one)
talks to a provider through this adapter, so swapping providers or changing
models, timeouts and retry policy never touches feature code.

Failure handling is deliberately narrow and honest:

  * only transient failures are retried (rate limit / timeout / 5xx / network),
  * ``Retry-After`` is respected when the provider sends it,
  * exponential backoff with jitter, bounded by the configured policy,
  * every error is normalized into one small ``ProviderError`` hierarchy whose
    ``to_public()`` form is safe to return to a browser: no API keys, no stack
    traces, truncated provider text.

Providers are never silent-failover targets: a service only uses the provider
it was configured with. Failover, if ever wanted, is an explicit per-service
configuration decision.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("codeoracle.ai")

# Anything that looks like a credential is stripped before a provider message,
# header or stack trace can reach a log line, an API response or the frontend.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|rk|pk|gsk|api)[-_][A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bAQ\.[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|authorization|x-api-key)\b\s*[:=]\s*[^\s,'\"]+"),
)


def sanitize_error_text(text: Any, limit: int = 400) -> str:
    """Return a log/response-safe single-line version of provider text."""
    if text is None:
        return ""
    cleaned = str(text).replace("\r", " ").replace("\n", " ").strip()
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "..."
    return cleaned


class ProviderError(Exception):
    """Base class for every normalized provider failure."""

    code = "provider_error"
    retryable = False
    public_message = "CodeOracle could not reach the configured AI provider. Please try again."

    def __init__(
        self,
        message: str = "",
        *,
        provider: str = "",
        status: Optional[int] = None,
        retry_after: Optional[float] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        self.provider = provider
        self.status = status
        self.retry_after = retry_after
        self.detail = sanitize_error_text(message)
        super().__init__(self.detail or self.public_message)

    def to_public(self) -> Dict[str, Any]:
        """Sanitized, frontend-safe representation (never contains secrets)."""
        return {
            "error_code": self.code,
            "message": self.public_message,
            "provider": self.provider or None,
            "retryable": bool(self.retryable),
        }

    def log_line(self) -> str:
        return (
            f"{self.code} provider={self.provider or 'n/a'} status={self.status or 'n/a'} "
            f"detail={self.detail or 'n/a'}"
        )


class ProviderConfigError(ProviderError):
    """The service is missing a provider, API key, model or base URL."""

    code = "provider_not_configured"
    public_message = (
        "CodeOracle's AI provider is not configured. Please check the backend configuration."
    )


class ProviderAuthError(ProviderError):
    """The provider rejected the credential (401/403)."""

    code = "provider_auth_error"
    public_message = "CodeOracle's AI provider rejected the configured credentials."


class ProviderRateLimitError(ProviderError):
    """The provider rate limited or quota-limited this request."""

    code = "provider_rate_limited"
    retryable = True
    public_message = "The AI provider is rate limiting requests. Please try again shortly."


class ProviderTimeoutError(ProviderError):
    """The provider did not answer inside the configured timeout."""

    code = "provider_timeout"
    retryable = True
    public_message = "CodeOracle could not reach the configured AI provider. Please try again."


class ProviderUnavailableError(ProviderError):
    """Transient transport/5xx failure."""

    code = "provider_unavailable"
    retryable = True
    public_message = "CodeOracle could not reach the configured AI provider. Please try again."


class ProviderResponseError(ProviderError):
    """The provider answered with something unusable (empty/ malformed)."""

    code = "provider_bad_response"
    public_message = "The AI provider returned an unusable response. Please try again."


@dataclass(frozen=True)
class AIServiceSettings:
    """Resolved configuration for ONE AI service (chatbot or test generation).

    Built from ``config.chatbot_ai_config()`` / ``config.testgen_ai_config()``
    so the two services can never alias each other's provider, model or keys.
    """

    service: str
    provider: str
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    timeout_seconds: float = 45.0
    max_retries: int = 2
    temperature: float = 0.2
    max_tokens: int = 2048
    retry_base_delay: float = 0.5
    max_backoff_seconds: float = 8.0

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AIServiceSettings":
        known = {field for field in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (raw or {}).items() if k in known})

    def safe_summary(self) -> Dict[str, Any]:
        """Frontend-safe description - no key, no full query strings."""
        return {
            "service": self.service,
            "provider": self.provider,
            "model": self.model,
            "configured": bool(self.api_key) and bool(self.model),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "base_url_host": _host_of(self.base_url),
        }


def _host_of(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = re.match(r"^[a-zA-Z]+://([^/]+)", url)
    return match.group(1) if match else None


@dataclass(frozen=True)
class ChatMessage:
    """One conversation turn handed to a provider."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    """Normalized provider answer (shape-independent by design)."""

    text: str
    provider: str
    model: str
    latency_ms: int
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    finish_reason: Optional[str] = None

    @property
    def total_tokens(self) -> Optional[int]:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


class BaseProvider(ABC):
    """Common contract every provider adapter implements.

    Subclasses only implement :meth:`_generate_once`; timeouts, retries,
    backoff and error normalization are handled once, here, so no provider
    can accidentally behave differently.
    """

    name = "base"
    requires_api_key = True

    def __init__(self, settings: AIServiceSettings) -> None:
        self.settings = settings
        self.model = settings.model
        self._last_error: Optional[str] = None
        self._last_success_at: Optional[float] = None

    # ------------------------------------------------------------------ state
    def is_configured(self) -> bool:
        if not self.model:
            return False
        if self.requires_api_key and not self.settings.api_key:
            return False
        return True

    def describe(self) -> Dict[str, Any]:
        """Safe operational status for the status endpoint / logs."""
        info = self.settings.safe_summary()
        info.update({
            "name": self.name,
            "configured": self.is_configured(),
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
        })
        return info

    def _require_configured(self) -> None:
        if not self.model:
            raise ProviderConfigError(
                f"{self.name}: no model configured for the {self.settings.service} service",
                provider=self.name,
            )
        if self.requires_api_key and not self.settings.api_key:
            raise ProviderConfigError(
                f"{self.name}: no API key configured for the {self.settings.service} service",
                provider=self.name,
            )

    # -------------------------------------------------------------- public API
    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        """Call the provider with retries; always returns text or raises."""
        self._require_configured()
        if not messages:
            raise ProviderConfigError("no prompt messages supplied", provider=self.name)

        attempts = max(1, self.settings.max_retries + 1)
        delay = max(0.0, self.settings.retry_base_delay)
        last_exc: Optional[ProviderError] = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._generate_once(
                    messages,
                    max_tokens=max_tokens or self.settings.max_tokens,
                    temperature=self.settings.temperature if temperature is None else temperature,
                    timeout=timeout or self.settings.timeout_seconds,
                )
            except ProviderError as exc:
                last_exc = exc
                self._last_error = exc.log_line()
                if not exc.retryable or attempt >= attempts:
                    logger.warning("[AI] %s (attempt %s/%s)", exc.log_line(), attempt, attempts)
                    raise
                wait = exc.retry_after if exc.retry_after is not None else delay
                wait = min(max(float(wait), 0.0), self.settings.max_backoff_seconds)
                logger.info(
                    "[AI] %s retrying in %.2fs (attempt %s/%s)",
                    exc.code, wait, attempt, attempts,
                )
                if wait:
                    await asyncio.sleep(wait + random.uniform(0, min(0.25, wait / 4 or 0.05)))
                delay = min(max(delay * 2, 0.1), self.settings.max_backoff_seconds)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never leak a raw provider error
                wrapped = ProviderUnavailableError(
                    f"{self.name}: unexpected error: {exc}", provider=self.name, cause=exc
                )
                self._last_error = wrapped.log_line()
                logger.warning("[AI] %s", wrapped.log_line())
                raise wrapped from exc

            self._last_error = None
            self._last_success_at = time.time()
            return response

        raise last_exc or ProviderUnavailableError("provider call failed", provider=self.name)

    @abstractmethod
    async def _generate_once(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> LLMResponse:
        """One provider round-trip with no retry logic (implemented per provider)."""


def split_system_messages(messages: Sequence[ChatMessage]) -> tuple:
    """Separate system instructions from the conversation turns."""
    system_parts: List[str] = []
    conversation: List[ChatMessage] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(message.content)
        else:
            conversation.append(message)
    return "\n\n".join(p for p in system_parts if p), conversation


def coerce_status_error(
    provider: str,
    status: int,
    body: str,
    retry_after: Optional[str] = None,
) -> ProviderError:
    """Map an HTTP status + body onto the normalized error hierarchy."""
    detail = sanitize_error_text(body)
    wait: Optional[float] = None
    if retry_after:
        try:
            wait = float(str(retry_after).strip())
        except (TypeError, ValueError):
            wait = None
    if status in (401, 403):
        return ProviderAuthError(detail, provider=provider, status=status)
    if status == 404:
        return ProviderConfigError(detail, provider=provider, status=status)
    if status == 429:
        return ProviderRateLimitError(detail, provider=provider, status=status, retry_after=wait)
    if status >= 500:
        return ProviderUnavailableError(detail, provider=provider, status=status, retry_after=wait)
    if 400 <= status < 500:
        return ProviderConfigError(detail, provider=provider, status=status)
    return ProviderUnavailableError(detail, provider=provider, status=status)
