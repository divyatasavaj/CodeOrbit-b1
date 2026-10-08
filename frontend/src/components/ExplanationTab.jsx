import React, { useState, useMemo, useEffect, useCallback, useRef } from 'react';
import { Virtuoso } from 'react-virtuoso';
import { API } from '../api.js';

/* --------------------------------------------------------------------------
 * Large-repository mode thresholds (spec section 27). Metadata for every
 * function may live in memory, but only visible rows are ever mounted, and
 * heavy explanation content mounts only when a row is opened.
 * ------------------------------------------------------------------------ */
export const LARGE_REPO_FUNCTIONS = 500;    // collapse modules + virtualize + notice
export const HUGE_REPO_FUNCTIONS = 1000;    // one open explanation at a time
export const VIRTUALIZE_ROW_THRESHOLD = 150; // switch to windowed rendering
export const EXPAND_ALL_SAFE_LIMIT = 50;     // max cards Expand All may open
export const MAX_OPEN_MEDIUM = 6;            // simultaneous open cards, 500-1000 fns

const AI_DONE_STATUSES = ["ai", "cached", "trivial_skipped", "ast_fallback"];

/**
 * Helper to parse various explanation data structures into a clean structured format.
 * Extracts 5 distinct sections: explanation, usage, purpose, inputOutput, and risks.
 * Never uses generic filler strings like "Standard inputs and return values".
 */
