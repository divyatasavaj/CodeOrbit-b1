"""
LLM Provider abstraction layer.
Groq (PRIMARY) → Gemini (FALLBACK)
With raw httpx to bypass SDK auto-retry, controlled concurrency, and short retry backoff.
"""
import os
import re
import random
import time
import asyncio
import logging
import httpx
from abc import ABC, abstractmethod
from typing import List, Optional, Any
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger("codeoracle")

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()

MAX_LLM_RETRIES = int(os.environ.get("MAX_LLM_RETRIES", "2"))
MAX_CONCURRENT_LLM_REQUESTS = int(os.environ.get("MAX_CONCURRENT_LLM_REQUESTS", "2"))
LLM_RETRY_BASE_DELAY = float(os.environ.get("LLM_RETRY_BASE_DELAY", "0.3"))
LLM_RETRY_MAX_DELAY = float(os.environ.get("LLM_RETRY_MAX_DELAY", "2.0"))
# Rate-limit (429) retries are counted separately and wait for the window the
# provider tells us to wait for, instead of the short transient backoff.
GROQ_429_MAX_RETRIES = int(os.environ.get("GROQ_429_MAX_RETRIES", "3"))
# A single throttled call must never stall a user action for minutes. Groq's
# ``retry-after`` is a conservative upper bound; the per-call cap plus the
# total budget below keep one 429 to seconds before we fail over or report.
GROQ_429_MAX_WAIT = float(os.environ.get("GROQ_429_MAX_WAIT", "10.0"))
GROQ_429_TOTAL_WAIT = float(os.environ.get("GROQ_429_TOTAL_WAIT", "25.0"))
# Generation output ceiling. The free tier meters a request against the 8k
# tokens/minute window, so reserving 4096 output tokens per call left room for
# barely one request before throttling. Tests/refactors for a single function
# fit well under this.
GROQ_MAX_OUTPUT_TOKENS = max(256, int(os.environ.get("GROQ_MAX_OUTPUT_TOKENS", "2048")))


def _parse_duration_seconds(value: str) -> float:
    """Parse provider duration strings such as '43.822s', '1m2.5s' or '500ms'."""
    if not value:
        return 0.0
    value = value.strip()
    try:
        if value.endswith("ms"):
            return float(value[:-2]) / 1000.0
        if "m" in value:
            minutes, _, seconds = value.partition("m")
            seconds = seconds.rstrip("s") or "0"
            return float(minutes) * 60 + float(seconds)
        if value.endswith("s"):
            value = value[:-1]
        return float(value)
    except ValueError:
        return 0.0


def _groq_retry_after(resp) -> float:
    """Seconds to wait before retrying a Groq 429, from its rate-limit headers.

    Groq sends a conservative ``retry-after`` (we have observed >=90s) but the
    *token* window that actually frees capacity is reported separately and is
    usually only a second or two. Taking the smaller positive signal keeps a
    throttled call to seconds instead of minutes; the caller still bounds the
    total time spent.
    """
    headers = resp.headers
    signals = [
        _parse_duration_seconds(headers.get("retry-after") or headers.get("Retry-After")),
        _parse_duration_seconds(headers.get("x-ratelimit-reset-tokens", "")),
    ]
    positive = [s for s in signals if s > 0]
    wait = min(positive) if positive else 2.0
    return min(max(wait, 0.5), GROQ_429_MAX_WAIT)


def _seconds_until_retry(message: str) -> float:
    """Parse a provider's 'try again in 6m28.4s' / '18h57m46.7s' wording."""
    if not message:
        return 0.0
    match = re.search(r"(?:try again|retry)\s+in\s+([0-9hms.]+)", message, re.IGNORECASE)
    if not match:
        return 0.0
    total = 0.0
    for value, unit in re.findall(r"([0-9.]+)([hms])", match.group(1)):
        try:
            total += float(value) * {"h": 3600.0, "m": 60.0, "s": 1.0}[unit]
        except (TypeError, ValueError):
            continue
    return total


def _is_daily_quota_message(body: str) -> bool:
    """True when a 429 is the *daily* token budget, which retrying cannot fix."""
    lowered = (body or "").lower()
    return "per day" in lowered or "tpd" in lowered or "per_day" in lowered

class QuotaExhaustedError(Exception):
    """Raised when API quota is exhausted (daily limit)."""
    def __init__(self, provider: str, message: str = ""):
        self.provider = provider
        super().__init__(f"{provider} quota exhausted: {message}")

class RateLimitError(Exception):
    """Raised when API rate limit is hit (transient, retryable)."""
    def __init__(self, provider: str, retry_after: float = 0, message: str = ""):
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(f"{provider} rate limited: {message}")

