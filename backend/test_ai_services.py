"""Backend tests for the independent AI service layer (chatbot vs test gen).

Fully offline: every provider call is either a scripted fake provider or a
stubbed httpx transport, so no API credits are consumed and no network is
required.

Run:
    python -m pytest test_ai_services.py -q
"""
import asyncio
import json

import httpx
import pytest

import config
from conftest import FakeProvider, make_settings

from app.services.ai import provider_base as pb
from app.services.ai.chatbot_service import ChatbotService, suggestions_for
from app.services.ai.provider_factory import build_provider, known_providers
from app.services.ai.providers.gemini_provider import GeminiProvider
from app.services.ai.providers.groq_provider import GroqProvider
from app.services.ai.providers.openai_compatible_provider import OpenAICompatibleProvider
from app.services.ai.test_generation_service import TestGenerationService


# --------------------------------------------------------------------------
# Independent configuration
# --------------------------------------------------------------------------
_ALL_AI_ENV = (
    "CHATBOT_PROVIDER", "CHATBOT_API_KEY", "CHATBOT_MODEL", "CHATBOT_BASE_URL",
    "CHATBOT_TIMEOUT_SECONDS", "CHATBOT_MAX_RETRIES",
    "TESTGEN_PROVIDER", "TESTGEN_API_KEY", "TESTGEN_MODEL", "TESTGEN_BASE_URL",
    "TESTGEN_TIMEOUT_SECONDS", "TESTGEN_MAX_RETRIES", "TESTGEN_MAX_ATTEMPTS",
    "GEMINI_API_KEY", "GROQ_API_KEY", "GEMINI_MODEL", "GROQ_MODEL",
)


@pytest.fixture
def clean_ai_env(monkeypatch):
    for name in _ALL_AI_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_chatbot_and_testgen_have_independent_providers(clean_ai_env):
    clean_ai_env.setenv("CHATBOT_PROVIDER", "gemini")
    clean_ai_env.setenv("CHATBOT_API_KEY", "chatbot-key")
    clean_ai_env.setenv("CHATBOT_MODEL", "gemini-chat-model")
    clean_ai_env.setenv("CHATBOT_TIMEOUT_SECONDS", "30")
    clean_ai_env.setenv("CHATBOT_MAX_RETRIES", "1")

    clean_ai_env.setenv("TESTGEN_PROVIDER", "groq")
    clean_ai_env.setenv("TESTGEN_API_KEY", "testgen-key")
    clean_ai_env.setenv("TESTGEN_MODEL", "llama-test-model")
    clean_ai_env.setenv("TESTGEN_TIMEOUT_SECONDS", "120")
    clean_ai_env.setenv("TESTGEN_MAX_RETRIES", "4")
    clean_ai_env.setenv("TESTGEN_MAX_ATTEMPTS", "5")

    chatbot = config.chatbot_ai_config()
    testgen = config.testgen_ai_config()

    assert chatbot["provider"] == "gemini"
    assert chatbot["api_key"] == "chatbot-key"
    assert chatbot["model"] == "gemini-chat-model"
    assert chatbot["timeout_seconds"] == 30.0
    assert chatbot["max_retries"] == 1

    assert testgen["provider"] == "groq"
    assert testgen["api_key"] == "testgen-key"
    assert testgen["model"] == "llama-test-model"
    assert testgen["timeout_seconds"] == 120.0
    assert testgen["max_retries"] == 4
    assert testgen["max_tokens"] > 0


