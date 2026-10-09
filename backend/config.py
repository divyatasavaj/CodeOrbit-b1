"""
Central configuration for CodeOracle.

Every tunable is read from an environment variable with a sensible default so
the pipeline can be scaled without code changes. API keys are never read here
(they are consumed directly by the provider modules) and never logged.
"""
import os
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# LLM batching / concurrency
# --------------------------------------------------------------------------
LLM_BATCH_SIZE = max(1, _int("CODEORACLE_LLM_BATCH_SIZE", 10))
# Every configured Groq key is an independent 8k tokens/minute budget, so the
# number of keys is the natural degree of parallelism. Falls back to 1 with a
# single key so one account never bursts past its own window.
GROQ_KEY_COUNT = max(1, len([
    k for k in (
        os.environ.get("GROQ_API_KEYS") or os.environ.get("GROQ_API_KEY") or ""
    ).split(",") if k.strip()
]))
LLM_CONCURRENCY = max(1, _int("CODEORACLE_LLM_CONCURRENCY", GROQ_KEY_COUNT))
LLM_MAX_RETRIES = max(0, _int("CODEORACLE_LLM_MAX_RETRIES", 2))
LLM_TIMEOUT = max(5.0, _float("CODEORACLE_LLM_TIMEOUT", 60.0))
# Approximate source-character ceiling for a single batch. Batches split early
# when they would exceed this even if the function count is still below
# LLM_BATCH_SIZE, so a single huge function never blows up a prompt.
LLM_BATCH_MAX_CHARS = max(2000, _int("CODEORACLE_LLM_BATCH_MAX_CHARS", 24000))
LLM_RETRY_BASE_DELAY = max(0.0, _float("LLM_RETRY_BASE_DELAY", 0.5))

# Explicit mock mode for offline benchmarking/tests. Never enabled implicitly.
LLM_MOCK = _bool("CODEORACLE_LLM_MOCK", False)
LLM_MOCK_LATENCY = max(0.0, _float("CODEORACLE_LLM_MOCK_LATENCY", 1.0))

# Per-function context ceilings for LLM prompts (spec sections 5, 7 and 35).
# A whole repository / file is never sent to the model: oversized function
# sources are truncated and the dependency list is capped so one huge
# function cannot blow up a batch prompt or stall generation.
MAX_FUNCTION_CONTEXT_LINES = max(50, _int("CODEORACLE_MAX_FUNCTION_CONTEXT_LINES", 300))
MAX_CONTEXT_CHARS = max(2000, _int("CODEORACLE_MAX_CONTEXT_CHARS", 20000))
MAX_DEPENDENCY_CONTEXT = max(1, _int("CODEORACLE_MAX_DEPENDENCY_CONTEXT", 5))

# AI analysis toggle and priority floor (low|medium|high).
AI_ANALYSIS_ENABLED = _bool("CODEORACLE_AI_ENABLED", True)
AI_MIN_PRIORITY = os.environ.get("CODEORACLE_AI_MIN_PRIORITY", "low").strip().lower()

# Explanation strategy. On by default explanations come from the deterministic,
# AST-grounded static analyzer: instant, no API key, no rate limit and no
# per-function latency. Set CODEORACLE_STATIC_EXPLANATIONS=0 to restore
# LLM-written explanations (tests and refactors are unaffected either way).
STATIC_EXPLANATIONS = _bool("CODEORACLE_STATIC_EXPLANATIONS", True)

# --------------------------------------------------------------------------
# Model / prompt versioning (bump to invalidate caches)
# --------------------------------------------------------------------------
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
PROMPT_VERSION_EXPLANATION = os.environ.get("CODEORACLE_PROMPT_VERSION_EXPLANATION", "explain-v2")
# v3: the placeholder gate now honours measured coverage, so suites that were
# wrongly recorded as failures under v2 must be regenerated rather than reused.
PROMPT_VERSION_TESTS = os.environ.get("CODEORACLE_PROMPT_VERSION_TESTS", "tests-v3")
PROMPT_VERSION_REFACTOR = os.environ.get("CODEORACLE_PROMPT_VERSION_REFACTOR", "refactor-v2")

