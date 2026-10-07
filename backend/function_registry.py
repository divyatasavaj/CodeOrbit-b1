"""
Normalized function registry, priority scoring and batch planning.

The registry is a thin, deterministic adapter over the AST/js parser output so
every downstream stage (caching, batching, the AI pipeline) works with one
consistent shape and never re-derives identity or priority via the LLM.
"""
import hashlib
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Tuple

import config

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"

_PRIORITY_RANK = {PRIORITY_LOW: 0, PRIORITY_MEDIUM: 1, PRIORITY_HIGH: 2}

_ENTRY_POINT_HINTS = (
    "main", "run", "start", "serve", "app", "handler", "handle_", "route",
    "endpoint", "view", "cli", "execute", "process", "dispatch", "on_",
)
_API_HINTS = (
    "api", "request", "response", "controller", "router", "get_", "post_",
    "put_", "delete_", "fetch", "send", "handle", "endpoint",
)
_TRIVIAL_NAME_PREFIXES = ("get_", "set_", "is_", "has_", "to_", "as_")
_TRIVIAL_DUNDERS = {"__str__", "__repr__", "__len__", "__eq__", "__hash__", "__bool__"}


def _language_for(path: str) -> str:
    ext = os.path.splitext(path or "")[1].lower()
    return "python" if ext == ".py" else "javascript"


def source_hash(source_code: str) -> str:
    return hashlib.sha256((source_code or "").encode("utf-8", "replace")).hexdigest()


def make_function_id(file_path: str, qualified_name: str, source_code: str) -> str:
    raw = f"{file_path}:{qualified_name}:{source_hash(source_code)}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:20]


@dataclass
class RegisteredFunction:
    """Normalized internal representation of one function/method."""

    id: str
    name: str
    qualified_name: str
    file_path: str
    filename: str
    language: str
    class_name: str
    source_code: str
    start_line: int
    end_line: int
    args: List[str]
    imports: List[str]
    calls: List[str]
    complexity: int
    line_count: int
    decorators: List[str]
    has_return: bool
    is_method: bool
    source_file: str
    priority: str = PRIORITY_MEDIUM
    priority_score: int = 0
    caller_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_llm_dict(self) -> Dict[str, Any]:
        """Compact payload sent to the model (no unrelated files)."""
        return {
            "function_id": self.id,
            "name": self.qualified_name,
            "language": self.language,
            "class_name": self.class_name or None,
            "parameters": self.args,
            "imports": self.imports,
            "called_functions": self.calls,
            "complexity": self.complexity,
            "source": self.source_code,
        }


def _body_statement_count(source_code: str) -> int:
    lines = [l.strip() for l in (source_code or "").splitlines()[1:]]
    return len([l for l in lines if l and not l.startswith("#") and l != "pass"])


def _score_function(func: RegisteredFunction) -> int:
    name = func.name or ""
    name_lower = name.lower()
    score = 0

    # Public surface
    if not name.startswith("_"):
        score += 40
    else:
        score -= 15

    # Entry points / handlers
    if any(h in name_lower for h in _ENTRY_POINT_HINTS):
        score += 25
    # API-ish surface
    if any(h in name_lower for h in _API_HINTS):
        score += 12

    # Class constructors matter structurally
    if name == "__init__":
        score += 18
    if func.is_method and not name.startswith("_"):
        score += 6

    # Route/endpoint decorators are strong entry-point signals
    decorators = " ".join(func.decorators or []).lower()
    if any(k in decorators for k in ("route", "get", "post", "put", "delete", "endpoint", "api", "task", "command")):
        score += 22

    # Exported JS symbols
    if func.language == "javascript" and ("export " in (func.source_code or "")[:400]):
        score += 20

    # Called by many other functions -> higher centrality
    score += min(func.caller_count * 3, 30)

    # Complexity / branching
    score += min(max(func.complexity - 1, 0) * 2, 20)

    # Long functions carry more behaviour worth explaining
    if func.line_count >= 30:
        score += 8
    elif func.line_count >= 15:
        score += 4

    # Trivial / boilerplate downranking
    body_stmts = _body_statement_count(func.source_code)
    if name in _TRIVIAL_DUNDERS:
        score -= 30
    if name_lower.startswith(_TRIVIAL_NAME_PREFIXES) and body_stmts <= 2:
        score -= 25
    if body_stmts == 0:
        score -= 20
    if "generated" in (func.file_path or "").lower() and body_stmts <= 2:
        score -= 15

    return score


def _priority_for_score(score: int) -> str:
    if score >= 75:
        return PRIORITY_HIGH
    if score >= 40:
        return PRIORITY_MEDIUM
    return PRIORITY_LOW


