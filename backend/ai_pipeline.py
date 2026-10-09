"""
Progressive AI analysis pipeline.

Responsibilities
----------------
* Look up per-function results in the content-addressed cache first.
* Order remaining work by deterministic static priority.
* Pack functions into size-bounded batches and run them with a bounded
  concurrency worker pool (the real global limit lives in llm.SEM).
* Validate JSON, retry once with a stricter prompt, fall back to the local
  grounded AST engine and keep going - one bad batch never fails the job.
* Publish lightweight, batch-level progress events for SSE consumers.
* Support cancellation without leaving orphaned tasks.
"""
import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import cache
import config
import function_registry as registry_mod
import llm

logger = logging.getLogger("codeoracle.ai")

# Job-level AI states (mirrored into the job record as `ai_status`).
STATUS_AI_QUEUED = "ai_queued"
STATUS_AI_GENERATING = "ai_generating"
STATUS_AI_PARTIAL = "ai_partial"
STATUS_AI_COMPLETE = "ai_complete"
STATUS_AI_FAILED = "ai_failed"
STATUS_AI_DISABLED = "ai_disabled"
STATUS_AI_CANCELLED = "ai_cancelled"


def _noop(*_args, **_kwargs) -> None:
    return None


class JobState:
    """Per-job AI state. One instance per job keeps jobs fully independent."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.total = 0
        self.completed = 0
        self.failed = 0
        self.cached = 0
        self.trivial = 0
        self.cancelled = False
        self.ai_status = STATUS_AI_QUEUED
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.first_result_at: Optional[float] = None
        self.batches_total = 0
        self.batches_completed = 0
        self.llm_calls = 0
        self.batch_durations: List[float] = []
        self.batch_sizes: List[int] = []
        self.in_flight = 0
        self.max_concurrency = 0
        self.failed_functions: List[Dict[str, Any]] = []
        self.subscribers: List["asyncio.Queue"] = []
        self.task: Optional[asyncio.Task] = None
        self.last_flush = 0.0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "ai_status": self.ai_status,
            "completed": self.completed,
            "total": self.total,
            "failed": self.failed,
            "cached": self.cached,
            "trivial": self.trivial,
            "batches_total": self.batches_total,
            "batches_completed": self.batches_completed,
            "llm_calls": self.llm_calls,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "first_result_at": self.first_result_at,
            "max_concurrency": self.max_concurrency,
            "cancelled": self.cancelled,
            "batch_sizes": list(self.batch_sizes),
            "avg_batch_size": (
                round(sum(self.batch_sizes) / len(self.batch_sizes), 2) if self.batch_sizes else 0
            ),
            "batch_durations": [round(d, 3) for d in self.batch_durations],
        }


_states: Dict[str, JobState] = {}
_states_lock = asyncio.Lock()


def _get_state(job_id: str) -> JobState:
    state = _states.get(job_id)
    if state is None:
        state = JobState(job_id)
        _states[job_id] = state
    return state


def get_state(job_id: str) -> Optional[JobState]:
    return _states.get(job_id)


def get_ai_status(job_id: str) -> Optional[Dict[str, Any]]:
    state = _states.get(job_id)
    if not state:
        return None
    data = state.snapshot()
    data["failed_functions"] = list(state.failed_functions)
    return data


# --------------------------------------------------------------------------
# Event fan-out (SSE)
# --------------------------------------------------------------------------
def subscribe(job_id: str) -> "asyncio.Queue":
    state = _get_state(job_id)
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    state.subscribers.append(queue)
    return queue


def unsubscribe(job_id: str, queue: "asyncio.Queue") -> None:
    state = _states.get(job_id)
    if not state:
        return
    try:
        state.subscribers.remove(queue)
    except ValueError:
        pass


def publish(job_id: str, event: Dict[str, Any]) -> None:
    state = _states.get(job_id)
    if not state:
        return
    event.setdefault("job_id", job_id)
    event.setdefault("ts", time.time())
    for queue in list(state.subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Slow consumer: drop the oldest event rather than blocking the pipeline.
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------
def request_cancel(job_id: str) -> bool:
    """Stop scheduling new batches. In-flight requests are allowed to finish."""
    state = _states.get(job_id)
    if not state:
        return False
    state.cancelled = True
    logger.info(f"[AI] job={job_id} cancellation_requested")
    return True


def is_cancelled(job_id: str) -> bool:
    state = _states.get(job_id)
    return bool(state and state.cancelled)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _store(state: JobState, func, explanation: Dict[str, Any], ai_status: str) -> Dict[str, Any]:
    entry = {
        "function_id": func.id,
        "filename": func.filename,
        "name": func.qualified_name,
        "explanation": explanation,
        "ai_status": ai_status,
    }
    try:
        import job_store
        job_store.save_explanation(state.job_id, func.id, entry)
    except Exception as exc:  # noqa: BLE001 - persistence must never break analysis
        logger.debug(f"Explanation persist skipped for {func.id}: {exc}")
    return entry


def _warmup_sync(state: JobState, ordered: List[Any], is_trivial) -> Any:
    """Cache/trivial pre-pass. Runs in a worker thread: over thousands of
    functions the per-function cache reads and JSON writes add up to seconds
    of event-loop stall if left inline (spec sections 6, 22 and 31)."""
    to_generate: List[Any] = []
    cached_entries: List[Dict[str, Any]] = []

    for func in ordered:
        if state.cancelled:
            break
        cached = cache.get_operation_cached(
            "explanation",
            func.source_code,
            config.PROMPT_VERSION_EXPLANATION,
            config.MODEL,
            func.id,
        )
        if isinstance(cached, dict) and llm.validate_explanation_object(cached):
            state.cached += 1
            state.completed += 1
            cached_entries.append(_store(state, func, cached, "cached"))
        elif is_trivial is not None and is_trivial(func.source_code):
            # Deterministic local explanation: never spend an LLM call on a
            # trivial getter/setter/init wrapper.
            state.trivial += 1
            state.completed += 1
            cached_entries.append(_store(state, func, _ast_fallback(func), "trivial_skipped"))
        else:
            to_generate.append(func)

    return to_generate, cached_entries


def _persist_batch_sync(job_id: str, batch: List[Any], parsed: Dict[str, Any], error: Optional[Exception]) -> Any:
    """Persist one batch's results (AST fallback + cache writes + per-function
    JSON files). Runs in a worker thread so fsyncs never touch the loop."""
    entries: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for func in batch:
        explanation = parsed.get(func.id)
        if explanation:
            # Static explanations are free to recompute, so they are never
            # written to the LLM result cache (that would poison it for a later
            # run with CODEORACLE_STATIC_EXPLANATIONS=0).
            if not config.STATIC_EXPLANATIONS:
                try:
                    cache.set_operation_cached(
                        "explanation",
                        func.source_code,
                        config.PROMPT_VERSION_EXPLANATION,
                        config.MODEL,
                        explanation,
                        func.id,
                    )
                except Exception:  # noqa: BLE001
                    pass
            ai_status = "ast_fallback" if config.STATIC_EXPLANATIONS else "ai"
        else:
            explanation = _ast_fallback(func)
            ai_status = "ast_fallback"
            reason = str(error) if error else "invalid or missing JSON for this function"
            failures.append(
                {"function_id": func.id, "name": func.qualified_name, "filename": func.filename, "reason": reason}
            )

        entry = {
            "function_id": func.id,
            "filename": func.filename,
            "name": func.qualified_name,
            "explanation": explanation,
            "ai_status": ai_status,
        }
        try:
            import job_store
            job_store.save_explanation(job_id, func.id, entry)
        except Exception as exc:  # noqa: BLE001 - persistence must never break analysis
            logger.debug(f"Explanation persist skipped for {func.id}: {exc}")
        entries.append(entry)

    return entries, failures


def _ast_fallback(func) -> Dict[str, Any]:
    return llm.normalize_explanation_item(
        llm.analyze_function_ast(
            {
                "display_name": func.qualified_name,
                "name": func.name,
                "args": func.args,
                "body": func.source_code,
            },
            func.filename,
        )
    )


async def _call_batch(state: JobState, batch: List[Any], strict: bool) -> Dict[str, Any]:
    """One LLM request. Returns {function_id: validated explanation}."""
    if not config.STATIC_EXPLANATIONS:
        state.llm_calls += 1
    state.in_flight += 1
    state.max_concurrency = max(state.max_concurrency, state.in_flight)
    try:
        data = await llm.analyze_functions_batch(batch, strict=strict)
    finally:
        state.in_flight -= 1

    results: Dict[str, Any] = {}
    allowed = {f.id for f in batch}
    for item in data.get("results", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        fid = item.get("function_id")
        if fid not in allowed:
            continue
        candidate = {
            "name": item.get("name", ""),
            "explanation": item.get("explanation", ""),
            "usage": item.get("usage", ""),
            "purpose": item.get("purpose", ""),
            "input_output": item.get("input_output", ""),
            "risks": item.get("risks", ""),
        }
        if llm.validate_explanation_object(candidate):
            results[fid] = candidate
    return results


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
async def run_ai_analysis(
    job_id: str,
    registry: List[Any],
    hooks: Optional[Dict[str, Callable]] = None,
) -> Dict[str, Any]:
    """Run the full progressive AI pass for a job. Never raises."""
    hooks = hooks or {}
    on_started = hooks.get("started", _noop)
    on_progress = hooks.get("progress", _noop)
    on_batch = hooks.get("batch", _noop)
    on_complete = hooks.get("complete", _noop)
    on_error = hooks.get("error", _noop)

    state = _get_state(job_id)
    state.cancelled = False
    state.started_at = time.time()
    state.total = len(registry)

    logger.info(f"[ANALYSIS] job={job_id} ai_start total={state.total}")

    if not config.AI_ANALYSIS_ENABLED or not registry:
        state.ai_status = STATUS_AI_DISABLED if not registry else STATUS_AI_COMPLETE
        state.finished_at = time.time()
        snapshot = state.snapshot()
        on_complete({"status": snapshot["ai_status"], **snapshot})
        publish(job_id, {"type": "analysis_completed", **snapshot})
        return snapshot

    try:
        ordered = registry_mod.order_functions(registry)
        is_trivial = hooks.get("is_trivial")
        to_generate: List[Any]
        cached_entries: List[Dict[str, Any]]
        to_generate, cached_entries = await asyncio.to_thread(
            _warmup_sync, state, ordered, is_trivial
        )

        batches = registry_mod.create_batches(to_generate)
        state.batches_total = len(batches)
        state.ai_status = STATUS_AI_GENERATING

        started_payload = {
            "type": "analysis_started",
            "total": state.total,
            "cached": state.cached,
            "trivial": state.trivial,
            "queued": len(to_generate),
            "batches": len(batches),
            "batch_size": config.LLM_BATCH_SIZE,
            "concurrency": config.LLM_CONCURRENCY,
        }
        logger.info(
            f"[AI] job={job_id} batches={len(batches)} batch_size={config.LLM_BATCH_SIZE} "
            f"concurrency={config.LLM_CONCURRENCY} cached={state.cached} trivial={state.trivial} queued={len(to_generate)}"
        )
        on_started(started_payload)
        publish(job_id, started_payload)

        if cached_entries:
            # Cached/trivial entries are already usable but they are NOT LLM
            # results; first_result_at is reserved for the first real AI result.
            batch_payload = {
                "type": "batch_completed",
                "batch": 0,
                "completed": state.completed,
                "total": state.total,
                "failed": state.failed,
                "cached": state.cached,
                "functions": cached_entries[:200],
            }
            on_batch(batch_payload)
            publish(job_id, batch_payload)

        # progress baseline (cached results already usable)
        on_progress(state.snapshot())
        publish(job_id, {"type": "progress", **state.snapshot()})

        if batches and not state.cancelled:
            queue: "asyncio.Queue" = asyncio.Queue()
            for index, batch in enumerate(batches, start=1):
                queue.put_nowait((index, batch))

            worker_count = max(1, min(config.LLM_CONCURRENCY, len(batches)))
            workers = [
                asyncio.create_task(
                    _worker(state, queue, on_progress, on_batch, publish),
                    name=f"ai-worker-{job_id}-{i}",
                )
                for i in range(worker_count)
            ]
            await asyncio.gather(*workers, return_exceptions=True)

        state.finished_at = time.time()
        if state.cancelled:
            state.ai_status = STATUS_AI_CANCELLED
        elif state.failed and state.completed < state.total:
            state.ai_status = STATUS_AI_PARTIAL
        elif state.failed:
            state.ai_status = STATUS_AI_PARTIAL
        else:
            state.ai_status = STATUS_AI_COMPLETE

        snapshot = state.snapshot()
        duration = snapshot["finished_at"] - snapshot["started_at"]
        logger.info(
            f"[ANALYSIS] job={job_id} ai_complete status={state.ai_status} "
            f"completed={state.completed}/{state.total} failed={state.failed} "
            f"llm_calls={state.llm_calls} duration={duration:.2f}s "
            f"max_concurrency={state.max_concurrency}"
        )
        payload = {
            "status": state.ai_status,
            "failed_functions": state.failed_functions,
            **snapshot,
        }
        on_complete(payload)
        publish(
            job_id,
            {
                "type": "analysis_cancelled" if state.cancelled else "analysis_completed",
                "status": state.ai_status,
                **snapshot,
            },
        )
        return snapshot

    except Exception as exc:  # noqa: BLE001 - the job must survive any failure
        logger.error(f"[ANALYSIS] job={job_id} ai_error: {exc}", exc_info=True)
        state.ai_status = STATUS_AI_FAILED
        state.finished_at = time.time()
        snapshot = state.snapshot()
        on_error(str(exc))
        publish(job_id, {"type": "analysis_error", "message": str(exc), **snapshot})
        return snapshot


async def _worker(state: JobState, queue: "asyncio.Queue", on_progress, on_batch, publish_fn) -> None:
    while True:
        if state.cancelled:
            return
        try:
            index, batch = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        state.batch_sizes.append(len(batch))
        batch_start = time.time()
        logger.info(f"[AI] job={state.job_id} batch={index} started size={len(batch)}")

        parsed: Dict[str, Any] = {}
        error: Optional[Exception] = None

        try:
            parsed.update(await _call_batch(state, batch, strict=False))
        except Exception as exc:  # noqa: BLE001
            error = exc
            logger.warning(f"[AI] job={state.job_id} batch={index} failed: {exc}")

        missing = [f for f in batch if f.id not in parsed]
        if missing and not state.cancelled:
            try:
                await asyncio.sleep(min(0.4 * (1 + state.failed), 2.0))
                parsed.update(await _call_batch(state, missing, strict=True))
            except Exception as exc:  # noqa: BLE001
                error = error or exc
                logger.warning(f"[AI] job={state.job_id} batch={index} strict retry failed: {exc}")

        # Disk writes (cache + per-function JSON + AST fallback) run in a
        # worker thread: at 10 functions/batch the fsyncs used to add ~100ms
        # of event-loop stall per batch (spec sections 23 and 31).
        entries, failures = await asyncio.to_thread(
            _persist_batch_sync, state.job_id, batch, parsed, error
        )

        if any(f.id in parsed for f in batch) and state.first_result_at is None:
            state.first_result_at = time.time()
        if failures:
            state.failed += len(failures)
            state.failed_functions.extend(failures)

        state.completed += len(batch)
        state.batches_completed += 1
        duration = time.time() - batch_start
        state.batch_durations.append(duration)

        logger.info(
            f"[AI] job={state.job_id} batch={index} completed duration={duration:.2f}s "
            f"progress={state.completed}/{state.total}"
        )

        batch_payload = {
            "type": "batch_completed",
            "batch": index,
            "completed": state.completed,
            "total": state.total,
            "failed": state.failed,
            "cached": state.cached,
            "functions": entries,
        }
        on_batch(batch_payload)
        publish_fn(state.job_id, batch_payload)

        now = time.time()
        if now - state.last_flush >= 1.0 or queue.empty():
            state.last_flush = now
            on_progress(state.snapshot())
            publish_fn(state.job_id, {"type": "progress", **state.snapshot()})


def schedule(job_id: str, registry: List[Any], hooks: Optional[Dict[str, Callable]] = None) -> "asyncio.Task":
    """Create the background AI task (idempotent per job)."""
    state = _get_state(job_id)
    if state.task is not None and not state.task.done():
        return state.task
    state.task = asyncio.create_task(
        run_ai_analysis(job_id, registry, hooks),
        name=f"ai-analysis-{job_id}",
    )
    return state.task
