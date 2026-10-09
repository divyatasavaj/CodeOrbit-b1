"""Concrete provider adapters.

Registering a new provider is two steps: add an adapter module here and add it
to ``PROVIDER_REGISTRY`` in ``provider_factory.py``. Nothing else in the
codebase needs to change.
"""
from app.services.ai.providers.gemini_provider import GeminiProvider  # noqa: F401
from app.services.ai.providers.groq_provider import GroqProvider  # noqa: F401
from app.services.ai.providers.openai_compatible_provider import (  # noqa: F401
    OpenAICompatibleProvider,
)

__all__ = ["GeminiProvider", "GroqProvider", "OpenAICompatibleProvider"]
