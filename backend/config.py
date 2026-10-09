"""
Central configuration for CodeOracle.

Every tunable is read from an environment variable with a sensible default so
the pipeline can be scaled without code changes. API keys are never read here
(they are consumed directly by the provider modules) and never logged.
"""
import os
from pathlib import Path

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
LLM_CONCURRENCY = max(1, _int("CODEORACLE_LLM_CONCURRENCY", 5))
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
PROMPT_VERSION_TESTS = os.environ.get("CODEORACLE_PROMPT_VERSION_TESTS", "tests-v2")
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
    _TOTAL_ATTEMPTS = 1 + max(0, _int("CODEORACLE_MAX_TEST_IMPROVEMENT_ATTEMPTS", 2))
TEST_MAX_ATTEMPTS = max(1, _TOTAL_ATTEMPTS)
# Extra "improve the tests, then re-run and re-measure" rounds after the first.
MAX_TEST_IMPROVEMENT_ATTEMPTS = TEST_MAX_ATTEMPTS - 1
# Cache-key component: a suite generated under a different coverage target or
# attempt budget must never be reused as if it satisfied the current one.
TESTS_CACHE_VERSION = (
    f"{PROMPT_VERSION_TESTS}:mc{int(MIN_TEST_COVERAGE)}:at{TEST_MAX_ATTEMPTS}"
)
