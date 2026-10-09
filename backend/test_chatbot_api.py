"""Backend tests for repository-grounded chat retrieval and the chatbot API.

Offline by design: the model is a scripted fake provider and the analysis job is
an in-memory fixture built from the same shape the pipeline persists, so no
network call and no API credit is used.

Run:
    python -m pytest test_chatbot_api.py -q
"""
import json

import pytest
from fastapi.testclient import TestClient

import config
import chatbot_api
import job_store
import main
from conftest import FakeProvider, make_settings

from app.services.ai import chat_conversations
from app.services.ai.chat_retrieval import clear_index_cache, retrieve
from app.services.ai.chatbot_service import ChatbotService
from app.services.ai.provider_base import ProviderRateLimitError, ProviderTimeoutError
from app.services.ai.test_generation_service import TestGenerationService


# --------------------------------------------------------------------------
# Fixtures: a small analyzed repository (same shape the pipeline persists)
# --------------------------------------------------------------------------
def _fn(fid, name, filename, source, start, end, calls=(), priority="medium", score=50,
        class_name="", language="python"):
    return {
        "id": fid,
        "name": name,
        "qualified_name": name,
        "file_path": f"/repo/{filename}",
        "filename": filename,
        "language": language,
        "class_name": class_name,
        "source_code": source,
        "start_line": start,
        "end_line": end,
        "args": [],
        "imports": [],
        "calls": list(calls),
        "complexity": 3,
        "line_count": source.count("\n") + 1,
        "decorators": [],
        "has_return": True,
        "is_method": bool(class_name),
        "source_file": filename,
        "priority": priority,
        "priority_score": score,
        "caller_count": 1 if calls else 0,
    }


JOB_ID = "job-auth-0001"

SAMPLE_JOB = {
    "job_id": JOB_ID,
    "repository": "acme/auth-service",
    "repository_url": "https://github.com/acme/auth-service",
    "source_type": "github",
    "branch": "main",
    "status": "complete",
    "structural_ready": True,
    "languages": ["Python"],
    "structural": {
        "files": 2, "functions": 4, "classes": 1, "graph_edges": 3,
        "file_function_counts": {"auth.py": 3, "api.py": 1},
    },
    "summary": {"languages": ["Python"], "files_analyzed": 2, "functions_found": 4},
    "registry": [
        _fn("fn-validate", "validate_token", "auth.py",
            "def validate_token(token):\n    claims = decode_jwt(token)\n    return claims\n",
            10, 20, calls=("decode_jwt",), priority="high", score=90),
        _fn("fn-decode", "decode_jwt", "auth.py",
            "def decode_jwt(token):\n    return {'sub': token}\n",
            22, 30, priority="medium", score=55),
        _fn("fn-login", "login_user", "auth.py",
            "def login_user(username, password):\n    token = issue_token(username)\n    return validate_token(token)\n",
            32, 45, calls=("issue_token", "validate_token"), priority="high", score=80),
        _fn("fn-endpoint", "login_endpoint", "api.py",
            "def login_endpoint(request):\n    return login_user(request.user, request.password)\n",
            5, 15, calls=("login_user",), priority="high", score=85),
    ],
    "tests": [],
}


@pytest.fixture(autouse=True)
def _clear_index():
    clear_index_cache()
    yield
    clear_index_cache()


# --------------------------------------------------------------------------
# Retrieval correctness
# --------------------------------------------------------------------------
def test_named_symbol_is_retrieved_with_real_line_ranges():
    result = retrieve(SAMPLE_JOB, JOB_ID, "Explain `validate_token` please")

    assert result.items, "expected at least one retrieved item"
    top = result.items[0]
    assert top.filename == "auth.py"
    assert top.name == "validate_token"
    assert (top.start_line, top.end_line) == (10, 20)
    assert top.function_id == "fn-validate"
    assert "auth.py:10-20" in result.context_text
    assert result.citations and result.citations[0]["filename"] == "auth.py"
    assert result.overview_only is False


def test_caller_question_returns_actual_callers_from_static_analysis():
    result = retrieve(SAMPLE_JOB, JOB_ID, "What calls validate_token?")

    names = [item.name for item in result.items]
    assert "login_user" in names
    caller_item = next(item for item in result.items if item.name == "login_user")
    assert "calls `validate_token`" in caller_item.reason
    # Only the symbol the user named is reported as matched; the caller is
    # discovered by static analysis and therefore ranks first for this question.
    assert result.matched_symbols == ["validate_token"]
    assert result.items[0].name == "login_user"


def test_unknown_symbol_is_reported_not_invented():
    result = retrieve(SAMPLE_JOB, JOB_ID, "Explain `totally_missing_helper` for me")

    assert "totally_missing_helper" in result.unresolved_mentions
    assert any("did not identify" in note for note in result.notes)
    assert all(item.name != "totally_missing_helper" for item in result.items)