class TokenBudgetLimiter:
    """Simple token budget limiter that prevents exceeding API rate limits."""
    
    def __init__(self, tokens_per_minute: int, tokens_per_day: int):
        self.tokens_per_minute = tokens_per_minute
        self.tokens_per_day = tokens_per_day
        self.tokens_available = tokens_per_minute
        self.last_refill = time.time()
        self.daily_tokens_used = 0
        self._lock = asyncio.Lock()
    
    async def acquire(self, estimated_tokens: int) -> bool:
        async with self._lock:
            self._refill()
            if self.tokens_available >= estimated_tokens:
                self.tokens_available -= estimated_tokens
                self.daily_tokens_used += estimated_tokens
                return True
            return False
    
    def release(self, estimated_tokens: int):
        self.tokens_available += estimated_tokens
    
    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        minute_elapsed = elapsed / 60.0
        refill_amount = int(minute_elapsed * self.tokens_per_minute)
        self.tokens_available = min(self.tokens_per_minute, self.tokens_available + refill_amount)
        self.last_refill = now
    
    def get_available(self) -> int:
        self._refill()
        return self.tokens_available
    
    def get_daily_usage(self) -> int:
        return self.daily_tokens_used

class LLMProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: pass
    
    @abstractmethod
    async def generate(self, prompt: str, model: Optional[str] = None) -> str: pass
    
    @abstractmethod
    def is_available(self) -> bool: pass

class GroqProvider(LLMProvider):
    GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
    
    def __init__(self):
        # One key, or several comma-separated keys in GROQ_API_KEYS. Every key
        # carries its own 8k tokens/minute and 200k tokens/day budget, so
        # adding keys multiplies throughput instead of queueing behind one cap.
        raw_keys = os.environ.get("GROQ_API_KEYS") or os.environ.get("GROQ_API_KEY") or ""
        self.api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        self.api_key = self.api_keys[0] if self.api_keys else None
        self.default_model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
        self._client: Optional[httpx.AsyncClient] = None
        # Per-key wall-clock time until which that key's daily budget is spent.
        self._key_quota_until = [0.0] * len(self.api_keys)
        self._key_index = 0
        self._init_client()

    @property
    def name(self) -> str: return "groq"

    def _init_client(self):
        if not self.api_keys:
            logger.warning("GROQ_API_KEY / GROQ_API_KEYS not set")
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        logger.info(
            f"Groq initialized (raw httpx): model={self.default_model} keys={len(self.api_keys)}"
        )

    def _available_key_indices(self) -> List[int]:
        now = time.time()
        return [i for i in range(len(self.api_keys)) if now >= self._key_quota_until[i]]

    def is_available(self) -> bool:
        return self._client is not None and bool(self._available_key_indices())

    async def generate(self, prompt: str, model: Optional[str] = None) -> str:
        if not self._client:
            raise ValueError("Groq client not initialized. Check GROQ_API_KEY.")
        model_name = model or self.default_model

        last_error: Optional[Exception] = None
        wait_hint = 0.0
        waited = 0.0

        for round_index in range(GROQ_429_MAX_RETRIES + 1):
            key_indices = self._available_key_indices()
            if not key_indices:
                raise last_error or QuotaExhaustedError(
                    "groq",
                    f"all {len(self.api_keys)} Groq API key(s) have spent their daily token budget",
                )

            # Spread load across keys, starting after the last one used.
            start = self._key_index % len(key_indices)
            self._key_index += 1
            ordered = key_indices[start:] + key_indices[:start]

            for key_index in ordered:
                resp = await self._client.post(
                    self.GROQ_API_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_keys[key_index]}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": GROQ_MAX_OUTPUT_TOKENS,
                        "temperature": 0.3,
                    },
                )

                if resp.status_code == 200:
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"].strip()
                    if not text:
                        raise Exception("Empty response from Groq")
                    return text

                if resp.status_code == 429:
                    body = resp.text or ""
                    if _is_daily_quota_message(body):
                        reset_in = max(_seconds_until_retry(body), 60.0)
                        self._key_quota_until[key_index] = time.time() + reset_in
                        logger.warning(
                            "Groq key #%d daily budget exhausted (resets in %.1f min); rotating to the next key",
                            key_index + 1, reset_in / 60.0,
                        )
                        last_error = QuotaExhaustedError("groq", body[:300])
                    else:
                        wait_hint = _groq_retry_after(resp)
                        last_error = RateLimitError("groq", wait_hint, body[:300])
                        logger.info(
                            "Groq key #%d hit its per-minute window; trying another key",
                            key_index + 1,
                        )
                    continue  # another key may still have headroom

                if resp.status_code in (502, 503):
                    last_error = Exception(f"Groq API error {resp.status_code}: {resp.text[:200]}")
                    continue

                raise Exception(f"Groq API error {resp.status_code}: {resp.text[:500]}")

            # Every available key was throttled this round. Wait out the short
            # token window (bounded) rather than stalling for minutes.
            delay = min(
                max(wait_hint, 0.5) * (2 ** round_index) + random.uniform(0, 0.5),
                GROQ_429_MAX_WAIT,
            )
            if waited + delay > GROQ_429_TOTAL_WAIT:
                break
            waited += delay
            logger.warning(
                "Groq rate limited on all %d key(s); waiting %.1fs then retrying "
                "(%d/%d, %.1fs of %.1fs budget used)",
                len(ordered), delay, round_index + 1, GROQ_429_MAX_RETRIES,
                waited, GROQ_429_TOTAL_WAIT,
            )
            await asyncio.sleep(delay)

        raise last_error or Exception("Groq: max retries exceeded")

