"""Rate-limit handling.

Groq's free tier is metered per *day* (200,000 tokens) as well as per minute.
A daily exhaustion cannot be retried away, so it must be detected, latched with
an automatic expiry, and skipped in favour of the fallback provider - never
retried in a loop that stalls the user's request for minutes.
"""
import asyncio

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


def _stub_groq(body: str) -> "lp.GroqProvider":
    provider = lp.GroqProvider.__new__(lp.GroqProvider)
    provider.api_key = "test-key"
    provider.default_model = "openai/gpt-oss-120b"
    provider._quota_exhausted_until = 0.0

    class _Client:
        async def post(self, *args, **kwargs):
            return _StubResponse(body)

    provider._client = _Client()
    return provider


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
    assert provider._quota_exhausted_until > 0


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
