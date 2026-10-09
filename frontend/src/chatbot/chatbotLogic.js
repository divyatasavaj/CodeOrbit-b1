/**
 * Pure helpers for the repository chatbot widget.
 *
 * Kept free of React and of the network so the behaviour that matters
 * (request shape, repository binding, submission guards, error copy, citation
 * formatting/linking) is unit-testable without a browser or a test framework.
 */

export const CHATBOT_MESSAGE_PATH = "/chatbot/message";
export const CHATBOT_CONVERSATIONS_PATH = "/chatbot/conversations";
export const CHATBOT_REPOSITORY_PATH = (jobId) =>
    `/chatbot/repository/${encodeURIComponent(String(jobId || ""))}`;
export const CHATBOT_CONVERSATION_PATH = (conversationId, jobId) =>
    `/chatbot/conversations/${encodeURIComponent(String(conversationId || ""))}` +
    `?job_id=${encodeURIComponent(String(jobId || ""))}`;

/** Mirrors the backend's CHATBOT_MAX_MESSAGE_CHARS default. */
export const MAX_MESSAGE_CHARS = 2000;

export const GREETING =
    "Hi! I can help you understand this codebase. Ask me about functions, " +
    "dependencies, architecture, authentication, data flow, or potential change impacts.";

export const DEFAULT_SUGGESTIONS = [
    "Give me an overview of this repository.",
    "Where does the application start?",
    "What are the most complex or risky areas?",
];

/**
 * The request body for POST /chatbot/message.
 * The repository is never chosen here - it is derived server-side from job_id.
 */
export function buildMessageRequest({ jobId, message, conversationId }) {
    const payload = { job_id: jobId, message: String(message || "").trim() };
    if (conversationId) payload.conversation_id = conversationId;
    return payload;
}

export function validateMessage(message) {
    const trimmed = String(message || "").trim();
    if (!trimmed) return { ok: false, error: "Enter a question about this repository." };
    if (trimmed.length > MAX_MESSAGE_CHARS) {
        return { ok: false, error: `Questions are limited to ${MAX_MESSAGE_CHARS} characters.` };
    }
    return { ok: true, value: trimmed };
}

/** Guards: no repository, empty text, or an in-flight request. */
export function canSubmit({ message, sending, hasRepository }) {
    if (!hasRepository) return false;
    if (sending) return false;
    return validateMessage(message).ok;
}

/** Short name used when revealing a symbol in the analysis view. */
export function revealQueryFor(reference) {
    if (!reference) return "";
    const name = String(reference.name || "").split(".").pop() || "";
    return name || String(reference.filename || "");
}

export function citationLabel(citation) {
    if (!citation) return "";
    const file = citation.filename || "unknown";
    const start = citation.start_line;
    const end = citation.end_line;
    if (!start) return file;
    if (!end || end === start) return `${file}:${start}`;
    return `${file}:${start}-${end}`;
}

export function dedupeCitations(citations) {
    const seen = new Set();
    const out = [];
    for (const citation of citations || []) {
        const key = `${citation.filename}:${citation.start_line}:${citation.name}`;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(citation);
    }
    return out;
}

const REF_PATTERN =
    /((?:[\w.-]+\/)*[\w.-]+\.(?:py|js|ts|jsx|tsx))(?::(\d+)(?:-(\d+))?)?/g;

/**
 * Split an answer into plain-text and reference segments so `file.py:12-40`
 * mentions become clickable, matching the structured citations.
 */
export function linkifyAnswer(text, citations) {
    const known = new Set((citations || []).map((c) => c.filename));
    const segments = [];
    let lastIndex = 0;
    const source = String(text || "");
    let match;
    REF_PATTERN.lastIndex = 0;
    while ((match = REF_PATTERN.exec(source)) !== null) {
        const [raw, filename, startLine, endLine] = match;
        if (known.size && !known.has(filename)) continue;
        if (match.index > lastIndex) {
            segments.push({ type: "text", text: source.slice(lastIndex, match.index) });
        }
        segments.push({
            type: "ref",
            text: raw,
            filename,
            start_line: startLine ? Number(startLine) : null,
            end_line: endLine ? Number(endLine) : (startLine ? Number(startLine) : null),
        });
        lastIndex = match.index + raw.length;
    }
    if (lastIndex < source.length) {
        segments.push({ type: "text", text: source.slice(lastIndex) });
    }
    return segments;
}

export function isAbortError(error) {
    return Boolean(error) && (error.name === "AbortError" || error.code === 20);
}

