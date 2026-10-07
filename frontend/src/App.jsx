import { useState, useEffect, useRef, useCallback } from "react";
import ExplanationTab from "./components/ExplanationTab.jsx";
import GraphTab from "./components/GraphTab.jsx";
import TestsTab from "./components/TestsTab.jsx";
import RefactorTab from "./components/RefactorTab.jsx";
import { API } from "./api.js";

export default function App() {
    const [view, setView] = useState("upload");
    const [theme, setTheme] = useState(() => document.documentElement.getAttribute("data-theme") || "dark");
    const [loading, setLoading] = useState(false);
    const [progress, setProgress] = useState("");
    const [results, setResults] = useState(null);
    const [jobId, setJobId] = useState(null);
    const [error, setError] = useState(null);
    const [activeTab, setActiveTab] = useState("explanation");
    const [selectedFile, setSelectedFile] = useState(null);
    const [sourceType, setSourceType] = useState("upload"); // "upload" | "github"
    const [discovery, setDiscovery] = useState(null);
    const [analysisProgress, setAnalysisProgress] = useState(null);
    const [structural, setStructural] = useState(null);
    const [aiStatus, setAiStatus] = useState(null);
    const pollingRef = useRef(null);
    const eventSourceRef = useRef(null);

    const handleCancel = () => {
        if (pollingRef.current) clearInterval(pollingRef.current);
        if (eventSourceRef.current) { eventSourceRef.current.close(); eventSourceRef.current = null; }
        // Ask the backend to stop scheduling further LLM batches.
        if (jobId) {
            fetch(`${API}/jobs/${jobId}/cancel`, { method: "POST" }).catch(() => {});
        }
        setLoading(false);
        setProgress("");
        setDiscovery(null);
        setAnalysisProgress(null);
        setStructural(null);
        setAiStatus(null);
        setResults(null);
        setError(null);
        setJobId(null);
        setActiveTab("explanation");
        setView("upload");
    };

    const toggleTheme = () => {
        const next = theme === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", next);
        try { localStorage.setItem("co-theme", next); } catch (e) {}
        window.dispatchEvent(new Event("co-theme-change"));
        setTheme(next);
    };

    const startPollingFallback = useCallback((id) => {
        if (pollingRef.current) clearInterval(pollingRef.current);
        let failedPolls = 0;
        pollingRef.current = setInterval(async () => {
            try {
                const res = await fetch(`${API}/results/${id}`);
                if (!res.ok) {
                    if (res.status === 404) throw new Error("job-not-found");
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                failedPolls = 0;

                if (data.status === "error") {
                    clearInterval(pollingRef.current);
                    setError(data.message || "Analysis failed");
                    setLoading(false);
                    setView("upload");
                    return;
                }

                if (data.structural_ready || data.status === "complete") {
                    // Structural analysis is ready - show real results while AI
                    // explanations continue to arrive on the next polls.
                    setResults(data);
                    setJobId(id);
                    setStructural(data.structural || null);
                    setAnalysisProgress(data.analysis_progress || null);
                    setAiStatus(data.ai_status || null);
                    setDiscovery(null);
                    setLoading(false);
                    setView("results");
                } else {
                    setProgress(data.progress || "Processing...");
                    if (data.functions_found !== undefined || data.files_found !== undefined) {
                        setDiscovery({ functions: data.functions_found, files: data.files_found });
                    }
                }

                if (data.status === "complete") clearInterval(pollingRef.current);
            } catch (err) {
                if (failedPolls >= 5) {
                    clearInterval(pollingRef.current);
                    setError("Backend became unavailable while analyzing. If the server restarted, the job was lost - please try again.");
                    setLoading(false);
                    setView("upload");
                } else {
                    failedPolls += 1;
                }
            }
        }, 2000);
    }, []);

    const mergeExplanationEntries = useCallback((entries) => {
        if (!Array.isArray(entries) || entries.length === 0) return;
        setResults(prev => {
            if (!prev) return prev;
            const groups = Array.isArray(prev.explanation) ? [...prev.explanation] : [];
            const byFile = {};
            const order = [];
            groups.forEach(g => {
                const fname = g.filename || "unknown";
                byFile[fname] = { ...g, functions: [...(g.functions || [])] };
                order.push(fname);
            });
            entries.forEach(entry => {
                const fname = entry.filename || "unknown";
                if (!byFile[fname]) {
                    byFile[fname] = { filename: fname, module_summary: `Module ${fname}`, functions: [] };
                    order.push(fname);
                }
                const funcs = byFile[fname].functions.filter(f => f.name !== entry.name);
                funcs.push({
                    name: entry.name,
                    function_id: entry.function_id,
                    ai_status: entry.ai_status,
                    explanation: entry.explanation
                });
                funcs.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
                byFile[fname].functions = funcs;
            });
            return { ...prev, explanation: order.map(f => byFile[f]) };
        });
    }, []);

    const openAnalysisStream = useCallback((id) => {
        if (pollingRef.current) clearInterval(pollingRef.current);
        if (eventSourceRef.current) { eventSourceRef.current.close(); eventSourceRef.current = null; }

        const finalize = async () => {
            try {
                const res = await fetch(`${API}/results/${id}`);
                const data = await res.json();
                setResults(data);
                setJobId(id);
                setStructural(data.structural || null);
                setAnalysisProgress(data.analysis_progress || null);
                setAiStatus(data.ai_status || null);
                setDiscovery(null);
                setView("results");
            } catch (e) { /* keep whatever we already have */ }
            setLoading(false);
            setDiscovery(null);
        };

        const loadStructural = async () => {
            try {
                const res = await fetch(`${API}/results/${id}`);
                const data = await res.json();
                setResults(data);
                setJobId(id);
                setStructural(data.structural || null);
                setAnalysisProgress(data.analysis_progress || null);
                setAiStatus(data.ai_status || null);
                setDiscovery(null);
                setLoading(false);
                setView("results");
            } catch (e) {
                startPollingFallback(id);
            }
        };

        try {
            const es = new EventSource(`${API}/jobs/${id}/events`);
            eventSourceRef.current = es;
            es.onmessage = async (event) => {
                let ev;
                try { ev = JSON.parse(event.data); } catch (e) { return; }
                const type = ev.type;

                if (type === "structural_complete") {
                    await loadStructural();
                } else if (type === "analysis_started") {
                    setAiStatus("ai_generating");
                    setAnalysisProgress({ completed: ev.cached || 0, total: ev.total, failed: 0, cached: ev.cached, trivial: ev.trivial });
                } else if (type === "batch_completed") {
                    if (ev.functions && ev.functions.length) mergeExplanationEntries(ev.functions);
                    setAnalysisProgress({ completed: ev.completed, total: ev.total, failed: ev.failed, cached: ev.cached, trivial: ev.trivial });
                    setAiStatus("ai_partial");
                } else if (type === "progress") {
                    setAnalysisProgress({ completed: ev.completed, total: ev.total, failed: ev.failed, cached: ev.cached, trivial: ev.trivial });
                    if (ev.ai_status) setAiStatus(ev.ai_status);
                } else if (type === "function_failed") {
                    setProgress(`Analysis issue on ${ev.name || "a function"} - continuing`);
                } else if (type === "cancel_requested") {
                    setAiStatus("ai_cancelled");
                } else if (type === "analysis_error") {
                    es.close();
                    eventSourceRef.current = null;
                    try {
                        const res = await fetch(`${API}/results/${id}`);
                        const data = await res.json();
                        if (data.status === "error") {
                            setError(data.message || "Analysis failed");
                            setLoading(false);
                            setView("upload");
                            return;
                        }
                        setResults(data);
                        setJobId(id);
                        setView("results");
                    } catch (e) { /* ignore */ }
                    setLoading(false);
                } else if (type === "analysis_completed" || type === "analysis_cancelled") {
                    es.close();
                    eventSourceRef.current = null;
                    await finalize();
                }
            };
            es.onerror = () => {
                es.close();
                eventSourceRef.current = null;
                startPollingFallback(id);
            };
        } catch (e) {
            startPollingFallback(id);
        }
    }, [mergeExplanationEntries, startPollingFallback]);

    useEffect(() => {
        // Wake the Render backend (free tier sleeps after ~15 min of inactivity)
        fetch(`${API}/health`).catch(() => {});
        return () => {
            if (pollingRef.current) clearInterval(pollingRef.current);
            if (eventSourceRef.current) eventSourceRef.current.close();
        };
    }, []);

    const uploadWithColdStartRetry = async (file) => {
        const formData = new FormData();
        formData.append("file", file);
        let lastErr = null;
        for (let attempt = 1; attempt <= 3; attempt++) {
            try {
                setProgress(attempt > 1
                    ? `Waking up backend (cold start)... attempt ${attempt}/3`
                    : "Uploading...");
                const res = await fetch(`${API}/analyze`, { method: "POST", body: formData });
                if (!res.ok) {
                    let detail = `Backend returned HTTP ${res.status}`;
                    try { detail = (await res.json()).detail || detail; } catch (e) {}
                    throw new Error(detail);
                }
                const data = await res.json();
                if (data.status === "error") {
                    setError(data.message || "Analysis failed");
                    setLoading(false);
                    return null;
                }
                return data.job_id;
            } catch (err) {
                lastErr = err;
                if (attempt < 3) {
                    await new Promise(r => setTimeout(r, 20000));
                }
            }
        }
        throw lastErr;
    };

    const handleUpload = async (file) => {
        if (!file) return;
        setLoading(true);
        setError(null);
        setProgress("Uploading...");
        try {
            const jobId = await uploadWithColdStartRetry(file);
            if (jobId) openAnalysisStream(jobId);
        } catch (err) {
            setError("Failed to connect to backend. The Render server may be sleeping — a cold start takes 30-60 seconds. Please try again, or use the demo below.");
            setLoading(false);
        }
    };

    const githubImportRef = useRef(false);

    // Backend error codes -> the exact user-facing copy.
    const GITHUB_ERROR_MESSAGES = {
        invalid_github_url: "Please enter a valid GitHub repository URL.",
        invalid_github_url: "Please enter a valid GitHub repository URL.",
        github_not_found: "Repository not found.\n\nPlease check that:\n\u2022 The URL is correct\n\u2022 The repository is public\n\u2022 The repository still exists",
        github_private: "This repository is private.\nCodeOracle currently supports public GitHub repositories only.",
        github_rate_limited: "GitHub request limit reached.\nPlease try again later.",
        github_unavailable: "Unable to reach GitHub right now.\nPlease try again later.",
        github_download_failed: "Unable to download this repository.\nPlease try again.",
        github_too_large: "Repository is too large to analyze.\nPlease choose a smaller repository.",
        github_empty: "This repository appears to be empty. Nothing to analyze.",
    };

    const handleGithubImport = async (repoUrl) => {
        if (githubImportRef.current) return; // never start two identical downloads
        githubImportRef.current = true;
        setLoading(true);
        setError(null);
        setProgress("Downloading repository from GitHub...");
        try {
            const res = await fetch(`${API}/analyze/github`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ repo_url: repoUrl }),
            });
            let data = null;
            try { data = await res.json(); } catch (e) { /* non-JSON error body */ }
            if (!res.ok || (data && data.status === "error")) {
                const code = data && data.error_code;
                setError(
                    GITHUB_ERROR_MESSAGES[code]
                    || (data && (data.message || data.detail))
                    || "Unable to download this repository. Please try again."
                );
                setLoading(false);
                return;
            }
            openAnalysisStream(data.job_id);
        } catch (err) {
            setError("Unable to reach CodeOracle backend. Please try again.");
            setLoading(false);
        } finally {
            githubImportRef.current = false;
        }
    };

    const handleAnalysis = async () => {
        if (!selectedFile) {
            setError("Please upload a ZIP file first");
            return;
        }
        handleUpload(selectedFile);
    };

    const handleDemo = async () => {
        setLoading(true);
        setError(null);
        setProgress("Running demo analysis...");
        try {
            const res = await fetch(`${API}/demo`);
            const data = await res.json();
            if (data.status === "error") {
                setError(data.message || "Failed to start demo");
                setLoading(false);
                return;
            }
            openAnalysisStream(data.job_id || "demo");
        } catch (err) {
            setError("Failed to connect to backend demo endpoint");
            setLoading(false);
        }
    };

    const tabs = [
        { id: "explanation", label: "Explanation" },
        { id: "graph", label: "Dependency Graph" },
        { id: "tests", label: "Generated Tests" },
        { id: "refactor", label: "Refactored Code" }
    ];

    return (
        <div style={{ minHeight: "100vh", background: "var(--bg)", color: "var(--text)" }}>

            {/* NAVBAR */}
            <nav className="co-nav" style={{ position: "sticky", top: 0, zIndex: 50 }}>
                <div style={{ maxWidth: 1200, margin: "0 auto", padding: "0 1.5rem", height: 52, display: "flex", alignItems: "center", gap: 32 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
                        <div style={{ width: 26, height: 26, borderRadius: 6, background: "linear-gradient(135deg,#7c3aed,#db2777)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                            <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="white" strokeWidth="2.5"><path strokeLinecap="round" strokeLinejoin="round" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" /></svg>
                        </div>
                        <span style={{ fontWeight: 700, fontSize: "0.9rem", letterSpacing: "-0.02em" }}>CodeOracle</span>
                    </div>
                    <div style={{ display: "flex", gap: 24, flex: 1 }}>
                        {["Features", "Pricing", "Blog", "Docs", "Company"].map(l => (
                            <a key={l} href="#" className="co-nav-link">{l}</a>
                        ))}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
                        <button
                            type="button"
                            className="co-theme-toggle"
                            onClick={toggleTheme}
                            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
                            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
                        >
                            {theme === "dark" ? (
                                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                    <circle cx="12" cy="12" r="4.5" />
                                    <path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32l1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41m11.32-11.32l1.41-1.41" />
                                </svg>
                            ) : (
                                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                                </svg>
                            )}
                        </button>
                        {view === "results" ? (
                            <button
                                onClick={() => { setView("upload"); setResults(null); setJobId(null); setError(null); setActiveTab("explanation"); }}
                                className="co-btn-signup"
                            >
                                New Analysis
                            </button>
                        ) : (
                            <button onClick={handleDemo} className="co-btn-signup">
                                Try Demo
                            </button>
                        )}
                    </div>
                </div>
            </nav>

            {/* HERO (only on upload & not loading) */}
            {view === "upload" && !loading && (
                <section style={{ position: "relative", overflow: "hidden", paddingTop: "5rem", paddingBottom: "3rem" }}>
                    <div className="hero-bg"></div>
                    <div className="hero-wave"></div>

                    <div style={{ position: "relative", zIndex: 1, maxWidth: 1200, margin: "0 auto", padding: "0 1.5rem" }}>
                        <div style={{ marginBottom: "1.5rem" }}>
                            <span className="hero-badge">
                                <span className="dot"></span>
                                AI · Python &amp; JavaScript · Instant
                            </span>
                        </div>

                        <h1 className="hero-title" style={{ marginBottom: "1.25rem" }}>
                            High-performance<br />Code Analysis
                        </h1>
                        <p className="hero-sub" style={{ marginBottom: "2rem" }}>
                            Automatically understand, test, and refactor legacy codebases
                            using state-of-the-art AI infrastructure — in minutes.
                        </p>

                        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                            <button className="hero-cta-primary" onClick={handleDemo}>
                                <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5"><path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" /></svg>
                                Start for free
                            </button>
                            <button className="hero-cta-secondary" onClick={handleDemo}>View a demo →</button>
                        </div>
                    </div>
                </section>
            )}

            {/* MAIN CONTENT AREA */}
            <main style={{ maxWidth: 1200, margin: "0 auto", padding: "2rem 1.5rem" }}>
                {error && !(view === "upload" && sourceType === "github") && (
                    <div style={{ background: "var(--error-bg)", border: "1px solid var(--error-border)", borderRadius: 10, padding: "1rem 1.25rem", marginBottom: "1.5rem" }}>
                        <p style={{ color: "var(--error-title)", fontWeight: 600, marginBottom: 4 }}>Error</p>
                        <p style={{ color: "var(--error-text)", fontSize: "0.875rem" }}>{error}</p>
                    </div>
                )}

                {view === "upload" && !loading && (
                    <UploadScreen onUpload={handleUpload} onAnalysis={handleAnalysis} selectedFile={selectedFile} setSelectedFile={setSelectedFile} onDemo={handleDemo} sourceType={sourceType} setSourceType={setSourceType} sourceError={error} onGithubImport={handleGithubImport} onClearSourceError={() => setError(null)} />
                )}

                {loading && (
                    <ProcessingScreen progress={progress} discovery={discovery} onCancel={handleCancel} analysisProgress={analysisProgress} />
                )}

                {view === "results" && results && (
                    <ResultsView
                        results={results}
                        activeTab={activeTab}
                        setActiveTab={setActiveTab}
                        tabs={tabs}
                        jobId={jobId}
                        onResultsUpdate={setResults}
                        analysisProgress={analysisProgress}
                        structural={structural}
                        aiStatus={aiStatus}
                        onCancel={handleCancel}
                    />
                )}
            </main>

            {/* HOW IT WORKS (only on upload screen) */}
            {view === "upload" && !loading && (
                <section className="steps-section">
                    <div style={{ maxWidth: 1200, margin: "0 auto", padding: "0 1.5rem" }}>
                        <p style={{ textAlign: "center", fontSize: "0.72rem", fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", color: "var(--text-faint)", marginBottom: "0.75rem" }}>How it works</p>
                        <h2 style={{ textAlign: "center", fontSize: "1.8rem", fontWeight: 800, letterSpacing: "-0.03em", marginBottom: "3.5rem", color: "var(--text)" }}>Three steps to clarity</h2>

                        <div style={{ display: "flex", flexDirection: "column", gap: 40, maxWidth: 760, margin: "0 auto" }}>
                            {[
                                { n: "Step 1", title: "Upload your codebase", desc: "Drag-and-drop or select a .zip of your Python or JavaScript project. Max 50 MB.",
                                  vis: <div style={{ padding: "2rem 1rem", display: "flex", flexDirection: "column", gap: 8 }}>
                                    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                                        <div style={{ background: "rgba(168,85,247,0.2)", border: "1px dashed rgba(168,85,247,0.5)", borderRadius: 8, padding: "1rem 2rem", fontSize: "0.8rem", color: "var(--text-dim)" }}>sample_project.zip</div>
                                        <svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="#a855f7" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                                    </div>
                                  </div> },
                                { n: "Step 2", title: "AI parses & analyses", desc: "Our pipeline extracts AST trees, builds dependency graphs, and sends structured prompts to the AI.",
                                  vis: <div style={{ padding: "1.5rem", display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
                                    {["AST Parser", "Dep Graph", "LLM Prompt", "Coverage", "Refactor", "Tests"].map(l => (
                                        <div key={l} className="step-card" style={{ fontSize: "0.7rem", color: "var(--text-dim)", textAlign: "center", padding: "0.5rem" }}>{l}</div>
                                    ))}
                                  </div> },
                                { n: "Step 3", title: "Review results", desc: "Explore AI explanations, generated tests, refactored code, and an interactive dependency graph.",
                                  vis: <div style={{ padding: "1.5rem", display: "flex", gap: 8, flexWrap: "wrap" }}>
                                    {["Explanation", "Dep Graph", "Tests", "Refactor"].map(l => (
                                        <div key={l} style={{ background: "var(--accent-bg)", border: "1px solid var(--accent-border)", borderRadius: 6, padding: "0.35rem 0.75rem", fontSize: "0.75rem", color: "var(--accent-text)", fontWeight: 600 }}>{l}</div>
                                    ))}
                                  </div> }
                            ].map(({ n, title, desc, vis }, i) => (
                                <div key={i} style={{ display: "flex", gap: 32, alignItems: "flex-start" }}>
                                    <div style={{ width: 220, flexShrink: 0 }}>
                                        <p className="step-label">{n}</p>
                                        <p className="step-title">{title}</p>
                                        <p className="step-desc">{desc}</p>
                                    </div>
                                    <div className="step-vis" style={{ flex: 1 }}>{vis}</div>
                                </div>
                            ))}
                        </div>
                    </div>
                </section>
            )}

            {/* Footer */}
            {view === "upload" && !loading && (
                <footer style={{ borderTop: "1px solid var(--line)", padding: "2rem 1.5rem", textAlign: "center" }}>
                    <p style={{ fontSize: "0.75rem", color: "var(--text-faint)" }}>© 2025 CodeOracle — HackOrbit · Built with Gemini AI</p>
                </footer>
            )}
        </div>
    );
}

function ProcessingScreen({ progress, discovery, onCancel, analysisProgress }) {
    const total = analysisProgress?.total || 0;
    const completed = analysisProgress?.completed || 0;
    const pct = total > 0 ? Math.round((completed / total) * 100) : null;

    return (
        <div className="flex flex-col items-center justify-center py-20 px-4 max-w-xl mx-auto text-center">
            <div className="relative flex items-center justify-center w-24 h-24 mb-8">
                <div className="absolute inset-0 rounded-full border-4 border-blue-500/20 animate-ping"></div>
                <div className="animate-spin rounded-full h-20 w-20 border-t-4 border-b-4 border-blue-500 border-r-transparent"></div>
                {pct !== null && <span className="absolute text-base font-bold text-blue-400">{pct}%</span>}
            </div>

            <h2 className="text-2xl font-bold text-fg mb-2 tracking-tight">
                {pct !== null ? "Generating AI explanations" : "Analyzing codebase structure"}
            </h2>

            <div className="bg-gray-800/80 border border-gray-700/80 rounded-xl px-5 py-3 mb-4 w-full shadow-lg backdrop-blur-sm">
                <p className="text-blue-300 font-medium text-sm flex items-center justify-center space-x-2">
                    <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>
                    <span>{progress || "Scanning files and parsing source..."}</span>
                </p>
            </div>

            {discovery && (discovery.functions !== undefined || discovery.files !== undefined) && (
                <div className="flex items-center space-x-2 text-xs font-semibold text-emerald-400 bg-emerald-950/60 border border-emerald-800/60 rounded-full px-4 py-1.5 mb-4">
                    <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <span>
                        Found <strong>{discovery.functions ?? "?"}</strong> functions across <strong>{discovery.files ?? "?"}</strong> files
                    </span>
                </div>
            )}

            <div className="w-full bg-gray-800 rounded-full h-3 mb-4 overflow-hidden p-0.5 border border-gray-700/50 shadow-inner">
                <div
                    className={`bg-gradient-to-r from-blue-600 via-indigo-500 to-cyan-400 h-full rounded-full transition-all duration-300 shadow-md shadow-blue-500/50 ${pct === null ? "animate-pulse" : ""}`}
                    style={{ width: pct === null ? "40%" : `${pct}%` }}
                ></div>
            </div>

            <div className="flex items-center space-x-2 text-xs text-gray-400 bg-gray-800/40 border border-gray-700/40 rounded-full px-4 py-1.5 mb-6">
                <svg className="w-4 h-4 text-amber-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
                </svg>
                <span>Structure is analyzed first (no AI). AI explanations then stream in - the UI becomes usable in seconds.</span>
            </div>

            <button
                onClick={onCancel}
                className="text-gray-400 hover:text-red-400 text-sm font-medium border border-gray-700 hover:border-red-800/60 bg-gray-800/60 hover:bg-red-950/40 rounded-lg px-5 py-2 transition-all duration-200 flex items-center space-x-2 cursor-pointer"
            >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
                </svg>
                <span>Cancel</span>
            </button>
        </div>
    );
}

function UploadScreen({ onUpload, onAnalysis, selectedFile, setSelectedFile, onDemo, sourceType = "upload", setSourceType, sourceError, onGithubImport, onClearSourceError }) {
    const [dragOver, setDragOver] = useState(false);
    const [validationError, setValidationError] = useState(null);
    const [repoUrl, setRepoUrl] = useState("");
    const [urlError, setUrlError] = useState(null);
    const fileRef = useRef(null);

    const mode = sourceType === "github" ? "github" : "upload";

    const validateFile = (file) => {
        if (!file) return false;

        if (!file.name.toLowerCase().endsWith(".zip")) {
            setValidationError("Invalid file type. Please select a .ZIP file.");
            setSelectedFile(null);
            if (fileRef.current) fileRef.current.value = "";
            return false;
        }

        const MAX_SIZE = 50 * 1024 * 1024;
        if (file.size > MAX_SIZE) {
            setValidationError("File size exceeds limit (50MB maximum).");
            setSelectedFile(null);
            if (fileRef.current) fileRef.current.value = "";
            return false;
        }

        setValidationError(null);
        setSelectedFile(file);
        return true;
    };

    const handleDrop = (e) => {
        e.preventDefault();
        setDragOver(false);
        const file = e.dataTransfer.files[0];
        if (file) validateFile(file);
    };

    const handleDragOver = (e) => { e.preventDefault(); setDragOver(true); };
    const handleDragLeave = () => setDragOver(false);

    const handleFileChange = (e) => {
        const file = e.target.files[0];
        if (file) validateFile(file);
    };

    const handleRemoveFile = (e) => {
        e.stopPropagation();
        setSelectedFile(null);
        setValidationError(null);
        if (fileRef.current) fileRef.current.value = "";
    };

    const formatFileSize = (bytes) => {
        if (!bytes) return "0 B";
        const k = 1024;
        const sizes = ["B", "KB", "MB", "GB"];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
    };

    const switchMode = (next) => {
        if (next === mode) return;
        setValidationError(null);
        setUrlError(null);
        if (onClearSourceError) onClearSourceError();
        if (setSourceType) setSourceType(next);
    };

    // Frontend validation is a UX nicety only; the backend re-validates fully.
    const validateGithubUrl = (value) => {
        const raw = (value || "").trim();
        if (!raw) return "Please enter a GitHub repository URL.";
        if (/\s/.test(raw)) return "Please enter a valid GitHub repository URL.";
        const scheme = raw.match(/^([A-Za-z][A-Za-z0-9+.-]*):\/\//);
        if (scheme && !/^https?$/i.test(scheme[1])) return "Please enter a valid GitHub repository URL.";
        if (!scheme && /^[A-Za-z][A-Za-z0-9+.-]*:/.test(raw)) return "Please enter a valid GitHub repository URL.";
        let rest = raw.replace(/^https?:\/\//i, "");
        rest = rest.split("#")[0].split("?")[0].split("@").pop();
        const parts = rest.split("/").filter(Boolean);
        const host = (parts[0] || "").toLowerCase();
        if (host !== "github.com" && host !== "www.github.com") return "Please enter a valid GitHub repository URL.";
        if (parts.length < 3) return "Please enter a valid GitHub repository URL.";
        if (parts.length > 3 && (!["tree", "blob"].includes(parts[3]) || parts.length < 5)) {
            return "Please enter a valid GitHub repository URL.";
        }
        return null;
    };

    const handleUrlChange = (e) => {
        setRepoUrl(e.target.value);
        if (urlError) setUrlError(null);
        if (sourceError && onClearSourceError) onClearSourceError();
    };

    const handleGithubSubmit = (e) => {
        if (e) e.preventDefault();
        const problem = validateGithubUrl(repoUrl);
        setUrlError(problem);
        if (problem) return;
        if (onGithubImport) onGithubImport(repoUrl.trim());
    };

    const githubReady = !!repoUrl.trim() && !validateGithubUrl(repoUrl);

    const zipZone = (
        <div
            onClick={() => fileRef.current?.click()}
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            className={`w-full max-w-2xl border-2 border-dashed rounded-2xl p-12 text-center cursor-pointer transition-all duration-200 relative ${
                dragOver
                    ? "border-blue-500 bg-blue-500/10 shadow-lg shadow-blue-500/20"
                    : selectedFile
                        ? "border-green-500 bg-green-500/10"
                        : validationError
                            ? "border-red-500/60 bg-red-500/5 hover:bg-red-500/10"
                            : "border-gray-600 hover:border-gray-500 hover:bg-gray-800/50"
            }`}
        >
            {selectedFile ? (
                <div className="flex flex-col items-center justify-center py-4">
                    <div className="relative flex items-center justify-center w-16 h-16 bg-gray-800 border border-gray-700 rounded-xl mb-4 text-green-400">
                        <svg className="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                        </svg>
                        <button
                            type="button"
                            onClick={handleRemoveFile}
                            title="Remove file"
                            className="absolute -top-2 -right-2 bg-red-600 hover:bg-red-700 text-white rounded-full p-1 shadow-lg transition-colors"
                        >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M6 18L18 6M6 6l12 12" />
                            </svg>
                        </button>
                    </div>
                    <p className="text-xl font-semibold text-green-400 mb-1">{selectedFile.name}</p>
                    <p className="text-sm text-gray-400">{formatFileSize(selectedFile.size)}</p>
                </div>
            ) : (
                <>
                    <svg className="w-16 h-16 mx-auto text-gray-500 mb-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
                    </svg>
                    <p className="text-xl text-gray-300 mb-2">Drop your ZIP file here or click to upload</p>
                    <p className="text-gray-500">Supports Python & JavaScript codebases in ZIP format (Max 50MB)</p>
                </>
            )}
        </div>
    );

    const errorIcon = (
        <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
    );

    const demoLink = (
        <button
            type="button"
            onClick={onDemo}
            className="text-blue-400 hover:text-blue-300 text-sm font-medium transition-colors hover:underline flex items-center space-x-1 cursor-pointer"
        >
            <span>Try Demo</span>
            <span>{"\u2192"}</span>
        </button>
    );

    return (
        <div className="flex flex-col items-center justify-center py-16">
            <h2 className="text-2xl font-semibold text-gray-200 mb-1">Analyze Repository</h2>
            <p className="text-sm text-gray-500 mb-6">Choose a repository source to begin analysis.</p>

            <div role="tablist" aria-label="Repository source" className="inline-flex rounded-xl border border-gray-700 bg-gray-900/60 p-1 mb-8">
                <button
                    type="button"
                    role="tab"
                    aria-selected={mode === "upload"}
                    onClick={() => switchMode("upload")}
                    className={`px-5 py-2 rounded-lg text-sm font-semibold transition-colors cursor-pointer ${
                        mode === "upload" ? "bg-blue-600 text-white shadow-lg shadow-blue-600/30" : "text-gray-400 hover:text-gray-200"
                    }`}
                >
                    Upload ZIP
                </button>
                <button
                    type="button"
                    role="tab"
                    aria-selected={mode === "github"}
                    onClick={() => switchMode("github")}
                    className={`px-5 py-2 rounded-lg text-sm font-semibold transition-colors cursor-pointer ${
                        mode === "github" ? "bg-blue-600 text-white shadow-lg shadow-blue-600/30" : "text-gray-400 hover:text-gray-200"
                    }`}
                >
                    GitHub Repository
                </button>
            </div>

            {mode === "upload" ? (
                <div className="w-full flex flex-col items-center">
                    {zipZone}
                    {validationError && (
                        <div className="mt-4 flex items-center space-x-2 text-red-400 bg-red-950/60 border border-red-800/80 rounded-lg px-4 py-2 text-sm font-medium">
                            {errorIcon}
                            <span>{validationError}</span>
                        </div>
                    )}
                    <input
                        ref={fileRef}
                        type="file"
                        accept=".zip"
                        className="hidden"
                        onChange={handleFileChange}
                    />
                    <div className="mt-8 flex flex-col items-center space-y-4">
                        <button
                            type="button"
                            onClick={onAnalysis}
                            disabled={!selectedFile}
                            className={`px-8 py-3 rounded-lg font-semibold text-white transition-all duration-200 ${
                                selectedFile
                                    ? "bg-blue-600 hover:bg-blue-700 shadow-lg shadow-blue-600/30 cursor-pointer"
                                    : "bg-blue-600/40 text-gray-400 cursor-not-allowed opacity-50"
                            }`}
                        >
                            Analyze Codebase
                        </button>
                        {demoLink}
                    </div>
                </div>
            ) : (
                <form className="w-full max-w-2xl" onSubmit={handleGithubSubmit} noValidate>
                    <label htmlFor="github-repo-url" className="block text-sm font-semibold text-gray-300 mb-2">
                        GitHub Repository URL
                    </label>
                    <input
                        id="github-repo-url"
                        name="repo_url"
                        type="url"
                        inputMode="url"
                        autoComplete="url"
                        spellCheck={false}
                        value={repoUrl}
                        onChange={handleUrlChange}
                        placeholder="https://github.com/user/repository"
                        aria-invalid={urlError ? "true" : undefined}
                        aria-describedby={urlError ? "github-url-error" : "github-url-hint"}
                        className={`w-full rounded-2xl border bg-gray-900/60 px-4 py-3 text-gray-200 placeholder-gray-600 outline-none transition-colors focus:border-blue-500 ${
                            urlError ? "border-red-500/60" : "border-gray-700 hover:border-gray-600"
                        }`}
                    />
                    <p id="github-url-hint" className="mt-2 text-xs text-gray-500">
                        Public repositories only. Example: https://github.com/facebook/react
                    </p>
                    {urlError ? (
                        <div id="github-url-error" role="alert" className="mt-4 flex items-center space-x-2 text-red-400 bg-red-950/60 border border-red-800/80 rounded-lg px-4 py-2 text-sm font-medium">
                            {errorIcon}
                            <span>{urlError}</span>
                        </div>
                    ) : sourceError ? (
                        <div role="alert" className="mt-4 flex items-start space-x-2 text-red-400 bg-red-950/60 border border-red-800/80 rounded-lg px-4 py-3 text-sm font-medium whitespace-pre-line">
                            {errorIcon}
                            <span>{sourceError}</span>
                        </div>
                    ) : null}
                    <div className="mt-8 flex flex-col items-center space-y-4">
                        <button
                            type="submit"
                            disabled={!githubReady}
                            className={`px-8 py-3 rounded-lg font-semibold text-white transition-all duration-200 ${
                                githubReady
                                    ? "bg-blue-600 hover:bg-blue-700 shadow-lg shadow-blue-600/30 cursor-pointer"
                                    : "bg-blue-600/40 text-gray-400 cursor-not-allowed opacity-50"
                            }`}
                        >
                            Analyze Repository
                        </button>
                        {demoLink}
                    </div>
                </form>
            )}
        </div>
    );
}
const AI_DONE_STATUSES = ["ai", "cached", "trivial_skipped", "ast_fallback"];

function AiProgressPanel({ progress, structural, aiStatus, explanation, onCancel }) {
    if (!progress || !progress.total) return null;

    const total = progress.total || 0;
    const completed = progress.completed || 0;
    const failed = progress.failed || 0;
    const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
    const finished = completed >= total;
    const running = !finished && aiStatus !== "ai_cancelled" && aiStatus !== "ai_failed"
        && aiStatus !== "ai_interrupted" && aiStatus !== "ai_disabled";

    if (finished && (aiStatus === "ai_complete" || aiStatus === "ai_disabled")) return null;

    const counts = (structural && structural.file_function_counts) || {};
    const groups = Array.isArray(explanation) ? explanation : [];
    const files = groups.map(g => {
        const funcs = g.functions || [];
        const aiDone = funcs.filter(f => AI_DONE_STATUSES.includes(f.ai_status)).length;
        const expected = counts[g.filename] || funcs.length;
        return { name: g.filename || "unknown", done: aiDone, total: expected };
    });
    const pending = files.filter(f => f.done < f.total);
    const visible = pending.length ? pending : files;
    const shown = visible.slice(0, 8);

    return (
        <div className="bg-gray-800/80 border border-gray-700/70 rounded-2xl p-5 shadow-lg">
            <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
                <div className="flex items-center gap-2 flex-wrap">
                    <span className={`w-2 h-2 rounded-full ${running ? "bg-blue-400 animate-pulse" : "bg-emerald-400"}`}></span>
                    <h3 className="text-sm font-semibold text-gray-200">AI explanations</h3>
                    <span className="text-xs text-gray-400 font-mono">{completed} / {total} analyzed</span>
                    {failed > 0 && (
                        <span className="text-xs text-amber-400 font-mono">({failed} used static analysis)</span>
                    )}
                </div>
                <div className="flex items-center gap-3">
                    <span className="text-sm font-bold text-blue-400">{pct}%</span>
                    {running && onCancel && (
                        <button
                            onClick={onCancel}
                            className="text-xs font-medium text-gray-400 hover:text-red-400 border border-gray-700 hover:border-red-800/60 bg-gray-800/60 hover:bg-red-950/40 rounded-lg px-3 py-1 transition-colors cursor-pointer"
                        >
                            Cancel
                        </button>
                    )}
                </div>
            </div>
            <div className="w-full bg-gray-900/80 rounded-full h-2.5 overflow-hidden">
                <div
                    className="bg-gradient-to-r from-blue-600 via-indigo-500 to-cyan-400 h-full rounded-full transition-all duration-500"
                    style={{ width: `${pct}%` }}
                ></div>
            </div>
            {shown.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5">
                    {shown.map(f => {
                        const done = f.done >= f.total;
                        return (
                            <span key={f.name} className={`text-xs font-mono flex items-center gap-1.5 ${done ? "text-emerald-400" : "text-gray-400"}`}>
                                <span>{done ? "\u2713" : "\u23f3"}</span>
                                <span className="truncate max-w-[220px]">{f.name}</span>
                                {!done && <span className="text-gray-500">({f.done}/{f.total})</span>}
                            </span>
                        );
                    })}
                    {visible.length > shown.length && (
                        <span className="text-xs text-gray-500">+{visible.length - shown.length} more</span>
                    )}
                </div>
            )}
            {aiStatus === "ai_failed" && (
                <p className="mt-2 text-xs text-amber-400">AI generation hit an issue; deterministic static analysis is shown for the remaining functions.</p>
            )}
            {aiStatus === "ai_cancelled" && (
                <p className="mt-2 text-xs text-gray-400">Analysis cancelled - showing what completed so far. Tests and refactors remain available on demand.</p>
            )}
        </div>
    );
}

function ResultsView({ results, activeTab, setActiveTab, tabs, jobId, onResultsUpdate, analysisProgress, structural, aiStatus, onCancel }) {
    return (
        <div className="space-y-6">
            <AiProgressPanel
                progress={analysisProgress || results.analysis_progress}
                structural={structural || results.structural}
                aiStatus={aiStatus || results.ai_status}
                explanation={results.explanation}
                onCancel={onCancel}
            />

            {results.source_type === "github" && (
                <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-xl border border-gray-800 bg-gray-800/40 px-5 py-4 shadow-sm">
                    <div className="flex items-center space-x-2 text-sm font-semibold text-gray-200">
                        <svg className="w-5 h-5 text-gray-300" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                            <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
                        </svg>
                        <span>GitHub Repository</span>
                    </div>
                    <span className="text-sm text-gray-300">{results.repository || results.repository_url}</span>
                    {results.branch && (
                        <span className="rounded-full border border-gray-700 bg-gray-900/60 px-2.5 py-0.5 text-xs font-medium text-gray-400">
                            {results.branch}
                        </span>
                    )}
                    {results.repository_url && (
                        <a
                            href={results.repository_url}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="text-xs font-semibold text-blue-400 hover:text-blue-300 hover:underline"
                        >
                            Open on GitHub {"\u2192"}
                        </a>
                    )}
                </div>
            )}

            {results.summary && (
                <SummaryCard summary={results.summary} tests={results.tests} refactor={results.refactor} />
            )}

            <div className="border-b border-gray-800 bg-gray-800/40 rounded-t-xl px-2 pt-2 shadow-sm backdrop-blur-md">
                <div className="flex space-x-2">
                    {tabs.map(tab => {
                        const isActive = activeTab === tab.id;
                        return (
                            <button
                                key={tab.id}
                                onClick={() => setActiveTab(tab.id)}
                                className={`px-6 py-3.5 text-sm font-semibold border-b-2 transition-all duration-200 cursor-pointer flex items-center space-x-2 ${
                                    isActive
                                        ? "border-blue-500 text-blue-400 bg-blue-500/10 rounded-t-lg shadow-sm"
                                        : "border-transparent text-gray-400 hover:text-gray-200 hover:bg-gray-700/30 rounded-t-lg"
                                }`}
                            >
                                <span>{tab.label}</span>
                            </button>
                        );
                    })}
                </div>
            </div>

            {activeTab === "explanation" && (
                <ExplanationTab explanation={results.explanation} />
            )}
            {activeTab === "graph" && (
                <GraphTab graph={results.graph} isLoading={!results.graph} />
            )}
            {activeTab === "tests" && (
                <TestsTab
                    tests={results.tests}
                    isLoading={!results.tests}
                    explanation={results.explanation}
                    jobId={jobId}
                    onUpdate={onResultsUpdate}
                />
            )}
            {activeTab === "refactor" && (
                <RefactorTab
                    refactor={results.refactor || []}
                    explanation={results.explanation}
                    jobId={jobId}
                    onUpdate={onResultsUpdate}
                />
            )}
        </div>
    );
}

function SummaryCard({ summary, tests = [], refactor = [] }) {
    // Tests are generated on demand now — show "—" until the user generates some
    const hasTests = Array.isArray(tests) && tests.length > 0;
    let avgCoverage = 0;
    if (hasTests) {
        const valid = tests.filter(t => typeof t.coverage_percent === "number" && !isNaN(t.coverage_percent));
        avgCoverage = valid.length
            ? Math.round((valid.reduce((acc, t) => acc + (t.coverage_percent || 0), 0) / valid.length) * 10) / 10
            : 0;
    }
    const breakingChangesCount = Array.isArray(refactor) && refactor.length > 0
        ? refactor.reduce((acc, r) => acc + ((r.breaking_changes && r.breaking_changes.length) || 0), 0)
        : (summary.breaking_changes !== undefined ? summary.breaking_changes : 0);
    return (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <div className="bg-gray-800/80 border border-gray-700/70 rounded-xl p-5 text-center shadow-lg backdrop-blur-sm hover:border-blue-500/50 transition-colors">
                <p className="text-3xl font-extrabold text-blue-400 tracking-tight">{summary.files_analyzed}</p>
                <p className="text-gray-400 text-xs font-semibold uppercase tracking-wider mt-1.5">Files Analyzed</p>
            </div>
            <div className="bg-gray-800/80 border border-gray-700/70 rounded-xl p-5 text-center shadow-lg backdrop-blur-sm hover:border-emerald-500/50 transition-colors">
                <p className="text-3xl font-extrabold text-emerald-400 tracking-tight">{summary.functions_found}</p>
                <p className="text-gray-400 text-xs font-semibold uppercase tracking-wider mt-1.5">Functions Found</p>
            </div>
            <div className="bg-gray-800/80 border border-gray-700/70 rounded-xl p-5 text-center shadow-lg backdrop-blur-sm hover:border-amber-500/50 transition-colors" title={hasTests ? "Average coverage across generated test suites" : "Generate tests to compute coverage"}>
                <p className="text-3xl font-extrabold text-amber-400 tracking-tight">{hasTests ? `${avgCoverage}%` : "—"}</p>
                <p className="text-gray-400 text-xs font-semibold uppercase tracking-wider mt-1.5">Avg Coverage</p>
            </div>
            <div className="bg-gray-800/80 border border-gray-700/70 rounded-xl p-5 text-center shadow-lg backdrop-blur-sm hover:border-purple-500/50 transition-colors">
                <p className="text-3xl font-extrabold text-purple-400 tracking-tight">{breakingChangesCount}</p>
                <p className="text-gray-400 text-xs font-semibold uppercase tracking-wider mt-1.5">Breaking Changes</p>
            </div>
        </div>
    );
}
