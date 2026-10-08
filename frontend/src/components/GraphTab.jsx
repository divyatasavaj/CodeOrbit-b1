import React, { useState, useMemo, useEffect, useCallback } from "react";
import {
    ReactFlow,
    ReactFlowProvider,
    MiniMap,
    Controls,
    Background,
    BackgroundVariant,
    Handle,
    Position,
    MarkerType,
    useNodesState,
    useEdgesState,
    useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import dagre from "@dagrejs/dagre";
import { API } from "../api.js";

/**
 * GraphTab Component for CodeOracle
 *
 * Interactive dependency graph built on React Flow:
 * - Dagre layered auto-layout (left-to-right) so nothing overlaps or bunches up
 * - Full-viewport canvas that fills the available screen space
 * - Clean, uniform node cards with type badges — every node is draggable
 * - Arrow labels appear on hover / selection so first-time users can read relationships
 * - Click a node to open a plain-English detail panel with its connections
 * - Search box to find and jump to any node
 *
 * @param {Object} props
 * @param {Object} [props.graph] - graph object; fetched lazily from
 *   /jobs/{id}/graph when omitted and jobId is provided (spec sections 9, 24)
 * @param {Array<Object>} props.graph.nodes - [{ id, name, type, file, line, description }]
 * @param {Array<Object>} props.graph.edges - [{ from, to, type, label }]
 * @param {boolean} [props.isLoading=false]
 * @param {string|null} [props.jobId] - job id for the lazy graph fetch
 */

const NODE_W = 236;
const NODE_H = 84;

/* Keep React Flow's rendered DOM bounded on huge repositories (spec sections
   9, 24 and 33): above GRAPH_RENDER_LIMIT only the first nodes render, and
   above DAGRE_NODE_LIMIT the expensive layered layout is skipped (nodes keep
   the simple grid positions from normalizeGraph). */
const GRAPH_RENDER_LIMIT = 1200;
const DAGRE_NODE_LIMIT = 800;

/* Theme-keyed palettes: dark = original design, light = darkened for contrast */
const NODE_COLORS = {
    dark: {
        file: "#38bdf8",
        class: "#818cf8",
        function: "#34d399",
        method: "#a78bfa",
        module: "#f472b6",
        external: "#fbbf24",
    },
    light: {
        file: "#0284c7",
        class: "#4f46e5",
        function: "#047857",
        method: "#6d28d9",
        module: "#be185d",
        external: "#b45309",
    },
};

const EDGE_COLORS = {
    dark: {
        contains: "#64748b",
        imports: "#f472b6",
        calls: "#34d399",
    },
    light: {
        contains: "#64748b",
        imports: "#db2777",
        calls: "#059669",
    },
};

function useTheme() {
    const [theme, setTheme] = useState(() => document.documentElement.getAttribute("data-theme") || "dark");
    useEffect(() => {
        const sync = () => setTheme(document.documentElement.getAttribute("data-theme") || "dark");
        window.addEventListener("co-theme-change", sync);
        return () => window.removeEventListener("co-theme-change", sync);
    }, []);
    return theme;
}

const TYPE_META = {
    file: { label: "File", icon: "📄" },
    class: { label: "Class", icon: "🏛" },
    function: { label: "Function", icon: "⚙" },
    method: { label: "Method", icon: "🧩" },
    module: { label: "Import", icon: "📦" },
    external: { label: "External", icon: "🔗" },
};

const EDGE_META = {
    contains: { dashed: false },
    imports: { dashed: true },
    calls: { dashed: false },
};

const FALLBACK_NODE_LEGEND = [
    { type: "file", label: "File", icon: "📄", color: "#38bdf8", description: "A source file from your project." },
    { type: "class", label: "Class", icon: "🏛", color: "#818cf8", description: "A class definition found in your code." },
    { type: "function", label: "Function", icon: "⚙", color: "#34d399", description: "A function defined in your project." },
    { type: "method", label: "Method", icon: "🧩", color: "#a78bfa", description: "A method that lives inside a class." },
    { type: "module", label: "Import", icon: "📦", color: "#f472b6", description: "A module or package your file imports." },
    { type: "external", label: "External call", icon: "🔗", color: "#fbbf24", description: "Called by your code but not defined in the analyzed files." },
];

const FALLBACK_EDGE_LEGEND = [
    { type: "contains", label: "contains", style: "solid", color: "#64748b", description: "A file contains this class or function." },
    { type: "imports", label: "imports", style: "dashed", color: "#f472b6", description: "A file loads this module or package." },
    { type: "calls", label: "calls", style: "solid", color: "#34d399", description: "One function invokes another." },
];

const REL_PHRASES = {
    calls: { out: "calls", in: "called by" },
    contains: { out: "contains", in: "part of" },
    imports: { out: "imports", in: "imported by" },
};

/* ------------------------------------------------------------------ */
/* Custom node: uniform rounded card, type badge, name, file subtitle  */
/* ------------------------------------------------------------------ */
function CodeNode({ data, selected }) {
    const showSub = data.file && data.nodeType !== "file" && data.nodeType !== "module";
    return (
        <div
            className={`co-node${selected ? " co-node--selected" : ""}${data.highlight ? " co-node--highlight" : ""}`}
            style={{ "--nc": data.color, "--nc-soft": `${data.color}26`, "--nc-mid": `${data.color}59` }}
            title={data.description}
        >
            <Handle type="target" position={Position.Left} />
            <span className="co-node-badge" aria-hidden="true">{data.icon}</span>
            <span className="co-node-text">
                <span className="co-node-type">{data.typeLabel}</span>
                <span className="co-node-name">{data.name}</span>
                {showSub && <span className="co-node-sub">{data.file}{data.line ? `:${data.line}` : ""}</span>}
            </span>
            <Handle type="source" position={Position.Right} />
        </div>
    );
}

const nodeTypes = { code: CodeNode };

/* ------------------------------------------------------------------ */
/* Build React Flow nodes/edges from backend payload                   */
/* ------------------------------------------------------------------ */
function normalizeGraph(graph, theme = "dark") {
    const nodeColors = NODE_COLORS[theme] || NODE_COLORS.dark;
    const edgeColors = EDGE_COLORS[theme] || EDGE_COLORS.dark;
    const rawNodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
    const nodeIds = new Set(rawNodes.map((n) => n.id));

    const rfEdges = (Array.isArray(graph?.edges) ? graph.edges : [])
        .map((e, i) => {
            const source = e.from ?? e.source;
            const target = e.to ?? e.target;
            if (!source || !target || !nodeIds.has(source) || !nodeIds.has(target)) return null;
            const type = String(e.type || e.edge_type || "calls").toLowerCase();
            const meta = EDGE_META[type] || EDGE_META.calls;
            const color = edgeColors[type] || edgeColors.calls;
            return {
                id: `edge-${i}-${source}-${target}`,
                source,
                target,
                type: "smoothstep",
                label: e.label || type,
                data: { edgeType: type },
                style: {
                    stroke: color,
                    strokeWidth: 1.7,
                    strokeDasharray: meta.dashed ? "7 5" : undefined,
                },
                markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color },
            };
        })
        .filter(Boolean);

    const rfNodes = rawNodes.map((n, i) => {
        const meta = TYPE_META[n.type] || TYPE_META.function;
        return {
            id: n.id,
            type: "code",
            position: { x: (i % 6) * (NODE_W + 56), y: Math.floor(i / 6) * (NODE_H + 48) },
            deletable: false,
            connectable: false,
            data: {
                name: n.name || n.id,
                nodeType: n.type || "function",
                typeLabel: meta.label,
                icon: meta.icon,
                color: nodeColors[n.type] || nodeColors.function,
                file: n.file || "",
                line: n.line || 0,
                description: n.description || `${meta.label} "${n.name || n.id}"`,
                highlight: false,
            },
        };
    });

    return { rfNodes, rfEdges };
}