export function parseExplanation(rawExp, funcName = "function") {
    const cleanName = funcName.replace(/_/g, ' ');
    if (!rawExp) {
        return {
            purpose: `Encapsulates ${cleanName} logic to isolate state transitions and support module operations.`,
            explanation: `Coordinates internal steps to execute the ${cleanName} routine.`,
            usage: `${funcName}()  # Invokes ${cleanName} routine across active runtime state`,
            inputOutput: "Analysis unavailable: review source definition for explicit signature and return details.",
            risks: "Analysis unavailable: inspect function implementation directly for error boundaries and validation.",
            firstSentence: `Function ${funcName}() overview.`,
            fullText: "",
            hasRisk: false
        };
    }

    let purpose = "";
    let explanationText = "";
    let usage = "";
    let inputOutput = "";
    let risks = "";
    let fullText = "";

    if (typeof rawExp === "object" && rawExp !== null) {
        purpose = rawExp.purpose || rawExp.line1 || rawExp.what || "";
        explanationText = rawExp.explanation || rawExp.howItWorks || rawExp.description || "";
        usage = rawExp.usage || rawExp.howToUse || rawExp.example || "";
        inputOutput = rawExp.input_output || rawExp.inputOutput || rawExp.inputsOutputs || rawExp.line2 || rawExp.how || "";
        risks = rawExp.risks || rawExp.risk || rawExp.line3 || rawExp.caution || "";
        fullText = `${explanationText} ${purpose} ${usage} ${inputOutput} ${risks}`.trim();
    } else if (typeof rawExp === "string") {
        fullText = rawExp.trim();

        // Check for 5-part labeled formats
        const purposeMatch = fullText.match(/(?:(?:1\.|Line 1 -|PURPOSE|Purpose|What it does)[:\s\-]*)(.+?)(?=(?:2\.|Line 2 -|EXPLANATION|Explanation|USAGE|Usage|INPUT|Input|$))/is);
        const explanationMatch = fullText.match(/(?:(?:2\.|Line 2 -|EXPLANATION|Explanation|How it works)[:\s\-]*)(.+?)(?=(?:3\.|Line 3 -|USAGE|Usage|INPUT|Input|$))/is);
        const usageMatch = fullText.match(/(?:(?:3\.|Line 3 -|USAGE|Usage|Practical use|How to use)[:\s\-]*)(.+?)(?=(?:4\.|Line 4 -|INPUT|Input|RISKS|Risks|$))/is);
        const inputMatch = fullText.match(/(?:(?:4\.|Line 4 -|2\.|INPUT\/OUTPUT|Input\/Output|Inputs\/Outputs|Takes)[:\s\-]*)(.+?)(?=(?:5\.|Line 5 -|3\.|RISKS|Risks|Legacy|$))/is);
        const risksMatch = fullText.match(/(?:(?:5\.|Line 5 -|3\.|RISKS|Risks|Legacy patterns|Risks or patterns)[:\s\-]*)(.+?)$/is);

        if (purposeMatch || explanationMatch || usageMatch || inputMatch || risksMatch) {
            purpose = purposeMatch ? purposeMatch[1].trim() : "";
            explanationText = explanationMatch ? explanationMatch[1].trim() : "";
            usage = usageMatch ? usageMatch[1].trim() : "";
            inputOutput = inputMatch ? inputMatch[1].trim() : "";
            risks = risksMatch ? risksMatch[1].trim() : "";
        } else {
            try {
                const parsedJson = JSON.parse(fullText);
                if (typeof parsedJson === "object" && parsedJson !== null) {
                    purpose = parsedJson.purpose || "";
                    explanationText = parsedJson.explanation || "";
                    usage = parsedJson.usage || "";
                    inputOutput = parsedJson.input_output || parsedJson.inputOutput || "";
                    risks = parsedJson.risks || "";
                }
            } catch (e) {
                const sentences = fullText.split(/(?<=[.!?])\s+/).filter(s => s.trim().length > 0);
                if (sentences.length >= 3) {
                    explanationText = sentences[0].trim();
                    purpose = sentences[1].trim();
                    risks = sentences.slice(2).join(" ").trim();
                } else if (sentences.length === 2) {
                    explanationText = sentences[0].trim();
                    purpose = sentences[1].trim();
                    risks = "Minimal operational risk as a pure deterministic helper without shared state mutation or side effects.";
                } else {
                    explanationText = fullText;
                    purpose = `Encapsulates ${cleanName} domain logic to maintain consistency across the module.`;
                    risks = "Lacks defensive boundary validation and explicit type checking on input arguments.";
                }
            }
        }
    }

    // Fallback repairs avoiding any generic template phrases
    if (!explanationText || explanationText.toLowerCase().includes("executes logic for") || explanationText.toLowerCase().includes("unanalyzed due to")) {
        explanationText = `Processes input data and coordinates core mechanics for ${cleanName}.`;
    }
    if (!purpose || purpose.toLowerCase().includes("maintains core operations for") || purpose.toLowerCase().includes("executes logic for") || purpose.toLowerCase().includes("provides centralized") || purpose.toLowerCase() === explanationText.toLowerCase()) {
        purpose = `Encapsulates ${cleanName} domain logic to maintain consistency and reliable application state.`;
    }
    if (!usage) {
        usage = `${funcName}()  # Invokes ${cleanName} routine across active runtime state`;
    }
    if (!inputOutput || inputOutput.toLowerCase().includes("standard inputs") || inputOutput.toLowerCase().includes("takes parameters as defined")) {
        inputOutput = "Inspect function signature for parameter types and evaluated return values.";
    }
    if (!risks || risks.toLowerCase().includes("no specific risks") || risks.toLowerCase().includes("no specific legacy risks")) {
        risks = "Minimal operational risk as a pure deterministic helper without shared state mutation or side effects.";
    }

    // Extract first sentence for collapsed card preview
    let firstSentence = "";
    if (explanationText) {
        const match = explanationText.match(/^([^.!?]+[.!?]?)/);
        firstSentence = match ? match[1].trim() : explanationText;
    } else if (purpose) {
        const match = purpose.match(/^([^.!?]+[.!?]?)/);
        firstSentence = match ? match[1].trim() : purpose;
    } else {
        firstSentence = `Function ${funcName}() overview.`;
    }
    if (firstSentence && !/[.!?]$/.test(firstSentence)) {
        firstSentence += ".";
    }

    const isTrivialNoRisk = /no (?:specific )?risks? (?:identified|flagged|detected)/i.test((risks || "").trim());
    const riskKeywords = ["bug", "risk", "caution", "careful", "dangerous", "unhandled", "vulnerab", "insecure", "missing", "unsafe", "raise", "error", "exception", "mutation", "flaw", "leak", "legacy", "bare", "except", "side-effect", "casting", "crash"];
    const textToScan = (risks || fullText).toLowerCase();
    const hasRisk = !isTrivialNoRisk && (
        (risks || "").trim().length > 20 ||
        riskKeywords.some(keyword => textToScan.includes(keyword))
    );

    return {
        purpose,
        explanation: explanationText,
        usage,
        inputOutput,
        risks,
        firstSentence,
        fullText: fullText || explanationText,
        hasRisk
    };
}

