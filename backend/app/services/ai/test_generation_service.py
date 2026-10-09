"""Test-generation AI service.

Owns ONE provider configuration (``TESTGEN_*``) and is the only path the test
pipeline uses to talk to a model. It is intentionally separate from the
chatbot: different provider, model, key, timeout, retry policy and attempt
budget, so changing one never changes the other, and one being unconfigured
never disables the other.

The service is a thin, honest gateway: it either returns model text or raises a
normalized :class:`ProviderError`. It never executes tests, measures coverage
or touches the repository - those stay in the existing, unchanged pipeline.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

import config

from app.services.ai.provider_base import (
    AIServiceSettings,
    BaseProvider,
    ChatMessage,
    ProviderConfigError,
    sanitize_error_text,
)
from app.services.ai.provider_factory import build_provider

logger = logging.getLogger("codeoracle.ai.testgen")


class TestGenerationService:
    """Provider gateway for the on-demand test-generation pipeline."""

    service_name = "testgen"
    # Not a pytest test class (silences pytest's collector).
    __test__ = False

    def __init__(
        self,
        settings: Optional[AIServiceSettings] = None,
        provider: Optional[BaseProvider] = None,
    ) -> None:
        self._settings = settings or AIServiceSettings.from_dict(config.testgen_ai_config())
        self._provider = provider

    # ------------------------------------------------------------------ config
    @property
    def settings(self) -> AIServiceSettings:
        return self._settings

    @property
    def provider(self) -> BaseProvider:
        if self._provider is None:
            self._provider = build_provider(self._settings)
        return self._provider

    def is_configured(self) -> bool:
        try:
            return bool(self.provider.is_configured())
        except ProviderConfigError:
            return False

    def describe(self) -> dict:
        """Safe status (no secrets) for logs and the /ai/status endpoint."""
        info = self._settings.safe_summary()
        info["service"] = self.service_name
        info["configured"] = self.is_configured()
        try:
            described = self.provider.describe()
            info["name"] = described.get("name")
            info["last_error"] = described.get("last_error")
            info["last_success_at"] = described.get("last_success_at")
        except ProviderConfigError as exc:
            info["name"] = None
            info["configured"] = False
            info["last_error"] = exc.log_line()
        return info

    # ----------------------------------------------------------------- generate
    async def generate_text(self, prompt: str, *, system: Optional[str] = None) -> str:
        """Return generated text or raise a normalized ProviderError."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderConfigError("empty prompt", provider=self._settings.provider)
        messages = []
        if system:
            messages.append(ChatMessage(role="system", content=system))
        messages.append(ChatMessage(role="user", content=prompt))
        response = await self.provider.generate(messages)
        logger.info(
            "[AI] service=testgen provider=%s model=%s latency_ms=%s tokens=%s",
            response.provider, response.model, response.latency_ms, response.total_tokens,
        )
        return response.text

    # Backwards/compat alias used by llm.py.
    async def generate(self, prompt: str, system: Optional[str] = None) -> str:
        return await self.generate_text(prompt, system=system)


_service: Optional[TestGenerationService] = None


def get_test_generation_service() -> TestGenerationService:
    """Process-wide lazy singleton (independent from the chatbot singleton)."""
    global _service
    if _service is None:
        _service = TestGenerationService()
    return _service


def reset_test_generation_service() -> None:
    """Test hook: drop the cached singleton (e.g. after changing env vars)."""
    global _service
    _service = None


__all__ = [
    "TestGenerationService",
    "get_test_generation_service",
    "reset_test_generation_service",
    "sanitize_error_text",
]
