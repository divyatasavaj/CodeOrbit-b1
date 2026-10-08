import React, { useState, useMemo, useCallback } from "react";
import FunctionPicker from "./FunctionPicker.jsx";
import { API } from "../api.js";

/**
 * Hard numeric threshold for code coverage compliance.
 * Mirrors the backend minimum (CODEORACLE_MIN_TEST_COVERAGE, default 65%) and
 * must use the exact >= boundary across all bars, borders, and summary counts.
 * The percentage shown is always the real measured coverage from the backend -
 * it is never rounded up or synthesized here.
 */
export const COVERAGE_THRESHOLD = 65;

/**
 * Spec sections 32/34/35: the backend reports an explicit status for every
 * generated suite; badges are driven by it (with a derivation fallback for
 * entries stored before the field existed).
 */
export const TEST_STATUS_META = {
    SUCCESS: {
        label: "Success",
        icon: "✓",
        badgeClass: "bg-emerald-950/90 border-emerald-700 text-emerald-300",
        tip: "Tests executed, passed, and reached the minimum measured coverage.",
    },
    TARGET_NOT_REACHED: {
        label: "Target not reached",
        icon: "◑",
        badgeClass: "bg-amber-950/80 border-amber-700 text-amber-300",
        tip: "Tests ran and passed, but measured coverage stayed below the target (best real result).",
    },
    TEST_FAILURE: {
        label: "Tests failed",
        icon: "✗",
        badgeClass: "bg-rose-950/90 border-rose-700 text-rose-300",
        tip: "Tests executed but one or more failed.",
    },
    EXECUTION_FAILURE: {
        label: "Execution failed",
        icon: "⚠",
        badgeClass: "bg-rose-950/90 border-rose-700 text-rose-300",
        tip: "Test execution could not be completed (nothing was executed).",
    },
    GENERATION_FAILURE: {
        label: "Generation failed",
        icon: "✗",
        badgeClass: "bg-rose-950/90 border-rose-700 text-rose-300",
        tip: "No usable test suite could be generated for this function.",
    },
};

export function deriveTestStatus(item) {
    if (item.status) return item.status;
    const tr = item.test_results || {};
    const executed = Number(item.executed) ||
        (Number(tr.passed || 0) + Number(tr.failed || 0) + Number(tr.errors || 0));
    const passed = Boolean(item.passed);
    const target = Number(item.coverage_target ?? item.min_coverage ?? COVERAGE_THRESHOLD) || COVERAGE_THRESHOLD;
    const cov = Number(item.coverage_percent);
    const hasCov = typeof item.coverage_percent === "number" && !isNaN(item.coverage_percent);
    if (!item.test_code) return "GENERATION_FAILURE";
    if (executed > 0 && !passed) return "TEST_FAILURE";
    if (executed > 0 && passed) return hasCov && cov >= target ? "SUCCESS" : "TARGET_NOT_REACHED";
    return passed ? (hasCov && cov >= target ? "SUCCESS" : "TARGET_NOT_REACHED") : "TEST_FAILURE";
}

/**
 * Validates if a coverage percentage value is numeric and valid.
 * @param {any} value
 * @returns {boolean}
 */
export function isCoverageValid(value) {
    return typeof value === "number" && !isNaN(value) && value >= 0;
}

/**
 * Centralized threshold check used uniformly across summary line,
 * per-function coverage bar colors, and card left borders.
 * @param {any} coveragePercent
 * @returns {boolean}
 */
export function meetsCoverageThreshold(coveragePercent) {
    return isCoverageValid(coveragePercent) && coveragePercent >= COVERAGE_THRESHOLD;
}

/**
 * Robust clipboard copy helper with fallback for restricted/iframe environments.
 * @param {string} text
 * @returns {Promise<boolean>}
 */
