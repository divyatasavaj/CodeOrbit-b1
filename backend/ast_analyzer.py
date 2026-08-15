"""
Enhanced AST analyzer with comprehensive static analysis.
Extracts detailed information without LLM calls.
"""
import ast
import os
import logging
from typing import Dict, List, Any
from dataclasses import dataclass, field

logger = logging.getLogger("codeoracle")


@dataclass
class FunctionInfo:
    """Detailed function information from AST."""
    name: str
    args: List[str]
    lineno: int
    end_lineno: int
    body: str
    calls: List[str]
    imports_used: List[str]
    complexity: int
    line_count: int
    has_return: bool
    decorators: List[str]
    is_method: bool = False
    class_name: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "args": self.args,
            "lineno": self.lineno,
            "end_lineno": self.end_lineno,
            "body": self.body,
            "calls": self.calls,
            "imports_used": self.imports_used,
            "complexity": self.complexity,
            "line_count": self.line_count,
            "has_return": self.has_return,
            "decorators": self.decorators,
            "is_method": self.is_method,
            "class_name": self.class_name
        }


@dataclass
class ClassInfo:
    """Detailed class information from AST."""
    name: str
    lineno: int
    end_lineno: int
    methods: List[FunctionInfo]
    bases: List[str]
    decorators: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "lineno": self.lineno,
            "end_lineno": self.end_lineno,
            "methods": [m.to_dict() for m in self.methods],
            "bases": self.bases,
            "decorators": self.decorators
        }


@dataclass
class FileAnalysis:
    """Complete analysis of a single file."""
    filepath: str
    filename: str
    source: str
    line_count: int
    functions: List[FunctionInfo]
    classes: List[ClassInfo]
    imports: List[str]
    global_variables: List[str]
    ast_nodes: int
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "filepath": self.filepath,
            "filename": self.filename,
            "line_count": self.line_count,
            "functions": [f.to_dict() for f in self.functions],
            "classes": [c.to_dict() for c in self.classes],
            "imports": self.imports,
            "global_variables": self.global_variables,
            "ast_nodes": self.ast_nodes
        }


def calculate_complexity(node: ast.AST) -> int:
    """Calculate cyclomatic complexity of a function."""
    complexity = 1
    
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            complexity += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            complexity += 1
    
    return complexity


def extract_imports(tree: ast.Module) -> List[str]:
    """Extract all imports from AST."""
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            for alias in node.names:
                imports.append(f"{module}.{alias.name}" if module else alias.name)
    return imports


