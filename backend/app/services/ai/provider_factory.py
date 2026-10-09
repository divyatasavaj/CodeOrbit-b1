"""Builds a provider adapter from a service's own settings.

The factory is intentionally the only place that maps provider names to
classes, so each AI service can select its provider independently and a new
provider is added cleanly (see the provider package docstring).
"""
from __future__ import annotations

from typing import Dict, Type

from app.services.ai.provider_base import (
    AIServiceSettings,
    BaseProvider,
    ProviderConfigError,
)
from app.services.ai.providers import (
    GeminiProvider,
    GroqProvider,
    OpenAICompatibleProvider,
)

PROVIDER_REGISTRY: Dict[str, Type[BaseProvider]] = {
    "gemini": GeminiProvider,
    "google": GeminiProvider,
    "groq": GroqProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "openai": OpenAICompatibleProvider,
}


def known_providers() -> list:
    """Provider names a deployment may select in the environment."""
    return sorted({cls.name for cls in PROVIDER_REGISTRY.values()})


def build_provider(settings: AIServiceSettings) -> BaseProvider:
    """Instantiate the configured provider, or fail loudly and safely."""
    provider_name = (settings.provider or "").strip().lower()
    if not provider_name:
        raise ProviderConfigError(
            f"no provider configured for the {settings.service} service",
            provider="",
        )
    factory = PROVIDER_REGISTRY.get(provider_name)
    if factory is None:
        raise ProviderConfigError(
            f"unsupported provider '{provider_name}' for the {settings.service} service "
            f"(known: {', '.join(known_providers())})",
            provider=provider_name,
        )
    return factory(settings)