# --------------------------------------------------------------------------
# Upload / repository safety limits
# --------------------------------------------------------------------------
MAX_ZIP_SIZE_MB = max(1, _int("CODEORACLE_MAX_ZIP_SIZE_MB", 200))
MAX_ZIP_SIZE_BYTES = MAX_ZIP_SIZE_MB * 1024 * 1024
MAX_EXTRACTED_SIZE_MB = max(1, _int("CODEORACLE_MAX_EXTRACTED_SIZE_MB", 500))
MAX_EXTRACTED_SIZE_BYTES = MAX_EXTRACTED_SIZE_MB * 1024 * 1024
MAX_FILE_SIZE_KB = max(1, _int("CODEORACLE_MAX_FILE_SIZE_KB", 800))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_KB * 1024
MAX_FILES = max(1, _int("CODEORACLE_MAX_FILES", 5000))
MAX_GRAPH_NODES = max(1, _int("CODEORACLE_MAX_GRAPH_NODES", 5000))

# ---------------------------------------------------------------------------
# GitHub repository import (public repositories only)
# ---------------------------------------------------------------------------
# Wall-clock budget for fetching a repository archive. Reuses MAX_ZIP_SIZE_* so
# remote imports obey exactly the same size limits as manual uploads.
GITHUB_TIMEOUT = max(10.0, _float("CODEORACLE_GITHUB_TIMEOUT", 120.0))

# Files that are never treated as analyzable source.
SKIP_DIRS = {
    "__pycache__", "node_modules", ".git", "dist", "build", "out",
    ".venv", "venv", "env", "coverage", ".pytest_cache", ".mypy_cache",
    "target", "vendor", ".tox", ".eggs", ".cache", ".next", ".nuxt",
    ".terraform", ".idea", ".vscode", "site-packages", "bower_components",
    ".gradle", ".parcel-cache", ".turbo", "tmp", "logs",
}
SKIP_DIR_SUFFIXES = (".egg-info",)
SKIP_FILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "Pipfile.lock", "composer.lock", "Gemfile.lock",
    "cargo.lock", "uv.lock",
}
SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx"}
JS_EXTENSIONS = {".js", ".ts", ".jsx", ".tsx"}

# Progress persistence throttling: how many completed functions between
# lightweight metadata flushes to the filesystem job store.
PROGRESS_FLUSH_EVERY = max(1, _int("CODEORACLE_PROGRESS_FLUSH_EVERY", 16))

# ---------------------------------------------------------------------------
# Test generation / coverage quality gate
# ---------------------------------------------------------------------------
# Minimum *measured* line coverage a generated per-function test suite must
# reach before it is treated as a successful result. The value always comes
# from a real pytest/node execution of the coverage tool - it is never
# synthesized, rounded up, or hardcoded anywhere in the pipeline.
MIN_TEST_COVERAGE = min(100.0, max(0.0, _float("CODEORACLE_MIN_TEST_COVERAGE", 65.0)))
# Total generation attempts allowed per suite (initial generation + improvement
# rounds) before the loop returns its best measured result. The spec-style
# CODEORACLE_TEST_MAX_ATTEMPTS (total attempts, default 3) takes precedence;
# the legacy improvement-rounds variable is still honoured when it is unset.
_TOTAL_ATTEMPTS = _int("CODEORACLE_TEST_MAX_ATTEMPTS", 0)
if _TOTAL_ATTEMPTS <= 0:
    # Spec-style total-attempt budget. Unset by default, so existing
    # deployments keep the legacy behaviour below; when set it wins.
    _TOTAL_ATTEMPTS = _int("TESTGEN_MAX_ATTEMPTS", 0)
