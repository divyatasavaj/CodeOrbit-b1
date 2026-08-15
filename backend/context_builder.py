"""
Smart context builder for LLM prompts.
Generates minimal, relevant context for each function including AST-derived information.
"""
import os
import json
import logging
import ast
from typing import Dict, List, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger("codeoracle")


@dataclass
class FunctionContext:
    """Context for a single function."""
    name: str
    filename: str
    lineno: int
    body: str
    args: List[str]
    imports_used: List[str]
    calls: List[str]
    class_name: str = ""
    return_type: str = ""
    has_return: bool = False
    complexity: int = 1
    decorators: List[str] = None
    globals_used: List[str] = None
    exceptions_raised: List[str] = None
    control_flow: List[str] = None

    def __post_init__(self):
        if self.decorators is None:
            self.decorators = []
        if self.globals_used is None:
            self.globals_used = []
        if self.exceptions_raised is None:
            self.exceptions_raised = []
        if self.control_flow is None:
            self.control_flow = []

    def to_prompt_context(self) -> str:
        """Generate rich context for LLM prompt."""
        context_parts = [
            f"Function: {self.name}",
            f"File: {self.filename}",
            f"Lines: {self.lineno}",
            f"Arguments: {', '.join(self.args) if self.args else 'none'}",
        ]

        if self.class_name:
            context_parts.append(f"Class: {self.class_name}")

        if self.has_return:
            context_parts.append("Has return: yes")
        else:
            context_parts.append("Has return: no")

        if self.complexity > 1:
            context_parts.append(f"Cyclomatic complexity: {self.complexity}")

        if self.imports_used:
            context_parts.append(f"Imports used: {', '.join(self.imports_used[:8])}")

        if self.calls:
            context_parts.append(f"Calls: {', '.join(self.calls[:8])}")

        if self.globals_used:
            context_parts.append(f"Global vars: {', '.join(self.globals_used[:5])}")

        if self.exceptions_raised:
            context_parts.append(f"Raises: {', '.join(self.exceptions_raised)}")

        if self.control_flow:
            context_parts.append(f"Control flow: {', '.join(self.control_flow[:5])}")

        context_parts.append(f"\nSource:\n```python\n{self.body}\n```")

        return "\n".join(context_parts)


def _extract_control_flow(node: ast.AST) -> List[str]:
    """Extract control flow patterns from a function AST node."""
    flow = []
    for child in ast.walk(node):
        if isinstance(child, ast.If):
            flow.append("if-branch")
        elif isinstance(child, ast.For):
            flow.append("for-loop")
        elif isinstance(child, ast.While):
            flow.append("while-loop")
        elif isinstance(child, ast.Try):
            flow.append("try-except")
        elif isinstance(child, ast.With):
            flow.append("with-context")
        elif isinstance(child, ast.ListComp):
            flow.append("list-comprehension")
        elif isinstance(child, ast.DictComp):
            flow.append("dict-comprehension")
    return list(set(flow))


def _extract_exceptions_raised(node: ast.AST) -> List[str]:
    """Extract exceptions raised in a function."""
    exceptions = []
    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            if isinstance(child.exc, ast.Call):
                if isinstance(child.exc.func, ast.Name):
                    exceptions.append(child.exc.func.id)
                elif isinstance(child.exc.func, ast.Attribute):
                    exceptions.append(child.exc.func.attr)
    return list(set(exceptions))


def _extract_globals_used(node: ast.AST, global_vars: List[str]) -> List[str]:
    """Find which global variables are used in a function."""
    used = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in global_vars:
            used.add(child.id)
    return list(used)


def build_function_context(
    func_info: Dict[str, Any],
    filename: str,
    source_file: str,
    all_imports: List[str],
    global_vars: List[str] = None,
    nearby_lines: int = 5
) -> FunctionContext:
    """Build rich context for a single function using AST information."""
    body = func_info.get("body", "")
    args = func_info.get("args", [])
    calls = func_info.get("calls", [])

    imports_used = [imp for imp in all_imports if any(
        call in imp for call in calls
    )]

    return FunctionContext(
        name=func_info.get("name", "unknown"),
        filename=filename,
        lineno=func_info.get("lineno", 0),
        body=body,
        args=args,
        imports_used=imports_used[:8],
        calls=calls[:10],
        class_name=func_info.get("class_name", ""),
        has_return=func_info.get("has_return", False),
        complexity=func_info.get("complexity", 1),
        decorators=func_info.get("decorators", []),
        globals_used=_extract_globals_used_from_info(func_info, global_vars or []),
        exceptions_raised=func_info.get("exceptions_raised", []),
        control_flow=func_info.get("control_flow", [])
    )


def _extract_globals_used_from_info(func_info: Dict[str, Any], global_vars: List[str]) -> List[str]:
    """Extract globals used from function info dict."""
    body = func_info.get("body", "")
    used = []
    for gv in global_vars:
        if gv in body:
            used.append(gv)
    return used[:5]


