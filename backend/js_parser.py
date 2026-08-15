"""
CodeOracle - JavaScript & TypeScript AST Static Parser
High-accuracy, zero-external-dependency parser for JS/TS/JSX/TSX source files.
Extracts functions, arrow functions, class methods, exports, calls, and dependencies.
"""
import os
import re
import logging
from typing import Dict, Any, List, Set

logger = logging.getLogger("codeoracle")


def extract_balanced_body(source: str, start_brace_idx: int) -> str:
    """Extract code block matching the opening brace at start_brace_idx."""
    if start_brace_idx >= len(source) or source[start_brace_idx] != '{':
        return ""
    
    depth = 0
    in_string = None
    in_single_comment = False
    in_multi_comment = False
    escape = False
    i = start_brace_idx

    while i < len(source):
        char = source[i]

        if escape:
            escape = False
            i += 1
            continue

        if char == '\\' and in_string:
            escape = True
            i += 1
            continue

        if in_single_comment:
            if char == '\n':
                in_single_comment = False
            i += 1
            continue

        if in_multi_comment:
            if char == '*' and i + 1 < len(source) and source[i + 1] == '/':
                in_multi_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_string:
            if char == in_string:
                in_string = None
            i += 1
            continue

        if char == '/' and i + 1 < len(source):
            if source[i + 1] == '/':
                in_single_comment = True
                i += 2
                continue
            elif source[i + 1] == '*':
                in_multi_comment = True
                i += 2
                continue

        if char in ('"', "'", '`'):
            in_string = char
            i += 1
            continue

        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return source[start_brace_idx:i + 1]

        i += 1

    return source[start_brace_idx:]


def extract_js_calls(body: str) -> List[str]:
    """Extract called function/method names from JS body."""
    calls = []
    # Match standalone function calls like foo(...) or obj.foo(...)
    pattern = re.compile(r'(?:(?:\b(\w+)\.)?(\w+))\s*\(')
    js_keywords = {'if', 'for', 'while', 'switch', 'catch', 'function', 'return', 'require', 'import', 'super', 'typeof'}
    
    for match in pattern.finditer(body):
        obj_name, fn_name = match.groups()
        target = fn_name if fn_name else obj_name
        if target and target not in js_keywords:
            calls.append(target)
    
    return list(dict.fromkeys(calls))


def parse_javascript_source(source: str, filename: str = "") -> Dict[str, Any]:
    """
    Pure Python AST/regex parser for JavaScript & TypeScript files.
    Extracts functions, arrow functions, class methods, calls, and imports.
    """
    functions: List[Dict[str, Any]] = []
    classes: List[Dict[str, Any]] = []
    imports: List[str] = []
    seen_funcs: Set[str] = set()

    # 1. Extract imports & requires
    import_patterns = [
        re.compile(r'import\s+(?:[\w\s{},*]+)\s+from\s+[\'"]([^\'"]+)[\'"]', re.MULTILINE),
        re.compile(r'(?:const|let|var)\s+(?:[\w\s{},*]+)\s*=\s*require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)', re.MULTILINE),
        re.compile(r'require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)', re.MULTILINE)
    ]
    for pattern in import_patterns:
        for match in pattern.finditer(source):
            imp = match.group(1)
            if imp not in imports:
                imports.append(imp)

    # 2. Extract standard function declarations: function foo(a, b) { ... } / async function foo(a, b) { ... }
    func_decl_pattern = re.compile(
        r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)\s*\{',
        re.MULTILINE
    )
    for match in func_decl_pattern.finditer(source):
        name = match.group(1)
        raw_args = match.group(2)
        args = [a.strip().split('=')[0].strip() for a in raw_args.split(',') if a.strip()]
        start_idx = match.end() - 1
        body = extract_balanced_body(source, start_idx)
        lineno = source[:match.start()].count('\n') + 1
        
        func_obj = {
            "name": name,
            "args": args,
            "lineno": lineno,
            "body": f"function {name}({raw_args}) {body}",
            "calls": extract_js_calls(body)
        }
        functions.append(func_obj)
        seen_funcs.add(name)

    # 3. Extract arrow & assigned functions: const foo = (a, b) => { ... } or const foo = function(a, b) { ... }
    assign_fn_pattern = re.compile(
        r'(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\(([^)]*)\)|(\w+))\s*=>\s*\{',
        re.MULTILINE
    )
    for match in assign_fn_pattern.finditer(source):
        name = match.group(1)
        if name in seen_funcs:
            continue
        raw_args = match.group(2) or match.group(3) or ""
        args = [a.strip().split('=')[0].strip() for a in raw_args.split(',') if a.strip()]
        start_idx = match.end() - 1
        body = extract_balanced_body(source, start_idx)
        lineno = source[:match.start()].count('\n') + 1

        func_obj = {
            "name": name,
            "args": args,
            "lineno": lineno,
            "body": f"const {name} = ({raw_args}) => {body}",
            "calls": extract_js_calls(body)
        }
        functions.append(func_obj)
        seen_funcs.add(name)

    # 4. Extract class declarations and methods
    class_decl_pattern = re.compile(r'class\s+(\w+)(?:\s+extends\s+(\w+))?\s*\{', re.MULTILINE)
    for match in class_decl_pattern.finditer(source):
        class_name = match.group(1)
        class_start_idx = match.end() - 1
        class_body = extract_balanced_body(source, class_start_idx)
        
        methods: List[Dict[str, Any]] = []
        method_pattern = re.compile(
            r'(?:(?:static|async|get|set)\s+)*(\w+)\s*\(([^)]*)\)\s*\{',
            re.MULTILINE
        )
        for m_match in method_pattern.finditer(class_body):
            method_name = m_match.group(1)
            if method_name in ('if', 'for', 'while', 'switch', 'catch'):
                continue
            raw_m_args = m_match.group(2)
            m_args = [a.strip().split('=')[0].strip() for a in raw_m_args.split(',') if a.strip()]
            m_start_idx = m_match.end() - 1
            m_body = extract_balanced_body(class_body, m_start_idx)
            m_lineno = source[:match.start() + m_match.start()].count('\n') + 1

            methods.append({
                "name": method_name,
                "args": m_args,
                "lineno": m_lineno,
                "body": f"{method_name}({raw_m_args}) {m_body}",
                "calls": extract_js_calls(m_body)
            })

        classes.append({
            "name": class_name,
            "methods": methods
        })

    # 5. Extract module.exports / exports functions
    export_fn_pattern = re.compile(
        r'exports\.(\w+)\s*=\s*(?:async\s+)?function\s*\(([^)]*)\)\s*\{',
        re.MULTILINE
    )
    for match in export_fn_pattern.finditer(source):
        name = match.group(1)
        if name in seen_funcs:
            continue
        raw_args = match.group(2)
        args = [a.strip().split('=')[0].strip() for a in raw_args.split(',') if a.strip()]
        start_idx = match.end() - 1
        body = extract_balanced_body(source, start_idx)
        lineno = source[:match.start()].count('\n') + 1

        functions.append({
            "name": name,
            "args": args,
            "lineno": lineno,
            "body": f"exports.{name} = function({raw_args}) {body}",
            "calls": extract_js_calls(body)
        })
        seen_funcs.add(name)

    return {
        "filename": filename or "unknown.js",
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "raw_source": source,
        "line_count": source.count('\n') + 1
    }


def parse_javascript_file(filepath: str) -> Dict[str, Any]:
    """Parse a JavaScript/TypeScript source file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        return {"error": f"Failed to read file: {str(e)}"}

    filename = os.path.basename(filepath)
    return parse_javascript_source(source, filename)