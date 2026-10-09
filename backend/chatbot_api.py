"""Repository chatbot HTTP API (read-only, repository-aware).

Every request is tied to the analysis job the user is currently looking at, so
the repository is resolved server-side and never chosen by the client. The job
id is the access boundary today (the same boundary ``/results/{job_id}``,
``/jobs/{job_id}/functions`` and ``/generate/tests/{job_id}`` already use); the
``owner_id`` checks below become the enforcement point once an account system
exists.

Routes follow the project's existing convention (no ``/api`` prefix) and the
``/api/...`` aliases are registered as well so both spellings work.

Security posture:
  * no provider credential ever reaches the response (errors are normalized and
    sanitized by the provider layer),
  * the repository is treated as untrusted data, never as instructions,
  * request size, message length, rate and concurrency are all bounded,
  * the chatbot is read-only - it cannot edit or execute anything.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import config
from app.services.ai import chat_conversations
from app.services.ai.chat_retrieval import load_registry, repo_overview
from app.services.ai.chatbot_service import get_chatbot_service, suggestions_for
from app.services.ai.provider_base import ProviderError
from app.services.ai.provider_factory import known_providers
from app.services.ai.test_generation_service import get_test_generation_service

logger = logging.getLogger("codeoracle.chat")

router = APIRouter(tags=["chatbot"])

_ERROR_STATUS: Dict[str, int] = {
    "provider_not_configured": 503,
    "provider_auth_error": 502,
    "provider_rate_limited": 429,
    "provider_timeout": 504,
    "provider_unavailable": 502,
    "provider_bad_response": 502,
}

_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_CONVERSATIONS_LISTED = 50

_job_resolver: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None


def configure(job_resolver: Callable[[str], Optional[Dict[str, Any]]]) -> None:
    """Inject main.get_job (in-memory cache -> job store -> MongoDB)."""
    global _job_resolver
    _job_resolver = job_resolver


# --------------------------------------------------------------------------
# Rate limiting / concurrency (in-process, bounded, per client and per job)
# --------------------------------------------------------------------------
class SlidingWindowLimiter:
    """Small sliding-window limiter; no external store, no unbounded growth."""

    def __init__(self, limit_per_minute: int, window_seconds: float = 60.0) -> None:
        self.limit = max(1, limit_per_minute)
        self.window = window_seconds
        self._hits: Dict[str, List[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.time()
        async with self._lock:
            hits = self._hits.setdefault(key, [])
            cutoff = now - self.window
            while hits and hits[0] < cutoff:
                hits.pop(0)
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 2048:
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] >= cutoff}
            return True


class ConcurrencyGuard:
    """Caps simultaneous chatbot calls per key (per repository/job)."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self._inflight: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> bool:
        async with self._lock:
            current = self._inflight.get(key, 0)
            if current >= self.limit:
                return False
            self._inflight[key] = current + 1
            return True

    async def release(self, key: str) -> None:
        async with self._lock:
            current = self._inflight.get(key, 0)
            if current <= 1:
                self._inflight.pop(key, None)
            else:
                self._inflight[key] = current - 1


_limiter = SlidingWindowLimiter(config.CHATBOT_RATE_LIMIT_PER_MINUTE)
_concurrency = ConcurrencyGuard(config.CHATBOT_MAX_CONCURRENT)


def _chat_error(
    status_code: int,
    error_code: str,
    message: str,
    *,
    retryable: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    body: Dict[str, Any] = {
        "error_code": error_code,
        "message": message,
        "retryable": bool(retryable),
        "service": "chatbot",
    }
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status_code)


def _client_key(request: Optional[Request]) -> str:
    if request is None or request.client is None:
        return "anonymous"
    return str(request.client.host or "anonymous")