def test_overview_question_falls_back_to_a_bounded_overview():
    result = retrieve(SAMPLE_JOB, JOB_ID, "Give me an overview of this repository.")

    assert result.overview_only is True
    assert result.items
    assert any("overview" in note for note in result.notes)
    assert len(result.items) <= config.CHATBOT_MAX_CONTEXT_ITEMS


def test_theme_question_reaches_authentication_code():
    result = retrieve(SAMPLE_JOB, JOB_ID, "Explain the login flow and how tokens are validated")

    files = {item.filename for item in result.items}
    assert "auth.py" in files
    assert any("auth" in item.filename or "token" in item.name for item in result.items)


def test_context_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(config, "CHATBOT_MAX_CONTEXT_ITEMS", 1)
    result = retrieve(SAMPLE_JOB, JOB_ID, "Explain the authentication flow")

    assert len(result.items) <= 1
    assert result.omitted_items >= 1


def test_retrieved_source_is_truncated_with_disclosure(monkeypatch):
    monkeypatch.setattr(config, "CHATBOT_MAX_SOURCE_LINES", 10)
    long_source = "\n".join(f"    line_{i} = {i}" for i in range(200))
    job = dict(SAMPLE_JOB)
    job["registry"] = [
        _fn("fn-long", "big_function", "big.py", f"def big_function():\n{long_source}\n", 1, 200)
    ]
    result = retrieve(job, "job-long", "Explain `big_function`")

    assert result.truncated is True
    assert "more lines not shown" in result.items[0].source
    assert any("truncated" in note for note in result.notes)


def test_repository_metadata_distinguishes_zip_and_github_jobs():
    zip_job = dict(SAMPLE_JOB, source_type="upload", repository=None, repository_url=None)
    meta = retrieve(zip_job, "job-zip", "overview").repo
    assert meta["source_type"] == "upload"
    assert meta["name"] == "Uploaded repository"

    meta_gh = retrieve(SAMPLE_JOB, JOB_ID, "overview").repo
    assert meta_gh["source_type"] == "github"
    assert meta_gh["name"] == "acme/auth-service"
    assert meta_gh["branch"] == "main"


def test_partial_analysis_is_disclosed_in_notes():
    job = dict(SAMPLE_JOB, structural_ready=False, status="processing")
    result = retrieve(job, JOB_ID, "What does validate_token do?")
    assert any("has not finished" in note for note in result.notes)


# --------------------------------------------------------------------------
# Conversation persistence / isolation
# --------------------------------------------------------------------------
@pytest.fixture
def conversation_jobs():
    ids = ("conv-job-a", "conv-job-b")
    for job_id in ids:
        job_store.delete_job(job_id)
    yield ids
    for job_id in ids:
        job_store.delete_job(job_id)


def test_conversation_round_trip_and_isolation(conversation_jobs):
    job_a, job_b = conversation_jobs
    conversation = chat_conversations.new_conversation(job_a, repository="repo-a")
    cid = conversation["conversation_id"]

    chat_conversations.append_message(job_a, cid, {"role": "user", "content": "hello", "created_at": 1})
    chat_conversations.append_message(job_a, cid, {"role": "assistant", "content": "hi", "created_at": 2})

    stored = chat_conversations.get_conversation(job_a, cid)
    assert stored["message_count"] == 2
    assert [m["role"] for m in stored["messages"]] == ["user", "assistant"]

    # Another job can never read it, even with the exact id.
    assert chat_conversations.get_conversation(job_b, cid) is None
    assert chat_conversations.list_conversations(job_b) == []

    # Ownership is enforced once an owner is known.
    owned = chat_conversations.new_conversation(job_a, owner_id="user-1")
    assert chat_conversations.get_conversation(job_a, owned["conversation_id"], owner_id="user-2") is None
    assert chat_conversations.get_conversation(job_a, owned["conversation_id"], owner_id="user-1") is not None

    assert chat_conversations.delete_conversation(job_b, cid) is False
    assert chat_conversations.delete_conversation(job_a, cid) is True
    assert chat_conversations.get_conversation(job_a, cid) is None


def test_invalid_conversation_ids_are_rejected():
    assert chat_conversations.is_valid_conversation_id("../../etc/passwd") is False
    assert chat_conversations.is_valid_conversation_id("a" * 200) is False
    assert chat_conversations.is_valid_conversation_id("") is False
    assert chat_conversations.is_valid_conversation_id("abc123") is True


