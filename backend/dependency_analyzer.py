"""
CodeOracle - Enhanced Multi-Language Dependency Graph Builder
Constructs semantic call graphs, import dependencies, and Mermaid diagrams from static AST models.
"""
import os
import re
import logging
from typing import Dict, List, Any, Set, Tuple
from dataclasses import dataclass, field

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
class DependencyNode:
    """A node in the dependency graph."""
    id: str
    name: str
    node_type: str  # file, function, class, method, module, external
    file: str
    line: int = 0
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.node_type,
            "file": self.file,
            "line": self.line,
            "description": self.description,
        }


@dataclass
class DependencyEdge:
    """An edge in the dependency graph."""
    source: str
    target: str
    edge_type: str  # calls, imports, contains
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from": self.source,
            "to": self.target,
            "type": self.edge_type,
            "label": self.label or self.edge_type,
        }


@dataclass
class DependencyGraph:
    """Complete dependency graph."""
    nodes: List[DependencyNode]
    edges: List[DependencyEdge]
    mermaid: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)
    legend: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "mermaid": self.mermaid,
            "stats": self.stats,
            "legend": self.legend,
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


def build_dependency_graph(parsed_files: List[Dict[str, Any]], max_nodes: int = 250) -> DependencyGraph:
    """
    Build multi-file dependency and call graph from static analysis models.
    Supports Python and JavaScript/TypeScript files with clean Mermaid output.

    Improvements over the naive builder:
    - Calls are resolved to real in-project functions/methods when possible
      (instead of always creating a duplicate "external" node).
    - Edges are deduplicated.
    - Every node carries a plain-English description; every edge carries a label,
      so the UI can explain the graph to first-time users.
    """
    nodes_set: Set[str] = set()
    node_info: Dict[str, DependencyNode] = {}
    edge_keys: Set[Tuple[str, str, str]] = set()
    edges: List[DependencyEdge] = []

    # name -> list of node ids defining that plain name (functions/methods)
    defined_by_name: Dict[str, List[str]] = {}
    # (filename, plain name) -> node id (fast same-file resolution)
    defined_by_file: Dict[Tuple[str, str], str] = {}

    def ensure_node(node: DependencyNode) -> bool:
        if node.id in nodes_set:
            return True
        if len(nodes_set) >= max_nodes:
            return False
        nodes_set.add(node.id)
        node_info[node.id] = node
        return True

    def add_edge(source: str, target: str, edge_type: str, label: str = "") -> None:
        if not source or not target or source == target:
            return
        if source not in nodes_set or target not in nodes_set:
            return
        key = (source, target, edge_type)
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append(DependencyEdge(source=source, target=target,
                                    edge_type=edge_type, label=label or edge_type))

    def register_definition(filename: str, plain_name: str, node_id: str) -> None:
        if not plain_name:
            return
        defined_by_name.setdefault(plain_name, [])
        if node_id not in defined_by_name[plain_name]:
            defined_by_name[plain_name].append(node_id)
        defined_by_file.setdefault((filename, plain_name), node_id)

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
                add_edge(caller_id, target, "calls", "calls")

        for cls in file_data.get("classes", []):
            class_name = cls.get("name", "")
            for method in cls.get("methods", []):
                caller_id = sanitize_node_name(f"{filename}_{class_name}_{method.get('name', '')}")
                for call in method.get("calls", []):
                    target = resolve_call(call, filename, caller_id)
                    add_edge(caller_id, target, "calls", "calls")

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
    legend = {"nodes": NODE_LEGEND, "edges": EDGE_LEGEND}

    return DependencyGraph(
        nodes=nodes,
        edges=edges,
        mermaid=mermaid,
        stats=stats,
        legend=legend,
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