def build_registry(parsed_files: List[Dict[str, Any]]) -> List[RegisteredFunction]:
    """Convert parsed file analyses into a normalized, priority-scored registry."""
    registry: List[RegisteredFunction] = []

    for file_data in parsed_files or []:
        if not file_data or "error" in file_data:
            continue
        file_path = file_data.get("filepath", "") or file_data.get("filename", "")
        filename = file_data.get("filename", "") or os.path.basename(file_path)
        imports = list(file_data.get("imports", []) or [])
        language = _language_for(file_path)

        entries: List[Tuple[Dict[str, Any], str]] = []
        for func in file_data.get("functions", []) or []:
            entries.append((func, ""))
        for cls in file_data.get("classes", []) or []:
            cls_name = cls.get("name", "")
            for method in cls.get("methods", []) or []:
                entries.append((method, cls_name))

        for func, class_name in entries:
            name = func.get("name", "") or "unknown"
            qualified = f"{class_name}.{name}" if class_name else name
            source_code = func.get("body", "") or ""
            decorators = list(func.get("decorators", []) or [])
            display = qualified

            registered = RegisteredFunction(
                id=make_function_id(file_path, qualified, source_code),
                name=name,
                qualified_name=display,
                file_path=file_path,
                filename=filename,
                language=language,
                class_name=class_name,
                source_code=source_code,
                start_line=func.get("lineno", 0) or 0,
                end_line=func.get("end_lineno", 0) or 0,
                args=list(func.get("args", []) or []),
                imports=imports,
                calls=list(func.get("calls", []) or []),
                complexity=int(func.get("complexity", 1) or 1),
                line_count=int(func.get("line_count", 0) or 0),
                decorators=decorators,
                has_return=bool(func.get("has_return", False)),
                is_method=bool(class_name),
                source_file=file_path,
            )
            registry.append(registered)

    _apply_caller_counts(registry)
    for func in registry:
        func.priority_score = _score_function(func)
        func.priority = _priority_for_score(func.priority_score)
    return registry


def _apply_caller_counts(registry: List[RegisteredFunction]) -> None:
    """Approximate in-degree of the call graph using static call names."""
    by_name: Dict[str, List[RegisteredFunction]] = {}
    for func in registry:
        by_name.setdefault(func.name, []).append(func)
    for func in registry:
        seen = set()
        for called in func.calls or []:
            if called in seen:
                continue
            seen.add(called)
            for target in by_name.get(called, ()):
                if target.id != func.id:
                    target.caller_count += 1


def registry_to_job_functions(registry: List[RegisteredFunction]) -> List[Dict[str, Any]]:
    """Public/serialisable view of the registry stored in the job record."""
    return [f.to_dict() for f in registry]


# --------------------------------------------------------------------------
# Batch planning
# --------------------------------------------------------------------------
def order_functions(registry: Iterable[RegisteredFunction]) -> List[RegisteredFunction]:
    """Order so the most important functions are analyzed first while keeping
    related functions (same file, then same class) adjacent for batching."""
    import collections

    by_file: Dict[str, List[RegisteredFunction]] = collections.defaultdict(list)
    for func in registry:
        by_file[func.file_path or func.filename].append(func)

    ordered_files = sorted(
        by_file.items(),
        key=lambda kv: (-max(f.priority_score for f in kv[1]), kv[0]),
    )

    result: List[RegisteredFunction] = []
    for _, funcs in ordered_files:
        funcs.sort(key=lambda f: (-f.priority_score, f.class_name, f.start_line))
        result.extend(funcs)
    return result


def create_batches(
    functions: List[RegisteredFunction],
    batch_size: int = None,
    max_chars: int = None,
) -> List[List[RegisteredFunction]]:
    """Split functions into batches bounded by both count and approximate size.

    A single oversized function still forms its own batch (truncation happens at
    prompt build time) so batching never drops work or loops forever.
    """
    batch_size = max(1, batch_size or config.LLM_BATCH_SIZE)
    max_chars = max(2000, max_chars or config.LLM_BATCH_MAX_CHARS)

    batches: List[List[RegisteredFunction]] = []
    current: List[RegisteredFunction] = []
    current_chars = 0

    for func in functions:
        size = len(func.source_code or "") + 400  # prompt scaffolding overhead
        if current and (len(current) >= batch_size or current_chars + size > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(func)
        current_chars += size

    if current:
        batches.append(current)
    return batches


def prioritize(registry: List[RegisteredFunction], min_priority: str = PRIORITY_LOW) -> List[RegisteredFunction]:
    """Filter by a priority floor (used to skip LOW work when configured)."""
    floor = _PRIORITY_RANK.get((min_priority or PRIORITY_LOW).lower(), 0)
    return [f for f in registry if _PRIORITY_RANK.get(f.priority, 0) >= floor]