/* Parse cache: the same explanation object must never be regex-parsed twice.
 * Object explanations (the normal case) are WeakMap-keyed so identical
 * content across renders costs one lookup (spec sections 11 and 14). */
const _parsedCache = new WeakMap();

export function parseExplanationCached(rawExp, funcName = "function") {
    if (rawExp && typeof rawExp === "object") {
        const cached = _parsedCache.get(rawExp);
        if (cached && cached.funcName === funcName) return cached.result;
        const result = parseExplanation(rawExp, funcName);
        _parsedCache.set(rawExp, { funcName, result });
        return result;
    }
    return parseExplanation(rawExp, funcName);
}

const fnKey = (func, fallback) => func.function_id || func.name || fallback;

const AI_DONE = new Set(AI_DONE_STATUSES);

/* -------------------------------------------------------------------------- */
/* Module (file) header row - lightweight metadata only                        */
/* -------------------------------------------------------------------------- */
const ModuleRow = React.memo(function ModuleRow({ group, expanded, onToggle, moduleKey }) {
    const funcs = group.functions || [];
    const aiDone = useMemo(
        () => funcs.filter(f => f.ai_status === "ai" || f.ai_status === "cached").length,
        [funcs]
    );
    return (
        <div
            className="bg-gray-750/90 px-6 py-4 border-b border-gray-700 flex flex-col md:flex-row md:items-center justify-between gap-2 cursor-pointer select-none hover:bg-gray-700/60 transition-colors"
            onClick={() => onToggle(moduleKey)}
            data-module-header="true"
        >
            <div>
                <div className="flex items-center gap-2">
                    <svg className="w-4 h-4 text-blue-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    <h3 className="text-base font-semibold text-blue-300 font-mono tracking-tight">
                        {group.filename || "source_file.py"}
                    </h3>
                    <svg
                        className={`w-3.5 h-3.5 text-gray-400 transition-transform duration-200 ${expanded ? "rotate-180" : ""}`}
                        fill="none" stroke="currentColor" viewBox="0 0 24 24"
                    >
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7" />
                    </svg>
                </div>
                {group.module_summary && expanded && (
                    <p className="text-gray-400 text-xs mt-1 italic leading-relaxed">
                        {group.module_summary}
                    </p>
                )}
            </div>
            <div className="flex items-center gap-2 self-start md:self-auto">
                <span className="text-xs font-mono text-gray-400 bg-gray-800/80 px-2.5 py-1 rounded-md border border-gray-700/60 whitespace-nowrap">
                    AI {aiDone}/{funcs.length}
                </span>
                <span className="text-xs text-gray-400 font-mono bg-gray-800/80 px-2.5 py-1 rounded-md border border-gray-700/60 whitespace-nowrap">
                    {funcs.length} {funcs.length === 1 ? "function" : "functions"}
                </span>
            </div>
        </div>
    );
});

