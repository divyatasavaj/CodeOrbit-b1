"""Groq adapter (official OpenAI-compatible endpoint)."""
from __future__ import annotations

from app.services.ai.providers.openai_compatible_provider import (
    OpenAICompatibleProvider,
)


class GroqProvider(OpenAICompatibleProvider):
    """Groq's hosted chat-completions API.

    ``GROQ``-style keys (``gsk_...``) authenticate here; the endpoint is the
    documented OpenAI-compatible one, overridable via ``*_BASE_URL``.
    """

    name = "groq"
    default_base_url = "https://api.groq.com/openai/v1"
