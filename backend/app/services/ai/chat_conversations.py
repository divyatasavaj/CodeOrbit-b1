"""Conversation persistence for the repository chatbot.

Conversations are stored with the existing filesystem job store
(``job_store.save_artifact(job_id, "conversations", ...)``), so they live next
to the analysis they belong to, survive a backend reload, and are deleted with
the job. No new database or migration is introduced.

Isolation rules enforced here:
  * every conversation is bound to exactly one analysis job (and therefore one
    repository) - the job id is part of the storage and read path,
  * reads refuse a conversation whose job/owner does not match the request,
  * message history is bounded so a long chat can never grow without limit.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

import job_store

logger = logging.getLogger("codeoracle.chat")

_KIND = "conversations"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_STORED_MESSAGES = 60
_MAX_TITLE_CHARS = 80


def is_valid_conversation_id(conversation_id: str) -> bool:
    return bool(conversation_id) and bool(_SAFE_ID.match(str(conversation_id)))


def title_from(message: str) -> str:
    """Cheap deterministic title (first line, trimmed) - no extra LLM call."""
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else "New conversation"
    first = re.sub(r"\s+", " ", first).strip()
    if len(first) > _MAX_TITLE_CHARS:
        first = first[:_MAX_TITLE_CHARS].rstrip() + "..."
    return first or "New conversation"


def new_conversation(
    job_id: str,
    *,
    repository: Optional[str] = None,
    owner_id: Optional[str] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    now = time.time()
    conversation = {
        "conversation_id": uuid.uuid4().hex,
        "job_id": job_id,
        "repository": repository,
        "owner_id": owner_id,
        "title": (title or "").strip()[: _MAX_TITLE_CHARS] or "New conversation",
        "created_at": now,
        "updated_at": now,
        "messages": [],
        "message_count": 0,
    }
    _save(job_id, conversation)
    return conversation


def _save(job_id: str, conversation: Dict[str, Any]) -> None:
    job_store.save_artifact(job_id, _KIND, conversation["conversation_id"], conversation)


def get_conversation(
    job_id: str,
    conversation_id: str,
    *,
    owner_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Load one conversation, enforcing job (and owner when known) isolation."""
    if not conversation_id or not is_valid_conversation_id(conversation_id):
        return None
    stored = job_store.load_artifact(job_id, _KIND, conversation_id)
    if not isinstance(stored, dict):
        return None
    if stored.get("job_id") != job_id:
        logger.warning("[CHAT] conversation/job mismatch refused for job=%s", job_id)
        return None
    stored_owner = stored.get("owner_id")
    if stored_owner and owner_id and stored_owner != owner_id:
        logger.warning("[CHAT] conversation ownership mismatch refused for job=%s", job_id)
        return None
    return stored


def list_conversations(job_id: str, *, owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Summaries (newest first) - never returns another job's conversations."""
    summaries: List[Dict[str, Any]] = []
    for stored in job_store.load_artifacts(job_id, _KIND):
        if not isinstance(stored, dict) or stored.get("job_id") != job_id:
            continue
        if stored.get("owner_id") and owner_id and stored.get("owner_id") != owner_id:
            continue
        messages = stored.get("messages") or []
        last = messages[-1] if messages else None
        summaries.append({
            "conversation_id": stored.get("conversation_id"),
            "job_id": stored.get("job_id"),
            "title": stored.get("title") or "New conversation",
            "created_at": stored.get("created_at"),
            "updated_at": stored.get("updated_at"),
            "message_count": stored.get("message_count") or len(messages),
            "last_message": (last or {}).get("content", "")[:140] if last else "",
        })
    summaries.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return summaries


def delete_conversation(
    job_id: str,
    conversation_id: str,
    *,
    owner_id: Optional[str] = None,
) -> bool:
    if get_conversation(job_id, conversation_id, owner_id=owner_id) is None:
        return False
    return job_store.delete_artifact(job_id, _KIND, conversation_id)


def append_message(job_id: str, conversation_id: str, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Append one message (bounded) and persist; returns the updated conversation."""
    conversation = get_conversation(job_id, conversation_id)
    if conversation is None:
        return None
    messages = list(conversation.get("messages") or [])
    messages.append(message)
    if len(messages) > _MAX_STORED_MESSAGES:
        messages = messages[-_MAX_STORED_MESSAGES:]
    conversation["messages"] = messages
    conversation["message_count"] = int(conversation.get("message_count") or 0) + 1
    conversation["updated_at"] = message.get("created_at") or time.time()
    if not conversation.get("title") or conversation.get("title") == "New conversation":
        first_user = next((m for m in messages if m.get("role") == "user"), None)
        if first_user:
            conversation["title"] = title_from(first_user.get("content", ""))
    _save(job_id, conversation)
    return conversation


def history_for_prompt(
    conversation: Optional[Dict[str, Any]],
    *,
    max_messages: int,
    max_chars: int,
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Bound the conversation history sent to the model.

    Returns ``(turns, omitted_note)`` where ``turns`` are ``{"role", "content"}``
    pairs (oldest first). When older turns must be dropped, the note records the
    questions that were dropped so the model knows the conversation continued
    instead of silently losing context.
    """
    if not conversation:
        return [], None
    stored = [m for m in (conversation.get("messages") or []) if m.get("content")]
    if not stored:
        return [], None

    turns: List[Dict[str, str]] = []
    used = 0
    for message in reversed(stored):
        content = str(message.get("content") or "")
        if used + len(content) > max_chars or len(turns) >= max_messages:
            break
        used += len(content)
        turns.append({"role": str(message.get("role") or "user"), "content": content})
    turns.reverse()

    included = len(turns)
    dropped = stored[: len(stored) - included]
    note = None
    if dropped:
        questions: Sequence[str] = [
            title_from(m.get("content", "")) for m in dropped if m.get("role") == "user"
        ]
        if questions:
            note = (
                "Earlier conversation turns were summarised to stay within the context budget. "
                "Earlier questions: " + "; ".join(questions[-5:])
            )
        else:
            note = "Earlier conversation turns were trimmed to stay within the context budget."
    return turns, note


def public_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """Message shape returned to the frontend (no internal/provider fields)."""
    return {
        "role": message.get("role"),
        "content": message.get("content"),
        "citations": message.get("citations") or [],
        "notes": message.get("notes") or [],
        "created_at": message.get("created_at"),
    }


__all__ = [
    "new_conversation",
    "get_conversation",
    "list_conversations",
    "delete_conversation",
    "append_message",
    "history_for_prompt",
    "public_message",
    "title_from",
    "is_valid_conversation_id",
]