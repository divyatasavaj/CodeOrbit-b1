"""
Batch processor for dynamic LLM request batching.
Groups functions into optimal batches based on token limits.
"""
import os
import asyncio
import logging
from typing import List, Dict, Any, Callable, Awaitable, Optional
from dataclasses import dataclass

logger = logging.getLogger("codeoracle")

MAX_CONCURRENT_LLM = int(os.environ.get("MAX_CONCURRENT_LLM_REQUESTS", "3"))
MAX_TOKENS_PER_BATCH = 8000
FUNCTION_TOKEN_ESTIMATE = 150


@dataclass
class BatchResult:
    """Result from processing a batch."""
    items: List[Any]
    errors: List[Exception]
    total_processed: int


def estimate_tokens(text: str) -> int:
    """Rough token estimate (4 chars ≈ 1 token)."""
    return len(text) // 4


def calculate_batch_size(functions: List[Dict[str, Any]], max_tokens: int = MAX_TOKENS_PER_BATCH) -> int:
    """Calculate optimal batch size based on function complexity."""
    if not functions:
        return 1
    
    total_tokens = 0
    for i, func in enumerate(functions):
        body_tokens = estimate_tokens(func.get("body", ""))
        func_tokens = FUNCTION_TOKEN_ESTIMATE + body_tokens
        total_tokens += func_tokens
    
    if total_tokens == 0:
        return len(functions)
    
    avg_tokens_per_func = total_tokens / len(functions)
    optimal_batch_size = max(1, min(len(functions), int(max_tokens / avg_tokens_per_func)))
    
    return optimal_batch_size


def create_batches(functions: List[Dict[str, Any]], max_tokens: int = MAX_TOKENS_PER_BATCH) -> List[List[Dict[str, Any]]]:
    """Split functions into optimally-sized batches."""
    if not functions:
        return []
    
    batch_size = calculate_batch_size(functions, max_tokens)
    batches = []
    
    for i in range(0, len(functions), batch_size):
        batch = functions[i:i + batch_size]
        batches.append(batch)
    
    return batches


async def process_batches(
    items: List[Any],
    process_fn: Callable[[List[Any]], Awaitable[List[Any]]],
    max_concurrent: int = MAX_CONCURRENT_LLM,
    batch_size: Optional[int] = None
) -> BatchResult:
    """
    Process items in batches with controlled concurrency.
    
    Args:
        items: List of items to process
        process_fn: Async function that processes a batch of items
        max_concurrent: Maximum concurrent batch processing
        batch_size: Optional fixed batch size (auto-calculated if None)
    
    Returns:
        BatchResult with processed items and any errors
    """
    if not items:
        return BatchResult(items=[], errors=[], total_processed=0)
    
    if batch_size:
        batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    else:
        batches = create_batches(items)
    
    logger.info(f"Processing {len(items)} items in {len(batches)} batches (max concurrent: {max_concurrent})")
    
    semaphore = asyncio.Semaphore(max_concurrent)
    all_results = []
    all_errors = []
    
    async def process_with_semaphore(batch: List[Any], batch_idx: int) -> List[Any]:
        async with semaphore:
            try:
                logger.info(f"Processing batch {batch_idx + 1}/{len(batches)} ({len(batch)} items)")
                result = await process_fn(batch)
                return result
            except Exception as e:
                logger.error(f"Batch {batch_idx + 1} failed: {e}")
                all_errors.append(e)
                return [None] * len(batch)
    
    tasks = [process_with_semaphore(batch, i) for i, batch in enumerate(batches)]
    batch_results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for result in batch_results:
        if isinstance(result, Exception):
            all_errors.append(result)
        elif isinstance(result, list):
            all_results.extend(result)
    
    return BatchResult(
        items=all_results,
        errors=all_errors,
        total_processed=len(all_results)
    )


async def process_with_retry(
    items: List[Any],
    process_fn: Callable[[Any], Awaitable[Any]],
    max_concurrent: int = MAX_CONCURRENT_LLM,
    retries: int = 2
) -> BatchResult:
    """Process items individually with concurrency control and retry."""
    semaphore = asyncio.Semaphore(max_concurrent)
    all_results = []
    all_errors = []
    
    async def process_with_semaphore(item: Any, idx: int) -> Any:
        async with semaphore:
            last_error = None
            for attempt in range(retries + 1):
                try:
                    result = await process_fn(item)
                    return result
                except Exception as e:
                    last_error = e
                    if attempt < retries:
                        await asyncio.sleep(1.0 * (attempt + 1))
            all_errors.append(last_error)
            return None
    
    tasks = [process_with_semaphore(item, i) for i, item in enumerate(items)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for result in results:
        if isinstance(result, Exception):
            all_errors.append(result)
        elif result is not None:
            all_results.append(result)
    
    return BatchResult(
        items=all_results,
        errors=all_errors,
        total_processed=len(all_results)
    )