def _verify_job_access(job_id: str) -> Dict[str, Any]:
    """Resolve the analysis job the caller is allowed to chat about."""
    if not job_id or not _SAFE_JOB_ID.match(job_id) or _job_resolver is None:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    job = _job_resolver(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    return job


def _repository_display_name(job: Dict[str, Any]) -> str:
    """Same fallback the widget header uses, so ZIP jobs are never blank."""
    return job.get("repository") or job.get("repository_url") or "Uploaded repository"


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------
@router.post("/chatbot/message")
@router.post("/api/chatbot/message")
async def chatbot_message(request: Request, payload: Dict[str, Any] = Body(...)):
    """Answer a question about the repository behind an analysis job."""
    job_id = str(payload.get("job_id") or payload.get("analysis_job_id") or "").strip()
    message = str(payload.get("message") or "").strip()
    conversation_id = str(payload.get("conversation_id") or "").strip() or None
    owner_id = str(payload.get("user_id") or "").strip() or None

    if not job_id:
        raise HTTPException(status_code=422, detail="job_id is required")
    if not message:
        raise HTTPException(status_code=422, detail="message is required")
    if len(message) > config.CHATBOT_MAX_MESSAGE_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"message exceeds the {config.CHATBOT_MAX_MESSAGE_CHARS} character limit",
        )
    if conversation_id and not chat_conversations.is_valid_conversation_id(conversation_id):
        raise HTTPException(status_code=422, detail="invalid conversation_id")

    job = _verify_job_access(job_id)

    service = get_chatbot_service()
    if not service.is_configured():
        logger.warning("[CHAT] job=%s rejected: chatbot provider not configured", job_id)
        return _chat_error(
            503,
            "chatbot_not_configured",
            "CodeOracle's repository chatbot is not configured. Please contact the administrator.",
        )

    if not job.get("structural_ready"):
        return _chat_error(
            409,
            "analysis_in_progress",
            "This repository is still being analyzed. You can ask about it once the structural "
            "analysis finishes.",
            retryable=True,
            extra={"status": job.get("status"), "progress": job.get("progress")},
        )

    client = _client_key(request)
    if not await _limiter.allow(f"chat:{client}"):
        return _chat_error(
            429,
            "chatbot_rate_limited",
            "Too many chat requests. Please wait a moment and try again.",
            retryable=True,
        )
    if not await _concurrency.acquire(job_id):
        return _chat_error(
            429,
            "chatbot_busy",
            "Another question about this repository is still being answered. Please try again shortly.",
            retryable=True,
        )

    try:
        if conversation_id:
            conversation = chat_conversations.get_conversation(
                job_id, conversation_id, owner_id=owner_id
            )
            if conversation is None:
                return _chat_error(404, "conversation_not_found", "Conversation not found.")
        else:
            conversation = chat_conversations.new_conversation(
                job_id,
                repository=_repository_display_name(job),
                owner_id=owner_id,
            )
            conversation_id = conversation["conversation_id"]

        history, history_note = chat_conversations.history_for_prompt(
            conversation,
            max_messages=config.CHATBOT_MAX_HISTORY_MESSAGES,
            max_chars=config.CHATBOT_MAX_HISTORY_CHARS,
        )

        try:
            result = await service.answer(
                job=job,
                job_id=job_id,
                question=message,
                history=history,
                history_note=history_note,
            )
        except ProviderError as exc:
            status = _ERROR_STATUS.get(exc.code, 502)
            logger.warning("[CHAT] job=%s %s", job_id, exc.log_line())
            headers = None
            if status == 429 and exc.retry_after:
                headers = {"Retry-After": str(int(max(1.0, float(exc.retry_after))))}
            public = exc.to_public()
            return JSONResponse(
                {
                    "error_code": public["error_code"],
                    "message": public["message"],
                    "provider": public.get("provider"),
                    "retryable": public["retryable"],
                    "service": "chatbot",
                },
                status_code=status,
                headers=headers,
            )
        except asyncio.CancelledError:
            raise

        now = time.time()
        chat_conversations.append_message(
            job_id,
            conversation_id,
            {"role": "user", "content": message, "created_at": now},
        )
        chat_conversations.append_message(
            job_id,
            conversation_id,
            {
                "role": "assistant",
                "content": result["answer"],
                "citations": result["citations"],
                "notes": result["notes"],
                "created_at": time.time(),
                "provider": result.get("provider"),
                "model": result.get("model"),
            },
        )

        return {
            "conversation_id": conversation_id,
            "job_id": job_id,
            "repository": _repository_display_name(job),
            "answer": result["answer"],
            "citations": result["citations"],
            "notes": result["notes"],
            "provider": result.get("provider"),
            "model": result.get("model"),
            "latency_ms": result.get("latency_ms"),
            "usage": result.get("usage"),
            "retrieval": result.get("retrieval"),
            "streaming": False,
            "created_at": now,
        }
    finally:
        await _concurrency.release(job_id)