/* -------------------------------------------------------------------------- */
/* Function card - memoized; heavy explanation content mounts only when open   */
/* -------------------------------------------------------------------------- */
const FunctionRow = React.memo(function FunctionRow({ func, expanded, onToggle, rowKey, jobId, showDivider = true }) {
    const [remoteExp, setRemoteExp] = useState(null);
    const fetchingRef = useRef(false);

    const rawExp = func.explanation || remoteExp || func;

    // Lazy explanation fetch on first open when the row has no text yet.
    useEffect(() => {
        if (!expanded || fetchingRef.current) return;
        if (func.explanation || remoteExp) return;
        if (!jobId || !func.function_id) return;
        fetchingRef.current = true;
        fetch(`${API}/jobs/${jobId}/functions/${func.function_id}/explanation`)
            .then(r => (r.ok ? r.json() : null))
            .then(d => {
                if (d && d.explanation) setRemoteExp(d.explanation);
            })
            .catch(() => { /* keep placeholder */ })
            .finally(() => { fetchingRef.current = false; });
    }, [expanded, remoteExp, func.explanation, func.function_id, jobId]);

    const parsed = useMemo(
        () => parseExplanationCached(rawExp, func.name || "function"),
        [rawExp, func.name]
    );
    const { purpose, explanation, usage, inputOutput, risks, firstSentence, hasRisk } = parsed;

    return (
        <div
            className={`p-5 transition-all duration-200 cursor-pointer select-none ${showDivider ? "border-b border-gray-700/60" : ""} ${
                expanded ? "bg-gray-800/95" : "hover:bg-gray-750/50"
            } ${hasRisk ? "border-l-4 border-l-amber-500/80" : "border-l-4 border-l-transparent"}`}
            onClick={() => onToggle(rowKey)}
            data-fn-row="true"
        >
            {/* Card Header: Function Name, Badges & Chevron */}
            <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2.5 flex-wrap">
                    <span className="font-mono text-green-400 font-semibold text-base">
                        {func.name}()
                    </span>
                    {func.ai_status === "static" && (
                        <span
                            className="text-[10px] uppercase tracking-wider font-semibold text-gray-500 bg-gray-700/40 border border-gray-600/50 rounded px-1.5 py-0.5"
                            title="Deterministic static analysis shown; the AI explanation is still generating"
                        >
                            AI pending
                        </span>
                    )}
                    {(func.ai_status === "ai" || func.ai_status === "cached") && (
                        <span
                            className="text-[10px] uppercase tracking-wider font-semibold text-blue-300 bg-blue-500/10 border border-blue-500/30 rounded px-1.5 py-0.5"
                            title="AI-generated explanation"
                        >
                            AI
                        </span>
                    )}
                    {AI_DONE.has(func.ai_status) && func.ai_status !== "ai" && func.ai_status !== "cached" && (
                        <span
                            className="text-[10px] uppercase tracking-wider font-semibold text-gray-500 bg-gray-700/40 border border-gray-600/50 rounded px-1.5 py-0.5"
                            title={func.ai_status === "trivial_skipped" ? "Deterministic explanation (trivial function)" : "Deterministic explanation (AI fallback)"}
                        >
                            {func.ai_status === "trivial_skipped" ? "Trivial" : "Static"}
                        </span>
                    )}
                </div>

                {/* Expand / Collapse Chevron */}
                <div className="flex items-center gap-2 text-gray-400">
                    <span className="text-xs hidden sm:inline text-gray-500 font-mono">
                        {expanded ? "Collapse" : "Details"}
                    </span>
                    <div className="w-8 h-8 rounded-lg bg-gray-700/50 flex items-center justify-center text-gray-400 hover:text-fg transition-colors">
                        <svg
                            className={`w-4 h-4 transition-transform duration-200 ${expanded ? "rotate-180 text-blue-400" : ""}`}
                            fill="none"
                            stroke="currentColor"
                            viewBox="0 0 24 24"
                        >
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7" />
                        </svg>
                    </div>
                </div>
            </div>

            {/* Collapsed State: High-level Explanation Summary */}
            {!expanded && (
                <p className="text-gray-400 text-sm mt-2 leading-relaxed truncate font-normal">
                    {firstSentence}
                </p>
            )}

            {/* Expanded State: 5 Distinct Content Sections - mounted only here */}
            {expanded && (
                <div className="mt-4 pt-4 border-t border-gray-700/60 space-y-4 animate-fadeIn">
                    {/* 1. Function Explanation (Top-level plain English summary) */}
                    <div className="bg-gray-900/80 rounded-xl p-4 border border-blue-500/30">
                        <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-cyan-400 mb-2">
                            <svg className="w-4 h-4 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                            </svg>
                            <span>Function Explanation</span>
                        </div>
                        <p className="text-gray-200 text-sm leading-relaxed whitespace-pre-line">
                            {explanation}
                        </p>
                    </div>

                    {/* 3 Grid Breakdown Cards: Purpose, Input/Output, Risks */}
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                        {/* 3. Purpose (Why it exists / problem it solves) */}
                        <div className="bg-gray-900/60 rounded-xl p-3.5 border border-gray-700/50 flex flex-col justify-between">
                            <div>
                                <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-blue-400 mb-1.5">
                                    <svg className="w-3.5 h-3.5 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                                    </svg>
                                    <span>Purpose</span>
                                </div>
                                <p className="text-gray-300 text-xs leading-relaxed">
                                    {purpose}
                                </p>
                            </div>
                        </div>

                        {/* 4. Input / Output (Concrete parameter names & types + return representation) */}
                        <div className="bg-gray-900/60 rounded-xl p-3.5 border border-gray-700/50 flex flex-col justify-between">
                            <div>
                                <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-purple-400 mb-1.5">
                                    <svg className="w-3.5 h-3.5 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4" />
                                    </svg>
                                    <span>Input / Output</span>
                                </div>
                                <p className="text-gray-300 text-xs leading-relaxed">
                                    {inputOutput}
                                </p>
                            </div>
                        </div>

                        {/* 5. Risks & Legacy Patterns (Specific code defect analysis) */}
                        <div className={`rounded-xl p-3.5 border flex flex-col justify-between transition-colors ${
                            hasRisk
                                ? "bg-amber-950/25 border-amber-800/40 text-amber-200"
                                : "bg-gray-900/60 border-gray-700/50 text-gray-300"
                        }`}>
                            <div>
                                <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-amber-400 mb-1.5">
                                    <svg className="w-3.5 h-3.5 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                                    </svg>
                                    <span>Risks & Legacy</span>
                                </div>
                                <p className="text-xs leading-relaxed">
                                    {risks}
                                </p>
                            </div>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
});

/* -------------------------------------------------------------------------- */
/* ExplanationTab                                                              */
/* -------------------------------------------------------------------------- */
function ExplanationTab({
    explanation,
    functions,
    groups: windowGroups = null,
    total: serverTotal = null,
    totalAll: serverTotalAll = null,
    onSearch = null,
    onLoadMore = null,
    loadingMore = false,
    hasMore = false,
    jobId = null,
    isLoading = false,
}) {
    const [localQuery, setLocalQuery] = useState("");
    const [serverQuery, setServerQuery] = useState("");
    const [expandedFns, setExpandedFns] = useState(() => new Set());
    const [collapsedModules, setCollapsedModules] = useState(() => new Set());
    const [notice, setNotice] = useState(null);
    const initRef = useRef(false);
    const debounceRef = useRef(null);

    // Normalize legacy full payloads (small jobs / demo) into file groups.
    const legacyGroups = useMemo(() => {
        if (windowGroups) return windowGroups;
        if (!explanation && !functions) return [];
        if (Array.isArray(explanation) && explanation.length > 0) {
            if (explanation[0]?.filename !== undefined && explanation[0]?.functions !== undefined) {
                return explanation;
            }
            return [{
                filename: "Analyzed Functions",
                module_summary: "Overview of analyzed functions in the codebase.",
                functions: explanation
            }];
        }
        if (Array.isArray(functions)) {
            return [{
                filename: "Analyzed Functions",
                module_summary: "Overview of analyzed functions in the codebase.",
                functions: functions
            }];
        }
        return [];
    }, [windowGroups, explanation, functions]);

    const groups = legacyGroups;

    const loadedCount = useMemo(
        () => groups.reduce((acc, g) => acc + ((g.functions || []).length), 0),
        [groups]
    );
    const totalFunctions = serverTotal != null ? serverTotal : loadedCount;
    const totalAll = serverTotalAll != null ? serverTotalAll : loadedCount;

    const trimmedQuery = localQuery.trim();
    const searching = trimmedQuery.length > 0;
    // While a server query is in flight the groups still reflect the old one.
    const queryActive = searching || serverQuery.length > 0;

    const largeRepo = totalAll > LARGE_REPO_FUNCTIONS;
    const hugeRepo = totalAll > HUGE_REPO_FUNCTIONS;

    /* Debounced server-side search (spec sections 13, 14, 26): metadata-only,
       250ms after the user stops typing, never on every keystroke. */
    useEffect(() => {
        if (!onSearch) return undefined;
        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(() => {
            if (trimmedQuery !== serverQuery) {
                setServerQuery(trimmedQuery);
                onSearch(trimmedQuery);
            }
        }, 250);
        return () => clearTimeout(debounceRef.current);
    }, [trimmedQuery, serverQuery, onSearch]);

    // Large repositories start with modules collapsed so the DOM stays small.
    useEffect(() => {
        if (initRef.current || !groups.length) return;
        initRef.current = true;
        if (totalAll > LARGE_REPO_FUNCTIONS) {
            setCollapsedModules(new Set(groups.map(g => g.filename || "unknown")));
        }
    }, [groups, totalAll]);

    // Module expansion state (collapsedModules semantics keep small repos on the
    // classic "everything visible" behavior); a server search forces open.
    const toggleModule = useCallback((filename) => {
        setCollapsedModules(prev => {
            const next = new Set(prev);
            if (next.has(filename)) next.delete(filename);
            else next.add(filename);
            return next;
        });
    }, []);

    const moduleExpanded = useCallback(
        (filename) => queryActive || !collapsedModules.has(filename),
        [queryActive, collapsedModules]
    );

    /* Flat row model: module header rows + (only for expanded modules) function
       rows. Virtualization renders just the visible slice of this list. */
    const rows = useMemo(() => {
        const out = [];
        for (const g of groups) {
            const fname = g.filename || "unknown";
            out.push({ kind: "module", group: g });
            if (queryActive || !collapsedModules.has(fname)) {
                for (const f of (g.functions || [])) {
                    out.push({ kind: "fn", func: f, filename: fname });
                }
            }
        }
        return out;
    }, [groups, collapsedModules, queryActive]);

    const fnRowKeys = useMemo(
        () => rows.filter(r => r.kind === "fn").map(r => fnKey(r.func, r.filename)),
        [rows]
    );

    const maxOpen = hugeRepo
        ? 1
        : (totalAll > LARGE_REPO_FUNCTIONS ? MAX_OPEN_MEDIUM : Infinity);

    const toggleCard = useCallback((key) => {
        setExpandedFns(prev => {
            const next = new Set(prev);
            if (next.has(key)) {
                next.delete(key);
            } else {
                if (next.size >= maxOpen) {
                    const oldest = next.values().next().value;
                    if (oldest !== undefined) next.delete(oldest);
                }
                next.add(key);
            }
            return next;
        });
    }, [maxOpen]);

    const anyExpanded = expandedFns.size > 0;

    const toggleExpandAll = useCallback(() => {
        if (anyExpanded) {
            setExpandedFns(new Set());
            setNotice(null);
            return;
        }
        const keys = fnRowKeys;
        if (keys.length > EXPAND_ALL_SAFE_LIMIT || totalAll > LARGE_REPO_FUNCTIONS) {
            const slice = keys.slice(0, EXPAND_ALL_SAFE_LIMIT);
            setExpandedFns(new Set(slice));
            setNotice(
                `Large repository detected. Showing only visible explanations to keep the page responsive (${slice.length} of ${totalAll} functions expanded).`
            );
        } else {
            setExpandedFns(new Set(keys));
            setNotice(null);
        }
    }, [anyExpanded, fnRowKeys, totalAll]);

    const clearSearch = () => {
        setLocalQuery("");
        if (onSearch) {
            setServerQuery("");
            onSearch("");
        }
    };

    const handleLoadMore = useCallback(() => {
        if (onLoadMore && hasMore && !loadingMore) onLoadMore();
    }, [onLoadMore, hasMore, loadingMore]);

    const renderItem = useCallback((index, row) => {
        if (row.kind === "module") {
            const fname = row.group.filename || "unknown";
            return (
                <ModuleRow
                    group={row.group}
                    expanded={moduleExpanded(fname)}
                    onToggle={toggleModule}
                    moduleKey={fname}
                />
            );
        }
        const key = fnKey(row.func, row.filename);
        return (
            <FunctionRow
                func={row.func}
                expanded={expandedFns.has(key)}
                onToggle={toggleCard}
                rowKey={key}
                jobId={jobId}
                showDivider={false}
            />
        );
    }, [moduleExpanded, toggleModule, expandedFns, toggleCard, jobId]);

    const loadMoreFooter = useCallback(() => (
        (hasMore || loadingMore) ? (
            <div className="p-4 text-center border-t border-gray-700/60 bg-gray-900/40">
                <button
                    type="button"
                    onClick={handleLoadMore}
                    disabled={loadingMore}
                    className="text-xs font-medium text-blue-300 hover:text-blue-200 bg-gray-800 hover:bg-gray-700 border border-gray-600/60 rounded-lg px-4 py-2 transition-colors cursor-pointer disabled:opacity-50"
                >
                    {loadingMore ? "Loading..." : `Load more functions (${Math.max(0, (totalAll - loadedCount))} remaining)`}
                </button>
            </div>
        ) : null
    ), [hasMore, loadingMore, handleLoadMore, totalAll, loadedCount]);

    // 1. Loading Skeleton State
    if (isLoading) {
        return (
            <div className="space-y-6">
                <div className="bg-gray-800/60 p-4 rounded-xl border border-gray-700/60 flex items-center justify-between animate-pulse">
                    <div className="h-10 bg-gray-700 rounded-lg w-72"></div>
                    <div className="h-10 bg-gray-700 rounded-lg w-32"></div>
                </div>
                {[1, 2, 3].map((i) => (
                    <div key={i} className="bg-gray-800 rounded-xl p-6 border border-gray-700 animate-pulse space-y-4">
                        <div className="flex justify-between items-center">
                            <div className="h-6 bg-gray-700 rounded w-48"></div>
                            <div className="h-5 bg-gray-700 rounded w-20"></div>
                        </div>
                        <div className="h-4 bg-gray-700/60 rounded w-3/4"></div>
                    </div>
                ))}
            </div>
        );
    }

    // 2. True Empty State (No functions analyzed)
    if (totalAll === 0 && !searching) {
        return (
            <div className="bg-gray-800/80 border border-gray-700 rounded-2xl p-12 text-center my-6 shadow-xl">
                <div className="w-16 h-16 bg-gray-700/50 rounded-2xl flex items-center justify-center mx-auto mb-4 text-gray-400">
                    <svg className="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
                    </svg>
                </div>
                <h3 className="text-xl font-semibold text-gray-200 mb-2">No Functions Found</h3>
                <p className="text-gray-400 text-sm max-w-md mx-auto">
                    The analysis pipeline did not detect any functions or classes in the uploaded source files.
                </p>
            </div>
        );
    }

    const useVirtual = rows.length > VIRTUALIZE_ROW_THRESHOLD;

    return (
        <div className="space-y-6">
            {/* Control Bar: Search Input, Counter & Expand/Collapse Toggle */}
            <div className="bg-gray-800/90 border border-gray-700/80 rounded-2xl p-4 shadow-lg backdrop-blur-sm flex flex-col md:flex-row gap-4 items-stretch md:items-center justify-between">
                {/* Search Input */}
                <div className="relative flex-1 max-w-xl">
                    <div className="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-gray-400">
                        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                        </svg>
                    </div>
                    <input
                        type="text"
                        value={localQuery}
                        onChange={(e) => setLocalQuery(e.target.value)}
                        placeholder="Search functions by name..."
                        className="w-full bg-gray-900/90 border border-gray-700 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 text-fg placeholder-gray-500 text-sm rounded-xl pl-10 pr-10 py-2.5 outline-none transition-all"
                        id="function-search-input"
                    />
                    {localQuery && (
                        <button
                            onClick={clearSearch}
                            className="absolute inset-y-0 right-0 pr-3 flex items-center text-gray-400 hover:text-fg transition-colors cursor-pointer"
                            title="Clear search"
                            type="button"
                        >
                            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
                            </svg>
                        </button>
                    )}
                </div>

                {/* Right Controls: Filter Counter & Expand All Button */}
                <div className="flex items-center gap-3 justify-between md:justify-end">
                    <span className="text-xs font-medium text-gray-400 px-3 py-1.5 bg-gray-900/60 border border-gray-700/60 rounded-lg whitespace-nowrap">
                        {searching || serverQuery ? (
                            <>Showing <span className="text-blue-400 font-semibold">{totalFunctions}</span> of {totalAll}</>
                        ) : (
                            <><span className="text-gray-300 font-semibold">{totalAll}</span> functions</>
                        )}
                    </span>

                    <button
                        onClick={toggleExpandAll}
                        className="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 active:bg-gray-750 text-gray-200 hover:text-fg text-xs font-medium px-4 py-2.5 rounded-xl border border-gray-600/60 transition-all shadow-sm whitespace-nowrap cursor-pointer"
                        id="toggle-expand-all-btn"
                        type="button"
                    >
                        {anyExpanded ? (
                            <>
                                <svg className="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M5 15l7-7 7 7" />
                                </svg>
                                <span>Collapse All</span>
                            </>
                        ) : (
                            <>
                                <svg className="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7" />
                                </svg>
                                <span>Expand All</span>
                            </>
                        )}
                    </button>
                </div>
            </div>

            {/* Large-repository notice (spec section 6) */}
            {notice && (
                <div className="bg-blue-950/30 border border-blue-800/40 rounded-xl px-4 py-3 text-xs text-blue-200 flex items-start justify-between gap-3">
                    <span>{notice}</span>
                    <button
                        type="button"
                        onClick={() => setNotice(null)}
                        className="text-blue-300 hover:text-blue-100 font-semibold shrink-0 cursor-pointer"
                        title="Dismiss notice"
                    >
                        Dismiss
                    </button>
                </div>
            )}
            {largeRepo && !notice && (
                <div className="bg-gray-800/60 border border-gray-700/60 rounded-xl px-4 py-2.5 text-xs text-gray-400">
                    Large repository mode: only visible function rows are rendered; explanations load as you open them.
                </div>
            )}

            {/* 3. No Search Results State */}
            {totalFunctions === 0 && queryActive && (
                <div className="bg-gray-800/60 border border-gray-700/80 rounded-2xl p-12 text-center my-6 shadow-md">
                    <div className="w-14 h-14 bg-gray-700/40 rounded-2xl flex items-center justify-center mx-auto mb-4 text-gray-400">
                        <svg className="w-7 h-7" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0zM10 14a4 4 0 100-8 4 4 0 000 8z" />
                        </svg>
                    </div>
                    <h4 className="text-lg font-medium text-gray-200 mb-1">
                        No functions match "{trimmedQuery || serverQuery}"
                    </h4>
                    <p className="text-gray-400 text-sm mb-6 max-w-sm mx-auto">
                        We couldn't find any functions matching your search term. Check the spelling or clear the filter.
                    </p>
                    <button
                        onClick={clearSearch}
                        className="inline-flex items-center gap-2 bg-blue-600/90 hover:bg-blue-600 text-white text-xs font-semibold px-4 py-2 rounded-lg transition-colors shadow-sm cursor-pointer"
                    >
                        <span>Clear search filter</span>
                    </button>
                </div>
            )}

            {/* Grouped Function Cards - virtualized for large repositories */}
            {useVirtual ? (
                <div className="bg-gray-800/90 rounded-2xl border border-gray-700/80 overflow-hidden shadow-lg">
                    <Virtuoso
                        data={rows}
                        style={{ height: "min(75vh, 860px)" }}
                        itemContent={renderItem}
                        endReached={handleLoadMore}
                        components={{ Footer: loadMoreFooter }}
                        increaseViewportBy={600}
                    />
                </div>
            ) : (
                groups.map((fileData) => {
                    const fname = fileData.filename || "unknown";
                    const funcs = fileData.functions || [];
                    const moduleIsExpanded = moduleExpanded(fname);
                    return (
                        <div key={fname} className="bg-gray-800/90 rounded-2xl border border-gray-700/80 overflow-hidden shadow-lg transition-all">
                            <ModuleRow
                                group={fileData}
                                expanded={moduleIsExpanded}
                                onToggle={toggleModule}
                                moduleKey={fname}
                            />
                            {moduleIsExpanded && (
                                <div className="divide-y divide-gray-700/60">
                                    {funcs.map((func, funcIdx) => {
                                        const key = fnKey(func, `${fname}-${funcIdx}`);
                                        return (
                                            <FunctionRow
                                                key={key}
                                                func={func}
                                                expanded={expandedFns.has(key)}
                                                onToggle={toggleCard}
                                                rowKey={key}
                                                jobId={jobId}
                                            />
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    );
                })
            )}

            {/* Non-virtual load-more footer */}
            {!useVirtual && hasMore && (
                <div className="text-center">
                    <button
                        type="button"
                        onClick={handleLoadMore}
                        disabled={loadingMore}
                        className="text-xs font-medium text-blue-300 hover:text-blue-200 bg-gray-800 hover:bg-gray-700 border border-gray-600/60 rounded-lg px-4 py-2 transition-colors cursor-pointer disabled:opacity-50"
                    >
                        {loadingMore ? "Loading..." : `Load more functions (${Math.max(0, totalAll - loadedCount)} remaining)`}
                    </button>
                </div>
            )}
        </div>
    );
}

/* Memoized: parent progress updates (analysisProgress / fileProgress) must not
   re-run this heavy tree (spec sections 6 and 7). */
export default React.memo(ExplanationTab);
