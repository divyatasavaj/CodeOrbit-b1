"""Key pooling in the AI service provider layer.

Test generation reaches the model through ``TestGenerationService`` -> a Groq
adapter that subclasses ``OpenAICompatibleProvider``. That adapter is the only
thing standing between a six-key pool and a single throttled key, so these
cover the two behaviours the pool exists for: rotating past a spent key inside
one request, and parking that key instead of retrying it forever.
"""
import asyncio
import time

import pytest

from app.services.ai.provider_base import (
    AIServiceSettings,
    ProviderAuthError,
    ProviderRateLimitError,
)
from app.services.ai.providers import openai_compatible_provider as ocp
from app.services.ai.providers.groq_provider import GroqProvider

DAILY_429 = (
    '{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` on '
    'tokens per day (TPD): Limit 200000, Used 199969. Please try again in 6m28.368s."}}'
)
MINUTE_429 = (
    '{"error":{"message":"Rate limit reached on tokens per minute (TPM): Limit 8000"}}'
)


class _Response:
    def __init__(self, status_code, text="", body=None):
        self.status_code = status_code
        self.text = text
        self.headers = {}
        self._body = body

    def json(self):
        return self._body


def _ok(text="GENERATED"):
    return _Response(200, body={"choices": [{"message": {"content": text}}], "usage": {}})


def _provider(keys, responder):
    settings = AIServiceSettings(
        service="testgen",
        provider="groq",
        model="openai/gpt-oss-120b",
        api_key=",".join(keys),
        base_url="https://api.groq.com/openai/v1",
    )
    provider = GroqProvider(settings)

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            return responder(headers["Authorization"])

    ocp.httpx.AsyncClient = _Client
    return provider


def test_key_pool_is_parsed_from_the_comma_separated_setting():
    provider = _provider(["k1", "k2", "k3"], lambda auth: _ok())
    assert provider._api_keys() == ["k1", "k2", "k3"]
    assert provider.is_configured() is True
    assert len(provider.key_status()) == 3


def test_rotation_skips_several_spent_keys_without_sleeping():
    keys = [f"gsk_key{i}" for i in range(1, 7)]
    seen = []

    def respond(auth):
        seen.append(auth)
        if auth in {f"Bearer gsk_key{i}" for i in (1, 2, 3)}:
            return _Response(429, DAILY_429)
        return _ok()

    provider = _provider(keys, respond)
    started = time.time()
    response = asyncio.run(provider.generate([ocp.ChatMessage(role="user", content="hi")]))
    elapsed = time.time() - started

    assert response.text == "GENERATED"
    assert len(seen) == 4          # three spent keys, then a live one
    assert elapsed < 1.0           # spent keys are skipped, not waited on
    assert provider._key_quota_until[0] > time.time()
    assert provider._key_quota_until[3] == 0.0


def test_all_keys_spent_raises_one_rate_limit_error_not_a_retry_loop():
    keys = [f"gsk_key{i}" for i in range(1, 7)]
    calls = []

    def respond(auth):
        calls.append(auth)
        return _Response(429, DAILY_429)

    provider = _provider(keys, respond)
    with pytest.raises(ProviderRateLimitError):
        asyncio.run(provider.generate([ocp.ChatMessage(role="user", content="hi")]))

    assert len(calls) == 6                                  # each key tried exactly once
    assert all(row["available"] is False for row in provider.key_status())


def test_second_request_does_not_retry_a_parked_key():
    keys = ["gsk_dead", "gsk_live"]
    calls = []

    def respond(auth):
        calls.append(auth)
        return _Response(429, DAILY_429) if auth == "Bearer gsk_dead" else _ok("SECOND")

    provider = _provider(keys, respond)
    messages = [ocp.ChatMessage(role="user", content="hi")]
    assert asyncio.run(provider.generate(messages)).text == "SECOND"
    calls.clear()

    assert asyncio.run(provider.generate(messages)).text == "SECOND"
    assert calls == ["Bearer gsk_live"]   # the parked key is never re-dialled


def test_non_quota_failures_are_not_swallowed_by_the_pool():
    provider = _provider(["k1", "k2"], lambda auth: _Response(401, "bad key"))
    with pytest.raises(ProviderAuthError):
        asyncio.run(provider.generate([ocp.ChatMessage(role="user", content="hi")]))


def test_daily_quota_detection_ignores_the_per_minute_window():
    assert ocp.is_daily_quota_text(DAILY_429) is True
    assert ocp.is_daily_quota_text(MINUTE_429) is False
    assert ocp.seconds_until_retry("Please try again in 6m28.368s") == pytest.approx(388.368, abs=0.01)
