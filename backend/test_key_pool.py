"""Multi-key Groq pool: parsing, rotation across many keys, and diagnostics.

The pool is the whole throughput story for a free-tier setup, so these cover
the parts that are easy to get subtly wrong: whitespace/blanks in the
comma-separated list, rotation moving past *several* dead keys in one call, and
the diagnostics endpoint never leaking a key.
"""
import asyncio
import time

import pytest

import llm_provider as lp


class _Ok:
    status_code = 200
    headers: dict = {}

    def __init__(self, text="GENERATED"):
        self._text = text

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


class _Daily429:
    status_code = 429
    headers: dict = {}
    text = '{"error":{"message":"Rate limit reached ... tokens per day (TPD): Limit 200000"}}'


def _provider(keys, responder):
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


def test_key_list_parsing_ignores_blanks_and_whitespace(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", " k1 , k2 ,, k3 ,")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert lp._configured_groq_keys() == ["k1", "k2", "k3"]


def test_single_key_var_is_the_fallback(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "solo")
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    assert lp._configured_groq_keys() == ["solo"]


def test_empty_pool_when_nothing_configured(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", "")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert lp._configured_groq_keys() == []


def test_six_keys_all_exhausted_reports_quota_once():
    keys = [f"key-{i}" for i in range(1, 7)]
    provider = _provider(keys, lambda auth: _Daily429())
    with pytest.raises(lp.QuotaExhaustedError):
        asyncio.run(provider.generate("write a test"))
    # Every key is parked, so the provider drops out and the router falls over.
    assert provider.is_available() is False
    assert all(until > time.time() for until in provider._key_quota_until)


def test_rotation_skips_several_dead_keys_without_stalling():
    keys = [f"key-{i}" for i in range(1, 7)]
    seen = []

    def respond(auth):
        seen.append(auth)
        return _Daily429() if auth in {"Bearer key-1", "Bearer key-2", "Bearer key-3"} else _Ok()

    provider = _provider(keys, respond)
    start = time.time()
    assert asyncio.run(provider.generate("write a test")) == "GENERATED"
    elapsed = time.time() - start

    assert len(seen) == 4                     # three dead keys, then a live one
    assert elapsed < 1.0                      # dead keys are skipped, not waited on
    assert provider._key_quota_until[0] > time.time()
    assert provider._key_quota_until[3] == 0.0  # the serving key is not parked
    assert provider.is_available() is True


def test_key_status_masks_the_key_itself():
    keys = ["gsk_aaaaaaaaaaaaaaaa1111", "gsk_bbbbbbbbbbbbbbbb2222"]
    provider = _provider(keys, lambda auth: _Ok())
    status = provider.key_status()

    assert [row["index"] for row in status] == [1, 2]
    assert all(row["available"] for row in status)
    assert [row["fingerprint"] for row in status] == ["1111", "2222"]
    assert all("gsk_" not in str(row) for row in status)