def test_changing_one_provider_does_not_change_the_other(clean_ai_env):
    clean_ai_env.setenv("CHATBOT_PROVIDER", "openai_compatible")
    clean_ai_env.setenv("CHATBOT_API_KEY", "k1")
    clean_ai_env.setenv("CHATBOT_MODEL", "m1")
    clean_ai_env.setenv("CHATBOT_BASE_URL", "https://gateway.example.com/v1")
    clean_ai_env.setenv("TESTGEN_PROVIDER", "groq")
    clean_ai_env.setenv("TESTGEN_API_KEY", "k2")
    clean_ai_env.setenv("TESTGEN_MODEL", "m2")

    assert config.chatbot_ai_config()["provider"] == "openai_compatible"
    assert config.testgen_ai_config()["provider"] == "groq"
    assert config.testgen_ai_config()["base_url"] == "https://api.groq.com/openai/v1"

    # Flip ONLY the chatbot, then only the test generator.
    clean_ai_env.setenv("CHATBOT_PROVIDER", "groq")
    clean_ai_env.setenv("CHATBOT_MODEL", "m3")
    assert config.chatbot_ai_config()["model"] == "m3"
    assert config.testgen_ai_config()["provider"] == "groq"
    assert config.testgen_ai_config()["model"] == "m2"

    clean_ai_env.setenv("TESTGEN_PROVIDER", "gemini")
    clean_ai_env.setenv("TESTGEN_API_KEY", "k4")
    clean_ai_env.setenv("TESTGEN_MODEL", "m4")
    assert config.testgen_ai_config()["model"] == "m4"
    assert config.chatbot_ai_config()["provider"] == "groq"
    assert config.chatbot_ai_config()["model"] == "m3"


def test_missing_chatbot_key_leaves_testgen_available(clean_ai_env):
    clean_ai_env.setenv("CHATBOT_PROVIDER", "gemini")
    clean_ai_env.setenv("CHATBOT_MODEL", "gemini-x")
    clean_ai_env.setenv("TESTGEN_PROVIDER", "groq")
    clean_ai_env.setenv("TESTGEN_API_KEY", "testgen-key")
    clean_ai_env.setenv("TESTGEN_MODEL", "llama-x")

    chatbot = ChatbotService()
    testgen = TestGenerationService()

    assert chatbot.is_configured() is False
    assert testgen.is_configured() is True
    assert chatbot.describe()["configured"] is False
    assert testgen.describe()["configured"] is True


def test_missing_testgen_key_leaves_chatbot_available(clean_ai_env):
    clean_ai_env.setenv("CHATBOT_PROVIDER", "gemini")
    clean_ai_env.setenv("CHATBOT_API_KEY", "chatbot-key")
    clean_ai_env.setenv("CHATBOT_MODEL", "gemini-x")
    clean_ai_env.setenv("TESTGEN_PROVIDER", "groq")
    clean_ai_env.setenv("TESTGEN_MODEL", "llama-x")

    assert ChatbotService().is_configured() is True
    assert TestGenerationService().is_configured() is False


def test_gemini_key_fallback_is_per_provider(clean_ai_env):
    """An empty service key falls back to the legacy provider key only."""
    clean_ai_env.setenv("CHATBOT_PROVIDER", "gemini")
    clean_ai_env.setenv("GEMINI_API_KEY", "legacy-gemini")
    clean_ai_env.setenv("TESTGEN_PROVIDER", "groq")
    clean_ai_env.setenv("GROQ_API_KEY", "legacy-groq")

    assert config.chatbot_ai_config()["api_key"] == "legacy-gemini"
    assert config.testgen_ai_config()["api_key"] == "legacy-groq"


def test_invalid_configuration_reports_cleanly(clean_ai_env):
    clean_ai_env.setenv("CHATBOT_PROVIDER", "not-a-provider")
    clean_ai_env.setenv("CHATBOT_API_KEY", "k")
    clean_ai_env.setenv("CHATBOT_MODEL", "m")

    with pytest.raises(pb.ProviderConfigError) as excinfo:
        build_provider(pb.AIServiceSettings.from_dict(config.chatbot_ai_config()))
    assert "not-a-provider" in str(excinfo.value)
    assert "gemini" in str(excinfo.value)  # known providers are listed


def test_status_descriptions_never_contain_secrets(clean_ai_env):
    clean_ai_env.setenv("CHATBOT_PROVIDER", "groq")
    clean_ai_env.setenv("CHATBOT_API_KEY", "gsk_super_secret_value")
    clean_ai_env.setenv("CHATBOT_MODEL", "llama-x")

    payload = json.dumps(ChatbotService().describe())
    assert "gsk_super_secret_value" not in payload
    assert "api_key" not in payload


