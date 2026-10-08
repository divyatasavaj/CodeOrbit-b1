import { useState, useMemo, useEffect, useRef } from "react";
import { API } from "../api.js";

/**
 * Shared on-demand generation picker: searchable, file-grouped function list.
 *
 * When `jobId` is provided the picker fetches metadata-only pages from
 * `/jobs/{id}/functions?include_explanation=0` (debounced, spec sections 9,
 * 10 and 26) so large repositories never ship their full function payload to
 * the client. Falls back to the legacy `explanation` prop otherwise.
 *
 * @param {Object} props
 * @param {Array<Object>} [props.explanation] - file groups [{filename, functions:[{name}]}]
 * @param {string|null} [props.jobId] - job id for server-side metadata fetch
 * @param {Set<string>} props.existingNames - display names already generated
 * @param {string|null} props.generatingName - function currently being generated
 * @param {string} props.generateLabel - button label ("Generate" / "Regenerate")
 * @param {string} props.title - panel heading
 * @param {string} props.description - panel subtext
 * @param {(name: string, filename: string) => void} props.onGenerate
 */
export default function FunctionPicker({
    explanation,
    jobId = null,
    existingNames,
    generatingName = null,
    generateLabel = "Generate",
    title = "Generate On Demand",
    description = "",
    onGenerate
}) {
    const [query, setQuery] = useState("");
    const [serverGroups, setServerGroups] = useState(null);
    const [serverTotal, setServerTotal] = useState(0);
    const debounceRef = useRef(null);
    const seqRef = useRef(0);

    useEffect(() => {
        if (!jobId) return undefined;
        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(() => {
            const q = query.trim();
            const seq = ++seqRef.current;
            fetch(`${API}/jobs/${jobId}/functions?offset=0&limit=500&include_explanation=0&search=${encodeURIComponent(q)}`)
                .then(res => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
                .then(data => {
                    if (seq !== seqRef.current) return; // stale response
                    setServerGroups(Array.isArray(data.groups) ? data.groups : []);
                    setServerTotal(data.total || 0);
                })
                .catch(() => {
                    /* keep previous list on failure */
                });
        }, 250);
        return () => clearTimeout(debounceRef.current);
    }, [jobId, query]);

    const groups = useMemo(() => {
        if (jobId && serverGroups) {
            return serverGroups
                .map(grp => ({
                    filename: grp.filename || "unknown",
                    functions: grp.functions || [],
                }))
                .filter(grp => grp.functions.length > 0);
        }
        if (!Array.isArray(explanation) || explanation.length === 0) return [];
        let normalized = explanation;
        if (explanation[0]?.filename === undefined || explanation[0]?.functions === undefined) {
            normalized = [{ filename: "Analyzed Functions", functions: explanation }];
        }
        const q = query.trim().toLowerCase();
        return normalized
            .map(grp => ({
                filename: grp.filename || "unknown",
                functions: (grp.functions || []).filter(f => {
                    if (!q) return true;
                    return (f.name || "").toLowerCase().includes(q)
                        || (grp.filename || "").toLowerCase().includes(q);
                })
            }))
            .filter(grp => grp.functions.length > 0);
    }, [jobId, serverGroups, explanation, query]);

    const loadedCount = groups.reduce((acc, g) => acc + g.functions.length, 0);
    const truncated = jobId && serverTotal > loadedCount;

    const busy = generatingName !== null;

    return (
        <div className="bg-gray-800/90 border border-gray-700/80 rounded-2xl p-6 shadow-xl backdrop-blur-sm space-y-4">
            <div>
                <div className="flex items-center gap-2">
                    <svg className="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
                    </svg>
                    <h3 className="text-lg font-bold text-fg tracking-tight">{title}</h3>
                </div>
                {description && (
                    <p className="text-xs text-gray-400 mt-1 leading-relaxed">{description}</p>
                )}
            </div>

            <input
                type="text"
                placeholder="Search functions or files..."
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className="w-full bg-gray-900 border border-gray-700 rounded-xl px-3.5 py-2 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-blue-500"
            />

            <div className="space-y-4 max-h-96 overflow-y-auto pr-1">
                {groups.map(grp => (
                    <div key={grp.filename}>
                        <p className="text-[11px] text-gray-500 uppercase tracking-wide font-semibold mb-1.5 font-mono">
                            {grp.filename}
                        </p>
                        <div className="space-y-1.5">
                            {grp.functions.map(f => {
                                const name = f.name;
                                const generated = existingNames.has(name);
                                const isGenerating = generatingName === name;
                                return (
                                    <div
                                        key={name}
                                        className="flex items-center justify-between gap-3 bg-gray-900/60 border border-gray-700/60 rounded-lg px-3.5 py-2"
                                    >
                                        <span className="font-mono text-sm text-emerald-400 truncate">{name}()</span>
                                        <div className="flex items-center gap-2 shrink-0">
                                            {generated && (
                                                <span className="text-[10px] px-2 py-0.5 rounded-full bg-emerald-950/80 border border-emerald-700 text-emerald-300 font-semibold">
                                                    Generated
                                                </span>
                                            )}
                                            <button
                                                type="button"
                                                disabled={busy}
                                                onClick={() => onGenerate(name, grp.filename)}
                                                className={`text-xs px-3 py-1.5 rounded-lg border font-medium transition-all duration-150 cursor-pointer flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed ${
                                                    isGenerating
                                                        ? "bg-blue-950/80 border-blue-600 text-blue-300"
                                                        : generated
                                                        ? "bg-gray-800 border-gray-700 text-gray-300 hover:border-blue-500 hover:text-blue-300"
                                                        : "bg-blue-600 border-blue-500 text-white hover:bg-blue-700"
                                                }`}
                                            >
                                                {isGenerating ? (
                                                    <>
                                                        <svg className="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
                                                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                                                            <path className="opacity-75" fill="currentColor" d="M4 12a8 0 011.898-5.23A8.002 8.002 0 0110 4a8 8 0 018 8 8.002 8.002 0 01-1.23 4.002A8 8 0 0112 20h-1.85a8 8 0 01-6.158-3.768L4 12z"></path>
                                                        </svg>
                                                        Generating…
                                                    </>
                                                ) : generated ? (
                                                    "Regenerate"
                                                ) : (
                                                    generateLabel
                                                )}
                                            </button>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    </div>
                ))}
                {groups.length === 0 && (
                    <p className="text-gray-500 text-sm text-center py-4">
                        {query.trim().length > 0
                            ? `No functions match "${query}".`
                            : jobId && serverGroups === null
                            ? "Loading function list..."
                            : "No function list is available for this analysis."}
                    </p>
                )}
                {truncated && (
                    <p className="text-[11px] text-gray-500 text-center py-1">
                        Showing {loadedCount} of {serverTotal} functions - search to narrow the list.
                    </p>
                )}
            </div>
        </div>
    );
}