def extract_global_variables(tree: ast.Module, source: str) -> List[str]:
    """Extract global variables from module."""
    globals_list = []
    lines = source.splitlines()
    
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    globals_list.append(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                globals_list.append(node.target.id)
    
    return globals_list


def extract_decorators(node: ast.AST) -> List[str]:
    """Extract decorator names from a function/class node."""
    decorators = []
    for decorator in getattr(node, 'decorator_list', []):
        if isinstance(decorator, ast.Name):
            decorators.append(decorator.id)
        elif isinstance(decorator, ast.Attribute):
            decorators.append(f"{ast.dump(decorator.value)}.{decorator.attr}")
        elif isinstance(decorator, ast.Call):
            if isinstance(decorator.func, ast.Name):
                decorators.append(decorator.func.id)
    return decorators


def analyze_function(node: ast.AST, source: str, is_method: bool = False, class_name: str = "") -> FunctionInfo:
    """Analyze a function/method AST node."""
    args = [arg.arg for arg in getattr(node, 'args', ast.arguments()).args]
    
    start_line = getattr(node, 'lineno', 1)
    end_line = getattr(node, 'end_lineno', start_line)
    lines = source.splitlines()
    body = "\n".join(lines[max(0, start_line - 1):end_line])
    
    calls = []
    imports_used = []
    has_return = False
    
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                calls.append(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                calls.append(child.func.attr)
        elif isinstance(child, ast.Return):
            has_return = True
        elif isinstance(child, ast.Name) and child.id in ('print', 'len', 'str', 'int', 'float', 'list', 'dict'):
            imports_used.append(child.id)
    
    return FunctionInfo(
        name=getattr(node, 'name', 'unknown'),
        args=args,
        lineno=start_line,
        end_lineno=end_line,
        body=body,
        calls=list(set(calls)),
        imports_used=list(set(imports_used)),
        complexity=calculate_complexity(node),
        line_count=end_line - start_line + 1,
        has_return=has_return,
        decorators=extract_decorators(node),
        is_method=is_method,
        class_name=class_name
    )


def analyze_class(node: ast.ClassDef, source: str) -> ClassInfo:
    """Analyze a class AST node."""
    methods = []
    
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method = analyze_function(item, source, is_method=True, class_name=node.name)
            methods.append(method)
    
    bases = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            bases.append(base.id)
        elif isinstance(base, ast.Attribute):
            bases.append(base.attr)
    
    return ClassInfo(
        name=node.name,
        lineno=getattr(node, 'lineno', 1),
        end_lineno=getattr(node, 'end_lineno', 1),
        methods=methods,
        bases=bases,
        decorators=extract_decorators(node)
    )


def analyze_file(filepath: str) -> FileAnalysis:
    """Perform comprehensive AST analysis of a Python file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        logger.error(f"Failed to read {filepath}: {e}")
        raise
    
    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError as e:
        logger.error(f"Syntax error in {filepath}: {e}")
        raise
    
    filename = os.path.basename(filepath)
    lines = source.splitlines()
    
    functions = []
    classes = []
    
    ast_node_count = sum(1 for _ in ast.walk(tree))
    
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = analyze_function(node, source)
            functions.append(func)
        elif isinstance(node, ast.ClassDef):
            cls = analyze_class(node, source)
            classes.append(cls)
    
    return FileAnalysis(
        filepath=filepath,
        filename=filename,
        source=source,
        line_count=len(lines),
        functions=functions,
        classes=classes,
        imports=extract_imports(tree),
        global_variables=extract_global_variables(tree, source),
        ast_nodes=ast_node_count
    )


def find_refactoring_candidates(analysis: FileAnalysis) -> List[Dict[str, Any]]:
    """Identify functions that would benefit from refactoring."""
    candidates = []
    
    for func in analysis.functions:
        issues = []
        
        if func.complexity > 5:
            issues.append(f"High complexity: {func.complexity}")
        
        if func.line_count > 30:
            issues.append(f"Long function: {func.line_count} lines")
        
        if func.body.count('    ') > 5:
            issues.append("Deep nesting detected")
        
        if not func.has_return and func.line_count > 5:
            issues.append("Missing return statement")
        
        if issues:
            candidates.append({
                "name": func.name,
                "issues": issues,
                "complexity": func.complexity,
                "line_count": func.line_count,
                "priority": "high" if func.complexity > 10 else "medium"
            })
    
    return candidates


def detect_breaking_changes(orig_func: Dict[str, Any], refactored_code: str, is_js: bool = False) -> List[Dict[str, str]]:
    """
    Statically analyzes original vs refactored function code to detect breaking changes.
    Checks signature changes, removed functions, renamed parameters, return semantics, and async conversions.
    """
    import re
    changes: List[Dict[str, str]] = []
    func_name = orig_func.get("name") or orig_func.get("display_name", "unknown")
    orig_args = [a.split('=')[0].strip() for a in orig_func.get("args", []) if a.strip() and a.strip() not in ("self", "cls")]
    orig_body = orig_func.get("body", "")

    if not refactored_code or not refactored_code.strip():
        return [{
            "change": "Refactored implementation is empty or missing.",
            "risk": "HIGH",
            "why": "Empty function body replaces working implementation."
        }]

    # 1. Check if function name still exists
    if func_name not in refactored_code:
        changes.append({
            "change": f"Public function `{func_name}` was renamed or removed.",
            "risk": "HIGH",
            "why": f"Callers expecting `{func_name}()` will encounter a NameError / ReferenceError."
        })
        return changes

    # 2. Extract refactored parameters
    refactored_args = []
    if is_js:
        match = re.search(rf'(?:function\s+{re.escape(func_name)}|const\s+{re.escape(func_name)}\s*=\s*(?:async\s+)?(?:\(([^)]*)\)|(\w+)))', refactored_code)
        if match:
            raw_args = match.group(1) or match.group(2) or ""
            refactored_args = [a.split('=')[0].strip() for a in raw_args.split(',') if a.strip()]
    else:
        try:
            tree = ast.parse(refactored_code)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                    refactored_args = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
                    break
        except Exception:
            match = re.search(rf'def\s+{re.escape(func_name)}\s*\(([^)]*)\)', refactored_code)
            if match:
                refactored_args = [a.split('=')[0].strip() for a in match.group(1).split(',') if a.strip() and a.strip() not in ("self", "cls")]

    # Compare parameters
    removed_args = [arg for arg in orig_args if arg not in refactored_args]
    if removed_args:
        changes.append({
            "change": f"Parameter(s) removed from signature: {', '.join(removed_args)}.",
            "risk": "HIGH",
            "why": "Existing call sites passing these positional or keyword arguments will raise TypeError."
        })

    added_args = [arg for arg in refactored_args if arg not in orig_args]
    if added_args and len(refactored_args) > len(orig_args):
        changes.append({
            "change": f"New parameter(s) added: {', '.join(added_args)}.",
            "risk": "HIGH",
            "why": "Existing call sites with fewer arguments may fail if parameters are mandatory."
        })
    elif added_args and len(refactored_args) == len(orig_args):
        changes.append({
            "change": f"Parameters renamed: {', '.join(orig_args)} -> {', '.join(refactored_args)}.",
            "risk": "MEDIUM",
            "why": "Callers relying on keyword arguments will break."
        })

    # 3. Check synchronous vs asynchronous conversion
    orig_is_async = "async def" in orig_body or "async function" in orig_body
    refactored_is_async = "async def" in refactored_code or "async function" in refactored_code
    if not orig_is_async and refactored_is_async:
        changes.append({
            "change": "Synchronous function converted to asynchronous (`async`).",
            "risk": "HIGH",
            "why": "Synchronous callers will receive a coroutine/promise instead of the evaluated result."
        })

    # 4. Check return value changes
    orig_returns = "return " in orig_body and "return None" not in orig_body
    refactored_returns = "return " in refactored_code and "return None" not in refactored_code
    if orig_returns and not refactored_returns:
        changes.append({
            "change": "Function no longer returns a value (returns None/void).",
            "risk": "HIGH",
            "why": "Call sites expecting a return value will receive None / undefined."
        })

    # 5. Safe / Low risk if clean refactoring
    if not changes:
        changes.append({
            "change": "API signature and contract preserved; internal implementation modernized.",
            "risk": "LOW",
            "why": "Function signature, parameter order, and return behavior remain backward compatible."
        })

    return changes
