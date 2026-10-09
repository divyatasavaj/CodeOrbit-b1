/**
 * Pure-logic tests for the repository chatbot widget.
 *
 * Runs with plain Node (no test framework, no browser, no network):
 *     node src/chatbot/chatbotLogic.test.mjs
 */
import assert from "node:assert/strict";
import {
    CHATBOT_CONVERSATION_PATH,
    CHATBOT_REPOSITORY_PATH,
    DEFAULT_SUGGESTIONS,
    MAX_MESSAGE_CHARS,
    assistantMessageFromResponse,
    buildMessageRequest,
    canSubmit,
    citationLabel,
    connectionLabel,
    dedupeCitations,
    errorMessageFor,
    isRetryable,
    latestConversationId,
    linkifyAnswer,
    messageForAnalysisState,
    messagesFromConversation,
    repositoryDisplayName,
    revealQueryFor,
    shortJobId,
    validateMessage,
    visibleMessages,
} from "./chatbotLogic.js";

let passed = 0;
let failed = 0;
function test(name, fn) {
    try {
        fn();
        passed += 1;
        console.log(`PASS ${name}`);
    } catch (err) {
        failed += 1;
        console.error(`FAIL ${name}`);
        console.error(`     ${err && err.message}`);
    }
}

/* --- request binding -------------------------------------------------- */

test("buildMessageRequest binds the analysis job id (no repository chosen client-side)", () => {
    const body = buildMessageRequest({ jobId: "job-123", message: "  explain auth  " });
    assert.deepEqual(body, { job_id: "job-123", message: "explain auth" });
    assert.equal("repository" in body, false);
    assert.equal("repository_id" in body, false);
});

test("buildMessageRequest includes conversation_id only when present", () => {
    assert.equal("conversation_id" in buildMessageRequest({ jobId: "j", message: "hi" }), false);
    assert.equal(
        buildMessageRequest({ jobId: "j", message: "hi", conversationId: "c1" }).conversation_id,
        "c1"
    );
});

test("chat routes are job-scoped and URL encoded", () => {
    assert.equal(CHATBOT_REPOSITORY_PATH("a/b c"), "/chatbot/repository/a%2Fb%20c");
    assert.equal(CHATBOT_CONVERSATION_PATH("c 1", "job 2"), "/chatbot/conversations/c%201?job_id=job%202");
});

/* --- validation + submit guards --------------------------------------- */

test("validateMessage rejects empty/whitespace and accepts trimmed text", () => {
    assert.equal(validateMessage("").ok, false);
    assert.equal(validateMessage("   ").ok, false);
    const ok = validateMessage("  what does main do?  ");
    assert.equal(ok.ok, true);
    assert.equal(ok.value, "what does main do?");
});

test("validateMessage enforces the character limit", () => {
    assert.equal(validateMessage("x".repeat(MAX_MESSAGE_CHARS)).ok, true);
    assert.equal(validateMessage("x".repeat(MAX_MESSAGE_CHARS + 1)).ok, false);
});

test("canSubmit requires a repository, text and an idle composer", () => {
    assert.equal(canSubmit({ message: "hi", sending: false, hasRepository: true }), true);
    assert.equal(canSubmit({ message: "hi", sending: true, hasRepository: true }), false);
    assert.equal(canSubmit({ message: "hi", sending: false, hasRepository: false }), false);
    assert.equal(canSubmit({ message: "  ", sending: false, hasRepository: true }), false);
});

/* --- error copy + retryability ---------------------------------------- */

test("errorMessageFor maps every documented failure code to user-safe copy", () => {
    assert.match(errorMessageFor(503, { error_code: "chatbot_not_configured" }), /not configured/i);
    assert.match(errorMessageFor(409, { error_code: "analysis_in_progress" }), /still being analyzed/i);
    assert.match(errorMessageFor(429, { error_code: "chatbot_rate_limited" }), /too quickly/i);
    assert.match(errorMessageFor(409, { error_code: "chatbot_busy" }), /still being answered/i);
    assert.match(errorMessageFor(503, { error_code: "provider_timeout" }), /could not reach/i);
    assert.match(errorMessageFor(502, { error_code: "provider_auth_error" }), /credentials/i);
});

test("errorMessageFor never leaks raw provider/stack text and falls back sensibly", () => {
    const msg = errorMessageFor(500, { detail: "Traceback: KeyError at gemini_provider.py:91" });
    assert.equal(msg, "CodeOracle could not reach the configured chatbot provider. Please try again.");
    assert.equal(errorMessageFor(0, null), "Network error - check your connection and try again.");
    assert.equal(errorMessageFor(404, null), "This analysis is no longer available.");
    assert.equal(errorMessageFor(undefined, null), "Network error - check your connection and try again.");
    assert.equal(errorMessageFor(404, { detail: "Analysis job not found" }), "Analysis job not found");
    assert.equal(errorMessageFor(422, { detail: "job_id is required" }), "job_id is required");
});

test("errorMessageFor surfaces a sanitized server message when no code is known", () => {
    assert.equal(errorMessageFor(400, { message: "Message is required." }), "Message is required.");
});

test("isRetryable treats transient failures as retryable and 4xx validation as not", () => {
    assert.equal(isRetryable(500, null), true);
    assert.equal(isRetryable(429, null), true);
    assert.equal(isRetryable(409, null), true);
    assert.equal(isRetryable(408, null), true);
    assert.equal(isRetryable(0, null), true);
    assert.equal(isRetryable(404, null), false);
    assert.equal(isRetryable(400, null), false);
    assert.equal(isRetryable(403, { retryable: true }), true);
});

/* --- citations --------------------------------------------------------- */