if _TOTAL_ATTEMPTS <= 0:
    _TOTAL_ATTEMPTS = 1 + max(0, _int("CODEORACLE_MAX_TEST_IMPROVEMENT_ATTEMPTS", 2))
TEST_MAX_ATTEMPTS = max(1, _TOTAL_ATTEMPTS)
# Extra "improve the tests, then re-run and re-measure" rounds after the first.
MAX_TEST_IMPROVEMENT_ATTEMPTS = TEST_MAX_ATTEMPTS - 1
# Cache-key component: a suite generated under a different coverage target or
# attempt budget must never be reused as if it satisfied the current one.
TESTS_CACHE_VERSION = (
    f"{PROMPT_VERSION_TESTS}:mc{int(MIN_TEST_COVERAGE)}:at{TEST_MAX_ATTEMPTS}"
)


# ---------------------------------------------------------------------------
# Independent AI service configuration
# ---------------------------------------------------------------------------
# CodeOracle runs two AI features that must never share credentials, models or
# failure domains: the repository chatbot (conversational, latency-sensitive)
# and test generation (long, high-token, correctness-critical). Both are wired
# through backend/app/services/ai/*. Nothing here is ever sent to the browser
# and API keys are never logged.
def _str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip() or default


# Legacy provider-specific variables. They are used ONLY as a fallback so an
# existing deployment keeps working after this change without new secrets - and
# never as a cross-service leak, because each service resolves its own key
# first and keeps its own provider/model/failure state.
_PROVIDER_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "google": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}
# Multi-key variables win over the single-key ones above. Groq keys are an
# independent 8k tokens/minute + 200k tokens/day budget each, so a pool of six
# gives six budgets; the provider adapter rotates across whatever is listed.
# ``GROQ_API_KEYS`` is comma-separated.
_PROVIDER_KEY_ENV_MULTI = {
    "groq": "GROQ_API_KEYS",
}
_PROVIDER_MODEL_ENV = {
    "gemini": "GEMINI_MODEL",
    "google": "GEMINI_MODEL",
    "groq": "GROQ_MODEL",
    "openai": "OPENAI_MODEL",
    "openai_compatible": "OPENAI_COMPATIBLE_MODEL",
}
_PROVIDER_MODEL_DEFAULT = {
    # Current, non-deprecated Gemini model. Older names (gemini-2.5-flash,
    # gemini-2.0-flash) now return 404 for new accounts.
    "gemini": "gemini-3.8-flash",
    "google": "gemini-3.8-flash",
    "groq": "llama-3.1-8b-instant",
    "openai": "gpt-4o-mini",
    "openai_compatible": "",
}
_PROVIDER_BASE_URL_DEFAULT = {
    "gemini": "",
    "google": "",
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "openai_compatible": "",
}
# Providers a deployment may select without code changes. Anything else needs a
# new adapter registered in app/services/ai/provider_factory.py.
SUPPORTED_AI_PROVIDERS = ("gemini", "groq", "openai_compatible")


