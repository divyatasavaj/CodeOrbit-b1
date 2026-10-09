"""Independent AI services: repository chatbot and test generation.

Both services are configured separately (see ``config.chatbot_ai_config`` /
``config.testgen_ai_config``) and only share this adapter layer, never each
other's credentials, model, retry policy or failure domain.
"""
from app.services.ai.provider_base import (  # noqa: F401
    AIServiceSettings,
    BaseProvider,
    ChatMessage,
    LLMResponse,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    sanitize_error_text,
)
from app.services.ai.provider_factory import (  # noqa: F401
    build_provider,
    known_providers,
)

__all__ = [
    "AIServiceSettings",
    "BaseProvider",
    "ChatMessage",
    "LLMResponse",
    "ProviderAuthError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "sanitize_error_text",
    "build_provider",
    "known_providers",
]
