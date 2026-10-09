"""Shared pytest configuration for the backend suite.

Adds the backend directory to ``sys.path`` (the app is a flat module layout that
boots from ``backend/main.py``) and exposes a deterministic fake provider so the
whole AI layer can be tested offline - no network, no API credits.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio  # noqa: E402
import inspect  # noqa: E402

import pytest  # noqa: E402

from app.services.ai.provider_base import (  # noqa: E402
    AIServiceSettings,
    BaseProvider,
    ChatMessage,
    LLMResponse,
)


class FakeProvider(BaseProvider):
    """Records every call and returns scripted text/errors."""

    name = "fake"

    def __init__(self, settings=None, *, text="FAKE ANSWER", error=None, delay=0.0):
        settings = settings or AIServiceSettings(
            service="test", provider="fake", model="fake-model", api_key="fake-key"
        )
        super().__init__(settings)
        self.calls = []
        self.text = text
        self.error = error
        self.delay = delay

    @property
    def prompt_text(self):
        return "\n".join(m.content for m in (self.calls[-1] if self.calls else []))

    async def _generate_once(self, messages, *, max_tokens, temperature, timeout):
        self.calls.append(list(messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return LLMResponse(
            text=self.text, provider=self.name, model=self.model, latency_ms=3,
            prompt_tokens=11, completion_tokens=7,
        )


def make_settings(**overrides):
    base = dict(service="chatbot", provider="fake", model="fake-model", api_key="fake-key")
    base.update(overrides)
    return AIServiceSettings(**base)


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run ``async def`` tests with asyncio.run - no pytest-asyncio required."""
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    params = inspect.signature(func).parameters
    kwargs = {name: value for name, value in pyfuncitem.funcargs.items() if name in params}
    asyncio.run(func(**kwargs))
    return True


__all__ = ["FakeProvider", "make_settings", "ChatMessage"]
