import React, { useState, useMemo, useEffect, useCallback, useRef } from "react";
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
    ViewportPortal,
    getConnectedEdges,
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
/* Layout modes                                                        */
/* ------------------------------------------------------------------ */
const LAYOUT_MODES = {
    dagre: { label: "Hierarchical (Dagre)", description: "Left-to-right layered layout" },
    force: { label: "Force-directed", description: "Physics-based layout for clusters" },
    circular: { label: "Circular", description: "Nodes arranged in circles by cluster" },
};

/* ------------------------------------------------------------------ */
/* Custom node: LOD-aware - renders differently at different zoom levels  */
/* ------------------------------------------------------------------ */
function CodeNode({ data, selected, zoom }) {
    const showSub = data.file && data.nodeType !== "file" && data.nodeType !== "module";
    const isZoomedOut = zoom < 0.4;
    const isVeryZoomedOut = zoom < 0.15;
    
    if (isVeryZoomedOut) {
        // Minimal dot at very low zoom
        return (
            <div
                className={`co-node co-node--dot${selected ? " co-node--selected" : ""}${data.highlight ? " co-node--highlight" : ""}`}
                style={{ 
                    "--nc": data.color, 
                    "--nc-soft": `${data.color}26`, 
                    width: 10, 
                    height: 10,
                    borderRadius: "50%",
                    background: data.color,
                    border: selected ? "2px solid white" : "none",
                }}
                title={data.description}
            />
        );
    }
    
    if (isZoomedOut) {
        // Compact label at low zoom
        return (
            <div
                className={`co-node co-node--compact${selected ? " co-node--selected" : ""}${data.highlight ? " co-node--highlight" : ""}`}
                style={{ "--nc": data.color, "--nc-soft": `${data.color}26`, "--nc-mid": `${data.color}59` }}
                title={data.description}
            >
                <Handle type="target" position={Position.Left} />
                <span className="co-node-badge" aria-hidden="true">{data.icon}</span>
                <span className="co-node-name-compact">{data.name}</span>
                <Handle type="source" position={Position.Right} />
            </div>
        );
    }
    
    // Full detail at normal zoom
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

/* Wrapper that injects zoom level into node data */
function CodeNodeWrapper({ data, selected }) {
    const { zoom } = useReactFlow();
    return <CodeNode data={data} selected={selected} zoom={zoom} />;
}

const nodeTypes = { code: CodeNodeWrapper };

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
function NodeInfoPanel({ node, connections, onClose, onFocus }) {
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
                    <button
                        onClick={() => onFocus?.(node.id)}
                        className="w-full mt-3 px-3 py-2 bg-purple-500/20 border border-purple-500/30 rounded-lg text-sm text-purple-200 hover:bg-purple-500/30 transition-colors flex items-center justify-center gap-2"
                    >
                        <span>🎯</span>
                        <span>Focus on this node (show 1-hop neighborhood)</span>
                    </button>
                </div>
            )}
        </div>
    );
}

