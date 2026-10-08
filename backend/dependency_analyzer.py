"""
CodeOracle - Enhanced Multi-Language Dependency Graph Builder
Constructs semantic call graphs, import dependencies, and Mermaid diagrams from static AST models.
"""
import os
import re
import logging
import math
from typing import Dict, List, Any, Set, Tuple, Optional
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger("codeoracle")

BUILTINS = {
    # Python builtins
    'print', 'len', 'str', 'int', 'float', 'list', 'dict', 'set', 'tuple',
    'range', 'enumerate', 'zip', 'map', 'filter', 'sorted', 'reversed',
    'abs', 'min', 'max', 'sum', 'round', 'input', 'open', 'type',
    'isinstance', 'hasattr', 'getattr', 'setattr', 'super', 'property',
    'staticmethod', 'classmethod', 'vars', 'dir', 'id', 'hash', 'repr',
    'format', 'bin', 'oct', 'hex', 'chr', 'ord', 'bool', 'any', 'all',
    'iter', 'next', 'callable', 'complex', 'divmod', 'pow', 'slice',
    'object', 'Exception', 'ValueError', 'TypeError', 'KeyError',
    'IndexError', 'AttributeError', 'ImportError', 'FileNotFoundError',
    'NotImplemented', 'Ellipsis', 'None', 'True', 'False',
    'breakpoint', 'exit', 'quit', 'copyright', 'credits',
    'license', 'help', '__name__', '__doc__', '__import__',
    # JavaScript builtins
    'console', 'log', 'error', 'warn', 'info', 'require', 'exports', 'module',
    'Math', 'JSON', 'Promise', 'Object', 'Array', 'String', 'Number', 'Boolean',
    'Error', 'TypeError', 'RangeError', 'setTimeout', 'setInterval', 'clearTimeout',
    'clearInterval', 'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'encodeURI',
    'decodeURI', 'encodeURIComponent', 'decodeURIComponent', 'push', 'pop', 'shift',
    'unshift', 'slice', 'splice', 'concat', 'join', 'indexOf', 'includes', 'forEach',
    'map', 'filter', 'reduce', 'find', 'findIndex', 'some', 'every', 'trim', 'split',
    'replace', 'toLowerCase', 'toUpperCase', 'substring', 'startsWith', 'endsWith',
    # Java builtins
    'System', 'out', 'println', 'printf', 'format', 'exit', 'currentTimeMillis',
    'nanoTime', 'arraycopy', 'equals', 'hashCode', 'toString', 'valueOf',
    'Integer', 'Long', 'Double', 'Float', 'Byte', 'Short', 'Char',
    'StringBuilder', 'StringBuffer', 'StringTokenizer',
    'StrictMath', 'Character', 'Class', 'Runtime', 'SecurityException',
    'setTimeout', 'clearTimeout', 'fetch', 'window', 'document',
}


@dataclass
class GraphFilter:
    """Filter options for graph construction."""
    max_nodes: int = 250
    node_types: Optional[Set[str]] = None
    files: Optional[Set[str]] = None
    min_degree: int = 0
    max_external_nodes: int = 20
    cluster_by_file: bool = True
    importance_threshold: float = 0.0


@dataclass
class DependencyNode:
    """A node in the dependency graph."""
    id: str
    name: str
    node_type: str  # file, function, class, method, module, external
    file: str
    line: int = 0
    description: str = ""
    # Enhanced fields for large graph handling
    importance: float = 0.0
    degree: int = 0
    cluster_id: Optional[str] = None
    is_entry_point: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.node_type,
            "file": self.file,
            "line": self.line,
            "description": self.description,
            "importance": self.importance,
            "degree": self.degree,
            "cluster_id": self.cluster_id,
            "is_entry_point": self.is_entry_point,
        }


@dataclass
class DependencyEdge:
    """An edge in the dependency graph."""
    source: str
    target: str
    edge_type: str  # calls, imports, contains
    label: str = ""
    weight: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from": self.source,
            "to": self.target,
            "type": self.edge_type,
            "label": self.label or self.edge_type,
            "weight": self.weight,
        }