def test_history_is_bounded_and_reports_dropped_turns():
    conversation = {
        "messages": [
            {"role": "user", "content": f"question {i} " + "x" * 100}
            for i in range(20)
        ]
    }
    turns, note = chat_conversations.history_for_prompt(
        conversation, max_messages=4, max_chars=100000
    )
    assert len(turns) == 4
    assert note and "Earlier conversation turns were summarised" in note
    assert "question 19" in json.dumps(turns)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(chatbot_api, "_job_resolver", lambda job_id: SAMPLE_JOB if job_id == JOB_ID else None)
    chat_conversations  # noqa: B018 - import kept explicit
    return TestClient(main.app)


@pytest.fixture
def fake_chatbot(monkeypatch):
    """Install a scripted chatbot provider and return it."""
    fake = FakeProvider(make_settings(service="chatbot", provider="fake", model="chat-model"),
                        text="The login flow validates tokens in auth.py.")
    service = ChatbotService(settings=fake.settings, provider=fake)
    monkeypatch.setattr(chatbot_api, "get_chatbot_service", lambda: service)
    return fake


def test_message_endpoint_answers_with_citations(client, fake_chatbot):
    response = client.post("/chatbot/message", json={
        "job_id": JOB_ID, "message": "Explain `validate_token`",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"]
    assert body["conversation_id"]
    assert body["citations"] and body["citations"][0]["filename"] == "auth.py"
    assert body["provider"] == "fake" and body["model"] == "chat-model"
    assert body["streaming"] is False
    assert body["retrieval"]["items"] >= 1
    # The provider prompt must carry the retrieved source, not the whole repo.
    assert "auth.py" in fake_chatbot.prompt_text
    assert "### Repository" in fake_chatbot.prompt_text
    # No credential ever leaks into the response.
    assert "fake-key" not in response.text


def test_follow_up_question_keeps_conversation_context(client, fake_chatbot):
    first = client.post("/chatbot/message", json={"job_id": JOB_ID, "message": "Explain the login flow"})
    assert first.status_code == 200
    cid = first.json()["conversation_id"]

    second = client.post("/chatbot/message", json={
        "job_id": JOB_ID,
        "message": "What happens if the token expires?",
        "conversation_id": cid,
    })
    assert second.status_code == 200
    assert second.json()["conversation_id"] == cid

    # The follow-up prompt includes the earlier turn -> multi-turn memory works.
    assert "Explain the login flow" in fake_chatbot.prompt_text
    assert "What happens if the token expires?" in fake_chatbot.prompt_text

    listing = client.get("/chatbot/conversations", params={"job_id": JOB_ID})
    assert listing.status_code == 200
    conversations = listing.json()["conversations"]
    assert any(c["conversation_id"] == cid for c in conversations)

    detail = client.get(f"/chatbot/conversations/{cid}", params={"job_id": JOB_ID})
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1]["citations"]


def test_api_alias_routes_work(client, fake_chatbot):
    assert client.post("/api/chatbot/message", json={"job_id": JOB_ID, "message": "hi"}).status_code == 200
    assert client.get("/api/chatbot/repository/" + JOB_ID).status_code == 200
    assert client.get("/api/ai/status").status_code == 200


def test_unknown_job_is_not_found(client, fake_chatbot):
    response = client.post("/chatbot/message", json={"job_id": "nope", "message": "hi"})
    assert response.status_code == 404


def test_request_validation(client, fake_chatbot):
    assert client.post("/chatbot/message", json={"message": "hi"}).status_code == 422
    assert client.post("/chatbot/message", json={"job_id": JOB_ID, "message": "   "}).status_code == 422
    assert client.post("/chatbot/message", json={"job_id": JOB_ID}).status_code == 422
    too_long = client.post("/chatbot/message", json={
        "job_id": JOB_ID, "message": "x" * (config.CHATBOT_MAX_MESSAGE_CHARS + 10),
    })
    assert too_long.status_code == 422
    bad_cid = client.post("/chatbot/message", json={
        "job_id": JOB_ID, "message": "hi", "conversation_id": "../../etc/passwd",
    })
    assert bad_cid.status_code == 422
    assert fake_chatbot.calls == []


def test_analysis_in_progress_is_explained(client, monkeypatch, fake_chatbot):
    processing = dict(SAMPLE_JOB, structural_ready=False, status="processing", progress="Parsing 7 files...")
    monkeypatch.setattr(chatbot_api, "_job_resolver", lambda job_id: processing)

    response = client.post("/chatbot/message", json={"job_id": JOB_ID, "message": "hi"})
    assert response.status_code == 409
    assert response.json()["error_code"] == "analysis_in_progress"


def test_unconfigured_chatbot_returns_503(client, monkeypatch):
    service = ChatbotService(settings=make_settings(api_key=None), provider=None)
    monkeypatch.setattr(chatbot_api, "get_chatbot_service", lambda: service)

    response = client.post("/chatbot/message", json={"job_id": JOB_ID, "message": "hi"})
    assert response.status_code == 503
    body = response.json()
    assert body["error_code"] == "chatbot_not_configured"
    assert "api_key" not in response.text.lower()


def test_provider_timeout_and_rate_limit_are_reported_cleanly(client, monkeypatch):
    for error, status, code in (
        (ProviderTimeoutError("timed out", provider="fake"), 504, "provider_timeout"),
        (ProviderRateLimitError("slow down", provider="fake", retry_after=7), 429, "provider_rate_limited"),
    ):
        fake = FakeProvider(make_settings(service="chatbot", provider="fake"), error=error)
        service = ChatbotService(settings=fake.settings, provider=fake)
        monkeypatch.setattr(chatbot_api, "get_chatbot_service", lambda service=service: service)

        response = client.post("/chatbot/message", json={"job_id": JOB_ID, "message": "hi"})
        assert response.status_code == status, response.text
        body = response.json()
        assert body["error_code"] == code
        assert body["retryable"] is True
        assert "Traceback" not in response.text
        if status == 429:
            assert response.headers.get("retry-after") == "7"


def test_rate_limiter_returns_429(client, monkeypatch, fake_chatbot):
    monkeypatch.setattr(chatbot_api, "_limiter", chatbot_api.SlidingWindowLimiter(1))

    assert client.post("/chatbot/message", json={"job_id": JOB_ID, "message": "one"}).status_code == 200
    second = client.post("/chatbot/message", json={"job_id": JOB_ID, "message": "two"})
    assert second.status_code == 429
    assert second.json()["error_code"] == "chatbot_rate_limited"


def test_concurrency_guard_caps_simultaneous_requests():
    import asyncio

    guard = chatbot_api.ConcurrencyGuard(1)

    async def scenario():
        assert await guard.acquire("job") is True
        assert await guard.acquire("job") is False
        await guard.release("job")
        assert await guard.acquire("job") is True
        await guard.release("job")

    asyncio.run(scenario())


def test_conversation_endpoints_are_scoped_to_the_job(client, fake_chatbot):
    created = client.post("/chatbot/conversations", json={"job_id": JOB_ID, "title": "Mine"})
    assert created.status_code == 200
    cid = created.json()["conversation_id"]

    assert client.get(f"/chatbot/conversations/{cid}", params={"job_id": "other"}).status_code == 404
    assert client.delete(f"/chatbot/conversations/{cid}", params={"job_id": "other"}).status_code == 404
    assert client.delete(f"/chatbot/conversations/{cid}", params={"job_id": JOB_ID}).status_code == 200
    assert client.get(f"/chatbot/conversations/{cid}", params={"job_id": JOB_ID}).status_code == 404


def test_repository_context_endpoint_is_adapted_and_secret_free(client, fake_chatbot):
    response = client.get(f"/chatbot/repository/{JOB_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["repository"]["name"] == "acme/auth-service"
    assert body["repository"]["analysis_ready"] is True
    assert body["suggestions"]
    assert any("authentication" in s.lower() for s in body["suggestions"])
    assert any("no tests" in s.lower() for s in body["suggestions"])
    assert body["chatbot"]["streaming"] is False
    assert "fake-key" not in response.text


def test_ai_status_keeps_the_two_services_independent(client, monkeypatch):
    chatbot_fake = FakeProvider(make_settings(service="chatbot", provider="fake", model="chat-model"))
    testgen_fake = FakeProvider(make_settings(service="testgen", provider="fake", model="test-model"))
    monkeypatch.setattr(
        chatbot_api, "get_chatbot_service",
        lambda: ChatbotService(settings=chatbot_fake.settings, provider=chatbot_fake),
    )
    monkeypatch.setattr(
        chatbot_api, "get_test_generation_service",
        lambda: TestGenerationService(settings=testgen_fake.settings, provider=testgen_fake),
    )

    body = client.get("/ai/status").json()
    assert body["chatbot"]["model"] == "chat-model"
    assert body["test_generation"]["model"] == "test-model"
    assert body["chatbot"]["configured"] is True
    assert "gemini" in body["providers"]


def test_chat_request_never_touches_the_test_generation_provider(client, monkeypatch):
    chatbot_fake = FakeProvider(make_settings(service="chatbot", provider="fake"), text="ok")
    testgen_fake = FakeProvider(make_settings(service="testgen", provider="fake"), text="TESTS")
    testgen_service = TestGenerationService(settings=testgen_fake.settings, provider=testgen_fake)

    monkeypatch.setattr(
        chatbot_api, "get_chatbot_service",
        lambda: ChatbotService(settings=chatbot_fake.settings, provider=chatbot_fake),
    )
    monkeypatch.setattr(chatbot_api, "get_test_generation_service", lambda: testgen_service)

    assert client.post("/chatbot/message", json={"job_id": JOB_ID, "message": "hi"}).status_code == 200
    assert len(chatbot_fake.calls) == 1
    assert testgen_fake.calls == []