# --------------------------------------------------------------------------
# Conversations
# --------------------------------------------------------------------------
@router.get("/chatbot/conversations")
@router.get("/api/chatbot/conversations")
async def list_chat_conversations(
    job_id: str = Query(...),
    user_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=_MAX_CONVERSATIONS_LISTED),
):
    _verify_job_access(job_id)
    conversations = chat_conversations.list_conversations(job_id, owner_id=user_id)
    return {"job_id": job_id, "conversations": conversations[:limit], "total": len(conversations)}


@router.post("/chatbot/conversations")
@router.post("/api/chatbot/conversations")
async def create_chat_conversation(payload: Dict[str, Any] = Body(...)):
    job_id = str(payload.get("job_id") or "").strip()
    title = str(payload.get("title") or "").strip() or None
    owner_id = str(payload.get("user_id") or "").strip() or None
    job = _verify_job_access(job_id)
    conversation = chat_conversations.new_conversation(
        job_id,
        repository=job.get("repository") or job.get("repository_url"),
        owner_id=owner_id,
        title=title,
    )
    return {
        "conversation_id": conversation["conversation_id"],
        "job_id": job_id,
        "title": conversation["title"],
        "created_at": conversation["created_at"],
        "messages": [],
    }


@router.get("/chatbot/conversations/{conversation_id}")
@router.get("/api/chatbot/conversations/{conversation_id}")
async def get_chat_conversation(
    conversation_id: str,
    job_id: str = Query(...),
    user_id: Optional[str] = Query(default=None),
):
    _verify_job_access(job_id)
    conversation = chat_conversations.get_conversation(job_id, conversation_id, owner_id=user_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {
        "conversation_id": conversation["conversation_id"],
        "job_id": conversation["job_id"],
        "repository": conversation.get("repository"),
        "title": conversation.get("title"),
        "created_at": conversation.get("created_at"),
        "updated_at": conversation.get("updated_at"),
        "messages": [chat_conversations.public_message(m) for m in conversation.get("messages") or []],
    }


@router.delete("/chatbot/conversations/{conversation_id}")
@router.delete("/api/chatbot/conversations/{conversation_id}")
async def delete_chat_conversation(
    conversation_id: str,
    job_id: str = Query(...),
    user_id: Optional[str] = Query(default=None),
):
    _verify_job_access(job_id)
    deleted = chat_conversations.delete_conversation(job_id, conversation_id, owner_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"conversation_id": conversation_id, "deleted": True}


# --------------------------------------------------------------------------
# Repository context + service status
# --------------------------------------------------------------------------
@router.get("/chatbot/repository/{job_id}")
@router.get("/api/chatbot/repository/{job_id}")
async def chatbot_repository(job_id: str):
    """Repository identity + data-adapted starter questions for the widget."""
    job = _verify_job_access(job_id)
    service = get_chatbot_service()
    # Live in-memory jobs do not carry the registry (see chat_retrieval), so
    # enrich the view from the persisted analysis before deriving suggestions.
    job_view = dict(job)
    if not job_view.get("registry"):
        job_view["registry"] = load_registry(job, job_id)
    return {
        "repository": repo_overview(job_view, job_id),
        "suggestions": suggestions_for(job_view),
        "chatbot": {
            "provider": service.settings.provider,
            "model": service.settings.model,
            "configured": service.is_configured(),
            "streaming": False,
        },
    }


def _provider_pool_status(service) -> Optional[dict]:
    """Key-pool health for one service, or None when its provider has no pool.

    Shows how many keys are configured and which are parked on a daily cap -
    enough to tell "my six keys were picked up" from "I am still on one key".
    Only availability and a 4-char fingerprint are exposed, never a key.
    """
    try:
        provider = service.provider
    except Exception:  # noqa: BLE001 - diagnostics must never 500
        return None
    key_status = getattr(provider, "key_status", None)
    if not callable(key_status):
        return None
    try:
        keys = key_status()
    except Exception:  # noqa: BLE001
        return None
    return {
        "key_count": len(keys),
        "available": sum(1 for row in keys if row.get("available")),
        "keys": keys,
    }


@router.get("/ai/status")
@router.get("/api/ai/status")
async def ai_service_status():
    """Admin/diagnostic view: both services' providers, models and last error.

    Never exposes API keys - ``AIServiceSettings.safe_summary`` deliberately
    omits them.
    """
    chatbot = get_chatbot_service()
    testgen = get_test_generation_service()
    return {
        "chatbot": chatbot.describe(),
        "test_generation": testgen.describe(),
        "providers": known_providers(),
        "key_pools": {
            "chatbot": _provider_pool_status(chatbot),
            "test_generation": _provider_pool_status(testgen),
        },
    }
