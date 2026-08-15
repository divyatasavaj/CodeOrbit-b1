import React, { useState, useEffect, useRef, useMemo, useCallback } from "react";

/**
 * GraphTab Component for CodeOracle
 * 
 * Visualizes multi-file code dependencies, module imports, and function call relationships
 * using Mermaid.js with structured node/edge metrics, zoom/pan controls, error handling,
 * and loading skeletons.
 * 
 * @param {Object} props
 * @param {Object} props.graph - The dependency graph object from /results/{job_id}
 * @param {string} [props.graph.mermaid] - Pre-formatted Mermaid.js diagram definition
 * @param {Array<Object>} [props.graph.nodes] - Array of node objects [{ id, name, type, file, line }]
 * @param {Array<Object>} [props.graph.edges] - Array of edge objects [{ from, to, type }]
 * @param {boolean} [props.isLoading=false] - Loading indicator for initial data retrieval
 */
export default function GraphTab({ graph, isLoading = false }) {
    const [renderError, setRenderError] = useState(null);
    const [isRendering, setIsRendering] = useState(false);
    const [zoomLevel, setZoomLevel] = useState(1);
    const [showRawCode, setShowRawCode] = useState(false);
    const [copied, setCopied] = useState(false);
    const containerRef = useRef(null);
    const renderIdRef = useRef(0);
    const svgWrapperRef = useRef(null);
    const contentSizeRef = useRef({ width: 0, height: 0 });
    const zoomRef = useRef(1);
    const MIN_ZOOM = 0.25;
    const MAX_ZOOM = 20;

    // 1. Structured Node & Edge Counts (Derived from actual arrays, never parsed from string)
    const nodes = useMemo(() => (graph && Array.isArray(graph.nodes) ? graph.nodes : []), [graph]);
    const edges = useMemo(() => (graph && Array.isArray(graph.edges) ? graph.edges : []), [graph]);
    const mermaidCode = useMemo(() => (graph && typeof graph.mermaid === "string" ? graph.mermaid.trim() : ""), [graph]);

    const nodeStats = useMemo(() => {
        const stats = { file: 0, func: 0, cls: 0, module: 0 };
        for (const n of nodes) {
            const type = (n.type || "").toLowerCase();
            if (type === "file") stats.file++;
            else if (type === "class") stats.cls++;
            else if (type === "module") stats.module++;
            else stats.func++;
        }
        return stats;
    }, [nodes]);

    const totalNodes = nodes.length;
    const totalEdges = edges.length;

    // 2. Mermaid Diagram Rendering Lifecycle
    useEffect(() => {
        if (isLoading || totalNodes === 0 || !mermaidCode) {
            setRenderError(null);
            return;
        }

        const renderContainer = containerRef.current;
        if (!renderContainer) return;

        // Clear container completely to avoid stale DOM or duplicate elements
        renderContainer.innerHTML = "";
        setRenderError(null);
        setIsRendering(true);

        const currentRenderId = ++renderIdRef.current;
        const uniqueElementId = `codeoracle_mermaid_${Date.now()}_${Math.random().toString(36).substring(2, 8)}`;

        const renderDiagram = async () => {
            try {
                // Ensure mermaid is loaded globally
                const mermaidApi = window.mermaid;
                if (!mermaidApi) {
                    throw new Error("Mermaid library is not loaded on the page.");
                }

                mermaidApi.initialize({
                    startOnLoad: false,
                    theme: "dark",
                    securityLevel: "loose",
                    flowchart: {
                        useMaxWidth: false,
                        htmlLabels: true,
                        curve: "basis"
                    }
                });

                // Execute render API
                const { svg } = await mermaidApi.render(uniqueElementId, mermaidCode);

                // Prevent race conditions if data changed during async render
                if (currentRenderId !== renderIdRef.current || !containerRef.current) {
                    return;
                }

                const svgWrapper = document.createElement("div");
                svgWrapper.style.transformOrigin = "top left";
                svgWrapper.style.flexShrink = "0";
                svgWrapper.innerHTML = svg;

                containerRef.current.innerHTML = "";
                containerRef.current.appendChild(svgWrapper);
                svgWrapperRef.current = svgWrapper;

                const svgEl = svgWrapper.querySelector("svg");
                const rect = svgEl ? svgEl.getBoundingClientRect() : null;
                const naturalWidth = rect && rect.width ? rect.width : 100;
                const naturalHeight = rect && rect.height ? rect.height : 100;
                contentSizeRef.current = { width: naturalWidth, height: naturalHeight };

                svgWrapper.style.transform = `scale(${zoomRef.current})`;

                try { setupNodeDragging(svgEl); } catch (err) { console.warn("CodeOracle node drag setup warning:", err); }

                setRenderError(null);
            } catch (err) {
                if (currentRenderId === renderIdRef.current) {
                    console.warn("CodeOracle Mermaid render warning:", err);
                    setRenderError(err?.message || "Failed to render visual dependency graph.");
                }
            } finally {
                if (currentRenderId === renderIdRef.current) {
                    setIsRendering(false);
                }
            }
        };

        renderDiagram();
    }, [mermaidCode, totalNodes, isLoading]);

    const parseEdgePathD = (d) => {
        const segs = [];
        const re = /([MLCQSTAZHV])\s*([-+0-9.,eE\s]*)/g;
        let m;
        while ((m = re.exec(d)) && m[1]) {
            const cmd = m[1];
            const nums = (m[2].match(/-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?/g) || []).map(Number);
            if (cmd === "Z") { segs.push({ cmd, nums: [], pairs: [] }); continue; }
            const pairs = [];
            if (cmd === "M" || cmd === "L" || cmd === "T" || cmd === "C" || cmd === "S" || cmd === "Q") {
                for (let i = 0; i + 1 < nums.length; i += 2) pairs.push([i, i + 1]);
            } else if (cmd === "A") {
                for (let i = 5; i + 1 < nums.length; i += 7) pairs.push([i, i + 1]);
            } else if (cmd === "H") {
                nums.forEach((_, i) => pairs.push([i]));
            } else if (cmd === "V") {
                nums.forEach((_, i) => pairs.push([i]));
            }
            if (pairs.length) segs.push({ cmd, nums, pairs });
        }
        return segs;
    };

    const computeEdgePoints = (segs) => {
        const pts = [];
        let x = 0, y = 0;
        for (const seg of segs) {
            for (const pair of seg.pairs) {
                if (pair.length === 2) { x = seg.nums[pair[0]]; y = seg.nums[pair[1]]; }
                else if (seg.cmd === "H") x = seg.nums[pair[0]];
                else y = seg.nums[pair[0]];
                pts.push({ x, y });
            }
        }
        return pts;
    };

    const nearestNodeId = (nodes, pt) => {
        let best = null, bestD = Infinity;
        for (const n of nodes.values()) {
            const d = (pt.x - n.cx) * (pt.x - n.cx) + (pt.y - n.cy) * (pt.y - n.cy);
            if (d < bestD) { bestD = d; best = n.nodeId; }
        }
        return best;
    };

    const round1 = (v) => Math.round(v * 10) / 10;

    const rebuildEdge = (edge, startNode, endNode) => {
        const { segs, pts, lens, total } = edge;
        const out = [];
        let pi = 0;
        for (const seg of segs) {
            const nums = seg.nums.slice();
            for (const pair of seg.pairs) {
                const t = total ? lens[pi] / total : 0;
                const dx = startNode.dx * (1 - t) + endNode.dx * t;
                const dy = startNode.dy * (1 - t) + endNode.dy * t;
                if (pair.length === 2) { nums[pair[0]] = round1(pts[pi].x + dx); nums[pair[1]] = round1(pts[pi].y + dy); }
                else if (seg.cmd === "H") nums[pair[0]] = round1(pts[pi].x + dx);
                else nums[pair[0]] = round1(pts[pi].y + dy);
                pi++;
            }
            out.push(seg.cmd + nums.join(","));
        }
        edge.el.setAttribute("d", out.join(""));
    };

    const growSvgBounds = (svgEl, nodes) => {
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        let any = false;
        for (const n of nodes.values()) {
            if (!n.bb) continue;
            minX = Math.min(minX, n.cx + n.dx + n.bb.x);
            minY = Math.min(minY, n.cy + n.dy + n.bb.y);
            maxX = Math.max(maxX, n.cx + n.dx + n.bb.x + n.bb.width);
            maxY = Math.max(maxY, n.cy + n.dy + n.bb.y + n.bb.height);
            any = true;
        }
        if (!any) return;
        const pad = 24;
        const w = (svgEl.width && svgEl.width.baseVal) ? svgEl.width.baseVal.value : 100;
        const h = (svgEl.height && svgEl.height.baseVal) ? svgEl.height.baseVal.value : 100;
        const cur = (svgEl.getAttribute("viewBox") || "0 0 100 100").split(/[\s,]+/).map(Number);
        const cx0 = cur[0] || 0, cy0 = cur[1] || 0;
        const cw = cur[2] || w, ch = cur[3] || h;
        const nw = Math.max(maxX + pad, cx0 + cw) - cx0;
        const nh = Math.max(maxY + pad, cy0 + ch) - cy0;
        if (nw !== cw || nh !== ch) {
            svgEl.setAttribute("viewBox", `${cx0} ${cy0} ${nw} ${nh}`);
            if (svgEl.width && svgEl.width.baseVal) { svgEl.width.baseVal.value = nw; svgEl.height.baseVal.value = nh; }
            else { svgEl.setAttribute("width", nw); svgEl.setAttribute("height", nh); }
        }
    };

    const setupNodeDragging = (svgEl) => {
        svgEl.style.overflow = "visible";
        const nodes = new Map();
        for (const g of svgEl.querySelectorAll("g.node")) {
            const idMatch = (g.getAttribute("id") || "").match(/-flowchart-([A-Za-z0-9_]+)-\d+$/);
            const tr = (g.getAttribute("transform") || "").match(/translate\(([-\d.]+),\s*([-\d.]+)\)/);
            if (!idMatch || !tr) continue;
            let bb = null;
            try { bb = g.getBBox(); } catch (_) {}
            nodes.set(idMatch[1], { el: g, nodeId: idMatch[1], cx: parseFloat(tr[1]), cy: parseFloat(tr[2]), dx: 0, dy: 0, bb });
        }
        if (nodes.size === 0) return;

        const edges = [];
        for (const path of svgEl.querySelectorAll(".edgePaths path")) {
            const d = path.getAttribute("d");
            if (!d) continue;
            const segs = parseEdgePathD(d);
            const pts = computeEdgePoints(segs);
            if (pts.length < 2) continue;
            const lens = [0];
            let total = 0;
            for (let i = 1; i < pts.length; i++) { total += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y); lens.push(total); }
            const startNode = nearestNodeId(nodes, pts[0]);
            const endNode = nearestNodeId(nodes, pts[pts.length - 1]);
            if (!startNode || !endNode) continue;
            edges.push({ el: path, segs, pts, lens, total, startNode, endNode });
        }

        for (const node of nodes.values()) {
            const g = node.el;
            g.style.cursor = "grab";
            g.style.userSelect = "none";
            g.style.touchAction = "none";
            let dragging = false, moved = false, startX = 0, startY = 0;
            const onDown = (e) => {
                if (e.pointerType === "mouse" && e.button !== 0) return;
                e.preventDefault();
                dragging = true;
                moved = false;
                startX = e.clientX;
                startY = e.clientY;
                g.style.cursor = "grabbing";
                if (g.setPointerCapture) { try { g.setPointerCapture(e.pointerId); } catch (_) {} }
            };
            const onMove = (e) => {
                if (!dragging) return;
                const rawX = e.clientX - startX;
                const rawY = e.clientY - startY;
                if (!moved && Math.hypot(rawX, rawY) < 4) return;
                moved = true;
                const zoom = zoomRef.current || 1;
                node.dx += rawX / zoom;
                node.dy += rawY / zoom;
                startX = e.clientX;
                startY = e.clientY;
                g.setAttribute("transform", `translate(${node.cx} ${node.cy}) translate(${round1(node.dx)} ${round1(node.dy)})`);
                for (const edge of edges) {
                    if (edge.startNode !== node.nodeId && edge.endNode !== node.nodeId) continue;
                    const sn = nodes.get(edge.startNode);
                    const en = nodes.get(edge.endNode);
                    if (sn && en) rebuildEdge(edge, sn, en);
                }
                growSvgBounds(svgEl, nodes);
            };
            const onUp = (e) => {
                if (!dragging) return;
                dragging = false;
                g.style.cursor = "grab";
                if (g.releasePointerCapture) { try { g.releasePointerCapture(e.pointerId); } catch (_) {} }
            };
            g.addEventListener("pointerdown", onDown);
            g.addEventListener("pointermove", onMove);
            g.addEventListener("pointerup", onUp);
            g.addEventListener("pointercancel", onUp);
        }
    };

    // Copy Mermaid definition
    const handleCopyCode = useCallback(() => {
        if (!mermaidCode) return;
        navigator.clipboard.writeText(mermaidCode).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        });
    }, [mermaidCode]);

    // Zoom Controls
    const applyZoom = useCallback((nextZoom, anchorX, anchorY) => {
        const wrap = svgWrapperRef.current;
        const ctr = containerRef.current;
        if (!wrap || !ctr) return;
        const clamped = Math.min(Math.max(nextZoom, MIN_ZOOM), MAX_ZOOM);
        const prevZoom = zoomRef.current;
        const contentX = (ctr.scrollLeft + anchorX) / prevZoom;
        const contentY = (ctr.scrollTop + anchorY) / prevZoom;
        zoomRef.current = clamped;
        setZoomLevel(clamped);
        wrap.style.transform = `scale(${clamped})`;
        ctr.scrollLeft = Math.max(0, contentX * clamped - anchorX);
        ctr.scrollTop = Math.max(0, contentY * clamped - anchorY);
    }, []);

    const handleZoomIn = useCallback(() => {
        const ctr = containerRef.current;
        applyZoom(zoomRef.current * 1.35, ctr ? ctr.clientWidth / 2 : 0, ctr ? ctr.clientHeight / 2 : 0);
    }, [applyZoom]);

    const handleZoomOut = useCallback(() => {
        const ctr = containerRef.current;
        applyZoom(zoomRef.current / 1.35, ctr ? ctr.clientWidth / 2 : 0, ctr ? ctr.clientHeight / 2 : 0);
    }, [applyZoom]);

    const handleResetZoom = useCallback(() => {
        const ctr = containerRef.current;
        applyZoom(1, ctr ? ctr.clientWidth / 2 : 0, ctr ? ctr.clientHeight / 2 : 0);
    }, [applyZoom]);

    useEffect(() => {
        const ctr = containerRef.current;
        if (!ctr) return;
        const onWheelNative = (e) => {
            if (!e.ctrlKey && !e.metaKey) return;
            e.preventDefault();
            const rect = ctr.getBoundingClientRect();
            const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
            applyZoom(zoomRef.current * factor, e.clientX - rect.left, e.clientY - rect.top);
        };
        ctr.addEventListener("wheel", onWheelNative, { passive: false });
        return () => ctr.removeEventListener("wheel", onWheelNative);
    }, [applyZoom, totalNodes]);

    // -------------------------------------------------------------
    // RENDER: Loading Skeleton State
    // -------------------------------------------------------------
    if (isLoading) {
        return (
            <div className="space-y-4 animate-pulse">
                {/* Header Skeleton */}
                <div className="bg-gray-800/80 rounded-2xl p-4 border border-gray-700/60 flex items-center justify-between">
                    <div className="flex gap-3">
                        <div className="h-8 w-28 bg-gray-700 rounded-lg"></div>
                        <div className="h-8 w-28 bg-gray-700 rounded-lg"></div>
                    </div>
                    <div className="h-8 w-36 bg-gray-700 rounded-lg"></div>
                </div>
                {/* Canvas Skeleton */}
                <div className="bg-gray-900 rounded-2xl border border-gray-800 p-8 min-h-[420px] flex flex-col items-center justify-center space-y-4">
                    <div className="w-12 h-12 border-4 border-blue-500/30 border-t-blue-500 rounded-full animate-spin"></div>
                    <p className="text-gray-400 text-sm font-medium">Constructing dependency graph...</p>
                </div>
            </div>
        );
    }

    // -------------------------------------------------------------
    // RENDER: Zero-Nodes Fallback State
    // -------------------------------------------------------------
    if (totalNodes === 0) {
        return (
            <div className="bg-gray-800/90 border border-gray-700/80 rounded-2xl p-12 text-center my-6 shadow-xl backdrop-blur-sm">
                <div className="w-16 h-16 bg-gray-700/50 rounded-2xl flex items-center justify-center mx-auto mb-4 text-gray-400">
                    <svg className="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
                    </svg>
                </div>
                <h3 className="text-xl font-semibold text-gray-200 mb-2">No Dependencies Found</h3>
                <p className="text-gray-400 text-sm max-w-md mx-auto leading-relaxed">
                    The analyzed source files do not contain external module imports or inter-function call relationships.
                </p>
            </div>
        );
    }

    // -------------------------------------------------------------
    // RENDER: Active Graph View
    // -------------------------------------------------------------
    return (
        <div className="space-y-4">
            {/* 1. Control & Metrics Header Bar */}
            <div className="bg-gray-800/90 border border-gray-700/80 rounded-2xl p-4 shadow-lg backdrop-blur-sm flex flex-col md:flex-row items-stretch md:items-center justify-between gap-4">
                {/* Left: Structured Counts */}
                <div className="flex flex-wrap items-center gap-2.5">
                    <div className="flex items-center gap-2 px-3.5 py-1.5 bg-gray-900/90 border border-blue-500/30 rounded-xl text-xs font-semibold text-blue-300">
                        <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>
                        <span>{totalNodes} Nodes</span>
                    </div>

                    <div className="flex items-center gap-2 px-3.5 py-1.5 bg-gray-900/90 border border-emerald-500/30 rounded-xl text-xs font-semibold text-emerald-300">
                        <svg className="w-3.5 h-3.5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M14 5l7 7m0 0l-7 7m7-7H3" />
                        </svg>
                        <span>{totalEdges} Calls & Imports</span>
                    </div>

                    {/* Breakdown Badges */}
                    <div className="hidden lg:flex items-center gap-2 text-xs text-gray-400 pl-2 border-l border-gray-700">
                        {nodeStats.file > 0 && <span>📁 {nodeStats.file} Files</span>}
                        {nodeStats.func > 0 && <span>⚡ {nodeStats.func} Functions</span>}
                        {nodeStats.cls > 0 && <span>🏛️ {nodeStats.cls} Classes</span>}
                        {nodeStats.module > 0 && <span>📦 {nodeStats.module} Modules</span>}
                    </div>
                </div>

                {/* Right: Interactive Zoom & View Controls */}
                <div className="flex items-center gap-2 justify-end">
                    {/* Zoom Buttons */}
                    <div className="flex items-center bg-gray-900/80 border border-gray-700 rounded-xl p-1 text-xs">
                        <button
                            onClick={handleZoomOut}
                            className="px-2.5 py-1 text-gray-300 hover:text-white hover:bg-gray-800 rounded-lg transition-colors cursor-pointer"
                            title="Zoom Out"
                            type="button"
                        >
                            −
                        </button>
                        <span className="px-2 text-gray-400 font-mono text-[11px]">
                            {Math.round(zoomLevel * 100)}%
                        </span>
                        <button
                            onClick={handleZoomIn}
                            className="px-2.5 py-1 text-gray-300 hover:text-white hover:bg-gray-800 rounded-lg transition-colors cursor-pointer"
                            title="Zoom In"
                            type="button"
                        >
                            +
                        </button>
                        <button
                            onClick={handleResetZoom}
                            className="px-2 py-1 text-gray-400 hover:text-white hover:bg-gray-800 rounded-lg transition-colors border-l border-gray-800 ml-1 cursor-pointer"
                            title="Reset Zoom"
                            type="button"
                        >
                            Reset
                        </button>
                    </div>

                    {/* Toggle Raw Definition */}
                    <button
                        onClick={() => setShowRawCode(prev => !prev)}
                        className={`px-3 py-1.5 rounded-xl text-xs font-medium border transition-colors cursor-pointer ${
                            showRawCode
                                ? "bg-indigo-600/30 border-indigo-500/60 text-indigo-200"
                                : "bg-gray-700 hover:bg-gray-600 border-gray-600/60 text-gray-200"
                        }`}
                        type="button"
                    >
                        {showRawCode ? "Hide Mermaid Code" : "View Code"}
                    </button>
                </div>
            </div>

            {/* 2. Legend Bar */}
            <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-2.5 bg-gray-900/50 border border-gray-800/80 rounded-xl text-xs text-gray-400">
                <div className="flex items-center gap-4">
                    <span className="font-semibold text-gray-300">Legend:</span>
                    <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded bg-[#38bdf8]"></span> File</span>
                    <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded bg-[#34d399]"></span> Function</span>
                    <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded bg-[#818cf8]"></span> Class</span>
                    <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded bg-[#a78bfa]"></span> External Module</span>
                </div>
                <div className="text-[11px] text-gray-500">
                    Scroll to pan · Ctrl + scroll or +/− to zoom (up to 2000%) · Drag a node to rearrange it
                </div>
            </div>

            {/* 3. Raw Mermaid Code View (Optional Collapsible) */}
            {showRawCode && (
                <div className="relative bg-gray-950 rounded-2xl p-4 border border-indigo-500/30 shadow-inner">
                    <div className="flex items-center justify-between pb-2 mb-2 border-b border-gray-800">
                        <span className="text-xs font-semibold text-indigo-300 font-mono">Mermaid.js Definition</span>
                        <button
                            onClick={handleCopyCode}
                            className="text-xs px-2.5 py-1 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg text-gray-300 transition-colors cursor-pointer"
                            type="button"
                        >
                            {copied ? "Copied!" : "Copy Definition"}
                        </button>
                    </div>
                    <pre className="text-xs text-emerald-400 font-mono overflow-x-auto max-h-60 overflow-y-auto leading-relaxed">
                        {mermaidCode}
                    </pre>
                </div>
            )}

            {/* 4. Scrollable Graph Canvas Container */}
            <div className="relative bg-gray-950/90 rounded-2xl border border-gray-800 shadow-2xl overflow-hidden">
                {/* Error Fallback Banner */}
                {renderError ? (
                    <div className="p-8 text-center space-y-4">
                        <div className="w-12 h-12 bg-rose-950/80 border border-rose-800/60 rounded-xl flex items-center justify-center mx-auto text-rose-400">
                            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                            </svg>
                        </div>
                        <div>
                            <h4 className="text-base font-semibold text-gray-200 mb-1">Visual Graph Rendering Notice</h4>
                            <p className="text-xs text-rose-400/90 max-w-md mx-auto font-mono bg-rose-950/30 p-2.5 rounded-lg border border-rose-900/40">
                                {renderError}
                            </p>
                        </div>
                        <div className="pt-2">
                            <button
                                onClick={() => setShowRawCode(true)}
                                className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-200 text-xs font-medium rounded-xl border border-gray-700 transition-colors cursor-pointer"
                                type="button"
                            >
                                View Raw Graph Definition
                            </button>
                        </div>
                    </div>
                ) : (
                    /* Scrollable Diagram Area: min-height 420px, scrollable in both dimensions */
                    <div
                        ref={containerRef}
                        className="w-full min-h-[420px] max-h-[720px] overflow-auto flex items-start cursor-grab active:cursor-grabbing"
                        style={{ scrollbarWidth: "thin", touchAction: "pan-x pan-y" }}
                    >
                        {isRendering && (
                            <div className="flex items-center justify-center py-24 text-gray-500 text-xs gap-2">
                                <div className="w-4 h-4 border-2 border-blue-400 border-t-transparent rounded-full animate-spin"></div>
                                <span>Rendering graph elements...</span>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