class GeminiProvider(LLMProvider):
    GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    
    def __init__(self):
        self.api_key = os.environ.get("GEMINI_API_KEY")
        self.default_model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
        self._client: Optional[httpx.AsyncClient] = None
        # Wall-clock time until which the daily quota stays exhausted, so the
        # provider recovers on its own instead of needing a process restart.
        self._quota_exhausted_until = 0.0
        self._init_client()
    
    @property
    def name(self) -> str: return "gemini"
    
    def _init_client(self):
        if not self.api_key:
            logger.warning("GEMINI_API_KEY not set")
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        logger.info(f"Gemini initialized (raw httpx): model={self.default_model}")
    
    def is_available(self) -> bool:
        if time.time() < self._quota_exhausted_until: return False
        return self._client is not None and self.api_key is not None
    
    def _extract_text(self, data: dict) -> str:
        try:
            candidates = data.get("candidates", [])
            if not candidates: return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            texts = [p.get("text", "") for p in parts if p.get("text")]
            return "".join(texts).strip()
        except Exception: return ""
    
    def _is_daily_quota_error(self, data: dict) -> bool:
        error = data.get("error", {})
        code = error.get("code", 0)
        status = error.get("status", "")
        message = error.get("message", "").lower()
        details = error.get("details", [])
        if code != 429: return False
        if status == "RESOURCE_EXHAUSTED":
            for detail in details:
                violations = detail.get("violations", [])
                for v in violations:
                    quota_id = v.get("quotaId", "")
                    if "PerDay" in quota_id or "PerProject" in quota_id: return True
            if "daily" in message or "perday" in message.replace(" ", ""): return True
        if "limit" in message: return True
        return False
    
    async def generate(self, prompt: str, model: Optional[str] = None) -> str:
        if not self._client: raise ValueError("Gemini client not initialized. Check GEMINI_API_KEY.")
        if time.time() < self._quota_exhausted_until:
            remaining = self._quota_exhausted_until - time.time()
            raise QuotaExhaustedError("gemini", f"daily quota exhausted; resets in {remaining/60.0:.0f} min")
        model_name = model or self.default_model
        request_start = time.time()
        
        for attempt in range(MAX_LLM_RETRIES + 1):
            try:
                resp = await self._client.post(
                    self.GEMINI_API_URL.format(model=model_name), params={"key": self.api_key},
                    json={"contents": [{"parts": [{"text": prompt}]}]}
                )
                
                if resp.status_code == 200:
                    data = resp.json()
                    text = self._extract_text(data)
                    if not text: raise Exception("Empty response from Gemini")
                    return text
                
                if resp.status_code == 429:
                    data = resp.json()
                    if self._is_daily_quota_error(data):
                        message = str(data.get("error", {}).get("message", ""))
                        reset_in = max(_seconds_until_retry(message), 60.0)
                        self._quota_exhausted_until = time.time() + reset_in
                        logger.warning(
                            "Gemini daily quota exhausted (resets in %.1f min): %s",
                            reset_in / 60.0, message[:200],
                        )
                        raise QuotaExhaustedError("gemini", message)
                    if attempt < MAX_LLM_RETRIES:
                        base_delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)
                        jitter = random.uniform(0, 0.3)
                        delay = min(base_delay + jitter, LLM_RETRY_MAX_DELAY)
                        await asyncio.sleep(delay)
                        continue
                    else:
                        raise RateLimitError("gemini", 0, f"429 after {MAX_LLM_RETRIES} retries")
                
                if resp.status_code in (503, 502):
                    if attempt < MAX_LLM_RETRIES:
                        base_delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)
                        delay = min(base_delay, 3.0)
                        await asyncio.sleep(delay)
                        continue
                
                raise Exception(f"Gemini API error {resp.status_code}: {resp.text[:500]}")
            
            except httpx.TimeoutException:
                if attempt < MAX_LLM_RETRIES:
                    await asyncio.sleep(LLM_RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                raise
            
        raise Exception("Gemini: max retries exceeded")

class LLMRouter:
    def __init__(self):
        self.groq = GroqProvider()
        self.gemini = GeminiProvider()
        self.primary_provider = None
        self.fallback_provider = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_REQUESTS)
        self.token_budget_limiter = TokenBudgetLimiter(
            int(os.environ.get("LLM_TOKENS_PER_MINUTE", "28000")),
            int(os.environ.get("LLM_TOKENS_PER_DAY", "1000000"))
        )
        
        provider_name = os.environ.get("LLM_PROVIDER", "groq").lower()
        if provider_name == "groq" and self.groq.is_available():
            self.primary_provider = self.groq
            self.fallback_provider = self.gemini
        elif self.gemini.is_available():
            self.primary_provider = self.gemini
            self.fallback_provider = None
        elif self.groq.is_available():
            self.primary_provider = self.groq
            self.fallback_provider = None
        else:
            logger.error("No LLM provider available!")
        
        logger.info(f"LLM Router: primary={self.primary_provider.name if self.primary_provider else 'none'}, fallback={self.fallback_provider.name if self.fallback_provider else 'none'}, max_retries={MAX_LLM_RETRIES}, max_concurrent={MAX_CONCURRENT_LLM_REQUESTS}")
    
    async def generate(self, prompt: str, model: Optional[str] = None) -> str:
        order = self.provider_order()
        if not order:
            raise QuotaExhaustedError(
                "llm",
                "No LLM provider has remaining quota right now. Groq's free tier is "
                "200,000 tokens/day and Gemini's is 500 requests/day; both refill on "
                "their own schedule or can be raised with a paid tier.",
            )
        async with self._semaphore:
            estimated_tokens = max(len(prompt) // 4, 100)
            if not await self.token_budget_limiter.acquire(estimated_tokens):
                raise Exception("Token budget exhausted - too many concurrent requests")
            try:
                last_error: Optional[Exception] = None
                for index, provider in enumerate(order):
                    if index:
                        logger.info(
                            "[Router] Falling back to (%s) after %s",
                            provider.name, last_error,
                        )
                    try:
                        return await provider.generate(prompt, model)
                    except (RateLimitError, QuotaExhaustedError) as exc:
                        # Out of capacity, not a code fault: try the next
                        # provider rather than making the user wait it out.
                        last_error = exc
                        continue
                raise last_error if last_error else RuntimeError("No provider produced a response")
            finally:
                self.token_budget_limiter.release(estimated_tokens)

    def provider_order(self) -> list:
        """Available providers, most-preferred first (exhausted ones skipped)."""
        order = []
        for provider in (self.primary_provider, self.fallback_provider):
            if provider is not None and provider.is_available() and provider not in order:
                order.append(provider)
        return order

    async def generate_split(self, prompts: list, model: Optional[str] = None) -> list:
        """Split prompts between Groq and Gemini, run concurrently, return merged results in original order."""
        if not self.primary_provider or not self.fallback_provider:
            # Fallback to sequential if only one provider available
            results = []
            for p in prompts:
                results.append(await self.generate(p, model))
            return results
        
        mid = len(prompts) // 2
        groq_prompts = prompts[:mid]
        gemini_prompts = prompts[mid:]
        
        async def run_on_provider(provider, provider_prompts):
            results = []
            for p in provider_prompts:
                try:
                    results.append(await provider.generate(p, model))
                except Exception as e:
                    logger.error(f"[Router] {provider.name} failed for prompt: {e}")
                    results.append(f"Error: {str(e)}")
            return results
        
        # Run both providers concurrently
        groq_task = run_on_provider(self.groq, groq_prompts)
        gemini_task = run_on_provider(self.gemini, gemini_prompts)
        
        groq_results, gemini_results = await asyncio.gather(groq_task, gemini_task, return_exceptions=True)
        
        # Handle exceptions
        if isinstance(groq_results, Exception):
            logger.error(f"[Router] Groq batch failed: {groq_results}")
            groq_results = [f"Error: {groq_results}"] * len(groq_prompts)
        if isinstance(gemini_results, Exception):
            logger.error(f"[Router] Gemini batch failed: {gemini_results}")
            gemini_results = [f"Error: {gemini_results}"] * len(gemini_prompts)
        
        # Merge results back in original order
        return groq_results + gemini_results

_router: Optional[LLMRouter] = None

def get_llm_provider() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router