@dataclass
class Cluster:
    """A cluster of related nodes (e.g., by file or module)."""
    id: str
    label: str
    node_ids: List[str]
    node_type: str  # file, module, etc.
    color: str = ""


@dataclass
class DependencyGraph:
    """Complete dependency graph."""
    nodes: List[DependencyNode]
    edges: List[DependencyEdge]
    clusters: List[Cluster] = field(default_factory=list)
    mermaid: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)
    legend: Dict[str, Any] = field(default_factory=dict)
    filter_applied: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "clusters": [
                {
                    "id": c.id,
                    "label": c.label,
                    "node_ids": c.node_ids,
                    "node_type": c.node_type,
                    "color": c.color,
                }
                for c in self.clusters
            ],
            "mermaid": self.mermaid,
            "stats": self.stats,
            "legend": self.legend,
            "filter_applied": self.filter_applied,
            "description": (
                "How to read this graph: each shape is a piece of your code. "
                "Arrows show how they relate — hover any arrow to see the relationship."
            ),
        }


NODE_LEGEND = [
    {"type": "file", "label": "File", "icon": "file", "color": "#38bdf8",
     "description": "A source file from your project."},
    {"type": "class", "label": "Class", "icon": "class", "color": "#818cf8",
     "description": "A class definition found in your code."},
    {"type": "function", "label": "Function", "icon": "function", "color": "#34d399",
     "description": "A function defined in your project."},
    {"type": "method", "label": "Method", "icon": "method", "color": "#a78bfa",
     "description": "A method that lives inside a class."},
    {"type": "module", "label": "Import", "icon": "module", "color": "#f472b6",
     "description": "A module or package your file imports."},
    {"type": "external", "label": "External call", "icon": "external", "color": "#fbbf24",
     "description": "Called by your code but not defined in the analyzed files."},
]

EDGE_LEGEND = [
    {"type": "contains", "label": "contains", "style": "solid", "color": "#64748b",
     "description": "A file contains this class or function."},
    {"type": "imports", "label": "imports", "style": "dashed", "color": "#f472b6",
     "description": "A file loads this module or package."},
    {"type": "calls", "label": "calls", "style": "solid", "color": "#34d399",
     "description": "One function invokes another."},
]


def sanitize_node_name(name: str) -> str:
    """Sanitize node name for Mermaid compatibility."""
    cleaned = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"n_{cleaned}"
    return cleaned


def _describe_node(node_type: str, name: str, file: str, line: int) -> str:
    location = f" ({file}:{line})" if file and line else (f" ({file})" if file else "")
    if node_type == "file":
        return f"Source file {name}{location}"
    if node_type == "class":
        return f"Class {name} defined in {file}"
    if node_type == "function":
        return f"Function {name} defined in {file}{location}"
    if node_type == "method":
        return f"Method {name} defined in {file}{location}"
    if node_type == "module":
        return f"Module imported by your code{location}"
    if node_type == "external":
        return f"Function {name} is called by your code but is not defined in the analyzed files"
    return name