const citations = [
    { filename: "src/auth/auth.service.ts", name: "AuthService.login", start_line: 42, end_line: 78, source: "x" },
    { filename: "src/auth/auth.service.ts", name: "AuthService.login", start_line: 42, end_line: 78, source: "x" },
    { filename: "src/auth/auth.controller.ts", name: "login", start_line: 15, end_line: 36, source: "y" },
];

test("citationLabel renders file:start-end", () => {
    assert.equal(citationLabel(citations[0]), "src/auth/auth.service.ts:42-78");
    assert.equal(citationLabel(citations[2]), "src/auth/auth.controller.ts:15-36");
    assert.equal(citationLabel({ filename: "a.py", start_line: 7 }), "a.py:7");
    assert.equal(citationLabel({ filename: "a.py" }), "a.py");
    assert.equal(citationLabel({ filename: "a.py", start_line: 7, end_line: 7 }), "a.py:7");
});

test("dedupeCitations removes repeats but keeps order", () => {
    const out = dedupeCitations(citations);
    assert.equal(out.length, 2);
    assert.equal(out[0].filename, "src/auth/auth.service.ts");
    assert.equal(out[1].filename, "src/auth/auth.controller.ts");
});

test("linkifyAnswer links only filenames that match structured citations", () => {
    const segments = linkifyAnswer(
        "The login lives in src/auth/auth.service.ts:42-78 and calls helper.py:9.",
        citations
    );
    const refs = segments.filter((s) => s.type === "ref");
    assert.equal(refs.length, 1);
    assert.equal(refs[0].filename, "src/auth/auth.service.ts");
    assert.equal(refs[0].start_line, 42);
    assert.equal(refs[0].end_line, 78);
    assert.match(segments.map((s) => s.text).join(""), /helper\.py:9/);
});

test("linkifyAnswer keeps plain text intact when there are no citations", () => {
    const segments = linkifyAnswer("Nothing to see here.", []);
    assert.equal(segments.length, 1);
    assert.equal(segments[0].text, "Nothing to see here.");
});

/* --- repository / status display -------------------------------------- */

test("connectionLabel reflects configuration state", () => {
    assert.deepEqual(connectionLabel({ configured: true, provider: "gemini" }), { text: "gemini ready", tone: "ok" });
    assert.equal(connectionLabel({ configured: false }).tone, "error");
    assert.equal(connectionLabel(null).tone, "pending");
});

test("messageForAnalysisState warns only while analysis is unfinished", () => {
    assert.equal(messageForAnalysisState({ analysis_ready: true }), "");
    assert.match(messageForAnalysisState({ analysis_ready: false }), /still running/i);
    assert.equal(messageForAnalysisState(null), "");
});

test("repositoryDisplayName prefers the repository name then the URL", () => {
    assert.equal(repositoryDisplayName({ repository: "acme/widget" }), "acme/widget");
    assert.equal(repositoryDisplayName({ repository_url: "https://github.com/acme/widget" }), "https://github.com/acme/widget");
    assert.equal(repositoryDisplayName(null), "Uploaded repository");
});

test("shortJobId and revealQueryFor are defensive", () => {
    assert.equal(shortJobId("abcdef123456"), "abcdef12");
    assert.equal(shortJobId(null), "");
    assert.equal(revealQueryFor({ name: "AuthService.login" }), "login");
    assert.equal(revealQueryFor({ filename: "a.py" }), "a.py");
    assert.equal(revealQueryFor(null), "");
});

test("visibleMessages keeps only the trailing window", () => {
    const list = Array.from({ length: 80 }, (_, i) => ({ id: String(i) }));
    assert.equal(visibleMessages(list, 60).length, 60);
    assert.equal(visibleMessages(list, 60)[0].id, "20");
    assert.equal(visibleMessages(list, 60).at(-1).id, "79");
    assert.equal(visibleMessages([{ id: "1" }], 60).length, 1);
});

test("assistantMessageFromResponse dedupes citations and defaults content", () => {
    const msg = assistantMessageFromResponse({ answer: "hi", citations, notes: ["n1"] });
    assert.equal(msg.role, "assistant");
    assert.equal(msg.content, "hi");
    assert.equal(msg.citations.length, 2);
    assert.deepEqual(msg.notes, ["n1"]);
});

test("default suggestions exist for the empty state", () => {
    assert.ok(DEFAULT_SUGGESTIONS.length >= 3);
    assert.ok(DEFAULT_SUGGESTIONS.every((q) => typeof q === "string" && q.length > 0));
});

/* --- persisted conversation restore ------------------------------------ */

test("latestConversationId picks the newest summary and tolerates empties", () => {
    assert.equal(latestConversationId({ conversations: [{ conversation_id: "c2" }, { conversation_id: "c1" }] }), "c2");
    assert.equal(latestConversationId({ conversations: [] }), null);
    assert.equal(latestConversationId(null), null);
});

test("messagesFromConversation hydrates valid turns and drops junk", () => {
    const restored = messagesFromConversation({
        messages: [
            { role: "user", content: "explain auth" },
            { role: "system", content: "ignore me" },
            { role: "assistant", content: "", citations: [] },
            { role: "assistant", content: "It lives in auth.py.", citations, notes: ["n1"] },
        ],
    });
    assert.equal(restored.length, 2);
    assert.equal(restored[0].role, "user");
    assert.equal(restored[1].content, "It lives in auth.py.");
    assert.equal(restored[1].citations.length, 2);
    assert.deepEqual(restored[1].notes, ["n1"]);
});

test("messagesFromConversation keeps only the trailing window", () => {
    const messages = Array.from({ length: 90 }, (_, i) => ({ role: "user", content: `q${i}` }));
    const restored = messagesFromConversation({ messages }, 60);
    assert.equal(restored.length, 60);
    assert.equal(restored[0].content, "q30");
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