def _ai_service_config(
    prefix: str,
    *,
    default_provider: str,
    default_temperature: float,
    default_max_tokens: int,
) -> Dict[str, Any]:
    """Resolve one independent AI service configuration from the environment."""
    provider = _str(f"{prefix}_PROVIDER", default_provider).lower()
    model = (
        _str(f"{prefix}_MODEL", "")
        or _str(_PROVIDER_MODEL_ENV.get(provider, ""), "")
        or _PROVIDER_MODEL_DEFAULT.get(provider, "")
    )
    api_key = (
        _str(f"{prefix}_API_KEY", "")
        or _str(_PROVIDER_KEY_ENV_MULTI.get(provider, ""), "")
        or _str(_PROVIDER_KEY_ENV.get(provider, ""), "")
    )
    base_url = _str(f"{prefix}_BASE_URL", "") or _PROVIDER_BASE_URL_DEFAULT.get(provider, "")
    return {
        "service": prefix.lower(),
        "provider": provider,
        "model": model,
        "api_key": api_key or None,
        "base_url": base_url or None,
        "timeout_seconds": max(5.0, _float(f"{prefix}_TIMEOUT_SECONDS", 45.0)),
        "max_retries": max(0, _int(f"{prefix}_MAX_RETRIES", 2)),
        "temperature": _float(f"{prefix}_TEMPERATURE", default_temperature),
        "max_tokens": max(256, _int(f"{prefix}_MAX_TOKENS", default_max_tokens)),
        "retry_base_delay": max(0.0, _float(f"{prefix}_RETRY_BASE_DELAY", 0.5)),
        "max_backoff_seconds": max(0.5, _float(f"{prefix}_MAX_BACKOFF_SECONDS", 8.0)),
    }


def chatbot_ai_config() -> Dict[str, Any]:
    """Chatbot AI configuration - independent from test generation."""
    return _ai_service_config(
        "CHATBOT", default_provider="gemini", default_temperature=0.2, default_max_tokens=2048
    )


def testgen_ai_config() -> Dict[str, Any]:
    """Test-generation AI configuration - independent from the chatbot.

    The default provider stays Gemini because that is what the existing
    per-function test pipeline used before these settings existed; a deployment
    that wants another provider only has to set TESTGEN_PROVIDER.
    """
    return _ai_service_config(
        "TESTGEN", default_provider="gemini", default_temperature=0.1, default_max_tokens=4096
    )


# Retrieval / context budget for the chatbot (spec section 9). The chatbot
# never sends a whole repository: these bound how much source is retrieved per
# question so latency, cost and context-overflow risk stay predictable.
CHATBOT_MAX_CONTEXT_ITEMS = max(1, _int("CHATBOT_MAX_CONTEXT_ITEMS", 12))
CHATBOT_MAX_CONTEXT_CHARS = max(2000, _int("CHATBOT_MAX_CONTEXT_CHARS", 24000))
CHATBOT_MAX_SOURCE_LINES = max(10, _int("CHATBOT_MAX_SOURCE_LINES", 90))
CHATBOT_MAX_CITATIONS = max(1, _int("CHATBOT_MAX_CITATIONS", 6))
CHATBOT_DEPENDENCY_LIMIT = max(0, _int("CHATBOT_DEPENDENCY_LIMIT", 3))
CHATBOT_GRAPH_SEEDS = max(1, _int("CHATBOT_GRAPH_SEEDS", 4))
CHATBOT_MAX_HISTORY_MESSAGES = max(2, _int("CHATBOT_MAX_HISTORY_MESSAGES", 12))
CHATBOT_MAX_HISTORY_CHARS = max(1000, _int("CHATBOT_MAX_HISTORY_CHARS", 6000))
CHATBOT_MAX_MESSAGE_CHARS = max(200, _int("CHATBOT_MAX_MESSAGE_CHARS", 2000))
CHATBOT_RATE_LIMIT_PER_MINUTE = max(1, _int("CHATBOT_RATE_LIMIT_PER_MINUTE", 20))
CHATBOT_MAX_CONCURRENT = max(1, _int("CHATBOT_MAX_CONCURRENT", 2))

_TESTGEN_CFG = testgen_ai_config()
TESTGEN_TEMPERATURE = _TESTGEN_CFG["temperature"]
TESTGEN_MAX_TOKENS = _TESTGEN_CFG["max_tokens"]
TESTGEN_MAX_ATTEMPTS = TEST_MAX_ATTEMPTS
# Identity of the current test-generation backend. Part of the tests cache key
# so a suite generated with one provider/model is never replayed as if it came
# from another.
TESTGEN_CACHE_ID = f"{_TESTGEN_CFG['provider']}:{_TESTGEN_CFG['model'] or 'unset'}"