def build_dependency_graph(parsed_files: List[Dict[str, Any]], max_nodes: int = 250,
                            graph_filter: Optional[GraphFilter] = None) -> DependencyGraph:
    """
    Build multi-file dependency and call graph from static analysis models.
    Supports Python and JavaScript/TypeScript files with clean Mermaid output.

    Improvements over the naive builder:
    - Calls are resolved to real in-project functions/methods when possible
      (instead of always creating a duplicate "external" node).
    - Edges are deduplicated.
    - Every node carries a plain-English description; every edge carries a label,
      so the UI can explain the graph to first-time users.
    - Supports filtering, clustering, and importance scoring for large graphs.
    """
    if graph_filter is None:
        graph_filter = GraphFilter(max_nodes=max_nodes)

    nodes_set: Set[str] = set()
    node_info: Dict[str, DependencyNode] = {}
    edge_keys: Set[Tuple[str, str, str]] = set()
    # key -> edge object, so duplicate-edge weight updates stay O(1) instead of
    # scanning the whole edge list (which was O(E) per duplicate).
    edge_index: Dict[Tuple[str, str, str], DependencyEdge] = {}
    edges: List[DependencyEdge] = []

    # name -> list of node ids defining that plain name (functions/methods)
    defined_by_name: Dict[str, List[str]] = {}
    # (filename, plain name) -> node id (fast same-file resolution)
    defined_by_file: Dict[Tuple[str, str], str] = {}
    # Track call counts for importance scoring
    call_counts: Dict[str, int] = defaultdict(int)
    # Track file -> node ids for clustering
    file_to_nodes: Dict[str, List[str]] = defaultdict(list)

    def ensure_node(node: DependencyNode) -> bool:
        if node.id in nodes_set:
            return True
        if len(nodes_set) >= graph_filter.max_nodes:
            return False
        # Check node type filter
        if graph_filter.node_types and node.node_type not in graph_filter.node_types:
            return False
        # Check file filter
        if graph_filter.files and node.file and node.file not in graph_filter.files:
            return False
        nodes_set.add(node.id)
        node_info[node.id] = node
        return True

    def add_edge(source: str, target: str, edge_type: str, label: str = "", weight: float = 1.0) -> None:
        if not source or not target or source == target:
            return
        if source not in nodes_set or target not in nodes_set:
            return
        key = (source, target, edge_type)
        if key in edge_keys:
            # Update weight for existing edge
            existing = edge_index.get(key)
            if existing is not None:
                existing.weight += weight
            return
        edge_keys.add(key)
        new_edge = DependencyEdge(source=source, target=target,
                                  edge_type=edge_type, label=label or edge_type, weight=weight)
        edge_index[key] = new_edge
        edges.append(new_edge)

    def register_definition(filename: str, plain_name: str, node_id: str) -> None:
        if not plain_name:
            return
        defined_by_name.setdefault(plain_name, [])
        if node_id not in defined_by_name[plain_name]:
            defined_by_name[plain_name].append(node_id)
        defined_by_file.setdefault((filename, plain_name), node_id)
        if filename:
            file_to_nodes[filename].append(node_id)

    # ------------------------------------------------------------------
    # Pass 1: register files, classes, functions, methods (definitions)
    # ------------------------------------------------------------------
    for file_data in parsed_files:
        if "error" in file_data:
            continue

        filename = file_data.get("filename", "")
        file_id = sanitize_node_name(filename)

        ensure_node(DependencyNode(
            id=file_id, name=filename, node_type="file", file=filename,
            description=_describe_node("file", filename, filename, 0),
        ))

        for func in file_data.get("functions", []):
            func_name = func.get("name", "")
            func_id = sanitize_node_name(f"{filename}_{func_name}")
            line = func.get("lineno", 0)
            if ensure_node(DependencyNode(
                id=func_id, name=func_name, node_type="function",
                file=filename, line=line,
                description=_describe_node("function", func_name, filename, line),
            )):
                register_definition(filename, func_name, func_id)

        for cls in file_data.get("classes", []):
            class_name = cls.get("name", "")
            class_id = sanitize_node_name(f"{filename}_{class_name}")
            if ensure_node(DependencyNode(
                id=class_id, name=class_name, node_type="class",
                file=filename, line=cls.get("lineno", 0),
                description=_describe_node("class", class_name, filename, cls.get("lineno", 0)),
            )):
                register_definition(filename, class_name, class_id)

            for method in cls.get("methods", []):
                method_name = method.get("name", "")
                method_id = sanitize_node_name(f"{filename}_{class_name}_{method_name}")
                line = method.get("lineno", 0)
                if ensure_node(DependencyNode(
                    id=method_id, name=f"{class_name}.{method_name}", node_type="method",
                    file=filename, line=line,
                    description=_describe_node("method", f"{class_name}.{method_name}", filename, line),
                )):
                    register_definition(filename, method_name, method_id)
                    register_definition(filename, f"{class_name}.{method_name}", method_id)

    # ------------------------------------------------------------------
    # Pass 2: structural edges (contains) and imports
    # ------------------------------------------------------------------
    for file_data in parsed_files:
        if "error" in file_data:
            continue

        filename = file_data.get("filename", "")
        file_id = sanitize_node_name(filename)
        if file_id not in nodes_set:
            continue

        for imp in file_data.get("imports", []):
            clean_imp = os.path.basename(imp).replace('.py', '').replace('.js', '').replace('.ts', '')
            imp_id = sanitize_node_name(f"imp_{clean_imp}")
            ensure_node(DependencyNode(
                id=imp_id, name=clean_imp, node_type="module", file=imp,
                description=_describe_node("module", clean_imp, imp, 0),
            ))
            add_edge(file_id, imp_id, "imports", "imports")

        for func in file_data.get("functions", []):
            func_id = sanitize_node_name(f"{filename}_{func.get('name', '')}")
            add_edge(file_id, func_id, "contains", "contains")

        for cls in file_data.get("classes", []):
            class_name = cls.get("name", "")
            class_id = sanitize_node_name(f"{filename}_{class_name}")
            add_edge(file_id, class_id, "contains", "contains")

            for method in cls.get("methods", []):
                method_id = sanitize_node_name(f"{filename}_{class_name}_{method.get('name', '')}")
                add_edge(class_id, method_id, "contains", "contains")

    # ------------------------------------------------------------------
    # Pass 3: call edges, resolved to in-project definitions when possible
    # ------------------------------------------------------------------
    def resolve_call(call_name: str, caller_file: str, caller_id: str) -> str:
        """Return the target node id for a call, creating an external node if unknown."""
        bare = call_name.split(".")[-1] if call_name else ""
        if not bare or bare in BUILTINS or len(bare) <= 1:
            return ""

        same_file_id = defined_by_file.get((caller_file, bare))
        if same_file_id and same_file_id != caller_id:
            return same_file_id

        candidates = defined_by_name.get(bare, [])
        if len(candidates) == 1 and candidates[0] != caller_id:
            return candidates[0]

        ext_id = sanitize_node_name(f"ext_{bare}")
        ensure_node(DependencyNode(
            id=ext_id, name=bare, node_type="external", file="",
            description=_describe_node("external", bare, "", 0),
        ))
        return ext_id if ext_id in nodes_set and ext_id != caller_id else ""

    for file_data in parsed_files:
        if "error" in file_data:
            continue

        filename = file_data.get("filename", "")

        for func in file_data.get("functions", []):
            caller_id = sanitize_node_name(f"{filename}_{func.get('name', '')}")
            for call in func.get("calls", []):
                target = resolve_call(call, filename, caller_id)
                if target:
                    call_counts[caller_id] += 1
                    call_counts[target] += 1
                    add_edge(caller_id, target, "calls", "calls")

        for cls in file_data.get("classes", []):
            class_name = cls.get("name", "")
            for method in cls.get("methods", []):
                caller_id = sanitize_node_name(f"{filename}_{class_name}_{method.get('name', '')}")
                for call in method.get("calls", []):
                    target = resolve_call(call, filename, caller_id)
                    if target:
                        call_counts[caller_id] += 1
                        call_counts[target] += 1
                        add_edge(caller_id, target, "calls", "calls")

    # ------------------------------------------------------------------
    # Post-processing: importance scoring, entry points, clustering
    # ------------------------------------------------------------------
    
    # Calculate degree for each node
    degree: Dict[str, int] = defaultdict(int)
    in_degree: Dict[str, int] = defaultdict(int)
    out_degree: Dict[str, int] = defaultdict(int)
    for edge in edges:
        degree[edge.source] += 1
        degree[edge.target] += 1
        out_degree[edge.source] += 1
        in_degree[edge.target] += 1

    # Update node info with degree and importance
    max_degree = max(degree.values()) if degree else 1
    max_calls = max(call_counts.values()) if call_counts else 1
    
    for node in node_info.values():
        node.degree = degree.get(node.id, 0)
        # Importance = normalized degree + normalized call count + entry point bonus
        deg_score = node.degree / max_degree if max_degree > 0 else 0
        call_score = call_counts.get(node.id, 0) / max_calls if max_calls > 0 else 0
        node.importance = round(0.6 * deg_score + 0.4 * call_score, 3)
        
        # Entry points: nodes with high out-degree but low in-degree (or just high degree)
        if node.degree > 0:
            in_deg = in_degree.get(node.id, 0)
            out_deg = out_degree.get(node.id, 0)
            if out_deg >= 2 and in_deg <= 1:
                node.is_entry_point = True

    # Filter by minimum degree if specified
    if graph_filter.min_degree > 0:
        filtered_nodes = {
            nid: node for nid, node in node_info.items()
            if node.degree >= graph_filter.min_degree
        }
        # Also keep file nodes that contain filtered nodes
        kept_files = {
            node_info[nid].file for nid in filtered_nodes
            if nid in node_info and node_info[nid].file
        }
        for node in list(node_info.values()):
            if node.node_type == "file" and node.file in kept_files:
                filtered_nodes[node.id] = node
        node_info = filtered_nodes
        nodes_set = set(node_info.keys())
        # Filter edges
        edges = [e for e in edges if e.source in nodes_set and e.target in nodes_set]

    # Limit external nodes - aggregate excess into a single "Other External" node
    external_nodes = [n for n in node_info.values() if n.node_type == "external"]
    if len(external_nodes) > graph_filter.max_external_nodes:
        # Keep top external nodes by degree, aggregate rest
        external_nodes.sort(key=lambda n: n.degree, reverse=True)
        keep_external = set(n.id for n in external_nodes[:graph_filter.max_external_nodes])
        remove_external = set(n.id for n in external_nodes[graph_filter.max_external_nodes:])
        
        # Create aggregated external node
        agg_id = sanitize_node_name("ext_Other External")
        agg_node = DependencyNode(
            id=agg_id, name="Other External", node_type="external", file="",
            description="Aggregated external calls not shown individually",
            degree=sum(n.degree for n in external_nodes[graph_filter.max_external_nodes:]),
            importance=0.1,
        )
        node_info[agg_id] = agg_node
        nodes_set.add(agg_id)
        
        # Remove aggregated external nodes from node_info and nodes_set
        for ext_id in remove_external:
            node_info.pop(ext_id, None)
            nodes_set.discard(ext_id)
        
        # Redirect edges from removed external nodes to aggregated node
        new_edges = []
        for e in edges:
            if e.target in keep_external or e.target == agg_id:
                new_edges.append(e)
            elif e.target in remove_external:
                # Redirect to aggregated
                e.target = agg_id
                new_edges.append(e)
            else:
                new_edges.append(e)
        edges = new_edges

    # Create clusters (group by file)
    clusters = []
    if graph_filter.cluster_by_file:
        cluster_colors = ["#38bdf8", "#818cf8", "#34d399", "#a78bfa", "#f472b6", "#fbbf24", "#fb923c", "#22d3ee"]
        for i, (filename, node_ids) in enumerate(file_to_nodes.items()):
            # Only include nodes that made it into the final graph
            cluster_node_ids = [nid for nid in node_ids if nid in nodes_set]
            if cluster_node_ids:
                # Find the file node
                file_node_id = sanitize_node_name(filename)
                if file_node_id in nodes_set:
                    clusters.append(Cluster(
                        id=f"cluster_{file_node_id}",
                        label=os.path.basename(filename),
                        node_ids=cluster_node_ids,
                        node_type="file",
                        color=cluster_colors[i % len(cluster_colors)],
                    ))

    nodes = [node_info[nid] for nid in sorted(nodes_set) if nid in node_info]

    # ------------------------------------------------------------------
    # Mermaid diagram (kept for backward compatibility / raw view)
    # ------------------------------------------------------------------
    mermaid_lines = ["graph TD"]
    for nid in sorted(nodes_set):
        n = node_info.get(nid)
        if not n:
            continue
        clean_label = n.name.replace('"', "'").replace('(', '').replace(')', '')
        if n.node_type == "file":
            mermaid_lines.append(f'  {nid}["📁 {clean_label}"]:::fileNode')
        elif n.node_type == "class":
            mermaid_lines.append(f'  {nid}["🏛️ {clean_label}"]:::classNode')
        elif n.node_type == "module":
            mermaid_lines.append(f'  {nid}["📦 {clean_label}"]:::modNode')
        elif n.node_type == "external":
            mermaid_lines.append(f'  {nid}["🔗 {clean_label}()"]:::extNode')
        else:
            mermaid_lines.append(f'  {nid}["⚡ {clean_label}()"]:::funcNode')

    for edge in edges[:400]:
        if edge.edge_type == "imports":
            mermaid_lines.append(f"  {edge.source} -.-> {edge.target}")
        else:
            mermaid_lines.append(f"  {edge.source} --> {edge.target}")

    mermaid_lines.append("  classDef fileNode fill:#0f172a,stroke:#38bdf8,stroke-width:2px,color:#f8fafc;")
    mermaid_lines.append("  classDef classNode fill:#1e1b4b,stroke:#818cf8,stroke-width:2px,color:#f8fafc;")
    mermaid_lines.append("  classDef modNode fill:#312e81,stroke:#a78bfa,stroke-width:1.5px,color:#f8fafc;")
    mermaid_lines.append("  classDef funcNode fill:#111827,stroke:#34d399,stroke-width:1.5px,color:#f8fafc;")
    mermaid_lines.append("  classDef extNode fill:#1c1917,stroke:#fbbf24,stroke-width:1.5px,color:#f8fafc;")

    mermaid = "\n".join(mermaid_lines)

    stats = get_graph_stats_for(nodes, edges)
    stats["clusters"] = len(clusters)
    stats["entry_points"] = sum(1 for n in nodes if n.is_entry_point)
    stats["external_aggregated"] = len(external_nodes) > graph_filter.max_external_nodes
    legend = {"nodes": NODE_LEGEND, "edges": EDGE_LEGEND}

    filter_applied = {
        "max_nodes": graph_filter.max_nodes,
        "node_types": list(graph_filter.node_types) if graph_filter.node_types else None,
        "files": list(graph_filter.files) if graph_filter.files else None,
        "min_degree": graph_filter.min_degree,
        "max_external_nodes": graph_filter.max_external_nodes,
        "cluster_by_file": graph_filter.cluster_by_file,
    }

    return DependencyGraph(
        nodes=nodes,
        edges=edges,
        clusters=clusters,
        mermaid=mermaid,
        stats=stats,
        legend=legend,
        filter_applied=filter_applied,
    )


def get_graph_stats_for(nodes: List[DependencyNode], edges: List[DependencyEdge]) -> Dict[str, Any]:
    """Compute graph statistics from raw node/edge lists."""
    node_types: Dict[str, int] = {}
    for node in nodes:
        node_types[node.node_type] = node_types.get(node.node_type, 0) + 1

    edge_types: Dict[str, int] = {}
    for edge in edges:
        edge_types[edge.edge_type] = edge_types.get(edge.edge_type, 0) + 1

    return {
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "node_types": node_types,
        "edge_types": edge_types,
    }


def get_graph_stats(graph: DependencyGraph) -> Dict[str, Any]:
    """Get statistics about the dependency graph."""
    stats = get_graph_stats_for(graph.nodes, graph.edges)
    stats["mermaid_lines"] = len(graph.mermaid.split('\n')) if graph.mermaid else 0
    return stats