export async function copyToClipboard(text) {
    if (!text) return false;
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (err) {
        console.warn("Clipboard API failed, using fallback textarea:", err);
    }

    try {
        const textArea = document.createElement("textarea");
        textArea.value = text;
        textArea.style.position = "fixed";
        textArea.style.left = "-999999px";
        textArea.style.top = "-999999px";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        const successful = document.execCommand("copy");
        document.body.removeChild(textArea);
        return successful;
    } catch (err) {
        console.error("Fallback copy failed:", err);
        return false;
    }
}

/**
 * Copy button component with instant visual feedback and error guard.
 */
function CopyButton({ code }) {
    const [copied, setCopied] = useState(false);
    const [copyFailed, setCopyFailed] = useState(false);

    const handleCopy = useCallback(async () => {
        if (!code) return;
        const success = await copyToClipboard(code);
        if (success) {
            setCopied(true);
            setCopyFailed(false);
            setTimeout(() => setCopied(false), 1500);
        } else {
            setCopyFailed(true);
            setTimeout(() => setCopyFailed(false), 2000);
        }
    }, [code]);

    return (
        <button
            onClick={handleCopy}
            type="button"
            className={`text-xs px-3 py-1.5 rounded-lg border font-medium transition-all duration-150 flex items-center gap-1.5 cursor-pointer ${
                copied
                    ? "bg-emerald-950/90 border-emerald-500 text-emerald-300 shadow-sm shadow-emerald-950"
                    : copyFailed
                    ? "bg-rose-950/90 border-rose-500 text-rose-300"
                    : "bg-gray-800 hover:bg-gray-700 border-gray-700 text-gray-300 hover:text-fg"
            }`}
            title="Copy test code to clipboard"
        >
            {copied ? (
                <>
                    <svg className="w-3.5 h-3.5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M5 13l4 4L19 7" />
                    </svg>
                    <span>Copied!</span>
                </>
            ) : copyFailed ? (
                <>
                    <svg className="w-3.5 h-3.5 text-rose-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                    <span>Copy Failed</span>
                </>
            ) : (
                <>
                    <svg className="w-3.5 h-3.5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-1M8 5a2 2 0 002 2h2a2 2 0 002-2M8 5a2 2 0 012-2h2a2 2 0 012 2m0 0h2a2 2 0 012 2v3m2 4H10m0 0l3-3m-3 3l3 3" />
                    </svg>
                    <span>Copy</span>
                </>
            )}
        </button>
    );
}

/**
 * Generated Tests Tab Component
 * 
 * Displays per-function unit tests, exact executed code coverage metrics,
 * pass/fail execution status, and compliance summary with the configured threshold.
 * 
 * @param {Object} props
 * @param {Array<Object>} [props.tests] - Array of test result objects from /results/{job_id}
 * @param {boolean} [props.isLoading=false] - Loading indicator for initial data retrieval
 * @param {Array<Object>} [props.explanation] - File groups used by the on-demand picker
 * @param {string|null} [props.jobId] - Job id for the on-demand generation endpoint
 * @param {Function} [props.onUpdate] - Functional updater to merge generated entries into results
 */
