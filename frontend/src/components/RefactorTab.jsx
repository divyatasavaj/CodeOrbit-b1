import { useState } from "react";

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
                    : "bg-gray-800 hover:bg-gray-700 border-gray-700 text-gray-300 hover:text-white"
            }`}
        >
            {copied ? <span>Copied!</span> : copyFailed ? <span>Copy Failed</span> : <span>Copy Code</span>}
        </button>
    );
}

export default function RefactorTab({ refactor }) {
    const getRiskColor = (risk) => {
        switch (risk?.toLowerCase()) {
            case "high": return "bg-rose-950 border border-rose-700 text-rose-300 font-semibold";
            case "medium": return "bg-amber-950 border border-amber-700 text-amber-300 font-semibold";
            case "low": return "bg-emerald-950 border border-emerald-700 text-emerald-300 font-semibold";
            default: return "bg-gray-800 border border-gray-700 text-gray-300";
        }
    };

    return (
        <div className="space-y-6">
            {refactor.map((item, i) => (
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
            {(!refactor || refactor.length === 0) && (
                <p className="text-gray-500 text-center py-10">No refactoring data available</p>
            )}
        </div>
    );
}
