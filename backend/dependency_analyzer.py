"""
CodeOracle - Enhanced Multi-Language Dependency Graph Builder
Constructs semantic call graphs, import dependencies, and Mermaid diagrams from static AST models.
"""
import os
import re
import logging
from typing import Dict, List, Any, Set
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
    'Integer', 'Long', 'Double', 'Float', 'Byte', 'Short', 'Char', 'Boolean',
    'Byte', 'Short', 'Char', 'Boolean', 'Byte', 'Short', 'Char', 'Boolean',
    'String', 'StringBuilder', 'StringBuffer', 'StringTokenizer',
    'Math', 'StrictMath', 'System', 'Integer', 'Long', 'Double', 'Float',
    'Character', 'Class', 'Exception', 'Runtime', 'SecurityException'
}


@dataclass
class DependencyNode:
    """A node in the dependency graph."""
    id: str
    name: str
    node_type: str  # file, function, class, method, module
    file: str
    line: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.node_type,
            "file": self.file,
            "line": self.line
        }


@dataclass
class DependencyEdge:
    """An edge in the dependency graph."""
    source: str
    target: str
    edge_type: str  # calls, imports, contains, inherits
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "from": self.source,
            "to": self.target,
            "type": self.edge_type
        }


@dataclass
class DependencyGraph:
    """Complete dependency graph."""
    nodes: List[DependencyNode]
    edges: List[DependencyEdge]
    mermaid: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "mermaid": self.mermaid
        }


def sanitize_node_name(name: str) -> str:
    """Sanitize node name for Mermaid compatibility."""
    cleaned = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"n_{cleaned}"
    return cleaned