function TestsTab({ tests, isLoading = false, explanation, jobId, onUpdate }) {
    const [searchQuery, setSearchQuery] = useState("");
    const [filterMode, setFilterMode] = useState("all"); // 'all' | 'meets' | 'below' | 'passed' | 'failed'
    const [expandedOutputs, setExpandedOutputs] = useState({});
    const [generating, setGenerating] = useState(null); // function name in flight
    const [genError, setGenError] = useState("");
    const [pickerOpen, setPickerOpen] = useState(false);

    const handleGenerate = async (functionName, filename) => {
        if (!jobId) {
            setGenError("Analysis job ID is missing — please run a new analysis.");
            return;
        }
        setGenerating(functionName);
        setGenError("");
        try {
            const res = await fetch(`${API}/generate/tests/${jobId}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ function_name: functionName, filename })
            });
            if (!res.ok) {
                let detail = `HTTP ${res.status}`;
                try { detail = (await res.json()).detail || detail; } catch (e) {}
                throw new Error(detail);
            }
            const entry = await res.json();
            if (onUpdate) {
                onUpdate(prev => ({
                    ...prev,
                    tests: [...(prev.tests || []).filter(t => t.name !== entry.name), entry]
                }));
            }
        } catch (err) {
            setGenError(err.message || "Test generation failed. Please try again.");
        } finally {
            setGenerating(null);
        }
    };

    // 1. Normalize test items to handle variations in API naming contracts safely
    const normalizedTests = useMemo(() => {
        if (!Array.isArray(tests)) return [];
        return tests.map((item, idx) => {
            const rawName = item.name || item.functionName || item.function_name || item.display_name || `function_${idx + 1}`;
            const rawCov = item.coverage_percent ?? item.coveragePercent ?? item.coverage ?? null;
            const validCov = isCoverageValid(rawCov) ? Number(rawCov) : null;
            const passed = Boolean(item.passed ?? item.isPassed ?? false);
            const testCode = item.test_code ?? item.testCode ?? item.code ?? "";
            const testOutput = item.test_output ?? item.testOutput ?? item.output ?? "";
            const error = item.error ?? null;
            // Coverage-gate metadata from the backend's generate/measure/improve loop.
            const meetsMinCoverage = item.meets_min_coverage ?? item.meetsMinCoverage ?? null;
            const coverageAttempts = Number(
                item.attempts ?? item.coverage_attempts ?? item.coverageAttempts ?? 1
            ) || 1;
            // Backend is the source of truth for the target; fall back to the
            // mirrored constant for entries generated before the field existed.
            const coverageTarget = Number(item.coverage_target ?? item.min_coverage ?? COVERAGE_THRESHOLD) || COVERAGE_THRESHOLD;
            const targetMet = meetsMinCoverage !== null
                ? meetsMinCoverage === true
                : (validCov !== null ? validCov >= coverageTarget : null);
            // Spec section 34: coverage is scoped to the FUNCTION's own lines
            // (FILE only when the function range could not be mapped).
            const coverageScope = item.coverage_scope ?? item.coverageScope ?? null;
            const rawFileCov = item.file_coverage_percent ?? item.fileCoveragePercent ?? null;
            const fileCoverage = isCoverageValid(rawFileCov) ? Number(rawFileCov) : null;
            const failureCategory = item.failure_category ?? item.failureCategory ?? null;
            const status = deriveTestStatus(item);

            return {
                id: `test-item-${idx}-${rawName}`,
                name: rawName,
                coverage: validCov,
                hasValidCoverage: validCov !== null,
                meetsThreshold: validCov !== null ? meetsCoverageThreshold(validCov) : false,
                passed,
                testCode,
                testOutput,
                error,
                meetsMinCoverage,
                coverageTarget,
                targetMet,
                attempts: coverageAttempts,
                status,
                coverageScope,
                fileCoverage,
                failureCategory,
                // Reported as a best effort (never as a success) when the
                // MEASURED coverage stayed below the target.
                bestEffort: targetMet === false
            };
        });
    }, [tests]);

    // Names already generated (for picker "Generated"/"Regenerate" states)
    const generatedNames = useMemo(() => new Set(normalizedTests.map(t => t.name)), [normalizedTests]);

    const errorBanner = genError ? (
        <div className="flex items-start gap-2.5 bg-rose-950/60 border border-rose-800/80 rounded-xl px-4 py-3 text-sm text-rose-300">
            <svg className="w-4 h-4 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <span className="flex-1">{genError}</span>
            <button type="button" onClick={() => setGenError("")} className="text-rose-400 hover:text-rose-200 font-bold leading-none" title="Dismiss">✕</button>
        </div>
    ) : null;

    // 2. Compute Summary Metrics
    const totalCount = normalizedTests.length;
    const meetsThresholdCount = useMemo(() => {
        return normalizedTests.filter(t => t.meetsThreshold).length;
    }, [normalizedTests]);

    const passedCount = useMemo(() => {
        return normalizedTests.filter(t => t.passed).length;
    }, [normalizedTests]);

    const averageCoverage = useMemo(() => {
        const withCov = normalizedTests.filter(t => t.hasValidCoverage);
        if (withCov.length === 0) return 0;
        const sum = withCov.reduce((acc, t) => acc + (t.coverage || 0), 0);
        return Math.round((sum / withCov.length) * 10) / 10;
    }, [normalizedTests]);

    const thresholdMeetPercent = totalCount > 0 ? Math.round((meetsThresholdCount / totalCount) * 100) : 0;

    // 3. Filter and Search
    const filteredTests = useMemo(() => {
        const q = searchQuery.trim().toLowerCase();
        return normalizedTests.filter(item => {
            const matchesSearch = !q || item.name.toLowerCase().includes(q) || item.testCode.toLowerCase().includes(q);
            if (!matchesSearch) return false;

            if (filterMode === "meets") return item.meetsThreshold;
            if (filterMode === "below") return item.hasValidCoverage && !item.meetsThreshold;
            if (filterMode === "passed") return item.passed;
            if (filterMode === "failed") return !item.passed;
            return true;
        });
    }, [normalizedTests, searchQuery, filterMode]);

    const toggleOutput = useCallback((id) => {
        setExpandedOutputs(prev => ({
            ...prev,
            [id]: !prev[id]
        }));
    }, []);

    // -------------------------------------------------------------
    // RENDER: Loading Skeleton State
    // -------------------------------------------------------------
    if (isLoading) {
        return (
            <div className="space-y-4 animate-pulse">
                {/* Summary Skeleton */}
                <div className="bg-gray-800/80 rounded-2xl p-6 border border-gray-700/60 flex flex-col md:flex-row items-center justify-between gap-4">
                    <div className="space-y-2 w-full md:w-1/2">
                        <div className="h-6 bg-gray-700 rounded-lg w-3/4"></div>
                        <div className="h-4 bg-gray-700/60 rounded-lg w-1/2"></div>
                    </div>
                    <div className="flex gap-3 w-full md:w-auto">
                        <div className="h-10 w-28 bg-gray-700 rounded-xl"></div>
                        <div className="h-10 w-28 bg-gray-700 rounded-xl"></div>
                    </div>
                </div>

                {/* Function Cards Skeletons */}
                {[1, 2, 3].map(i => (
                    <div key={i} className="bg-gray-800/80 rounded-2xl p-6 border border-gray-700/60 space-y-4">
                        <div className="flex justify-between items-center">
                            <div className="h-5 bg-gray-700 rounded w-44"></div>
                            <div className="flex gap-2">
                                <div className="h-6 w-24 bg-gray-700 rounded-lg"></div>
                                <div className="h-6 w-28 bg-gray-700 rounded-lg"></div>
                            </div>
                        </div>
                        <div className="h-2.5 bg-gray-700/60 rounded-full w-full"></div>
                        <div className="h-32 bg-gray-900 rounded-xl"></div>
                    </div>
                ))}
            </div>
        );
    }

    // -------------------------------------------------------------
    // RENDER: Empty State — on-demand picker (tests are generated per function)
    // -------------------------------------------------------------
    if (totalCount === 0) {
        return (
            <div className="space-y-4">
                {errorBanner}
                <FunctionPicker
                    explanation={explanation}
                    existingNames={generatedNames}
                    generatingName={generating}
                    generateLabel="Generate Tests"
                    title="Generate Tests On Demand"
                    description="The initial analysis produces explanations and the dependency graph only — skipping test generation to save time. Pick any function below to generate its unit test suite with real coverage (one AI call per function, a few seconds)."
                    jobId={jobId}
                    onGenerate={handleGenerate}
                />
            </div>
        );
    }

    // -------------------------------------------------------------
    // RENDER: Active Tests View
    // -------------------------------------------------------------
    return (
        <div className="space-y-6">
            {errorBanner}

            {/* On-demand generation: add tests for another function */}
            {pickerOpen ? (
                <div className="space-y-2">
                    <FunctionPicker
                        explanation={explanation}
                        existingNames={generatedNames}
                        generatingName={generating}
                        generateLabel="Generate Tests"
                        title="Generate Tests For Another Function"
                        description="One AI call per function. Results appear in the list below as soon as coverage finishes running."
                        jobId={jobId}
                        onGenerate={handleGenerate}
                    />
                    <div className="flex justify-end">
                        <button
                            type="button"
                            onClick={() => setPickerOpen(false)}
                            className="text-xs text-gray-400 hover:text-fg px-3 py-1.5 rounded-lg border border-gray-700 hover:border-gray-500 transition-colors cursor-pointer"
                        >
                            Hide Picker
                        </button>
                    </div>
                </div>
            ) : (
                <button
                    type="button"
                    onClick={() => setPickerOpen(true)}
                    className="w-full py-3 rounded-xl border border-dashed border-gray-700 hover:border-blue-500/60 hover:bg-blue-500/5 text-sm text-gray-400 hover:text-blue-300 font-medium transition-all duration-150 cursor-pointer flex items-center justify-center gap-2"
                >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 4v16m8-8H4" />
                    </svg>
                    Generate tests for another function
                </button>
            )}
            {/* 1. Summary Header Card */}
            <div className="bg-gray-800/90 border border-gray-700/80 rounded-2xl p-6 shadow-xl backdrop-blur-sm space-y-5">
                <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                    {/* Primary requirement: "X of Y functions meet the coverage threshold" */}
                    <div>
                        <div className="flex items-center gap-3">
                            <h3 className="text-lg md:text-xl font-bold text-fg tracking-tight">
                                <span className={meetsThresholdCount === totalCount ? "text-emerald-400" : meetsThresholdCount > 0 ? "text-blue-400" : "text-rose-400"}>
                                    {meetsThresholdCount} of {totalCount}
                                </span>{" "}
                                functions meet the {COVERAGE_THRESHOLD}% threshold
                            </h3>
                            <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold border ${
                                meetsThresholdCount === totalCount
                                    ? "bg-emerald-950/80 border-emerald-700 text-emerald-300"
                                    : meetsThresholdCount > 0
                                    ? "bg-blue-950/80 border-blue-700 text-blue-300"
                                    : "bg-rose-950/80 border-rose-700 text-rose-300"
                            }`}>
                                {thresholdMeetPercent}% Compliance
                            </span>
                        </div>
                        <p className="text-xs text-gray-400 mt-1">
                            Real line coverage computed via dynamic unit test execution against analyzed source modules.
                        </p>
                    </div>

                    {/* Quick Stats Badges */}
                    <div className="flex flex-wrap items-center gap-3 text-xs">
                        <div className="px-3.5 py-2 bg-gray-900/80 border border-gray-700/80 rounded-xl flex items-center gap-2">
                            <span className="text-gray-400">Avg Coverage:</span>
                            <span className={`font-mono font-bold ${
                                averageCoverage >= COVERAGE_THRESHOLD ? "text-emerald-400" : "text-rose-400"
                            }`}>
                                {averageCoverage}%
                            </span>
                        </div>
                        <div className="px-3.5 py-2 bg-gray-900/80 border border-gray-700/80 rounded-xl flex items-center gap-2">
                            <span className="text-gray-400">Execution Status:</span>
                            <span className="font-mono font-semibold text-emerald-400">{passedCount} Passed</span>
                            {totalCount - passedCount > 0 && (
                                <span className="font-mono font-semibold text-rose-400">/ {totalCount - passedCount} Failed</span>
                            )}
                        </div>
                    </div>
                </div>

                {/* Overall Compliance Progress Bar */}
                <div className="space-y-1.5">
                    <div className="flex justify-between text-xs text-gray-400 font-medium">
                        <span>Threshold Compliance ({COVERAGE_THRESHOLD}% Line Coverage Goal)</span>
                        <span>{meetsThresholdCount}/{totalCount} Functions ({thresholdMeetPercent}%)</span>
                    </div>
                    <div className="w-full bg-gray-900 rounded-full h-2.5 overflow-hidden border border-gray-700/50">
                        <div
                            className={`h-full transition-all duration-500 rounded-full ${
                                meetsThresholdCount === totalCount
                                    ? "bg-emerald-500"
                                    : meetsThresholdCount > 0
                                    ? "bg-gradient-to-r from-blue-500 to-emerald-500"
                                    : "bg-rose-500"
                            }`}
                            style={{ width: `${Math.min(100, Math.max(0, thresholdMeetPercent))}%` }}
                        />
                    </div>
                </div>

                {/* Search & Filter Controls */}
                <div className="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 pt-3 border-t border-gray-700/60">
                    <div className="relative flex-1 max-w-md">
                        <input
                            type="text"
                            placeholder="Filter by function name or code..."
                            value={searchQuery}
                            onChange={(e) => setSearchQuery(e.target.value)}
                            className="w-full bg-gray-900 border border-gray-700 rounded-xl px-3.5 py-2 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-blue-500"
                        />
                        {searchQuery && (
                            <button
                                onClick={() => setSearchQuery("")}
                                className="absolute right-3 top-2.5 text-xs text-gray-400 hover:text-fg"
                                type="button"
                            >
                                ✕
                            </button>
                        )}
                    </div>

                    <div className="flex flex-wrap items-center gap-1.5 text-xs">
                        <button
                            onClick={() => setFilterMode("all")}
                            className={`px-3 py-1.5 rounded-lg border transition-colors cursor-pointer ${
                                filterMode === "all"
                                    ? "bg-blue-600 border-blue-500 text-white font-medium"
                                    : "bg-gray-800 border-gray-700 text-gray-400 hover:text-gray-200"
                            }`}
                            type="button"
                        >
                            All ({totalCount})
                        </button>
                        <button
                            onClick={() => setFilterMode("meets")}
                            className={`px-3 py-1.5 rounded-lg border transition-colors cursor-pointer ${
                                filterMode === "meets"
                                    ? "bg-emerald-950 border-emerald-600 text-emerald-300 font-medium"
                                    : "bg-gray-800 border-gray-700 text-gray-400 hover:text-emerald-300"
                            }`}
                            type="button"
                        >
                            ≥ {COVERAGE_THRESHOLD}% ({meetsThresholdCount})
                        </button>
                        <button
                            onClick={() => setFilterMode("below")}
                            className={`px-3 py-1.5 rounded-lg border transition-colors cursor-pointer ${
                                filterMode === "below"
                                    ? "bg-rose-950 border-rose-600 text-rose-300 font-medium"
                                    : "bg-gray-800 border-gray-700 text-gray-400 hover:text-rose-300"
                            }`}
                            type="button"
                        >
                            &lt; {COVERAGE_THRESHOLD}% ({totalCount - meetsThresholdCount})
                        </button>
                    </div>
                </div>
            </div>

            {/* 2. Per-Function Test List */}
            <div className="space-y-4">
                {filteredTests.map((item) => {
                    const meets = item.meetsThreshold;
                    const hasCov = item.hasValidCoverage;
                    const coverageValue = item.coverage;

                    // Hard requirement: red left border when coverage is below the threshold
                    const borderStyle = hasCov
                        ? (meets ? "border-l-4 border-l-emerald-500" : "border-l-4 border-l-rose-500")
                        : "border-l-4 border-l-gray-600";

                    return (
                        <div
                            key={item.id}
                            className={`bg-gray-800/90 rounded-2xl p-6 border border-gray-700/80 shadow-lg transition-all duration-150 space-y-4 ${borderStyle}`}
                        >
                            {/* Card Header: Function Name + Numeric Coverage + Pass/Fail Badges */}
                            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                                <div className="flex items-center gap-2">
                                    <span className="font-mono text-emerald-400 font-bold text-base tracking-tight">
                                        {item.name}()
                                    </span>
                                </div>

                                <div className="flex flex-wrap items-center gap-2.5">
                                    {/* 0. Explicit status badge (spec section 35) */}
                                    {TEST_STATUS_META[item.status] && (
                                        <div
                                            className={`px-3 py-1 rounded-xl text-xs font-bold border flex items-center gap-1.5 ${TEST_STATUS_META[item.status].badgeClass}`}
                                            title={item.failureCategory
                                                ? `${TEST_STATUS_META[item.status].tip} (${item.failureCategory})`
                                                : TEST_STATUS_META[item.status].tip}
                                        >
                                            <span>{TEST_STATUS_META[item.status].icon}</span>
                                            <span>{TEST_STATUS_META[item.status].label}</span>
                                        </div>
                                    )}

                                    {/* 1. Coverage percentage number badge */}
                                    <div
                                        className={`px-3 py-1 rounded-xl text-xs font-bold border flex items-center gap-1.5 ${
                                            !hasCov
                                                ? "bg-gray-900 border-gray-700 text-gray-400"
                                                : meets
                                                ? "bg-emerald-950/90 border-emerald-700 text-emerald-300"
                                                : "bg-rose-950/90 border-rose-700 text-rose-300"
                                        }`}
                                        title={hasCov
                                            ? `${coverageValue}% real line coverage` +
                                              (item.coverageScope === "FUNCTION"
                                                  ? " of this function's own lines" +
                                                    (item.fileCoverage !== null ? ` (file: ${item.fileCoverage}%)` : "")
                                                  : " (whole file - function range not mapped)")
                                            : "Coverage data not available"}
                                    >
                                        <span className={`w-2 h-2 rounded-full ${
                                            !hasCov ? "bg-gray-500" : meets ? "bg-emerald-400" : "bg-rose-400"
                                        }`}></span>
                                        <span>{hasCov ? `${coverageValue}% Coverage` : "N/A Coverage"}</span>
                                        {hasCov && item.coverageScope === "FUNCTION" && (
                                            <span className="opacity-70 font-normal">fn</span>
                                        )}
                                    </div>

                                    {/* Target status: the configured minimum vs the REAL measured value */}
                                    {hasCov && (
                                        <div
                                            className={`px-3 py-1 rounded-xl text-xs font-semibold border flex items-center gap-1.5 ${
                                                item.targetMet
                                                    ? "bg-emerald-950/90 border-emerald-700 text-emerald-300"
                                                    : "bg-rose-950/90 border-rose-700 text-rose-300"
                                            }`}
                                            title={`Minimum measured coverage target: ${item.coverageTarget}%`}
                                        >
                                            <span>{item.targetMet ? "✓" : "⚠"}</span>
                                            <span>
                                                Target: {item.coverageTarget}% {item.targetMet ? "reached" : "not reached"}
                                            </span>
                                        </div>
                                    )}

                                    {/* 2. Distinct Test Run Pass/Fail badge (independent of coverage threshold) */}
                                    <div
                                        className={`px-3 py-1 rounded-xl text-xs font-semibold border flex items-center gap-1.5 ${
                                            item.passed
                                                ? "bg-emerald-950/90 border-emerald-700 text-emerald-300"
                                                : "bg-rose-950/90 border-rose-700 text-rose-300"
                                        }`}
                                        title={item.passed ? "All unit tests executed and passed" : "One or more tests failed during execution"}
                                    >
                                        <span>{item.passed ? "Tests Pass ✅" : "Tests Failed ❌"}</span>
                                    </div>
                                </div>
                            </div>

                            {/* Per-Function Coverage Bar (0–100%) */}
                            <div className="space-y-1.5">
                                <div className="flex justify-between text-[11px] font-mono text-gray-400">
                                    <span>
                                        {item.coverageScope === "FUNCTION"
                                            ? "Function Coverage"
                                            : item.coverageScope === "FILE"
                                            ? "File Coverage"
                                            : "Coverage Proportion"}
                                        {item.coverageScope === "FUNCTION" && item.fileCoverage !== null && (
                                            <span className="opacity-70"> (file {item.fileCoverage}%)</span>
                                        )}
                                    </span>
                                    <span className={meets ? "text-emerald-400 font-bold" : "text-rose-400 font-bold"}>
                                        {hasCov ? `${coverageValue}% / 100%` : "N/A"}
                                    </span>
                                </div>
                                <div className="relative w-full bg-gray-950 rounded-full h-2.5 overflow-hidden border border-gray-800">
                                    {/* Proportional horizontal bar */}
                                    {hasCov && (
                                        <div
                                            className={`h-full transition-all duration-300 rounded-full ${
                                                meets ? "bg-emerald-500" : "bg-rose-500"
                                            }`}
                                            style={{ width: `${Math.min(100, Math.max(0, coverageValue))}%` }}
                                        />
                                    )}
                                </div>
                            </div>

                            {/* Generation failure: nothing usable was produced - show the
                                reason right on the card instead of an empty code block. */}
                            {item.status === "GENERATION_FAILURE" && item.error && (
                                <div className="flex items-start gap-2 rounded-xl border border-rose-700/60 bg-rose-950/40 px-3.5 py-2.5 text-xs text-rose-300">
                                    <svg className="w-4 h-4 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                                    </svg>
                                    <span className="font-mono whitespace-pre-wrap break-all">
                                        {String(item.error).slice(0, 500)}
                                    </span>
                                </div>
                            )}

                            {/* Honest below-target notice: the suite was re-generated but the
                                MEASURED coverage still fell short of the configured minimum. */}
                            {item.bestEffort && (
                                <div className="flex items-start gap-2 rounded-xl border border-amber-700/60 bg-amber-950/40 px-3.5 py-2.5 text-xs text-amber-300">
                                    <svg className="w-4 h-4 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                                    </svg>
                                    <span>
                                        Best effort — not a success: the tests were regenerated{" "}
                                        {item.attempts > 1 ? `${item.attempts} times` : "once"} but only reached{" "}
                                        {hasCov ? `${coverageValue}%` : "unmeasured"} measured coverage, below the{" "}
                                        {item.coverageTarget}% target. The value shown is the real measured result.
                                    </span>
                                </div>
                            )}

                            {/* Code Display + Copy Button */}
                            <div className="relative mt-2">
                                <div className="flex items-center justify-between pb-2">
                                    <span className="text-xs font-semibold text-gray-400 font-mono">
                                        Generated Unit Test Suite
                                    </span>
                                    <CopyButton code={item.testCode} />
                                </div>

                                <pre className="bg-gray-950 rounded-xl p-4 text-xs text-gray-200 font-mono overflow-x-auto max-h-72 overflow-y-auto border border-gray-800 leading-relaxed">
                                    {item.testCode || "# No test code generated for this function."}
                                </pre>
                            </div>

                            {/* Optional Test Output Drawer (If error/output available) */}
                            {(item.testOutput || item.error) && (
                                <div className="pt-1">
                                    <button
                                        onClick={() => toggleOutput(item.id)}
                                        className="text-xs text-gray-400 hover:text-gray-200 flex items-center gap-1.5 transition-colors cursor-pointer"
                                        type="button"
                                    >
                                        <svg
                                            className={`w-3.5 h-3.5 transform transition-transform ${expandedOutputs[item.id] ? "rotate-90" : ""}`}
                                            fill="none"
                                            stroke="currentColor"
                                            viewBox="0 0 24 24"
                                        >
                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 5l7 7-7 7" />
                                        </svg>
                                        <span>{expandedOutputs[item.id] ? "Hide Test Runner Output" : "View Test Runner Output"}</span>
                                    </button>

                                    {expandedOutputs[item.id] && (
                                        <pre className="mt-2 bg-gray-950/80 rounded-xl p-3 text-[11px] font-mono text-gray-300 border border-gray-800 overflow-x-auto max-h-48 overflow-y-auto">
                                            {item.error || item.testOutput}
                                        </pre>
                                    )}
                                </div>
                            )}
                        </div>
                    );
                })}

                {filteredTests.length === 0 && (
                    <div className="p-8 text-center bg-gray-800/50 rounded-2xl border border-gray-700/50 text-gray-400 text-sm">
                        No functions matched your current filter criteria ("{searchQuery}").
                    </div>
                )}
            </div>
        </div>
    );
}

export default React.memo(TestsTab);
