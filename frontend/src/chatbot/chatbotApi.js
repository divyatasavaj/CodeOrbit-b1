/**
 * Chatbot HTTP client.
 *
 * Every call is repository-scoped by the analysis job id the user is viewing;
 * the widget never asks the user to choose a repository and never sends a
 * repository id of its own.
 */
import { API } from "../api.js";
import {
    CHATBOT_CONVERSATION_PATH,
    CHATBOT_CONVERSATIONS_PATH,
    CHATBOT_MESSAGE_PATH,
    CHATBOT_REPOSITORY_PATH,
} from "./chatbotLogic.js";

async function requestJson(path, { method = "GET", body, signal } = {}) {
    const response = await fetch(`${API}${path}`, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal,
    });

    let payload = null;
    try {
        payload = await response.json();
    } catch (err) {
        payload = null;
    }
    if (!response.ok) {
        const error = new Error(`chatbot request failed (${response.status})`);
        error.status = response.status;
        error.payload = payload;
        throw error;
    }
    return payload;
}

export function sendChatMessage({ jobId, message, conversationId, signal }) {
    const body = { job_id: jobId, message };
    if (conversationId) body.conversation_id = conversationId;
    return requestJson(CHATBOT_MESSAGE_PATH, { method: "POST", body, signal });
}

export function fetchRepositoryContext(jobId, signal) {
    return requestJson(CHATBOT_REPOSITORY_PATH(jobId), { signal });
}

export function fetchConversation(conversationId, jobId, signal) {
    return requestJson(CHATBOT_CONVERSATION_PATH(conversationId, jobId), { signal });
}

export function listConversations(jobId, signal) {
    return requestJson(
        `${CHATBOT_CONVERSATIONS_PATH}?job_id=${encodeURIComponent(String(jobId || ""))}`,
        { signal }
    );
}

export async function deleteConversation(conversationId, jobId, signal) {
    const response = await fetch(`${API}${CHATBOT_CONVERSATION_PATH(conversationId, jobId)}`, {
        method: "DELETE",
        signal,
    });
    if (!response.ok && response.status !== 404) {
        throw new Error(`delete failed (${response.status})`);
    }
    return true;
}