/* ------------------------------------------------------------------ */
/* Dagre layered layout — spreads nodes evenly, no overlap             */
/* ------------------------------------------------------------------ */
function layoutWithDagre(nodes, edges) {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: "LR", ranksep: 110, nodesep: 46, marginx: 64, marginy: 64 });
    g.setDefaultEdgeLabel(() => ({}));

    nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
    edges.forEach((e) => {
        if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
    });

    try {
        dagre.layout(g);
    } catch (_) {
        return nodes;
    }

    return nodes.map((n) => {
        const p = g.node(n.id);
        if (!p) return n;
        return { ...n, position: { x: Math.round(p.x - NODE_W / 2), y: Math.round(p.y - NODE_H / 2) } };
    });
}

/* ------------------------------------------------------------------ */
/* Detail panel shown when a node is clicked                           */
/* ------------------------------------------------------------------ */
function NodeInfoPanel({ node, connections, onClose }) {
    if (!node) return null;
    const d = node.data;
    return (
        <div className="co-node-panel">
            <button type="button" className="co-node-panel-close" onClick={onClose} title="Close details">×</button>
            <div className="co-node-panel-head">
                <span className="co-node-panel-badge" style={{ color: d.color, background: `${d.color}1f`, borderColor: `${d.color}59` }}>
                    {d.icon}
                </span>
                <div style={{ minWidth: 0 }}>
                    <div className="co-node-panel-type" style={{ color: d.color }}>{d.typeLabel}</div>
                    <div className="co-node-panel-name">{d.name}</div>
                </div>
            </div>
            {d.file && (
                <div className="co-node-panel-loc">
                    {d.file}{d.line ? ` · line ${d.line}` : ""}
                </div>
            )}
            <p className="co-node-panel-desc">{d.description}</p>
            {connections.length > 0 && (
                <div className="co-node-panel-rels">
                    <div className="co-node-panel-rels-title">Connections ({connections.length})</div>
                    {connections.slice(0, 10).map((c) => (
                        <div key={c.id} className="co-node-panel-rel">
                            <span className="co-node-panel-arrow">{c.arrow}</span>
                            <span className="co-node-panel-phrase">{c.phrase}</span>
                            <span className="co-node-panel-target">{c.name}</span>
                        </div>
                    ))}
                    {connections.length > 10 && (
                        <div className="co-node-panel-more">+ {connections.length - 10} more</div>
                    )}
                </div>
            )}
        </div>
    );
}

