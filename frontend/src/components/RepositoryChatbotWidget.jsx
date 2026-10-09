import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
    fetchConversation,
    fetchRepositoryContext,
    listConversations,
    sendChatMessage,
} from "../chatbot/chatbotApi.js";
import {
    DEFAULT_SUGGESTIONS,
    GREETING,
    assistantMessageFromResponse,
    canSubmit,
    citationLabel,
    connectionLabel,
    dedupeCitations,
    errorMessageFor,
    isAbortError,
    isRetryable,
    linkifyAnswer,
    latestConversationId,
    messageForAnalysisState,
    messagesFromConversation,
    revealQueryFor,
    shortJobId,
    validateMessage,
    visibleMessages,
} from "../chatbot/chatbotLogic.js";

/* -------------------------------------------------------------------------- */
/* Icons (inline - no new dependency)                                         */
/* -------------------------------------------------------------------------- */
function ChatGlyph({ size = 22, stroke = "currentColor" }) {
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={stroke}
             strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H8l-4.5 3v-4.2A8.5 8.5 0 0 1 12.5 3 8.5 8.5 0 0 1 21 11.5z" />
            <path d="M9.2 11.9l2 2 3.6-4" />
        </svg>
    );
}

function SparkGlyph({ size = 14 }) {
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="2" strokeLinecap="round" aria-hidden="true">
            <path d="M12 3v4m0 10v4M3 12h4m10 0h4M5.6 5.6l2.8 2.8m7.2 7.2l2.8 2.8m0-12.8l-2.8 2.8M8.4 15.6l-2.8 2.8" />
        </svg>
    );
}

