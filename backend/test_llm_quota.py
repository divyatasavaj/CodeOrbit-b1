"""Rate-limit handling.

Groq's free tier is metered per *day* (200,000 tokens) as well as per minute.
A daily exhaustion cannot be retried away, so it must be detected, latched with
an automatic expiry, and skipped in favour of the fallback provider - never
retried in a loop that stalls the user's request for minutes.
"""
import asyncio
import time

import pytest

import llm_provider as lp

DAILY_429 = (
    '{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` on '
    'tokens per day (TPD): Limit 200000, Used 199969, Requested 930. '
    'Please try again in 6m28.368s. Need more tokens? Upgrade to Dev Tier."}}'
)


class _StubResponse:
    status_code = 429
    headers: dict = {}

    def __init__(self, text):
        self.text = text


class _OkResponse:
    status_code = 200
    headers: dict = {}
    text = ""

    def __init__(self, text):
        self._text = text

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


def _stub_multi_key(keys, responder) -> "lp.GroqProvider":
    """A GroqProvider with several keys and no real network client."""
    provider = lp.GroqProvider.__new__(lp.GroqProvider)
    provider.api_keys = list(keys)
    provider.api_key = keys[0] if keys else None
    provider.default_model = "openai/gpt-oss-120b"
    provider._key_quota_until = [0.0] * len(keys)
    provider._key_index = 0

    class _Client:
        async def post(self, url, headers=None, json=None):
            return responder(headers["Authorization"])

    provider._client = _Client()
    return provider


def _stub_groq(body: str) -> "lp.GroqProvider":
    return _stub_multi_key(["test-key"], lambda auth: _StubResponse(body))


def test_daily_quota_is_distinguished_from_per_minute():
    assert lp._is_daily_quota_message(DAILY_429)
    assert not lp._is_daily_quota_message(
        '{"error":{"message":"Rate limit reached on tokens per minute (TPM): Limit 8000"}}'
    )


def test_retry_delays_are_parsed_from_provider_wording():
    assert lp._seconds_until_retry("Please try again in 6m28.368s") == pytest.approx(388.368, abs=0.01)
    assert lp._seconds_until_retry("Please retry in 18h39m39.09s") == pytest.approx(
        18 * 3600 + 39 * 60 + 39.09, abs=0.05
    )
    assert lp._seconds_until_retry("no duration here") == 0.0


def test_daily_quota_latches_and_disables_the_provider():
    provider = _stub_groq(DAILY_429)
    assert provider.is_available() is True

    with pytest.raises(lp.QuotaExhaustedError):
        asyncio.run(provider.generate("write a test"))

    # Latched: no further network attempts until the window expires.
    assert provider.is_available() is False
    assert provider._key_quota_until[0] > 0


def test_router_prefers_an_available_provider_over_an_exhausted_one():
    class _Provider:
        def __init__(self, name, available):
            self.name = name
            self._available = available

        def is_available(self):
            return self._available

    router = lp.LLMRouter.__new__(lp.LLMRouter)
    dead = _Provider("groq", False)
    alive = _Provider("gemini", True)
    router.primary_provider = dead
    router.fallback_provider = alive

    assert router.provider_order() == [alive]


def test_a_second_key_is_used_when_the_first_is_daily_exhausted():
    seen = []

    def respond(auth):
        seen.append(auth)
        if auth == "Bearer key-one":
            return _StubResponse(DAILY_429)
        return _OkResponse("GENERATED")

    provider = _stub_multi_key(["key-one", "key-two"], respond)

    assert asyncio.run(provider.generate("write a test")) == "GENERATED"
    assert "Bearer key-one" in seen and "Bearer key-two" in seen
    assert provider._key_quota_until[0] > time.time()  # first key latched
    assert provider.is_available() is True             # second key still serves


def test_all_keys_exhausted_reports_quota_not_an_endless_retry():
    provider = _stub_multi_key(
        ["key-one", "key-two"], lambda auth: _StubResponse(DAILY_429)
    )

    with pytest.raises(lp.QuotaExhaustedError):
        asyncio.run(provider.generate("write a test"))

    assert provider.is_available() is False