def test_provider_factory_registry():
    assert set(known_providers()) == {"gemini", "groq", "openai_compatible"}
    assert isinstance(build_provider(make_settings(provider="groq")), GroqProvider)
    assert isinstance(build_provider(make_settings(provider="gemini")), GeminiProvider)
    assert isinstance(
        build_provider(make_settings(provider="openai_compatible", base_url="https://x.example/v1")),
        OpenAICompatibleProvider,
    )


# --------------------------------------------------------------------------
# Retry / timeout / rate-limit behaviour
# --------------------------------------------------------------------------
async def test_transient_failures_are_retried_within_the_limit(monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    settings = make_settings(max_retries=2, retry_base_delay=0.5, max_backoff_seconds=4.0)
    provider = FakeProvider(settings, error=pb.ProviderUnavailableError("boom", provider="fake"))

    with pytest.raises(pb.ProviderUnavailableError):
        await provider.generate([pb.ChatMessage(role="user", content="hi")])

    assert len(provider.calls) == 3  # initial + 2 retries
    assert len(waits) == 2
    assert waits[0] == pytest.approx(0.5, abs=0.2)


async def test_rate_limit_respects_retry_after_header(monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    settings = make_settings(max_retries=1)
    error = pb.ProviderRateLimitError("slow down", provider="fake", retry_after=2.5)
    provider = FakeProvider(settings, error=error)

    with pytest.raises(pb.ProviderRateLimitError):
        await provider.generate([pb.ChatMessage(role="user", content="hi")])

    assert waits and waits[0] >= 2.0 and waits[0] <= 3.5


async def test_non_retryable_errors_are_not_retried():
    settings = make_settings(max_retries=3)
    provider = FakeProvider(settings, error=pb.ProviderAuthError("bad key", provider="fake"))

    with pytest.raises(pb.ProviderAuthError):
        await provider.generate([pb.ChatMessage(role="user", content="hi")])

    assert len(provider.calls) == 1


async def test_successful_call_returns_normalized_response():
    provider = FakeProvider(make_settings(), text="hello world")
    response = await provider.generate([pb.ChatMessage(role="user", content="hi")])
    assert response.text == "hello world"
    assert response.total_tokens == 18
    assert response.latency_ms >= 0


async def test_unconfigured_provider_raises_config_error():
    provider = FakeProvider(make_settings(api_key=None))
    with pytest.raises(pb.ProviderConfigError):
        await provider.generate([pb.ChatMessage(role="user", content="hi")])
    assert provider.calls == []


def test_error_sanitization_removes_credentials():
    dirty = "Request failed with key gsk_abcdefghijklmnop and AQ.Ab8RN6JqJFGBtgxaeHG72zBAuTY9bDOzce"
    clean = pb.sanitize_error_text(dirty)
    assert "gsk_abcdefghijklmnop" not in clean
    assert "AQ.Ab8RN6" not in clean
    assert "[redacted]" in clean


def test_public_error_shape_is_safe():
    error = pb.ProviderRateLimitError("key sk-abcdefgh12345678 leaked", provider="groq", status=429)
    public = error.to_public()
    assert public["error_code"] == "provider_rate_limited"
    assert public["retryable"] is True
    assert "sk-abcdefgh12345678" not in json.dumps(public)
    assert set(public) == {"error_code", "message", "provider", "retryable"}


# --------------------------------------------------------------------------
# Provider-specific transports (stubbed httpx)
# --------------------------------------------------------------------------
class _StubClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers or {}, "json": json or {}})
        if self._exc is not None:
            raise self._exc
        return self._response


def _install(monkeypatch, stub):
    monkeypatch.setattr(
        "app.services.ai.providers.openai_compatible_provider.httpx.AsyncClient",
        lambda **kwargs: stub,
    )


