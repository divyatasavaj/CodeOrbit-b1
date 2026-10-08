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


def _clean_js_args(raw_args: str) -> List[str]:
    """Normalize a JS/TS parameter list: drop defaults, types, modifiers."""
    args: List[str] = []
    for part in raw_args.split(","):
        part = part.strip()
        if not part:
            continue
        part = part.split("=")[0].strip()          # drop default value
        if ":" in part:
            part = part.split(":", 1)[0].strip()   # drop TS type annotation
        toks = [t for t in part.split() if t not in
                ("private", "public", "protected", "readonly", "override", "static")]
        cleaned = " ".join(toks)
        if cleaned:
            args.append(cleaned)
    return args


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
    # Optional generics + TS return type: function foo(a: string): number { ... }
    func_decl_pattern = re.compile(
        r'(?:export\s+)?(?:async\s+)?function\s+(\w+)(?:\s*<[^<>{}]*>)?\s*'
        r'\(([^)]*)\)\s*(?::\s*[^{;=\n]*)?\{',
        re.MULTILINE
    )
    for match in func_decl_pattern.finditer(source):
        name = match.group(1)
        raw_args = match.group(2)
        args = _clean_js_args(raw_args)
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
    # Optional TS return type: const foo = (a: string): number => { ... }
    assign_fn_pattern = re.compile(
        r'(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?'
        r'(?:\(([^)]*)\)|(\w+))\s*(?::\s*[^{=>\n]*)?=>\s*\{',
        re.MULTILINE
    )
    for match in assign_fn_pattern.finditer(source):
        name = match.group(1)
        if name in seen_funcs:
            continue
        raw_args = match.group(2) or match.group(3) or ""
        args = _clean_js_args(raw_args)
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

    # 4. Extract class declarations and methods (JS *and* TypeScript signatures)
    #
    # A pure regex cannot handle TS: return types (`foo(): object {`),
    # parameter properties (`ctor(clock: { now(): number })`) and generics all
    # break the naive `\w+\(...\)\s*\{` shape, and method line numbers must be
    # absolute (they feed per-function coverage scoping).
    class_decl_pattern = re.compile(
        r'class\s+(\w+)(?:\s+extends\s+[\w.$]+(?:<[^>{}]*>)?)?\s*\{', re.MULTILINE)
    method_head = re.compile(
        r'(?:static\s+|async\s+|get\s+|set\s+|\*\s*)?'
        r'([A-Za-z_$][\w$]*)(?:\s*<[^<>{}]*>)?\s*\(')
    method_keywords = {
        "if", "for", "while", "switch", "catch", "function", "return", "typeof",
        "new", "else", "do", "try", "throw", "await", "case", "in", "of",
        "instanceof", "yield", "delete", "void", "super", "with", "debugger",
    }

    def _scan_balanced_parens(text: str, start: int) -> int:
        """Index of the ')' matching the '(' at start (string/comment aware)."""
        depth = 0
        in_str = None
        esc = False
        in_lc = False
        in_mc = False
        i = start
        n = len(text)
        while i < n:
            ch = text[i]
            if esc:
                esc = False
                i += 1
                continue
            if in_lc:
                if ch == "\n":
                    in_lc = False
                i += 1
                continue
            if in_mc:
                if ch == "*" and i + 1 < n and text[i + 1] == "/":
                    in_mc = False
                    i += 2
                    continue
                i += 1
                continue
            if in_str:
                if ch == "\\":
                    esc = True
                elif ch == in_str:
                    in_str = None
                i += 1
                continue
            if ch in ('"', "'", "`"):
                in_str = ch
            elif ch == "/" and i + 1 < n and text[i + 1] == "/":
                in_lc = True
                i += 2
                continue
            elif ch == "/" and i + 1 < n and text[i + 1] == "*":
                in_mc = True
                i += 2
                continue
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    for match in class_decl_pattern.finditer(source):
        class_name = match.group(1)
        class_start_idx = match.end() - 1
        class_body = extract_balanced_body(source, class_start_idx)

        methods: List[Dict[str, Any]] = []
        pos = 0
        body_len = len(class_body)
        while pos < body_len:
            m = method_head.search(class_body, pos)
            if not m:
                break
            method_name = m.group(1)
            if method_name in method_keywords:
                pos = m.end()
                continue
            open_idx = m.end() - 1
            close_paren = _scan_balanced_parens(class_body, open_idx)
            if close_paren < 0:
                pos = m.end()
                continue
            q = close_paren + 1
            has_body = False
            if q < body_len and class_body[q] == ":":
                # TS return type: scan to the body '{' (or give up at ; = newline)
                q += 1
                angle = paren = bracket = 0
                aborted = False
                while q < body_len:
                    ch = class_body[q]
                    if ch == "<":
                        angle += 1
                    elif ch == ">":
                        angle = max(0, angle - 1)
                    elif ch == "(":
                        paren += 1
                    elif ch == ")":
                        paren = max(0, paren - 1)
                    elif ch == "[":
                        bracket += 1
                    elif ch == "]":
                        bracket = max(0, bracket - 1)
                    elif angle == 0 and paren == 0 and bracket == 0:
                        if ch == "{":
                            has_body = True
                            break
                        if ch in (";", "=", "\n"):
                            aborted = True
                            break
                    q += 1
                if aborted or q >= body_len:
                    pos = close_paren + 1
                    continue
            else:
                while q < body_len and class_body[q] in " \t\r\n":
                    q += 1
                if q >= body_len or class_body[q] != "{":
                    pos = close_paren + 1
                    continue
                has_body = True
            if not has_body:
                pos = close_paren + 1
                continue

            m_body = extract_balanced_body(class_body, q)
            raw_m_args = class_body[open_idx + 1:close_paren]
            abs_offset = class_start_idx + m.start()
            m_lineno = source[:abs_offset].count("\n") + 1

            methods.append({
                "name": method_name,
                "args": _clean_js_args(raw_m_args),
                "lineno": m_lineno,
                "body": f"{method_name}({raw_m_args}) {m_body}",
                "calls": extract_js_calls(m_body)
            })
            pos = q + max(len(m_body) - 1, 1)

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