/* -------------------------------------------------------------------------- */
/* Source reference viewer (lightweight, opens from any citation)             */
/* -------------------------------------------------------------------------- */
const SourcePeek = React.memo(function SourcePeek({ reference, onClose, onOpenInAnalysis }) {
    useEffect(() => {
        const onKey = (event) => { if (event.key === "Escape") onClose(); };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [onClose]);

    if (!reference) return null;
    const lines = String(reference.source || "").replace(/\s+$/, "").split("\n");
    const firstLine = reference.start_line || 1;

    return (
        <div className="co-chat-peek-overlay" role="dialog" aria-modal="true"
             aria-label={`Source for ${reference.name || reference.filename}`}
             onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
            <div className="co-chat-peek">
                <div className="co-chat-peek-head">
                    <div style={{ minWidth: 0 }}>
                        <div className="co-chat-peek-title">{reference.name || reference.filename}</div>
                        <div className="co-chat-peek-path">{citationLabel(reference)}</div>
                    </div>
                    <button type="button" className="co-chat-icon-btn" onClick={onClose} aria-label="Close source viewer">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>
                    </button>
                </div>
                <div className="co-chat-peek-body">
                    {lines.map((line, index) => (
                        <div className="co-chat-peek-line" key={index}>
                            <span className="co-chat-peek-ln">{firstLine + index}</span>
                            <span className="co-chat-peek-code">{line || " "}</span>
                        </div>
                    ))}
                </div>
                <div className="co-chat-peek-foot">
                    <span className="co-chat-peek-note">Static source from the analyzed snapshot</span>
                    {onOpenInAnalysis && (
                        <button type="button" className="co-chat-btn co-chat-btn--primary"
                                onClick={() => { onOpenInAnalysis(reference); onClose(); }}>
                            Open in analysis
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
});

/* -------------------------------------------------------------------------- */
/* One message                                                                */
/* -------------------------------------------------------------------------- */
const MessageBubble = React.memo(function MessageBubble({ message, onOpenReference }) {
    const isUser = message.role === "user";
    const citations = useMemo(
        () => dedupeCitations(message.citations || []),
        [message.citations]
    );
    const segments = useMemo(
        () => (isUser ? [] : linkifyAnswer(message.content, citations)),
        [isUser, message.content, citations]
    );

    return (
        <div className={`co-chat-msg ${isUser ? "co-chat-msg--user" : "co-chat-msg--bot"}`}>
            <div className="co-chat-bubble">
                {isUser ? (
                    <span className="co-chat-text">{message.content}</span>
                ) : (
                    <span className="co-chat-text">
                        {segments.map((segment, index) => (
                            segment.type === "ref" ? (
                                <button
                                    type="button"
                                    key={index}
                                    className="co-chat-inline-ref"
                                    onClick={() => onOpenReference({
                                        filename: segment.filename,
                                        start_line: segment.start_line,
                                        end_line: segment.end_line,
                                    })}
                                    title="View this source"
                                >
                                    {segment.text}
                                </button>
                            ) : (
                                <React.Fragment key={index}>{segment.text}</React.Fragment>
                            )
                        ))}
                    </span>
                )}
            </div>

            {!isUser && message.notes && message.notes.length > 0 && (
                <ul className="co-chat-notes">
                    {message.notes.map((note, index) => <li key={index}>{note}</li>)}
                </ul>
            )}

            {!isUser && citations.length > 0 && (
                <div className="co-chat-cites">
                    <span className="co-chat-cites-label">Sources</span>
                    {citations.map((citation, index) => (
                        <button
                            type="button"
                            key={`${citation.filename}:${citation.start_line}:${index}`}
                            className="co-chat-cite"
                            onClick={() => onOpenReference(citation)}
                            title={`${citation.reason || "retrieved source"}`}
                        >
                            {citationLabel(citation)}
                        </button>
                    ))}
                </div>
            )}

            {message.stopped && <div className="co-chat-stopped">Stopped.</div>}
            {message.failed && <div className="co-chat-stopped">Not sent.</div>}
        </div>
    );
});

/* -------------------------------------------------------------------------- */
/* Panel                                                                      */
/* -------------------------------------------------------------------------- */
function ChatPanel({ jobId, repositoryName, analysisReady, onClose, onOpenSourceRef, launcherRef }) {
    const [messages, setMessages] = useState([]);
    const [input, setInput] = useState("");
    const [sending, setSending] = useState(false);
    const [error, setError] = useState(null);
    const [retryQuestion, setRetryQuestion] = useState(null);
    const [conversationId, setConversationId] = useState(null);
    const [suggestions, setSuggestions] = useState(DEFAULT_SUGGESTIONS);
    const [chatbot, setChatbot] = useState(null);
    const [repository, setRepository] = useState(null);
    const [peek, setPeek] = useState(null);
    const [contextFailed, setContextFailed] = useState(false);

    const abortRef = useRef(null);
    const sendingRef = useRef(false);
    const scrollRef = useRef(null);
    const inputRef = useRef(null);
    const panelRef = useRef(null);

    const displayName = repositoryName || repository?.name || "this repository";
    const status = connectionLabel(chatbot);
    const analysisNote = messageForAnalysisState(
        repository || { analysis_ready: analysisReady !== false }
    );

    /* One small context request when the panel opens - the repository is
       derived server-side from jobId, so there is nothing for the user to pick. */
    useEffect(() => {
        const controller = new AbortController();
        let cancelled = false;
        fetchRepositoryContext(jobId, controller.signal)
            .then((context) => {
                if (cancelled) return;
                setChatbot(context.chatbot || null);
                setRepository(context.repository || null);
                if (Array.isArray(context.suggestions) && context.suggestions.length) {
                    setSuggestions(context.suggestions.slice(0, 6));
                }
            })
            .catch((err) => {
                if (!cancelled && !isAbortError(err)) setContextFailed(true);
            });
        /* Restore this job's most recent persisted thread (bounded). The
           backend re-verifies job access on every call, and the panel stays
           empty when there is no history. */
        listConversations(jobId, controller.signal)
            .then((summary) => {
                if (cancelled) return null;
                const latestId = latestConversationId(summary);
                if (!latestId) return null;
                return fetchConversation(latestId, jobId, controller.signal).then((payload) => {
                    if (cancelled) return;
                    const restored = messagesFromConversation(payload);
                    if (!restored.length) return;
                    setMessages((prev) => (prev.length ? prev : restored));
                    setConversationId((prev) => prev || latestId);
                });
            })
            .catch(() => {});
        return () => { cancelled = true; controller.abort(); };
    }, [jobId]);

    useEffect(() => {
        if (inputRef.current) inputRef.current.focus();
    }, []);

    useEffect(() => {
        const node = scrollRef.current;
        if (node) node.scrollTop = node.scrollHeight;
    }, [messages, sending]);

    useEffect(() => () => {
        if (abortRef.current) abortRef.current.abort();
    }, []);

    useEffect(() => {
        const onKey = (event) => {
            if (event.key === "Escape" && !peek) {
                onClose();
            }
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [onClose, peek]);

    const send = useCallback(async (rawQuestion) => {
        const check = validateMessage(rawQuestion);
        if (!check.ok) { setError({ text: check.error, retryable: false }); return; }
        if (sendingRef.current) return;

        const question = check.value;
        sendingRef.current = true;
        setSending(true);
        setError(null);
        setRetryQuestion(null);
        setInput("");
        const localId = `u-${Date.now()}`;
        setMessages((prev) => [...prev, { id: localId, role: "user", content: question }]);

        const controller = new AbortController();
        abortRef.current = controller;
        try {
            const response = await sendChatMessage({
                jobId, message: question, conversationId, signal: controller.signal,
            });
            if (response && response.conversation_id) setConversationId(response.conversation_id);
            setMessages((prev) => [...prev, assistantMessageFromResponse(response)]);
        } catch (err) {
            if (isAbortError(err)) {
                setMessages((prev) => [...prev, {
                    id: `s-${Date.now()}`, role: "assistant", content: "", stopped: true, citations: [],
                }]);
            } else {
                const status = err && err.status;
                const payload = err && err.payload;
                setMessages((prev) => prev.map((message) => (
                    message.id === localId ? { ...message, failed: true } : message
                )));
                setRetryQuestion(question);
                setError({ text: errorMessageFor(status, payload), retryable: isRetryable(status, payload) });
            }
        } finally {
            abortRef.current = null;
            sendingRef.current = false;
            setSending(false);
        }
    }, [conversationId, jobId]);

    const handleSubmit = useCallback((event) => {
        if (event) event.preventDefault();
        send(input);
    }, [input, send]);

    const handleComposerKeyDown = useCallback((event) => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            send(input);
        }
    }, [input, send]);

    const handleStop = useCallback(() => {
        if (abortRef.current) abortRef.current.abort();
    }, []);

    const handleRetry = useCallback(() => {
        if (!retryQuestion) return;
        setMessages((prev) => {
            const index = prev.findIndex((message) => message.failed);
            return index === -1 ? prev : prev.slice(0, index);
        });
        setError(null);
        send(retryQuestion);
    }, [retryQuestion, send]);

    const startNewConversation = useCallback(() => {
        if (abortRef.current) abortRef.current.abort();
        setMessages([]);
        setError(null);
        setRetryQuestion(null);
        setConversationId(null);
        setPeek(null);
        if (inputRef.current) inputRef.current.focus();
    }, []);

    const openReference = useCallback((reference) => {
        if (!reference) return;
        if (reference.source) {
            setPeek(reference);
            return;
        }
        if (onOpenSourceRef) onOpenSourceRef(reference);
    }, [onOpenSourceRef]);

    const showSuggestions = messages.length === 0 && !sending;
    const submitEnabled = canSubmit({ message: input, sending, hasRepository: Boolean(jobId) });

    return (
        <section
            className="co-chat-panel"
            ref={panelRef}
            role="dialog"
            aria-modal="false"
            aria-label={`Ask CodeOracle about ${displayName}`}
        >
            <header className="co-chat-head">
                <div className="co-chat-head-icon"><ChatGlyph size={18} /></div>
                <div className="co-chat-head-text">
                    <div className="co-chat-head-title">Ask CodeOracle</div>
                    <div className="co-chat-head-repo" title={`${displayName} · job ${jobId}`}>
                        <span>{displayName}</span>
                        <span className="co-chat-head-job">{shortJobId(jobId)}</span>
                    </div>
                </div>
                <span className={`co-chat-status co-chat-status--${status.tone}`} title={status.text}>
                    <span className="co-chat-dot" aria-hidden="true" />
                    <span className="co-chat-status-text">{status.text}</span>
                </span>
                <button type="button" className="co-chat-icon-btn" onClick={startNewConversation}
                        aria-label="Start a new conversation" title="New conversation">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
                </button>
                <button type="button" className="co-chat-icon-btn" onClick={onClose}
                        aria-label="Minimize chatbot" title="Minimize">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M5 12h14" /></svg>
                </button>
            </header>

            {analysisNote && <div className="co-chat-banner">{analysisNote}</div>}
            {contextFailed && (
                <div className="co-chat-banner co-chat-banner--soft">
                    Repository details could not be loaded, but your analysis is still connected.
                </div>
            )}

            <div className="co-chat-body" ref={scrollRef} aria-live="polite">
                {showSuggestions && (
                    <div className="co-chat-greet">
                        <p className="co-chat-greet-text">{GREETING}</p>
                        <div className="co-chat-suggestions">
                            {suggestions.map((question) => (
                                <button type="button" key={question} className="co-chat-suggestion"
                                        onClick={() => send(question)} disabled={sending}>
                                    <SparkGlyph />
                                    <span>{question}</span>
                                </button>
                            ))}
                        </div>
                    </div>
                )}

                {visibleMessages(messages, 60).map((message) => (
                    <MessageBubble key={message.id} message={message} onOpenReference={openReference} />
                ))}

                {sending && (
                    <div className="co-chat-thinking" role="status">
                        <span className="co-chat-thinking-text">CodeOracle is reading this repository...</span>
                        <button type="button" className="co-chat-btn co-chat-btn--ghost" onClick={handleStop}>
                            Stop
                        </button>
                    </div>
                )}
            </div>

            {error && (
                <div className="co-chat-error" role="alert">
                    <span>{error.text}</span>
                    {error.retryable && retryQuestion && (
                        <button type="button" className="co-chat-btn co-chat-btn--ghost" onClick={handleRetry}>
                            Try again
                        </button>
                    )}
                </div>
            )}

            <form className="co-chat-composer" onSubmit={handleSubmit}>
                <textarea
                    ref={inputRef}
                    className="co-chat-input"
                    rows={2}
                    value={input}
                    placeholder={jobId ? "Ask about this repository..." : "No repository selected"}
                    onChange={(event) => setInput(event.target.value)}
                    onKeyDown={handleComposerKeyDown}
                    disabled={!jobId}
                    aria-label="Ask a question about this repository"
                />
                <div className="co-chat-composer-row">
                    <span className="co-chat-hint">Enter to send · Shift+Enter for a new line</span>
                    <button type="submit" className="co-chat-send" disabled={!submitEnabled}
                            aria-label="Send question">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 12l16-8-6 8 6 8-16-8z" /></svg>
                    </button>
                </div>
            </form>

            <SourcePeek reference={peek} onClose={() => setPeek(null)} onOpenInAnalysis={onOpenSourceRef} />
        </section>
    );
}

/* -------------------------------------------------------------------------- */
/* Launcher + lazy panel                                                      */
/* -------------------------------------------------------------------------- */
const RepositoryChatbotWidget = React.memo(function RepositoryChatbotWidget({
    jobId,
    repositoryName = "",
    analysisReady = true,
    sourceType = "upload",
    onOpenSourceRef,
}) {
    const [open, setOpen] = useState(false);
    const launcherRef = useRef(null);

    const close = useCallback(() => {
        setOpen(false);
        if (launcherRef.current) launcherRef.current.focus();
    }, []);

    if (!jobId) return null;

    return (
        <>
            {open && (
                <ChatPanel
                    jobId={jobId}
                    repositoryName={repositoryName}
                    analysisReady={analysisReady}
                    sourceType={sourceType}
                    onClose={close}
                    onOpenSourceRef={onOpenSourceRef}
                    launcherRef={launcherRef}
                />
            )}
            <button
                type="button"
                ref={launcherRef}
                className={`co-chat-launcher${open ? " is-open" : ""}`}
                onClick={() => (open ? close() : setOpen(true))}
                aria-label="Ask about this repository"
                aria-expanded={open}
                aria-haspopup="dialog"
                title="Ask CodeOracle about this repository"
            >
                <span className="co-chat-launcher-ring" aria-hidden="true" />
                {open ? (
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true">
                        <path d="M6 6l12 12M18 6L6 18" />
                    </svg>
                ) : (
                    <ChatGlyph size={23} />
                )}
            </button>
        </>
    );
});

export default RepositoryChatbotWidget;
export { ChatPanel as _ChatPanel, GREETING as _GREETING, revealQueryFor as _revealQueryFor };