def build_dependency_graph(parsed_files: List[Dict[str, Any]], max_nodes: int = 150) -> DependencyGraph:
    """
    Build multi-file dependency and call graph from static analysis models.
    Supports Python and JavaScript/TypeScript files with clean Mermaid output.
    """
    nodes_set: Set[str] = set()
    edges: List[DependencyEdge] = []
    node_info: Dict[str, DependencyNode] = {}
    
    for file_data in parsed_files:
        if "error" in file_data:
            continue
        
        filename = file_data.get("filename", "")
        file_id = sanitize_node_name(filename)
        
        if file_id not in nodes_set:
            nodes_set.add(file_id)
            node_info[file_id] = DependencyNode(
                id=file_id,
                name=filename,
                node_type="file",
                file=filename
            )

        # File imports
        for imp in file_data.get("imports", []):
            clean_imp = os.path.basename(imp).replace('.py', '').replace('.js', '').replace('.ts', '')
            imp_id = sanitize_node_name(f"imp_{clean_imp}")
            if imp_id not in nodes_set and len(nodes_set) < max_nodes:
                nodes_set.add(imp_id)
                node_info[imp_id] = DependencyNode(
                    id=imp_id,
                    name=clean_imp,
                    node_type="module",
                    file=imp
                )
            if file_id in nodes_set and imp_id in nodes_set:
                edges.append(DependencyEdge(
                    source=file_id,
                    target=imp_id,
                    edge_type="imports"
                ))
        
        # Functions
        for func in file_data.get("functions", []):
            func_name = func.get("name", "")
            func_id = sanitize_node_name(f"{filename}_{func_name}")
            
            if func_id not in nodes_set and len(nodes_set) < max_nodes:
                nodes_set.add(func_id)
                node_info[func_id] = DependencyNode(
                    id=func_id,
                    name=func_name,
                    node_type="function",
                    file=filename,
                    line=func.get("lineno", 0)
                )
            
            if file_id in nodes_set and func_id in nodes_set:
                edges.append(DependencyEdge(
                    source=file_id,
                    target=func_id,
                    edge_type="contains"
                ))

            for call in func.get("calls", []):
                if call in BUILTINS or len(call) <= 1:
                    continue
                call_id = sanitize_node_name(f"call_{call}")
                
                if call_id not in nodes_set and len(nodes_set) < max_nodes:
                    nodes_set.add(call_id)
                    node_info[call_id] = DependencyNode(
                        id=call_id,
                        name=call,
                        node_type="function",
                        file="external"
                    )
                
                if func_id in nodes_set and call_id in nodes_set:
                    edges.append(DependencyEdge(
                        source=func_id,
                        target=call_id,
                        edge_type="calls"
                    ))
        
        # Classes & Methods
        for cls in file_data.get("classes", []):
            class_name = cls.get("name", "")
            class_id = sanitize_node_name(f"{filename}_{class_name}")
            
            if class_id not in nodes_set and len(nodes_set) < max_nodes:
                nodes_set.add(class_id)
                node_info[class_id] = DependencyNode(
                    id=class_id,
                    name=class_name,
                    node_type="class",
                    file=filename
                )
            
            if file_id in nodes_set and class_id in nodes_set:
                edges.append(DependencyEdge(
                    source=file_id,
                    target=class_id,
                    edge_type="contains"
                ))

            for method in cls.get("methods", []):
                method_name = method.get("name", "")
                method_id = sanitize_node_name(f"{filename}_{class_name}_{method_name}")
                
                if method_id not in nodes_set and len(nodes_set) < max_nodes:
                    nodes_set.add(method_id)
                    node_info[method_id] = DependencyNode(
                        id=method_id,
                        name=f"{class_name}.{method_name}",
                        node_type="method",
                        file=filename,
                        line=method.get("lineno", 0)
                    )
                
                if class_id in nodes_set and method_id in nodes_set:
                    edges.append(DependencyEdge(
                        source=class_id,
                        target=method_id,
                        edge_type="contains"
                    ))
                
                for call in method.get("calls", []):
                    if call in BUILTINS or len(call) <= 1:
                        continue
                    call_id = sanitize_node_name(f"call_{call}")
                    
                    if call_id not in nodes_set and len(nodes_set) < max_nodes:
                        nodes_set.add(call_id)
                        node_info[call_id] = DependencyNode(
                            id=call_id,
                            name=call,
                            node_type="function",
                            file="external"
                        )
                    
                    if method_id in nodes_set and call_id in nodes_set:
                        edges.append(DependencyEdge(
                            source=method_id,
                            target=call_id,
                            edge_type="calls"
                        ))
    
    nodes = [node_info[nid] for nid in sorted(nodes_set) if nid in node_info]
    
    # Generate clean, reliable Mermaid diagram code
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
        else:
            mermaid_lines.append(f'  {nid}["⚡ {clean_label}()"]:::funcNode')

    for edge in edges[:250]:
        if edge.edge_type == "imports":
            mermaid_lines.append(f"  {edge.source} -.-> {edge.target}")
        elif edge.edge_type == "contains":
            mermaid_lines.append(f"  {edge.source} --> {edge.target}")
        else:
            mermaid_lines.append(f"  {edge.source} --> {edge.target}")

    mermaid_lines.append("  classDef fileNode fill:#0f172a,stroke:#38bdf8,stroke-width:2px,color:#f8fafc;")
    mermaid_lines.append("  classDef classNode fill:#1e1b4b,stroke:#818cf8,stroke-width:2px,color:#f8fafc;")
    mermaid_lines.append("  classDef modNode fill:#312e81,stroke:#a78bfa,stroke-width:1.5px,color:#f8fafc;")
    mermaid_lines.append("  classDef funcNode fill:#111827,stroke:#34d399,stroke-width:1.5px,color:#f8fafc;")
    
    mermaid = "\n".join(mermaid_lines)
    
    return DependencyGraph(
        nodes=nodes,
        edges=edges,
        mermaid=mermaid
    )


def get_graph_stats(graph: DependencyGraph) -> Dict[str, Any]:
    """Get statistics about the dependency graph."""
    node_types = {}
    for node in graph.nodes:
        node_types[node.node_type] = node_types.get(node.node_type, 0) + 1
    
    edge_types = {}
    for edge in graph.edges:
        edge_types[edge.edge_type] = edge_types.get(edge.edge_type, 0) + 1
    
    return {
        "total_nodes": len(graph.nodes),
        "total_edges": len(graph.edges),
        "node_types": node_types,
        "edge_types": edge_types,
        "mermaid_lines": len(graph.mermaid.split('\n'))
    }