/* ------------------------------------------------------------------ */
/* Filter Panel Component                                              */
/* ------------------------------------------------------------------ */
function FilterPanel({ 
    rfNodes, 
    rfEdges, 
    graph,
    onFilterChange, 
    filters,
    layoutMode,
    setLayoutMode,
    onFocusNode,
    onClearFocus,
    hasFocus,
    showClusters,
    setShowClusters,
}) {
    const theme = useTheme();
    const nodeColors = NODE_COLORS[theme] || NODE_COLORS.dark;
    
    // Get unique files from nodes
    const files = useMemo(() => {
        const fileSet = new Set();
        rfNodes.forEach(n => {
            if (n.data.file) fileSet.add(n.data.file);
        });
        return Array.from(fileSet).sort();
    }, [rfNodes]);
    
    // Get unique node types
    const nodeTypes = useMemo(() => {
        const typeSet = new Set();
        rfNodes.forEach(n => typeSet.add(n.data.nodeType));
        return Array.from(typeSet).sort();
    }, [rfNodes]);
    
    const visibleNodes = rfNodes.filter(n => {
        if (filters.files.length && n.data.file && !filters.files.includes(n.data.file)) return false;
        if (filters.nodeTypes.length && !filters.nodeTypes.includes(n.data.nodeType)) return false;
        if (filters.minDegree > 0 && (n.data.degree || 0) < filters.minDegree) return false;
        return true;
    });
    
    const visibleEdges = rfEdges.filter(e => {
        return visibleNodes.some(n => n.id === e.source) && visibleNodes.some(n => n.id === e.target);
    });
    
    return (
        <div className="bg-gray-800/90 border border-gray-700/80 rounded-2xl p-4 shadow-lg backdrop-blur-sm space-y-4">
            {/* Active filters summary */}
            {(filters.files.length || filters.nodeTypes.length || filters.minDegree > 0 || hasFocus) && (
                <div className="flex flex-wrap gap-2">
                    {filters.files.map(f => (
                        <span key={f} className="px-2 py-1 bg-blue-500/20 border border-blue-500/30 rounded-lg text-xs flex items-center gap-1">
                            <span>📄</span>
                            <span>{f.split('/').pop()}</span>
                            <button onClick={() => onFilterChange('files', filters.files.filter(x => x !== f))} className="text-blue-300 hover:text-blue-100">×</button>
                        </span>
                    ))}
                    {filters.nodeTypes.map(t => (
                        <span key={t} className="px-2 py-1 bg-emerald-500/20 border border-emerald-500/30 rounded-lg text-xs flex items-center gap-1">
                            <span style={{color: nodeColors[t]}}>{TYPE_META[t]?.icon || '•'}</span>
                            <span>{TYPE_META[t]?.label || t}</span>
                            <button onClick={() => onFilterChange('nodeTypes', filters.nodeTypes.filter(x => x !== t))} className="text-emerald-300 hover:text-emerald-100">×</button>
                        </span>
                    ))}
                    {filters.minDegree > 0 && (
                        <span className="px-2 py-1 bg-amber-500/20 border border-amber-500/30 rounded-lg text-xs flex items-center gap-1">
                            <span>🔗</span>
                            <span>Min degree: {filters.minDegree}</span>
                            <button onClick={() => onFilterChange('minDegree', 0)} className="text-amber-300 hover:text-amber-100">×</button>
                        </span>
                    )}
                    {hasFocus && (
                        <span className="px-2 py-1 bg-purple-500/20 border border-purple-500/30 rounded-lg text-xs flex items-center gap-1">
                            <span>🎯</span>
                            <span>Focus mode</span>
                            <button onClick={onClearFocus} className="text-purple-300 hover:text-purple-100">×</button>
                        </span>
                    )}
                    {(filters.files.length || filters.nodeTypes.length || filters.minDegree > 0) && (
                        <button 
                            onClick={() => onFilterChange('reset', null)}
                            className="px-3 py-1 bg-gray-700 border border-gray-600 rounded-lg text-xs text-gray-300 hover:text-white"
                        >
                            Clear All
                        </button>
                    )}
                </div>
            )}
            
            {/* Filter controls */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                {/* File filter */}
                <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1">Filter by File</label>
                    <select
                        multiple
                        value={filters.files}
                        onChange={(e) => {
                            const selected = Array.from(e.target.selectedOptions).map(o => o.value);
                            onFilterChange('files', selected);
                        }}
                        className="w-full bg-gray-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    >
                        {files.map(f => (
                            <option key={f} value={f}>{f.split('/').pop()}</option>
                        ))}
                    </select>
                </div>
                
                {/* Node type filter */}
                <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1">Node Types</label>
                    <div className="flex flex-wrap gap-2">
                        {nodeTypes.map(t => (
                            <label key={t} className="flex items-center gap-1.5 cursor-pointer">
                                <input
                                    type="checkbox"
                                    checked={filters.nodeTypes.includes(t)}
                                    onChange={(e) => {
                                        const newTypes = e.target.checked
                                            ? [...filters.nodeTypes, t]
                                            : filters.nodeTypes.filter(x => x !== t);
                                        onFilterChange('nodeTypes', newTypes);
                                    }}
                                    className="w-4 h-4 rounded border-gray-600 bg-gray-800 text-blue-500 focus:ring-blue-500"
                                />
                                <span className="text-xs" style={{color: nodeColors[t]}}>
                                    {TYPE_META[t]?.icon || '•'} {TYPE_META[t]?.label || t}
                                </span>
                            </label>
                        ))}
                    </div>
                </div>
                
                {/* Min degree filter */}
                <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1">
                        Min Connections: {filters.minDegree}
                    </label>
                    <input
                        type="range"
                        min="0"
                        max="10"
                        value={filters.minDegree}
                        onChange={(e) => onFilterChange('minDegree', parseInt(e.target.value))}
                        className="w-full h-2 bg-gray-700 rounded-lg appearance-none accent-emerald-500"
                    />
                </div>
                
                {/* Layout mode + Cluster toggle */}
                <div className="flex flex-col gap-3">
                    <div>
                        <label className="block text-xs font-medium text-gray-400 mb-1">Layout Mode</label>
                        <select
                            value={layoutMode}
                            onChange={(e) => setLayoutMode(e.target.value)}
                            className="w-full bg-gray-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                        >
                            {Object.entries(LAYOUT_MODES).map(([key, meta]) => (
                                <option key={key} value={key}>{meta.label}</option>
                            ))}
                        </select>
                    </div>
                    <label className="flex items-center gap-2 cursor-pointer pt-2">
                        <input
                            type="checkbox"
                            checked={showClusters}
                            onChange={(e) => setShowClusters(e.target.checked)}
                            className="w-4 h-4 rounded border-gray-600 bg-gray-800 text-blue-500 focus:ring-blue-500"
                        />
                        <span className="text-xs text-gray-300">📦 Show file clusters</span>
                    </label>
                </div>
            </div>
            
            {/* Stats */}
            <div className="flex flex-wrap items-center gap-4 text-xs text-gray-400 pt-2 border-t border-gray-700">
                <span>Showing <strong className="text-white">{visibleNodes.length}</strong> of <strong className="text-white">{rfNodes.length}</strong> nodes</span>
                <span>| <strong className="text-white">{visibleEdges.length}</strong> of <strong className="text-white">{rfEdges.length}</strong> edges</span>
                {graph?.stats && (
                    <>
                        <span>| Entry points: <strong className="text-white">{graph.stats.entry_points || 0}</strong></span>
                        <span>| Clusters: <strong className="text-white">{graph.stats.clusters || 0}</strong></span>
                    </>
                )}
            </div>
        </div>
    );
}

/* ------------------------------------------------------------------ */
/* Force-directed layout (simple implementation)                       */
/* ------------------------------------------------------------------ */
function applyForceLayout(nodes, edges, width = 1200, height = 800) {
    const nodeMap = new Map(nodes.map(n => [n.id, { ...n, vx: 0, vy: 0, x: n.position?.x || Math.random() * width, y: n.position?.y || Math.random() * height }]));
    const adjacency = new Map();
    
    edges.forEach(e => {
        if (!adjacency.has(e.source)) adjacency.set(e.source, []);
        if (!adjacency.has(e.target)) adjacency.set(e.target, []);
        adjacency.get(e.source).push({ target: e.target, weight: e.data?.weight || 1 });
        adjacency.get(e.target).push({ target: e.source, weight: e.data?.weight || 1 });
    });
    
    const iterations = 100;
    const k = Math.sqrt((width * height) / nodes.length) * 0.5;
    const repulsion = k * k;
    const attraction = k / 10;
    const damping = 0.85;
    const minDist = 0.01;
    
    for (let iter = 0; iter < iterations; iter++) {
        const temp = k * (1 - iter / iterations) * 10;
        
        // Repulsion
        const nodeArray = Array.from(nodeMap.values());
        for (let i = 0; i < nodeArray.length; i++) {
            const n1 = nodeArray[i];
            for (let j = i + 1; j < nodeArray.length; j++) {
                const n2 = nodeArray[j];
                const dx = n1.x - n2.x;
                const dy = n1.y - n2.y;
                const dist = Math.sqrt(dx * dx + dy * dy) || minDist;
                const force = repulsion / dist;
                const fx = (dx / dist) * force;
                const fy = (dy / dist) * force;
                n1.vx += fx;
                n1.vy += fy;
                n2.vx -= fx;
                n2.vy -= fy;
            }
        }
        
        // Attraction
        edges.forEach(e => {
            const n1 = nodeMap.get(e.source);
            const n2 = nodeMap.get(e.target);
            if (!n1 || !n2) return;
            const dx = n2.x - n1.x;
            const dy = n2.y - n1.y;
            const dist = Math.sqrt(dx * dx + dy * dy) || minDist;
            const force = attraction * dist * (e.data?.weight || 1);
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            n1.vx += fx;
            n1.vy += fy;
            n2.vx -= fx;
            n2.vy -= fy;
        });
        
        // Apply velocity with damping and bounds
        nodeArray.forEach(n => {
            n.vx *= damping;
            n.vy *= damping;
            const speed = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
            if (speed > temp) {
                n.vx = (n.vx / speed) * temp;
                n.vy = (n.vy / speed) * temp;
            }
            n.x += n.vx;
            n.y += n.vy;
            // Keep in bounds
            n.x = Math.max(50, Math.min(width - 50, n.x));
            n.y = Math.max(50, Math.min(height - 50, n.y));
        });
    }
    
    return Array.from(nodeMap.values()).map(n => ({
        ...n,
        position: { x: Math.round(n.x), y: Math.round(n.y) },
        vx: undefined,
        vy: undefined,
    }));
}

/* ------------------------------------------------------------------ */
/* Circular layout (grouped by file)                                   */
/* ------------------------------------------------------------------ */
function applyCircularLayout(nodes, edges, width = 1200, height = 800) {
    const centerX = width / 2;
    const centerY = height / 2;
    const radius = Math.min(width, height) * 0.35;
    
    // Group nodes by file
    const fileGroups = new Map();
    nodes.forEach(n => {
        const file = n.data.file || "unknown";
        if (!fileGroups.has(file)) fileGroups.set(file, []);
        fileGroups.get(file).push(n);
    });
    
    const fileNames = Array.from(fileGroups.keys());
    const angleStep = (2 * Math.PI) / fileNames.length;
    
    const positionedNodes = [];
    fileNames.forEach((file, fileIndex) => {
        const groupNodes = fileGroups.get(file);
        const groupCenterAngle = fileIndex * angleStep - Math.PI / 2;
        const groupRadius = radius * 0.7;
        const groupCenterX = centerX + groupRadius * Math.cos(groupCenterAngle);
        const groupCenterY = centerY + groupRadius * Math.sin(groupCenterAngle);
        
        const nodeAngleStep = (2 * Math.PI) / Math.max(groupNodes.length, 1);
        groupNodes.forEach((node, nodeIndex) => {
            const nodeAngle = nodeIndex * nodeAngleStep - Math.PI / 2;
            const nodeRadius = Math.min(150, 30 + groupNodes.length * 8);
            positionedNodes.push({
                ...node,
                position: {
                    x: groupCenterX + nodeRadius * Math.cos(nodeAngle),
                    y: groupCenterY + nodeRadius * Math.sin(nodeAngle),
                },
            });
        });
    });
    
    return positionedNodes;
}

/* ------------------------------------------------------------------ */
/* Canvas: header, legend, search, React Flow surface                  */
/* ------------------------------------------------------------------ */
function GraphCanvas({ graph }) {
    const theme = useTheme();
    const { rfNodes, rfEdges } = useMemo(() => normalizeGraph(graph, theme), [graph, theme]);
    
    // Filter state
    const [filters, setFilters] = useState({
        files: [],
        nodeTypes: [],
        minDegree: 0,
    });
    const [layoutMode, setLayoutMode] = useState("dagre");
    const [focusedNodeId, setFocusedNodeId] = useState(null);
    const [showFilters, setShowFilters] = useState(true);
    const [showClusters, setShowClusters] = useState(true);
    
    // Apply filters to get visible nodes/edges
    const visibleNodes = useMemo(() => {
        return rfNodes.filter(n => {
            if (filters.files.length && n.data.file && !filters.files.includes(n.data.file)) return false;
            if (filters.nodeTypes.length && !filters.nodeTypes.includes(n.data.nodeType)) return false;
            if (filters.minDegree > 0 && (n.data.degree || 0) < filters.minDegree) return false;
            return true;
        });
    }, [rfNodes, filters]);
    
    const visibleNodeIds = useMemo(() => new Set(visibleNodes.map(n => n.id)), [visibleNodes]);
    
    const visibleEdges = useMemo(() => {
        return rfEdges.filter(e => visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target));
    }, [rfEdges, visibleNodeIds]);
    
    // Focus mode: show only connected subgraph
    const [focusedNodes, setFocusedNodes] = useState(null);
    const [focusedEdges, setFocusedEdges] = useState(null);
    
    const handleFocusNode = useCallback((nodeId) => {
        if (focusedNodeId === nodeId) {
            setFocusedNodeId(null);
            setFocusedNodes(null);
            setFocusedEdges(null);
        } else {
            setFocusedNodeId(nodeId);
            // Find connected nodes (1-hop)
            const connected = new Set([nodeId]);
            rfEdges.forEach(e => {
                if (e.source === nodeId) connected.add(e.target);
                if (e.target === nodeId) connected.add(e.source);
            });
            const focusNodes = rfNodes.filter(n => connected.has(n.id));
            const focusEdges = rfEdges.filter(e => connected.has(e.source) && connected.has(e.target));
            setFocusedNodes(focusNodes);
            setFocusedEdges(focusEdges);
        }
    }, [focusedNodeId, rfNodes, rfEdges]);
    
    // Determine which nodes/edges to use for layout
    const layoutNodes = focusedNodes || visibleNodes;
    const layoutEdges = focusedEdges || visibleEdges;
    
    // Apply layout based on mode
    const initialNodes = useMemo(() => {
        // Skip the expensive layered layout on very large graphs (spec 9/24/33)
        if (layoutMode === "dagre" && layoutNodes.length > DAGRE_NODE_LIMIT) {
            return layoutNodes;
        }
        if (layoutMode === "dagre") {
            return layoutWithDagre(layoutNodes, layoutEdges);
        } else if (layoutMode === "force") {
            return applyForceLayout(layoutNodes, layoutEdges);
        } else if (layoutMode === "circular") {
            // Simple circular layout grouped by file
            return applyCircularLayout(layoutNodes, layoutEdges);
        }
        return layoutWithDagre(layoutNodes, layoutEdges);
    }, [layoutNodes, layoutEdges, layoutMode]);

    const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(layoutEdges);
    const [selectedId, setSelectedId] = useState(null);
    const [query, setQuery] = useState("");
    const { setCenter, zoom } = useReactFlow();
    
    const onFilterChange = useCallback((key, value) => {
        setFilters(prev => ({ ...prev, [key]: value }));
    }, []);
    
    const onClearFocus = useCallback(() => {
        setFocusedNodeId(null);
        setFocusedNodes(null);
        setFocusedEdges(null);
    }, []);

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
    
    const handleNodeDoubleClick = useCallback((_, node) => {
        handleFocusNode(node.id);
    }, [handleFocusNode]);

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
    
    // Calculate stats for visible nodes
    const visibleNodeStats = useMemo(() => {
        const stats = { file: 0, func: 0, cls: 0, module: 0, external: 0 };
        const targetNodes = focusedNodes || visibleNodes;
        for (const n of targetNodes) {
            const t = n.data.nodeType;
            if (t === "file") stats.file++;
            else if (t === "class") stats.cls++;
            else if (t === "module") stats.module++;
            else if (t === "external") stats.external++;
            else stats.func++;
        }
        return stats;
    }, [focusedNodes, visibleNodes]);
    
    const hasFocus = focusedNodeId !== null;
    const displayNodes = focusedNodes || visibleNodes;
    const displayEdges = focusedEdges || visibleEdges;

    return (
        <div className="space-y-3">
            {graph?.truncated && (
                <div className="rounded-xl border border-amber-700/60 bg-amber-950/30 px-4 py-2.5 text-xs text-amber-300">
                    Large graph - rendering {rfNodes.length} of {graph.nodes_total} nodes to keep the page responsive. Search to jump to a node by name.
                </div>
            )}

            {/* Filter Panel */}
            {showFilters && (
                <FilterPanel
                    rfNodes={rfNodes}
                    rfEdges={rfEdges}
                    graph={graph}
                    onFilterChange={onFilterChange}
                    filters={filters}
                    layoutMode={layoutMode}
                    setLayoutMode={setLayoutMode}
                    onFocusNode={handleFocusNode}
                    onClearFocus={onClearFocus}
                    hasFocus={hasFocus}
                    showClusters={showClusters}
                    setShowClusters={setShowClusters}
                />
            )}

            {/* Header: metrics + search */}
            <div className="bg-gray-800/90 border border-gray-700/80 rounded-2xl p-4 shadow-lg backdrop-blur-sm flex flex-col md:flex-row items-stretch md:items-center justify-between gap-4">
                <div className="flex flex-wrap items-center gap-2.5">
                    <div className="flex items-center gap-2 px-3.5 py-1.5 bg-gray-900/90 border border-blue-500/30 rounded-xl text-xs font-semibold text-blue-300">
                        <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>
                        <span>{displayNodes.length} Nodes</span>
                    </div>
                    <div className="flex items-center gap-2 px-3.5 py-1.5 bg-gray-900/90 border border-emerald-500/30 rounded-xl text-xs font-semibold text-emerald-300">
                        <svg className="w-3.5 h-3.5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M14 5l7 7m0 0l-7 7m7-7H3" />
                        </svg>
                        <span>{displayEdges.length} Relationships</span>
                    </div>
                    <div className="hidden lg:flex items-center gap-2 text-xs text-gray-400 pl-2 border-l border-gray-700">
                        {visibleNodeStats.file > 0 && <span>📄 {visibleNodeStats.file} Files</span>}
                        {visibleNodeStats.func > 0 && <span>⚙ {visibleNodeStats.func} Functions</span>}
                        {visibleNodeStats.cls > 0 && <span>🏛 {visibleNodeStats.cls} Classes</span>}
                        {visibleNodeStats.module > 0 && <span>📦 {visibleNodeStats.module} Imports</span>}
                        {visibleNodeStats.external > 0 && <span>🔗 {visibleNodeStats.external} External</span>}
                    </div>
                </div>

                {/* Search + Toggle Filters */}
                <div className="relative flex items-center gap-2">
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
                    <button
                        onClick={() => setShowFilters(!showFilters)}
                        className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                            showFilters
                                ? 'bg-gray-700 text-white border border-gray-600'
                                : 'bg-gray-800 text-gray-400 border border-gray-700 hover:border-gray-600'
                        }`}
                        title={showFilters ? "Hide filters" : "Show filters"}
                    >
                        {showFilters ? "🔧 Filters" : "🔧 Filters"}
                    </button>
                </div>
            </div>

            {/* Focus mode indicator */}
            {hasFocus && (
                <div className="bg-purple-500/20 border border-purple-500/30 rounded-xl p-3 flex items-center justify-between">
                    <div className="flex items-center gap-2 text-sm">
                        <span className="text-purple-300">🎯</span>
                        <span className="text-purple-100 font-medium">Focus Mode Active</span>
                        <span className="text-gray-400">Showing 1-hop neighborhood of selected node</span>
                    </div>
                    <button
                        onClick={onClearFocus}
                        className="px-3 py-1 text-sm bg-purple-500/20 border border-purple-500/30 rounded-lg text-purple-200 hover:bg-purple-500/30 transition-colors"
                    >
                        Clear Focus
                    </button>
                </div>
            )}

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
                    Drag nodes to rearrange · Scroll to zoom · Click a node for details · Double-click to focus
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
                    onNodeDoubleClick={handleNodeDoubleClick}
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
                    {showClusters && (
                        <ClusterBoundaries 
                            nodes={nodes} 
                            clusters={graph?.clusters} 
                            theme={theme}
                            zoom={zoom}
                        />
                    )}
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
                    onFocus={handleFocusNode}
                />
            </div>
        </div>
    );
}

/* ------------------------------------------------------------------ */
/* Cluster Boundaries - renders convex hulls around file clusters       */
/* ------------------------------------------------------------------ */
function ClusterBoundaries({ nodes, clusters, theme, zoom }) {
    const scale = Number.isFinite(zoom) ? zoom : 1;
    if (!clusters?.length || scale < 0.2) return null; // Hide at very low zoom
    
    const nodeMap = new Map(nodes.map(n => [n.id, n]));
    const nodeColors = NODE_COLORS[theme] || NODE_COLORS.dark;
    
    const clusterPaths = clusters.map(cluster => {
        const clusterNodes = cluster.node_ids
            .map(id => nodeMap.get(id))
            .filter(n => n && n.position);
        
        if (clusterNodes.length < 2) return null;
        
        // Compute bounding box with padding
        const positions = clusterNodes.map(n => n.position);
        const minX = Math.min(...positions.map(p => p.x)) - 40;
        const maxX = Math.max(...positions.map(p => p.x)) + NODE_W + 40;
        const minY = Math.min(...positions.map(p => p.y)) - 30;
        const maxY = Math.max(...positions.map(p => p.y)) + NODE_H + 30;
        
        const cx = (minX + maxX) / 2;
        const cy = (minY + maxY) / 2;
        const rx = (maxX - minX) / 2;
        const ry = (maxY - minY) / 2;
        
        // Rounded rectangle path
        const r = 16;
        const path = `
            M ${minX + r} ${minY}
            H ${maxX - r}
            Q ${maxX} ${minY} ${maxX} ${minY + r}
            V ${maxY - r}
            Q ${maxX} ${maxY} ${maxX - r} ${maxY}
            H ${minX + r}
            Q ${minX} ${maxY} ${minX} ${maxY - r}
            V ${minY + r}
            Q ${minX} ${minY} ${minX + r} ${minY}
            Z
        `;
        
        const clusterColor = cluster.color || nodeColors.file;
        
        return (
            <path
                key={cluster.id}
                d={path.trim()}
                fill="none"
                stroke={clusterColor}
                strokeWidth={scale < 0.5 ? 1.5 : 2}
                strokeDasharray="8 6"
                opacity={0.35 + Math.min(scale * 0.3, 0.3)}
                style={{ 
                    filter: `drop-shadow(0 0 ${scale < 0.5 ? 4 : 8}px ${clusterColor}80)`,
                    pointerEvents: "none",
                }}
            />
        );
    }).filter(Boolean);
    
    // Cluster labels
    const clusterLabels = clusters.map(cluster => {
        const clusterNodes = cluster.node_ids
            .map(id => nodeMap.get(id))
            .filter(n => n && n.position);
        
        if (clusterNodes.length === 0) return null;
        
        const positions = clusterNodes.map(n => n.position);
        const minX = Math.min(...positions.map(p => p.x));
        const minY = Math.min(...positions.map(p => p.y));
        
        return (
            <text
                key={cluster.id}
                x={minX + 16}
                y={minY - 12}
                fill={theme === "light" ? "#475569" : "#94a3b8"}
                fontSize={11}
                fontWeight={600}
                fontFamily="Inter, system-ui, sans-serif"
                style={{ 
                    pointerEvents: "none",
                    textShadow: theme === "light" ? "0 0 4px white" : "0 0 4px #0a0e16",
                }}
            >
                📁 {cluster.label}
            </text>
        );
    }).filter(Boolean);
    
    return (
        <ViewportPortal>
            <svg
                className="cluster-boundaries"
                style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    height: "100%",
                    overflow: "visible",
                    pointerEvents: "none",
                }}
            >
                <defs>
                    <filter id="cluster-glow" x="-50%" y="-50%" width="200%" height="200%">
                        <feGaussianBlur stdDeviation={3} result="blur"/>
                        <feMerge>
                            <feMergeNode in="blur"/>
                            <feMergeNode in="SourceGraphic"/>
                        </feMerge>
                    </filter>
                </defs>
                {clusterPaths}
                {clusterLabels}
            </svg>
        </ViewportPortal>
    );
}

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
