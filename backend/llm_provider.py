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
from typing import Optional, Any
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
        self.api_key = os.environ.get("GROQ_API_KEY")
        self.default_model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
        self._client: Optional[httpx.AsyncClient] = None
        self._init_client()
    
    @property
    def name(self) -> str: return "groq"
    
    def _init_client(self):
        if not self.api_key:
            logger.warning("GROQ_API_KEY not set")
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        logger.info(f"Groq initialized (raw httpx): model={self.default_model}")
    
    def is_available(self) -> bool:
        return self._client is not None and self.api_key is not None
    
    async def generate(self, prompt: str, model: Optional[str] = None) -> str:
        if not self._client:
            raise ValueError("Groq client not initialized. Check GROQ_API_KEY.")
        model_name = model or self.default_model
        request_start = time.time()
        
        for attempt in range(MAX_LLM_RETRIES + 1):
            try:
                resp = await self._client.post(
                    self.GROQ_API_URL,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={"model": model_name, "messages": [{"role": "user", "content": prompt}], "max_tokens": 4096, "temperature": 0.3},
                )
                
                if resp.status_code == 200:
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"].strip()
                    if not text:
                        raise Exception("Empty response from Groq")
                    return text
                
                if resp.status_code == 429:
                    retry_after = 0
                    ra_header = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
                    if ra_header:
                        try:
                            retry_after = float(ra_header)
                        except ValueError:
                            pass
                    if attempt < MAX_LLM_RETRIES:
                        base_delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)
                        jitter = random.uniform(0, 0.3)
                        delay = min(base_delay + jitter, LLM_RETRY_MAX_DELAY)
                        await asyncio.sleep(delay)
                        continue
                    else:
                        raise RateLimitError("groq", retry_after, f"429 after {MAX_LLM_RETRIES} retries")
                
                if resp.status_code in (503, 502):
                    if attempt < MAX_LLM_RETRIES:
                        base_delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)
                        delay = min(base_delay, 3.0)
                        await asyncio.sleep(delay)
                        continue
                
                raise Exception(f"Groq API error {resp.status_code}: {resp.text[:500]}")
            
            except httpx.TimeoutException:
                if attempt < MAX_LLM_RETRIES:
                    await asyncio.sleep(LLM_RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                raise
            
        raise Exception("Groq: max retries exceeded")

class GeminiProvider(LLMProvider):
    GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    
    def __init__(self):
        self.api_key = os.environ.get("GEMINI_API_KEY")
        self.default_model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
        self._client: Optional[httpx.AsyncClient] = None
        self._quota_exhausted = False
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
        if self._quota_exhausted: return False
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
        if self._quota_exhausted: raise QuotaExhaustedError("gemini", "daily quota previously exhausted")
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
                        self._quota_exhausted = True
                        raise QuotaExhaustedError("gemini", str(data.get("error", {}).get("message", "")))
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
        if not self.primary_provider: raise ValueError("No LLM provider available. Check API keys.")
        async with self._semaphore:
            estimated_tokens = max(len(prompt) // 4, 100)
            if not await self.token_budget_limiter.acquire(estimated_tokens):
                raise Exception("Token budget exhausted - too many concurrent requests")
            try:
                return await self.primary_provider.generate(prompt, model)
            except RateLimitError:
                if self.fallback_provider and self.fallback_provider.is_available():
                    logger.info(f"[Router] Primary ({self.primary_provider.name}) rate limited, falling back to ({self.fallback_provider.name})")
                    try:
                        return await self.fallback_provider.generate(prompt, model)
                    except QuotaExhaustedError: raise
                    except Exception as fallback_err:
                        logger.error(f"[Router] Fallback ({self.fallback_provider.name}) failed: {fallback_err}")
                        raise
                raise
            except QuotaExhaustedError:
                if self.fallback_provider and self.fallback_provider.is_available():
                    logger.info(f"[Router] Primary ({self.primary_provider.name}) quota exhausted, falling back to ({self.fallback_provider.name})")
                    try:
                        return await self.fallback_provider.generate(prompt, model)
                    except QuotaExhaustedError: raise
                    except Exception as fallback_err:
                        logger.error(f"[Router] Fallback ({self.fallback_provider.name}) failed: {fallback_err}")
                        raise
                raise
            finally:
                self.token_budget_limiter.release(estimated_tokens)

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