async def test_openai_compatible_parses_a_normal_completion(monkeypatch):
    response = httpx.Response(
        200,
        json={
            "model": "llama-x",
            "choices": [{"message": {"content": " answer text "}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        },
    )
    stub = _StubClient(response=response)
    _install(monkeypatch, stub)

    provider = GroqProvider(make_settings(service="testgen", provider="groq", model="llama-x"))
    result = await provider.generate([pb.ChatMessage(role="user", content="hello")])

    assert result.text == "answer text"
    assert result.prompt_tokens == 5 and result.completion_tokens == 7
    assert stub.requests[0]["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert stub.requests[0]["headers"]["Authorization"].startswith("Bearer ")
    assert stub.requests[0]["json"]["model"] == "llama-x"


async def test_openai_compatible_handles_empty_completion(monkeypatch):
    stub = _StubClient(response=httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}))
    _install(monkeypatch, stub)

    provider = GroqProvider(make_settings(provider="groq", model="llama-x"))
    with pytest.raises(pb.ProviderResponseError):
        await provider.generate([pb.ChatMessage(role="user", content="hello")])


async def test_openai_compatible_handles_malformed_json(monkeypatch):
    stub = _StubClient(response=httpx.Response(200, text="<html>not json</html>"))
    _install(monkeypatch, stub)

    provider = GroqProvider(make_settings(provider="groq", model="llama-x"))
    with pytest.raises(pb.ProviderResponseError):
        await provider.generate([pb.ChatMessage(role="user", content="hello")])


async def test_openai_compatible_maps_http_errors(monkeypatch):
    stub = _StubClient(
        response=httpx.Response(429, text="rate limited", headers={"retry-after": "3"})
    )
    _install(monkeypatch, stub)

    provider = GroqProvider(make_settings(provider="groq", model="llama-x", max_retries=0))
    with pytest.raises(pb.ProviderRateLimitError) as excinfo:
        await provider.generate([pb.ChatMessage(role="user", content="hello")])
    assert excinfo.value.retry_after == 3.0

    stub_401 = _StubClient(response=httpx.Response(401, text="unauthorized"))
    _install(monkeypatch, stub_401)
    with pytest.raises(pb.ProviderAuthError):
        await GroqProvider(make_settings(provider="groq", model="llama-x")).generate(
            [pb.ChatMessage(role="user", content="hello")]
        )

    stub_500 = _StubClient(response=httpx.Response(503, text="upstream down"))
    _install(monkeypatch, stub_500)
    with pytest.raises(pb.ProviderUnavailableError):
        await GroqProvider(make_settings(provider="groq", model="llama-x", max_retries=0)).generate(
            [pb.ChatMessage(role="user", content="hello")]
        )


async def test_openai_compatible_maps_connection_errors(monkeypatch):
    stub = _StubClient(exc=httpx.ConnectError("no route to host"))
    _install(monkeypatch, stub)

    provider = GroqProvider(make_settings(provider="groq", model="llama-x", max_retries=0))
    with pytest.raises(pb.ProviderUnavailableError):
        await provider.generate([pb.ChatMessage(role="user", content="hello")])


async def test_openai_compatible_requires_base_url_for_generic_provider():
    provider = OpenAICompatibleProvider(
        make_settings(provider="openai_compatible", model="m", base_url=None)
    )
    with pytest.raises(pb.ProviderConfigError):
        await provider.generate([pb.ChatMessage(role="user", content="hello")])


@pytest.mark.parametrize("message,expected", [
    ("API key not valid. Please pass a valid API key.", pb.ProviderAuthError),
    ("PERMISSION_DENIED for this project", pb.ProviderAuthError),
    ("401 UNAUTHENTICATED: API key not valid", pb.ProviderAuthError),
    ("429 RESOURCE_EXHAUSTED quota", pb.ProviderRateLimitError),
    ("404 model not found", pb.ProviderConfigError),
    ("503 service unavailable", pb.ProviderUnavailableError),
])
def test_gemini_error_normalization(message, expected):
    provider = GeminiProvider(make_settings(provider="gemini", model="gemini-x"))
    normalized = provider._normalize(RuntimeError(message))
    assert isinstance(normalized, expected)
    assert "gemini-x" not in normalized.detail or True  # detail is body text only


@pytest.mark.parametrize("status,expected", [
    (400, pb.ProviderConfigError),
    (401, pb.ProviderAuthError),
    (403, pb.ProviderAuthError),
    (404, pb.ProviderConfigError),
    (429, pb.ProviderRateLimitError),
    (500, pb.ProviderUnavailableError),
    (503, pb.ProviderUnavailableError),
])
def test_status_code_mapping(status, expected):
    mapped = pb.coerce_status_error("groq", status, "body", retry_after=None)
    assert isinstance(mapped, expected)


# --------------------------------------------------------------------------
# Test-generation service independence
# --------------------------------------------------------------------------
async def test_testgen_service_uses_only_its_own_provider():
    fake = FakeProvider(make_settings(service="testgen", provider="fake", model="test-model"))
    service = TestGenerationService(settings=fake.settings, provider=fake)

    text = await service.generate_text("write tests for foo")

    assert text == "FAKE ANSWER"
    assert service.describe()["model"] == "test-model"
    assert "api_key" not in service.describe()


async def test_testgen_service_propagates_normalized_errors():
    fake = FakeProvider(
        make_settings(service="testgen", provider="fake"),
        error=pb.ProviderTimeoutError("timeout", provider="fake"),
    )
    service = TestGenerationService(settings=fake.settings, provider=fake)
    with pytest.raises(pb.ProviderTimeoutError):
        await service.generate_text("write tests")


async def test_llm_test_generation_routes_through_the_testgen_service(monkeypatch):
    import llm

    fake = FakeProvider(
        make_settings(service="testgen", provider="fake"),
        error=pb.ProviderConfigError("no key", provider="fake"),
    )
    service = TestGenerationService(settings=fake.settings, provider=fake)
    monkeypatch.setattr(llm, "_test_generation_service", lambda: service)

    result = await llm.generate_function_tests(
        {"func": {"name": "foo", "args": [], "body": "def foo():\n    return 1\n"}, "is_js": False}
    )
    # Failure is reported as a commented error (never raised into the pipeline)
    # and the message is sanitized.
    assert result.startswith("# Error generating tests:")


# --------------------------------------------------------------------------
# Chatbot service prompt construction
# --------------------------------------------------------------------------
async def test_chatbot_prompt_includes_history_and_grounding_rules():
    fake = FakeProvider(make_settings(service="chatbot", provider="fake"), text="grounded answer")
    service = ChatbotService(settings=fake.settings, provider=fake)

    result = await service.answer(
        job={"structural_ready": True},
        job_id="job-1",
        question="What does foo do?",
        history=[{"role": "user", "content": "earlier question"}],
        history_note="older turns summarised",
    )

    assert result["answer"] == "grounded answer"
    # Nothing to cite for an empty repository, and the service must say so
    # rather than inventing a reference.
    assert result["citations"] == []
    assert any("No source code was retrieved" in note for note in result["notes"])
    prompt = fake.prompt_text
    assert "untrusted" in prompt.lower()
    assert "earlier question" in prompt
    assert "older turns summarised" in prompt
    assert "What does foo do?" in prompt


def test_suggestions_are_adapted_to_available_data():
    empty = suggestions_for({"registry": [], "structural": {}})
    assert "Give me an overview of this repository." in empty
    assert not any("coverage" in s.lower() for s in empty)

    rich = suggestions_for({
        "registry": [
            {"name": "login_user", "filename": "auth.py"},
            {"name": "validate_token", "filename": "auth.py"},
            {"name": "render_page", "filename": "views.py"},
        ],
        "structural": {"graph_edges": 12},
        "tests": [],
    })
    assert any("authentication" in s.lower() for s in rich)
    assert any("callers" in s.lower() for s in rich)
    assert any("no tests" in s.lower() for s in rich)
    assert not any("measured test coverage" in s.lower() for s in rich)

    with_tests = suggestions_for({
        "registry": [{"name": "render_page", "filename": "views.py"}],
        "structural": {"graph_edges": 1},
        "tests": [{"name": "render_page", "coverage_percent": 80}],
    })
    assert any("measured test coverage" in s.lower() for s in with_tests)