/* ------------------------------------------------------------------ */
/* Canvas: header, legend, search, React Flow surface                  */
/* ------------------------------------------------------------------ */
function GraphCanvas({ graph }) {
    const theme = useTheme();
    const { rfNodes, rfEdges } = useMemo(() => normalizeGraph(graph, theme), [graph, theme]);
    const initialNodes = useMemo(
        () => (rfNodes.length > DAGRE_NODE_LIMIT ? rfNodes : layoutWithDagre(rfNodes, rfEdges)),
        [rfNodes, rfEdges]
    );

    const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(rfEdges);
    const [selectedId, setSelectedId] = useState(null);
    const [query, setQuery] = useState("");
    const { setCenter } = useReactFlow();

    // Re-color nodes/edges when the theme changes (drag positions are preserved)
    useEffect(() => {
        const nodeColors = NODE_COLORS[theme] || NODE_COLORS.dark;
        const edgeColors = EDGE_COLORS[theme] || EDGE_COLORS.dark;
        setNodes((ns) =>
            ns.map((n) => {
                const color = nodeColors[n.data.nodeType] || nodeColors.function;
                return n.data.color === color ? n : { ...n, data: { ...n.data, color } };
            })
        );
        setEdges((es) =>
            es.map((e) => {
                const t = e.data?.edgeType || "calls";
                const color = edgeColors[t] || edgeColors.calls;
                if (e.style?.stroke === color) return e;
                return {
                    ...e,
                    style: { ...e.style, stroke: color },
                    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color },
                };
            })
        );
    }, [theme, setNodes, setEdges]);

    const totalNodes = rfNodes.length;
    const totalEdges = rfEdges.length;

    const nodeStats = useMemo(() => {
        const stats = { file: 0, func: 0, cls: 0, module: 0, external: 0 };
        for (const n of rfNodes) {
            const t = n.data.nodeType;
            if (t === "file") stats.file++;
            else if (t === "class") stats.cls++;
            else if (t === "module") stats.module++;
            else if (t === "external") stats.external++;
            else stats.func++;
        }
        return stats;
    }, [rfNodes]);

    const matchCount = useMemo(() => {
        const q = query.trim().toLowerCase();
        if (!q) return 0;
        return nodes.filter((n) => n.data.name.toLowerCase().includes(q)).length;
    }, [query, nodes]);

    // Highlight nodes matching the search query
    useEffect(() => {
        const q = query.trim().toLowerCase();
        setNodes((ns) =>
            ns.map((n) => {
                const highlight = !!q && n.data.name.toLowerCase().includes(q);
                return n.data.highlight === highlight ? n : { ...n, data: { ...n.data, highlight } };
            })
        );
    }, [query, setNodes]);

    const highlightConnections = useCallback((id) => {
        setEdges((es) =>
            es.map((e) => {
                const selected = id != null && (e.source === id || e.target === id);
                return e.selected === selected ? e : { ...e, selected };
            })
        );
    }, [setEdges]);

    const handleNodeClick = useCallback((_, node) => {
        setSelectedId(node.id);
        highlightConnections(node.id);
    }, [highlightConnections]);

    const handlePaneClick = useCallback(() => {
        setSelectedId(null);
        highlightConnections(null);
    }, [highlightConnections]);

    const handleSearchKeyDown = useCallback((e) => {
        if (e.key !== "Enter") return;
        const q = query.trim().toLowerCase();
        if (!q) return;
        const hit = nodes.find((n) => n.data.name.toLowerCase().includes(q));
        if (hit) {
            setSelectedId(hit.id);
            highlightConnections(hit.id);
            setCenter(hit.position.x + NODE_W / 2, hit.position.y + NODE_H / 2, { zoom: 1.05, duration: 550 });
        }
    }, [query, nodes, setCenter, highlightConnections]);

    const selectedNode = selectedId ? nodes.find((n) => n.id === selectedId) : null;

    const connections = useMemo(() => {
        if (!selectedId) return [];
        const nameById = new Map(nodes.map((n) => [n.id, n.data.name]));
        return edges
            .filter((e) => e.source === selectedId || e.target === selectedId)
            .map((e) => {
                const outgoing = e.source === selectedId;
                const otherId = outgoing ? e.target : e.source;
                const rel = REL_PHRASES[e.data?.edgeType] || REL_PHRASES.calls;
                return {
                    id: e.id,
                    arrow: outgoing ? "→" : "←",
                    phrase: outgoing ? rel.out : rel.in,
                    name: nameById.get(otherId) || otherId,
                };
            });
    }, [selectedId, edges, nodes]);

    const nodeLegend = graph?.legend?.nodes?.length ? graph.legend.nodes : FALLBACK_NODE_LEGEND;
    const edgeLegend = graph?.legend?.edges?.length ? graph.legend.edges : FALLBACK_EDGE_LEGEND;
    const nodeColors = NODE_COLORS[theme] || NODE_COLORS.dark;
    const edgeColors = EDGE_COLORS[theme] || EDGE_COLORS.dark;

    return (
        <div className="space-y-3">
            {graph?.truncated && (
                <div className="rounded-xl border border-amber-700/60 bg-amber-950/30 px-4 py-2.5 text-xs text-amber-300">
                    Large graph - rendering {rfNodes.length} of {graph.nodes_total} nodes to keep the page responsive. Search to jump to a node by name.
                </div>
            )}
            {/* Header: metrics + search */}
            <div className="bg-gray-800/90 border border-gray-700/80 rounded-2xl p-4 shadow-lg backdrop-blur-sm flex flex-col md:flex-row items-stretch md:items-center justify-between gap-4">
                <div className="flex flex-wrap items-center gap-2.5">
                    <div className="flex items-center gap-2 px-3.5 py-1.5 bg-gray-900/90 border border-blue-500/30 rounded-xl text-xs font-semibold text-blue-300">
                        <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>
                        <span>{totalNodes} Nodes</span>
                    </div>
                    <div className="flex items-center gap-2 px-3.5 py-1.5 bg-gray-900/90 border border-emerald-500/30 rounded-xl text-xs font-semibold text-emerald-300">
                        <svg className="w-3.5 h-3.5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M14 5l7 7m0 0l-7 7m7-7H3" />
                        </svg>
                        <span>{totalEdges} Relationships</span>
                    </div>
                    <div className="hidden lg:flex items-center gap-2 text-xs text-gray-400 pl-2 border-l border-gray-700">
                        {nodeStats.file > 0 && <span>📄 {nodeStats.file} Files</span>}
                        {nodeStats.func > 0 && <span>⚙ {nodeStats.func} Functions</span>}
                        {nodeStats.cls > 0 && <span>🏛 {nodeStats.cls} Classes</span>}
                        {nodeStats.module > 0 && <span>📦 {nodeStats.module} Imports</span>}
                        {nodeStats.external > 0 && <span>🔗 {nodeStats.external} External</span>}
                    </div>
                </div>

                {/* Search */}
                <div className="relative flex items-center">
                    <svg className="w-4 h-4 absolute left-3 text-gray-500 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" />
                    </svg>
                    <input
                        type="text"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        onKeyDown={handleSearchKeyDown}
                        placeholder="Find a node… (press Enter to jump)"
                        className="co-search-input"
                        aria-label="Search graph nodes"
                    />
                    {query.trim() && (
                        <span className="absolute right-3 text-[10px] font-semibold text-gray-500">
                            {matchCount} found
                        </span>
                    )}
                </div>
            </div>

            {/* Legend */}
            <div className="co-legend">
                <div className="co-legend-group">
                    <span className="co-legend-title">Shapes</span>
                    {nodeLegend.map((item) => {
                        const icon = item.icon || TYPE_META[item.type]?.icon || "•";
                        const color = nodeColors[item.type] || item.color;
                        return (
                            <span key={item.type} className="co-legend-chip" title={item.description}>
                                <span className="co-legend-swatch" style={{ background: `${color}1f`, borderColor: `${color}59`, color }}>{icon}</span>
                                <span>{item.label}</span>
                            </span>
                        );
                    })}
                </div>
                <div className="co-legend-group">
                    <span className="co-legend-title">Arrows</span>
                    {edgeLegend.map((item) => {
                        const color = edgeColors[item.type] || item.color;
                        return (
                            <span key={item.type} className="co-legend-chip" title={item.description}>
                                <span className={`co-legend-line${item.style === "dashed" ? " dashed" : ""}`} style={{ borderColor: color }}></span>
                                <span>{item.label} →</span>
                            </span>
                        );
                    })}
                </div>
                <div className="co-graph-hint">
                    Drag nodes to rearrange · Scroll to zoom · Click a node for details
                </div>
            </div>

            {/* Canvas */}
            <div className="co-graph-canvas co-graph" style={{ height: "clamp(520px, calc(100vh - 350px), 1000px)" }}>
                <ReactFlow
                    nodes={nodes}
                    edges={edges}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    onNodeClick={handleNodeClick}
                    onPaneClick={handlePaneClick}
                    nodeTypes={nodeTypes}
                    fitView
                    fitViewOptions={{ padding: 0.14, maxZoom: 1.1 }}
                    minZoom={0.05}
                    maxZoom={2.5}
                    nodesConnectable={false}
                    deleteKeyCode={null}
                    proOptions={{ hideAttribution: true }}
                >
                    <Background
                        variant={BackgroundVariant.Dots}
                        gap={26}
                        size={1.5}
                        color={theme === "light" ? "rgba(100,116,139,0.35)" : "rgba(148,163,184,0.20)"}
                    />
                    <Controls position="bottom-left" showInteractive />
                    <MiniMap
                        position="bottom-right"
                        pannable
                        zoomable
                        nodeColor={(n) => n.data?.color || "#475569"}
                        maskColor={theme === "light" ? "rgba(241,245,249,0.75)" : "rgba(8,12,20,0.78)"}
                        style={{
                            background: theme === "light" ? "#f1f5f9" : "#0a0e16",
                            border: `1px solid ${theme === "light" ? "#e2e8f0" : "#1e293b"}`,
                            borderRadius: 10,
                        }}
                    />
                </ReactFlow>

                <NodeInfoPanel
                    node={selectedNode}
                    connections={connections}
                    onClose={() => { setSelectedId(null); highlightConnections(null); }}
                />
            </div>
        </div>
    );
}

