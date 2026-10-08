import React, { useState, useMemo } from "react";
import FunctionPicker from "./FunctionPicker.jsx";
import { API } from "../api.js";

async function copyToClipboard(text) {
    if (!text) return false;
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (err) {
        console.warn("Clipboard API failed, using fallback:", err);
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
        return false;
    }
}

function CopyButton({ text }) {
    const [copied, setCopied] = useState(false);
    const [copyFailed, setCopyFailed] = useState(false);

    const handleCopy = async () => {
        const success = await copyToClipboard(text);
        if (success) {
            setCopied(true);
            setCopyFailed(false);
            setTimeout(() => setCopied(false), 1500);
        } else {
            setCopyFailed(true);
            setTimeout(() => setCopyFailed(false), 2000);
        }
    };

    return (
        <button
            onClick={handleCopy}
            type="button"
            className={`text-xs px-3 py-1.5 rounded-lg border font-medium transition-all duration-150 flex items-center gap-1.5 cursor-pointer ${
                copied
                    ? "bg-emerald-950/90 border-emerald-500 text-emerald-300"
                    : copyFailed
                    ? "bg-rose-950/90 border-rose-500 text-rose-300"
                    : "bg-gray-800 hover:bg-gray-700 border-gray-700 text-gray-300 hover:text-fg"
            }`}
        >
            {copied ? <span>Copied!</span> : copyFailed ? <span>Copy Failed</span> : <span>Copy Code</span>}
        </button>
    );
}

function RefactorTab({ refactor = [], explanation, jobId, onUpdate }) {
    const [generating, setGenerating] = useState(null); // function name in flight
    const [genError, setGenError] = useState("");
    const [pickerOpen, setPickerOpen] = useState(false);

    const list = Array.isArray(refactor) ? refactor : [];
    const generatedNames = useMemo(() => new Set(list.map(r => r.name)), [list]);

    const handleGenerate = async (functionName, filename) => {
        if (!jobId) {
            setGenError("Analysis job ID is missing — please run a new analysis.");
            return;
        }
        setGenerating(functionName);
        setGenError("");
        try {
            const res = await fetch(`${API}/generate/refactor/${jobId}`, {
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
                    refactor: [...(prev.refactor || []).filter(r => r.name !== entry.name), entry]
                }));
            }
        } catch (err) {
            setGenError(err.message || "Refactoring failed. Please try again.");
        } finally {
            setGenerating(null);
        }
    };

    const errorBanner = genError ? (
        <div className="flex items-start gap-2.5 bg-rose-950/60 border border-rose-800/80 rounded-xl px-4 py-3 text-sm text-rose-300">
            <svg className="w-4 h-4 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <span className="flex-1">{genError}</span>
            <button type="button" onClick={() => setGenError("")} className="text-rose-400 hover:text-rose-200 font-bold leading-none" title="Dismiss">✕</button>
        </div>
    ) : null;

    const getRiskColor = (risk) => {
        switch (risk?.toLowerCase()) {
            case "high": return "bg-rose-950 border border-rose-700 text-rose-300 font-semibold";
            case "medium": return "bg-amber-950 border border-amber-700 text-amber-300 font-semibold";
            case "low": return "bg-emerald-950 border border-emerald-700 text-emerald-300 font-semibold";
            default: return "bg-gray-800 border border-gray-700 text-gray-300";
        }
    };

    // Empty state — on-demand picker (refactoring is generated per function)
    if (list.length === 0) {
        return (
            <div className="space-y-4">
                {errorBanner}
                <FunctionPicker
                    explanation={explanation}
                    existingNames={generatedNames}
                    generatingName={generating}
                    generateLabel="Generate"
                    title="Generate Refactored Code On Demand"
                    description="The initial analysis skips refactoring to save time. Pick any function below to get its modernized version with breaking-change analysis (one AI call per function, a few seconds)."
                    jobId={jobId}
                    onGenerate={handleGenerate}
                />
            </div>
        );
    }

    return (
        <div className="space-y-6">
            {errorBanner}

            {/* On-demand generation: refactor another function */}
            {pickerOpen ? (
                <div className="space-y-2">
                    <FunctionPicker
                        explanation={explanation}
                        existingNames={generatedNames}
                        generatingName={generating}
                        generateLabel="Generate"
                        title="Generate Refactored Code For Another Function"
                        description="One AI call per function. The result appears in the list below when it finishes."
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
                    Generate refactored code for another function
                </button>
            )}

            {list.map((item, i) => (
                <div key={i} className="bg-gray-800 rounded-xl p-6">
                    <div className="flex items-center justify-between mb-4">
                        <span className="font-mono text-green-400 font-medium text-lg">{item.name}()</span>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <p className="text-xs text-gray-500 uppercase tracking-wide">Original</p>
                                <CopyButton text={item.original_code} />
                            </div>
                            <pre className="bg-gray-900 rounded-lg p-4 text-sm text-gray-300 font-mono overflow-x-auto max-h-64 overflow-y-auto">
                                {item.original_code}
                            </pre>
                        </div>
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <p className="text-xs text-gray-500 uppercase tracking-wide">Refactored</p>
                                <CopyButton text={item.refactored_code} />
                            </div>
                            <pre className="bg-gray-800 rounded-lg p-4 text-sm text-blue-200 font-mono overflow-x-auto max-h-64 overflow-y-auto border border-gray-700">
                                {item.refactored_code}
                            </pre>
                        </div>
                    </div>

                    {item.breaking_changes && item.breaking_changes.length > 0 && (
                        <div>
                            <p className="text-xs text-gray-500 mb-2 uppercase tracking-wide">Breaking Changes</p>
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="text-left text-gray-500 border-b border-gray-700">
                                        <th className="pb-2 pr-4">Change</th>
                                        <th className="pb-2 pr-4">Risk</th>
                                        <th className="pb-2">Why</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {item.breaking_changes.map((change, ci) => (
                                        <tr key={ci} className="border-b border-gray-700/50">
                                            <td className="py-2 pr-4 text-gray-300">{change.change}</td>
                                            <td className="py-2 pr-4">
                                                <span className={`px-2 py-0.5 rounded text-xs ${getRiskColor(change.risk)}`}>
                                                    {change.risk}
                                                </span>
                                            </td>
                                            <td className="py-2 text-gray-400">{change.why}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>
            ))}
        </div>
    );
}

export default React.memo(RefactorTab);
