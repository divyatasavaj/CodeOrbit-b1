import React, { useState, useMemo, useCallback } from "react";

/**
 * Hard numeric threshold for code coverage compliance (PS-06 requirement).
 * Must use exact >= 60% boundary across all bars, borders, and summary counts.
 */
export const COVERAGE_THRESHOLD = 60;

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
                    : "bg-gray-800 hover:bg-gray-700 border-gray-700 text-gray-300 hover:text-white"
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
 * pass/fail execution status, and compliance summary with the 60% threshold.
 * 
 * @param {Object} props
 * @param {Array<Object>} [props.tests] - Array of test result objects from /results/{job_id}
 * @param {boolean} [props.isLoading=false] - Loading indicator for initial data retrieval
 */
export default function TestsTab({ tests, isLoading = false }) {
    const [searchQuery, setSearchQuery] = useState("");
    const [filterMode, setFilterMode] = useState("all"); // 'all' | 'meets' | 'below' | 'passed' | 'failed'
    const [expandedOutputs, setExpandedOutputs] = useState({});

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

            return {
                id: `test-item-${idx}-${rawName}`,
                name: rawName,
                coverage: validCov,
                hasValidCoverage: validCov !== null,
                meetsThreshold: validCov !== null ? meetsCoverageThreshold(validCov) : false,
                passed,
                testCode,
                testOutput,
                error
            };
        });
    }, [tests]);

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
    // RENDER: Empty State (Zero Tests)
    // -------------------------------------------------------------
    if (totalCount === 0) {
        return (
            <div className="bg-gray-800/90 border border-gray-700/80 rounded-2xl p-12 text-center my-6 shadow-xl backdrop-blur-sm">
                <div className="w-16 h-16 bg-gray-700/50 rounded-2xl flex items-center justify-center mx-auto mb-4 text-gray-400">
                    <svg className="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
                    </svg>
                </div>
                <h3 className="text-xl font-semibold text-gray-200 mb-2">No Tests Were Generated</h3>
                <p className="text-gray-400 text-sm max-w-md mx-auto leading-relaxed">
                    No unit tests were produced for the selected codebase. Ensure source files contain testable functions and rerun analysis.
                </p>
            </div>
        );
    }

    // -------------------------------------------------------------
    // RENDER: Active Tests View
    // -------------------------------------------------------------
    return (
        <div className="space-y-6">
            {/* 1. Summary Header Card */}
            <div className="bg-gray-800/90 border border-gray-700/80 rounded-2xl p-6 shadow-xl backdrop-blur-sm space-y-5">
                <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                    {/* Primary Requirement: "X of Y functions meet the 60% threshold" */}
                    <div>
                        <div className="flex items-center gap-3">
                            <h3 className="text-lg md:text-xl font-bold text-white tracking-tight">
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
                                className="absolute right-3 top-2.5 text-xs text-gray-400 hover:text-white"
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
                            ≥ 60% ({meetsThresholdCount})
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
                            &lt; 60% ({totalCount - meetsThresholdCount})
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

                    // Hard requirement: red left border when coverage < 60%, neutral/green when >= 60%
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
                                    {/* 1. Coverage percentage number badge */}
                                    <div
                                        className={`px-3 py-1 rounded-xl text-xs font-bold border flex items-center gap-1.5 ${
                                            !hasCov
                                                ? "bg-gray-900 border-gray-700 text-gray-400"
                                                : meets
                                                ? "bg-emerald-950/90 border-emerald-700 text-emerald-300"
                                                : "bg-rose-950/90 border-rose-700 text-rose-300"
                                        }`}
                                        title={hasCov ? `${coverageValue}% real line coverage` : "Coverage data not available"}
                                    >
                                        <span className={`w-2 h-2 rounded-full ${
                                            !hasCov ? "bg-gray-500" : meets ? "bg-emerald-400" : "bg-rose-400"
                                        }`}></span>
                                        <span>{hasCov ? `${coverageValue}% Coverage` : "N/A Coverage"}</span>
                                    </div>

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
                                    <span>Coverage Proportion</span>
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