def build_file_context(
    analysis: Dict[str, Any],
    source_code: str,
    max_functions_per_batch: int = 20
) -> List[Dict[str, Any]]:
    """Build contexts for all functions in a file, grouped into optimal batches."""
    filename = analysis.get("filename", "unknown")
    functions = analysis.get("functions", [])
    classes = analysis.get("classes", [])
    imports = analysis.get("imports", [])
    global_vars = analysis.get("global_variables", [])

    all_funcs = []

    for func in functions:
        ctx = build_function_context(func, filename, analysis.get("filepath", ""), imports, global_vars)
        all_funcs.append({
            "context": ctx,
            "info": func
        })

    for cls in classes:
        for method in cls.get("methods", []):
            method["class_name"] = cls["name"]
            ctx = build_function_context(method, filename, analysis.get("filepath", ""), imports, global_vars)
            all_funcs.append({
                "context": ctx,
                "info": method
            })

    batches = []
    for i in range(0, len(all_funcs), max_functions_per_batch):
        batch = all_funcs[i:i + max_functions_per_batch]
        batches.append({
            "filename": filename,
            "functions": batch,
            "imports": imports,
            "global_variables": global_vars,
            "source_preview": source_code[:2000]
        })

    return batches


def build_test_generation_prompt(
    functions: List[Dict[str, Any]],
    source_code: str,
    source_file: str,
    uncovered_segments: List[Dict[str, Any]] = None,
    previous_failures: List[Dict[str, Any]] = None,
    is_retry: bool = False
) -> str:
    """Build a rich prompt for test generation with function-specific context."""
    source_module = os.path.basename(source_file).replace('.py', '') if source_file else "source_module"

    func_contexts = []
    for func in functions:
        name = func.get("display_name") or func.get("name", "unknown")
        args = func.get("args", [])
        body = func.get("body", "")
        has_return = func.get("has_return", False)
        complexity = func.get("complexity", 1)
        calls = func.get("calls", [])
        class_name = func.get("class_name", "")

        ctx_parts = [f"- {name}({', '.join(args)})"]
        if class_name:
            ctx_parts.append(f"  Class: {class_name}")
        if has_return:
            ctx_parts.append(f"  Returns: yes")
        if complexity > 1:
            ctx_parts.append(f"  Complexity: {complexity}")
        if calls:
            ctx_parts.append(f"  Calls: {', '.join(calls[:5])}")
        if body:
            ctx_parts.append(f"  Body:\n```python\n{body}\n```")
        func_contexts.append("\n".join(ctx_parts))

    func_list = "\n\n".join(func_contexts)

    prompt = f"""Generate comprehensive pytest unit tests for this Python module.

Module: {source_module}
File: {source_file}

Functions to test (with context):
{func_list}

Source code (full):
```python
{source_code[:6000]}
```

Requirements:
1. Import using: import {source_module}
2. Create a SEPARATE test function for EACH function listed above
3. Name tests: test_<function_name>_<scenario> (e.g. test_add_positive, test_add_zero)
4. For EACH function, test:
   - Normal/success cases
   - Boundary values (zero, negative, empty)
   - Invalid inputs (wrong types, None)
   - Exception cases (raise ValueError, etc.)
   - Conditional branches (if/else paths)
   - Edge cases from the function's logic
5. Mock side effects (print, file I/O, network)
6. Use pytest.raises for exception testing
7. Do NOT use one generic template for every function
8. Skip main() functions
9. Each test must be self-contained and independent

Return ONLY Python test code. No explanation. No markdown fences."""

    if is_retry and uncovered_segments:
        uncovered_info = []
        for seg in uncovered_segments[:5]:
            seg_type = seg.get("type", "code")
            seg_name = seg.get("name", "unknown")
            missing = seg.get("missing_lines", [])
            uncovered_info.append(f"- {seg_type} {seg_name}: lines {missing[:10]}")
        uncovered_text = "\n".join(uncovered_info)

        prompt += f"""

COVERAGE FEEDBACK - These lines are NOT covered yet:
{uncovered_text}

Generate ADDITIONAL tests specifically targeting these uncovered lines.
Focus on the exact code paths that are missing coverage.
Do NOT regenerate tests that already pass."""

    if previous_failures:
        failure_info = []
        for fail in previous_failures[:3]:
            test_name = fail.get("test_name", "unknown")
            error = fail.get("error", "unknown error")
            failure_info.append(f"- {test_name}: {error[:200]}")
        failure_text = "\n".join(failure_info)

        prompt += f"""

TEST FAILURES to fix:
{failure_text}

Regenerate ONLY the failing tests with correct assertions/mocks."""

    return prompt


def build_test_context(
    functions: List[Dict[str, Any]],
    source_code: str,
    source_file: str
) -> str:
    """Build context for test generation (legacy interface)."""
    return build_test_generation_prompt(functions, source_code, source_file)


def build_refactor_context(functions: List[Dict[str, Any]]) -> str:
    """Build context for refactoring."""
    func_data = []
    for f in functions:
        body = f.get("body", "").strip()[:300]
        func_data.append({
            "name": f.get("display_name") or f.get("name", "unknown"),
            "body": body,
            "complexity": f.get("complexity", 1),
            "line_count": f.get("line_count", len(body.split('\n')))
        })

    return json.dumps(func_data, indent=2)
