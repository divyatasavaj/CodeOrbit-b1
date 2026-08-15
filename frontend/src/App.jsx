import { useState, useEffect, useRef, useCallback } from "react";
import ExplanationTab from "./components/ExplanationTab.jsx";
import GraphTab from "./components/GraphTab.jsx";
import TestsTab from "./components/TestsTab.jsx";
import RefactorTab from "./components/RefactorTab.jsx";

const API = "https://codeorbit-wi1m.onrender.com";

export default function App() {
    const [view, setView] = useState("upload");
    const [loading, setLoading] = useState(false);
    const [progress, setProgress] = useState("");
    const [results, setResults] = useState(null);
    const [error, setError] = useState(null);
    const [activeTab, setActiveTab] = useState("explanation");
    const [selectedFile, setSelectedFile] = useState(null);
    const [discovery, setDiscovery] = useState(null);
    const pollingRef = useRef(null);
    const eventSourceRef = useRef(null);

    const handleCancel = () => {
        if (pollingRef.current) clearInterval(pollingRef.current);
        if (eventSourceRef.current) { eventSourceRef.current.close(); eventSourceRef.current = null; }
        setLoading(false);
        setProgress("");
        setDiscovery(null);
        setError(null);
        setView("upload");
    };

    const startPollingFallback = useCallback((jobId) => {
        if (pollingRef.current) clearInterval(pollingRef.current);
        let failedPolls = 0;
        pollingRef.current = setInterval(async () => {
            try {
                const res = await fetch(`${API}/results/${jobId}`);
                if (!res.ok) {
                    if (res.status === 404) throw new Error("job-not-found");
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                failedPolls = 0;
                if (data.status === "complete" || data.status === "error") {
                    clearInterval(pollingRef.current);
                    if (data.status === "error") {
                        setError(data.message || "Analysis failed");
                        setLoading(false);
                        setView("upload");
                    } else {
                        setResults(data);
                        setLoading(false);
                        setView("results");
                    }
                } else {
                    setProgress(data.progress || "Processing...");
                    if (data.functions_found !== undefined || data.files_found !== undefined) {
                        setDiscovery({ functions: data.functions_found, files: data.files_found });
                    }
                }
            } catch (err) {
                if (failedPolls >= 5) {
                    clearInterval(pollingRef.current);
                    setError("Backend became unavailable while analyzing. If the server restarted, the job was lost — please try again.");
                    setLoading(false);
                    setView("upload");
                } else {
                    failedPolls += 1;
                }
            }
        }, 3000);
    }, []);

    const pollResults = useCallback((jobId) => {
        if (pollingRef.current) clearInterval(pollingRef.current);
        if (eventSourceRef.current) {
            eventSourceRef.current.close();
            eventSourceRef.current = null;
        }

        try {
            const es = new EventSource(`${API}/progress/${jobId}`);
            eventSourceRef.current = es;

            es.onmessage = async (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data.progress) setProgress(data.progress);
                    if (data.functions_found !== undefined || data.files_found !== undefined) {
                        setDiscovery({ functions: data.functions_found, files: data.files_found });
                    }
                    if (data.status === "complete" || data.done) {
                        es.close(); eventSourceRef.current = null;
                        const res = await fetch(`${API}/results/${jobId}`);
                        const fullData = await res.json();
                        setDiscovery(null); setResults(fullData); setLoading(false); setView("results");
                    } else if (data.status === "error") {
                        es.close(); eventSourceRef.current = null;
                        setError(data.message || data.error || "Analysis failed");
                        setLoading(false); setView("upload");
                    }
                } catch (err) {}
            };

            es.onerror = () => {
                es.close(); eventSourceRef.current = null;
                startPollingFallback(jobId);
            };
        } catch (e) {
            startPollingFallback(jobId);
        }
    }, [startPollingFallback]);

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
            if (jobId) pollResults(jobId);
        } catch (err) {
            setError("Failed to connect to backend. The Render server may be sleeping — a cold start takes 30-60 seconds. Please try again, or use the demo below.");
            setLoading(false);
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
            pollResults(data.job_id || "demo");
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
        <div style={{ minHeight: "100vh", background: "#080808", color: "#fff" }}>

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
                    {view === "results" ? (
                        <button
                            onClick={() => { setView("upload"); setResults(null); setError(null); }}
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
                {error && (
                    <div style={{ background: "rgba(220,38,38,0.1)", border: "1px solid rgba(220,38,38,0.35)", borderRadius: 10, padding: "1rem 1.25rem", marginBottom: "1.5rem" }}>
                        <p style={{ color: "#fca5a5", fontWeight: 600, marginBottom: 4 }}>Error</p>
                        <p style={{ color: "#fecaca", fontSize: "0.875rem" }}>{error}</p>
                    </div>
                )}

                {view === "upload" && !loading && (
                    <UploadScreen onUpload={handleUpload} onAnalysis={handleAnalysis} selectedFile={selectedFile} setSelectedFile={setSelectedFile} onDemo={handleDemo} />
                )}

                {loading && (
                    <ProcessingScreen progress={progress} discovery={discovery} onCancel={handleCancel} />
                )}

                {view === "results" && results && (
                    <ResultsView results={results} activeTab={activeTab} setActiveTab={setActiveTab} tabs={tabs} />
                )}
            </main>

            {/* HOW IT WORKS (only on upload screen) */}
            {view === "upload" && !loading && (
                <section className="steps-section">
                    <div style={{ maxWidth: 1200, margin: "0 auto", padding: "0 1.5rem" }}>
                        <p style={{ textAlign: "center", fontSize: "0.72rem", fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", color: "rgba(255,255,255,0.3)", marginBottom: "0.75rem" }}>How it works</p>
                        <h2 style={{ textAlign: "center", fontSize: "1.8rem", fontWeight: 800, letterSpacing: "-0.03em", marginBottom: "3.5rem", color: "#fff" }}>Three steps to clarity</h2>

                        <div style={{ display: "flex", flexDirection: "column", gap: 40, maxWidth: 760, margin: "0 auto" }}>
                            {[
                                { n: "Step 1", title: "Upload your codebase", desc: "Drag-and-drop or select a .zip of your Python or JavaScript project. Max 50 MB.",
                                  vis: <div style={{ padding: "2rem 1rem", display: "flex", flexDirection: "column", gap: 8 }}>
                                    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                                        <div style={{ background: "rgba(168,85,247,0.2)", border: "1px dashed rgba(168,85,247,0.5)", borderRadius: 8, padding: "1rem 2rem", fontSize: "0.8rem", color: "rgba(255,255,255,0.5)" }}>sample_project.zip</div>
                                        <svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="#a855f7" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                                    </div>
                                  </div> },
                                { n: "Step 2", title: "AI parses & analyses", desc: "Our pipeline extracts AST trees, builds dependency graphs, and sends structured prompts to the AI.",
                                  vis: <div style={{ padding: "1.5rem", display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
                                    {["AST Parser", "Dep Graph", "LLM Prompt", "Coverage", "Refactor", "Tests"].map(l => (
                                        <div key={l} className="step-card" style={{ fontSize: "0.7rem", color: "rgba(255,255,255,0.5)", textAlign: "center", padding: "0.5rem" }}>{l}</div>
                                    ))}
                                  </div> },
                                { n: "Step 3", title: "Review results", desc: "Explore AI explanations, generated tests, refactored code, and an interactive dependency graph.",
                                  vis: <div style={{ padding: "1.5rem", display: "flex", gap: 8, flexWrap: "wrap" }}>
                                    {["Explanation", "Dep Graph", "Tests", "Refactor"].map(l => (
                                        <div key={l} style={{ background: "rgba(168,85,247,0.12)", border: "1px solid rgba(168,85,247,0.25)", borderRadius: 6, padding: "0.35rem 0.75rem", fontSize: "0.75rem", color: "#c084fc", fontWeight: 600 }}>{l}</div>
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
                <footer style={{ borderTop: "1px solid rgba(255,255,255,0.06)", padding: "2rem 1.5rem", textAlign: "center" }}>
                    <p style={{ fontSize: "0.75rem", color: "rgba(255,255,255,0.25)" }}>© 2025 CodeOracle — HackOrbit · Built with Gemini AI</p>
                </footer>
            )}
        </div>
    );
}

function ProcessingScreen({ progress, discovery, onCancel }) {
    const [percent, setPercent] = useState(5);

    useEffect(() => {
        const timer = setInterval(() => {
            setPercent((prev) => {
                if (prev >= 90) return 90;
                return Math.min(90, prev + 0.6);
            });
        }, 1000);
        return () => clearInterval(timer);
    }, []);

    return (
        <div className="flex flex-col items-center justify-center py-20 px-4 max-w-xl mx-auto text-center">
            <div className="relative flex items-center justify-center w-24 h-24 mb-8">
                <div className="absolute inset-0 rounded-full border-4 border-blue-500/20 animate-ping"></div>
                <div className="animate-spin rounded-full h-20 w-20 border-t-4 border-b-4 border-blue-500 border-r-transparent"></div>
                <span className="absolute text-base font-bold text-blue-400">{Math.round(percent)}%</span>
            </div>

            <h2 className="text-2xl font-bold text-white mb-2 tracking-tight">Analyzing Codebase</h2>

            <div className="bg-gray-800/80 border border-gray-700/80 rounded-xl px-5 py-3 mb-4 w-full shadow-lg backdrop-blur-sm">
                <p className="text-blue-300 font-medium text-sm flex items-center justify-center space-x-2">
                    <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>
                    <span>{progress || "Processing codebase structure..."}</span>
                </p>
            </div>

            {discovery && (discovery.functions !== undefined || discovery.files !== undefined) && (
                <div className="flex items-center space-x-2 text-xs font-semibold text-emerald-400 bg-emerald-950/60 border border-emerald-800/60 rounded-full px-4 py-1.5 mb-4 animate-fade-in">
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
                    className="bg-gradient-to-r from-blue-600 via-indigo-500 to-cyan-400 h-full rounded-full transition-all duration-300 shadow-md shadow-blue-500/50"
                    style={{ width: `${percent}%` }}
                ></div>
            </div>

            <div className="flex items-center space-x-2 text-xs text-gray-400 bg-gray-800/40 border border-gray-700/40 rounded-full px-4 py-1.5 mb-6">
                <svg className="w-4 h-4 text-amber-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span>Estimated time: <strong>2–5 minutes</strong> for complete AI analysis</span>
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

function UploadScreen({ onUpload, onAnalysis, selectedFile, setSelectedFile, onDemo }) {
    const [dragOver, setDragOver] = useState(false);
    const [validationError, setValidationError] = useState(null);
    const fileRef = useRef(null);

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

    return (
        <div className="flex flex-col items-center justify-center py-16">
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
            {validationError && (
                <div className="mt-4 flex items-center space-x-2 text-red-400 bg-red-950/60 border border-red-800/80 rounded-lg px-4 py-2 text-sm font-medium">
                    <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
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
                <button
                    type="button"
                    onClick={onDemo}
                    className="text-blue-400 hover:text-blue-300 text-sm font-medium transition-colors hover:underline flex items-center space-x-1 cursor-pointer"
                >
                    <span>Try Demo</span>
                    <span>→</span>
                </button>
            </div>
        </div>
    );
}

function ResultsView({ results, activeTab, setActiveTab, tabs }) {
    return (
        <div className="space-y-6">
            {results.summary && <SummaryCard summary={results.summary} />}

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
                <TestsTab tests={results.tests} isLoading={!results.tests} />
            )}
            {activeTab === "refactor" && results.refactor && (
                <RefactorTab refactor={results.refactor} />
            )}
        </div>
    );
}

function SummaryCard({ summary }) {
    const breakingChangesCount = summary.breaking_changes !== undefined ? summary.breaking_changes : 0;
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
            <div className="bg-gray-800/80 border border-gray-700/70 rounded-xl p-5 text-center shadow-lg backdrop-blur-sm hover:border-amber-500/50 transition-colors">
                <p className="text-3xl font-extrabold text-amber-400 tracking-tight">{summary.avg_coverage}%</p>
                <p className="text-gray-400 text-xs font-semibold uppercase tracking-wider mt-1.5">Avg Coverage</p>
            </div>
            <div className="bg-gray-800/80 border border-gray-700/70 rounded-xl p-5 text-center shadow-lg backdrop-blur-sm hover:border-purple-500/50 transition-colors">
                <p className="text-3xl font-extrabold text-purple-400 tracking-tight">{breakingChangesCount}</p>
                <p className="text-gray-400 text-xs font-semibold uppercase tracking-wider mt-1.5">Breaking Changes</p>
            </div>
        </div>
    );
}