/* ------------------------------------------------------------------ */
/* Public component                                                    */
/* ------------------------------------------------------------------ */
function GraphTab({ graph: graphProp = null, isLoading = false, jobId = null }) {
    const [fetchedGraph, setFetchedGraph] = useState(null);
    const [fetchFailed, setFetchFailed] = useState(false);

    /* Lazy graph fetch: the graph payload is only requested when the tab
       opens (spec sections 9 and 24), never as part of the initial results. */
    useEffect(() => {
        if (!jobId || graphProp) return undefined;
        let cancelled = false;
        fetch(`${API}/jobs/${jobId}/graph`)
            .then(res => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
            .then(data => {
                if (cancelled) return;
                setFetchedGraph(data && Array.isArray(data.nodes) ? data : null);
            })
            .catch(() => {
                if (!cancelled) setFetchFailed(true);
            });
        return () => { cancelled = true; };
    }, [jobId, graphProp]);

    const fullGraph = graphProp || fetchedGraph;

    /* Cap the rendered node/edge set on huge graphs (render-time DOM budget). */
    const graph = useMemo(() => {
        if (!fullGraph) return null;
        const nodes = Array.isArray(fullGraph.nodes) ? fullGraph.nodes : [];
        if (nodes.length <= GRAPH_RENDER_LIMIT || fullGraph.truncated) return fullGraph;
        const keep = nodes.slice(0, GRAPH_RENDER_LIMIT);
        const keepIds = new Set(keep.map(n => n.id));
        const edges = (Array.isArray(fullGraph.edges) ? fullGraph.edges : [])
            .filter(e => keepIds.has(e.from ?? e.source) && keepIds.has(e.to ?? e.target));
        return { ...fullGraph, nodes: keep, edges, truncated: true, nodes_total: nodes.length };
    }, [fullGraph]);

    const nodes = useMemo(() => (Array.isArray(graph?.nodes) ? graph.nodes : []), [graph]);
    const edges = useMemo(() => (Array.isArray(graph?.edges) ? graph.edges : []), [graph]);
    const totalNodes = nodes.length;
    const totalEdges = edges.length;

    const loading = isLoading || (!!jobId && !graphProp && !fetchedGraph && !fetchFailed);

    // Remount the canvas when the underlying analysis changes
    const graphKey = useMemo(
        () => `${totalNodes}:${totalEdges}:${nodes[0]?.id || ""}:${nodes[totalNodes - 1]?.id || ""}`,
        [nodes, totalNodes, totalEdges]
    );

    if (loading) {
        return (
            <div className="space-y-4 animate-pulse">
                <div className="bg-gray-800/80 rounded-2xl p-4 border border-gray-700/60 flex items-center justify-between">
                    <div className="flex gap-3">
                        <div className="h-8 w-28 bg-gray-700 rounded-lg"></div>
                        <div className="h-8 w-28 bg-gray-700 rounded-lg"></div>
                    </div>
                    <div className="h-8 w-36 bg-gray-700 rounded-lg"></div>
                </div>
                <div className="bg-gray-900 rounded-2xl border border-gray-800 p-8 min-h-[420px] flex flex-col items-center justify-center space-y-4">
                    <div className="w-12 h-12 border-4 border-blue-500/30 border-t-blue-500 rounded-full animate-spin"></div>
                    <p className="text-gray-400 text-sm font-medium">Constructing dependency graph...</p>
                </div>
            </div>
        );
    }

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

    return (
        <div className="co-graph-breakout">
            <ReactFlowProvider>
                <GraphCanvas key={graphKey} graph={graph} />
            </ReactFlowProvider>
        </div>
    );
}

export default React.memo(GraphTab);