/** Friendly, non-technical copy for every documented failure state. */
export function errorMessageFor(status, payload) {
    /* ``message`` is our own sanitized contract field; ``detail`` can carry
       framework text, so it is only trusted for client-side (4xx) errors. */
    const serverMessage =
        (payload && typeof payload.message === "string" && payload.message) || "";
    const detailMessage =
        (payload && typeof payload.detail === "string" && payload.detail) || "";
    const code = (payload && payload.error_code) || "";
    const byCode = {
        chatbot_not_configured:
            "CodeOracle's repository chatbot is not configured on the server. Please contact the administrator.",
        analysis_in_progress:
            "This repository is still being analyzed. Please try again once the structural analysis finishes.",
        chatbot_rate_limited: "You're sending questions too quickly. Please wait a moment.",
        chatbot_busy: "Another question about this repository is still being answered.",
        provider_rate_limited: "The AI provider is rate limiting requests. Please try again shortly.",
        provider_timeout: "CodeOracle could not reach the configured chatbot provider. Please try again.",
        provider_unavailable: "CodeOracle could not reach the configured chatbot provider. Please try again.",
        provider_auth_error: "The chatbot provider rejected the configured credentials.",
        provider_bad_response: "The chatbot provider returned an unusable response. Please try again.",
        conversation_not_found: "That conversation is no longer available. Starting a new one.",
    };
    if (byCode[code]) return byCode[code];
    if (serverMessage) return serverMessage;
    if (status === 404) return detailMessage || "This analysis is no longer available.";
    if (status === 413) return "That request was too large.";
    if (status && status < 500 && detailMessage) return detailMessage;
    if (status >= 500) return "CodeOracle could not reach the configured chatbot provider. Please try again.";
    if (status === 0 || status === undefined) return "Network error - check your connection and try again.";
    return "Something went wrong while answering. Please try again.";
}

export function isRetryable(status, payload) {
    if (status === 0 || status === undefined) return true;
    if (status === 429 || status === 408 || status === 409 || status >= 500) return true;
    return Boolean(payload && payload.retryable);
}

export function repositoryDisplayName(result, fallback = "Uploaded repository") {
    if (!result) return fallback;
    return result.repository || result.repository_url || fallback;
}

export function shortJobId(jobId) {
    const value = String(jobId || "");
    return value ? value.slice(0, 8) : "";
}

export function connectionLabel(chatbot) {
    if (!chatbot) return { text: "Checking provider...", tone: "pending" };
    if (!chatbot.configured) return { text: "Chatbot not configured", tone: "error" };
    return { text: `${chatbot.provider || "AI"} ready`, tone: "ok" };
}

export function messageForAnalysisState(repository) {
    if (!repository) return "";
    if (repository.analysis_ready) return "";
    return "Structural analysis is still running for this repository - answers may be partial.";
}

/** Keep only the trailing window of messages so the DOM stays small. */
export function visibleMessages(messages, limit = 60) {
    const list = messages || [];
    return list.length <= limit ? list : list.slice(list.length - limit);
}

export function assistantMessageFromResponse(response) {
    return {
        id: String(Date.now()),
        role: "assistant",
        content: response.answer || "",
        citations: dedupeCitations(response.citations || []),
        notes: response.notes || [],
        retrieval: response.retrieval || null,
        model: response.model || null,
        provider: response.provider || null,
    };
}
/**
 * Newest conversation id from a GET /chatbot/conversations payload, if any.
 * Used to restore the previous thread after a page refresh.
 */
export function latestConversationId(payload) {
    const list = (payload && payload.conversations) || [];
    if (!list.length) return null;
    return list[0].conversation_id || null;
}

/**
 * Hydrate stored (already-public) conversation messages for the widget.
 * Drops anything malformed and keeps only the trailing window so a long
 * persisted history can never blow up the DOM on open.
 */
export function messagesFromConversation(payload, limit = 60) {
    const list = (payload && payload.messages) || [];
    const messages = [];
    list.forEach((message, index) => {
        if (!message || (message.role !== "user" && message.role !== "assistant")) return;
        const content = typeof message.content === "string" ? message.content : "";
        if (!content.trim()) return;
        messages.push({
            id: `h-${index}-${message.created_at || index}`,
            role: message.role,
            content,
            citations: dedupeCitations(message.citations || []),
            notes: message.notes || [],
        });
    });
    return visibleMessages(messages, limit);
}