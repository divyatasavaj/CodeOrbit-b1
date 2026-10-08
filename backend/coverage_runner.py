"""
CodeOracle - Multi-Language Test Execution & Coverage Engine
Executes real unit tests using pytest (Python) and Node.js built-in runner (JavaScript/TypeScript).
Computes exact line-by-line code coverage, uncovered-line analysis, and validates refactored outputs.
"""
import subprocess
import json
import os
import ast
import tempfile
import shutil
import logging
import re
from typing import Dict, Any, List, Optional

import config

logger = logging.getLogger("codeoracle")

# Single source of truth: the configured minimum *measured* coverage target.
# Kept under the historical names so existing imports keep working.
COVERAGE_THRESHOLD = config.MIN_TEST_COVERAGE
MAX_COVERAGE_RETRIES = config.MAX_TEST_IMPROVEMENT_ATTEMPTS


def meets_min_coverage(cov_result: Dict[str, Any]) -> bool:
    """True only when the *measured* coverage reaches the configured minimum.

    Reads the real value produced by the coverage tool; never estimates,
    rounds up, or invents a percentage.
    """
    if not isinstance(cov_result, dict):
        return False
    try:
        measured = float(cov_result.get("coverage_percent") or 0)
    except (TypeError, ValueError):
        return False
    return measured >= config.MIN_TEST_COVERAGE


def measured_coverage(cov_result: Dict[str, Any]) -> float:
    """The measured coverage percentage, clamped to a sane 0-100 range."""
    try:
        measured = float((cov_result or {}).get("coverage_percent") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, measured))


def _is_valid_test_code(test_code: str, is_js: bool = False) -> bool:
    """Check if test code is valid syntax and not an error string."""
    if not test_code or not test_code.strip():
        return False
    stripped = test_code.strip()
    
    if (stripped.startswith("#") or stripped.startswith("//")) and ("error" in stripped.lower() or "quota" in stripped.lower() or "unavailable" in stripped.lower()):
        return False
    
    if is_js:
        return any(k in stripped for k in ("test(", "it(", "describe(", "assert", "expect("))
    else:
        if stripped.startswith("#"):
            lines = [l for l in stripped.split('\n') if l.strip() and not l.strip().startswith('#')]
            if not lines:
                return False
        try:
            compile(stripped, '<test>', 'exec')
            return True
        except SyntaxError:
            return False


def _get_test_dir() -> str:
    """Get a unique test directory for this process to avoid collisions."""
    pid = os.getpid()
    test_dir = os.path.join(tempfile.gettempdir(), f"oracle_tests_{pid}")
    os.makedirs(test_dir, exist_ok=True)
    return test_dir


def _cleanup_coverage_json(source_dir: str) -> None:
    """Remove stale coverage.json before running pytest."""
    for name in ["coverage.json", ".coverage", ".coverage.*"]:
        import glob as globmod
        for path in globmod.glob(os.path.join(source_dir, name)):
            try:
                os.remove(path)
            except Exception:
                pass


def _write_test_file(test_dir: str, test_filename: str, test_code: str, source_dir: str,
                     extra_sys_paths: Optional[List[str]] = None,
                     stub_imports: Optional[List[str]] = None) -> Optional[str]:
    """Write test file and conftest.py. Returns path or None on error."""
    test_path = os.path.join(test_dir, test_filename)
    try:
        with open(test_path, 'w', encoding='utf-8') as f:
            f.write(test_code)
    except Exception:
        return None

    conftest_path = os.path.join(test_dir, "conftest.py")
    lines = [f"import sys", f"sys.path.insert(0, r'{source_dir}')"]
    for extra in (extra_sys_paths or []):
        if extra and os.path.abspath(extra) != os.path.abspath(source_dir):
            # Appended (not prepended) so the source dir keeps priority.
            lines.append(f"sys.path.append(r'{extra}')")
    lines.append(_python_stub_conftest_block(stub_imports or []))
    try:
        with open(conftest_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass

    return test_path


def _parse_coverage_json(test_dir: str, source_file: str) -> Dict[str, Any]:
    """Parse coverage.json and extract per-file and total coverage.

    The entry is matched by EXACT basename (or exact path suffix) so a
    similarly-named file can never be mistaken for the module under test.
    """
    source_base = os.path.basename(source_file)
    result = {
        "coverage_percent": 0,
        "lines_total": 0,
        "lines_covered": 0,
        "lines_missing": 0,
        "missing_lines": [],
        "covered_lines": [],
        "file_coverage": {}
    }

    coverage_json_path = os.path.join(test_dir, "coverage.json")
    if not os.path.exists(coverage_json_path):
        return result

    try:
        with open(coverage_json_path, 'r', encoding='utf-8') as f:
            cov_data = json.load(f)

        files_data = cov_data.get("files", {})

        # Exact match first; stem match second; never a plain substring match.
        source_stem = os.path.splitext(source_base)[0]
        candidate_keys = []
        for file_path in files_data.keys():
            base = os.path.basename(file_path)
            if base == source_base or file_path.replace("\\", "/").endswith("/" + source_base):
                candidate_keys.insert(0, file_path)
        if not candidate_keys:
            for file_path in files_data.keys():
                if os.path.splitext(os.path.basename(file_path))[0] == source_stem:
                    candidate_keys.append(file_path)

        for file_path in candidate_keys:
            file_data = files_data[file_path]
            summary = file_data.get("summary", {})
            result["lines_total"] = summary.get("num_statements", 0)
            result["lines_covered"] = summary.get("covered_lines", 0)
            result["lines_missing"] = summary.get("missing_lines", 0)
            result["coverage_percent"] = round(
                (result["lines_covered"] / result["lines_total"] * 100), 1
            ) if result["lines_total"] > 0 else 0

            analysis = file_data.get("analysis", [])
            if analysis:
                for line_data in analysis:
                    line_no = line_data[0]
                    coverage_count = line_data[1]
                    if coverage_count is None:
                        result["missing_lines"].append(line_no)
                    else:
                        result["covered_lines"].append(line_no)
            else:
                # Newer coverage.py JSON reports: executed_lines/missing_lines lists
                result["covered_lines"] = [
                    ln for ln in (file_data.get("executed_lines") or []) if isinstance(ln, int)
                ]
                result["missing_lines"] = [
                    ln for ln in (file_data.get("missing_lines") or []) if isinstance(ln, int)
                ]

            result["file_coverage"][file_path] = {
                "summary": summary,
                "missing_lines": result["missing_lines"]
            }
            break

        if result["lines_total"] == 0 and not candidate_keys:
            # Target module never even imported (import failure) - totals of
            # other files must NOT be presented as this module's coverage.
            pass
    except Exception:
        pass

    return result


def _parse_test_results(output: str) -> Dict[str, int]:
    """Parse pytest output for passed/failed/error/skipped counts.

    The end-of-run summary line (`= 2 failed, 3 passed in 0.5s =`) is
    authoritative; verbose per-test lines are the fallback.
    """
    results = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}

    summary_hit = False
    for key, word in (("failed", "failed"), ("passed", "passed"),
                      ("errors", "error"), ("skipped", "skipped")):
        m = re.search(rf"(\d+)\s+{word}s?\b", output)
        if m:
            results[key] = int(m.group(1))
            summary_hit = True
    if summary_hit:
        return results

    for line in output.split('\n'):
        stripped = line.strip()
        if " PASSED" in line or stripped.startswith("PASSED"):
            results["passed"] += 1
        elif " FAILED" in line or stripped.startswith("FAILED"):
            results["failed"] += 1
        elif (" ERROR" in line and "::" in line) or (stripped.startswith("ERROR ") and "::" in stripped):
            results["errors"] += 1
        elif " SKIPPED" in line or stripped.startswith("SKIPPED"):
            results["skipped"] += 1
    return results


def collect_python_imports(source_file: str, test_code: str = "") -> List[str]:
    """Dotted import names used by the source file and the generated test.

    Used to auto-stub third-party modules that are not installed in this
    environment (the uploaded ZIP never contains a virtualenv), so the module
    under test can actually import and execute (spec sections 14/16/41).
    """
    names = set()
    texts: List[str] = []
    if source_file and os.path.exists(source_file):
        try:
            with open(source_file, "r", encoding="utf-8", errors="replace") as f:
                texts.append(f.read(2 * 1024 * 1024))
        except OSError:
            pass
    if test_code:
        texts.append(test_code)
    for text in texts:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name:
                        names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import - resolves locally, skip
                    continue
                if node.module:
                    names.add(node.module)
    return sorted(names)


def _python_stub_conftest_block(imports: List[str]) -> str:
    """Conftest snippet: register MagicMock-backed stubs for missing modules.

    Only modules that FAIL ``importlib.util.find_spec`` are stubbed - real
    installed packages and the standard library are always used untouched.
    """
    if not imports:
        return ""
    return f'''
import sys as __oracle_sys, types as __oracle_types, importlib.util as __oracle_iu
from unittest.mock import MagicMock as __oracle_MM

def __oracle_getattr(__attr, __prefix):
    if __attr.startswith("__") and __attr.endswith("__"):
        raise AttributeError(__attr)
    return __oracle_MM(name=f"{{__prefix}}.{{__attr}}")

for __oracle_m in {imports!r}:
    __oracle_top = __oracle_m.split(".")[0]
    if __oracle_top in __oracle_sys.builtin_module_names:
        continue
    try:
        __oracle_found = __oracle_iu.find_spec(__oracle_top) is not None
    except Exception:
        __oracle_found = __oracle_top in __oracle_sys.modules
    if __oracle_found or __oracle_top in __oracle_sys.modules:
        continue
    __oracle_parts = __oracle_m.split(".")
    for __oracle_i in range(1, len(__oracle_parts) + 1):
        __oracle_name = ".".join(__oracle_parts[:__oracle_i])
        if __oracle_name in __oracle_sys.modules:
            continue
        __oracle_mod = __oracle_types.ModuleType(__oracle_name)
        __oracle_mod.__path__ = []
        __oracle_mod.__getattr__ = (
            lambda __attr, __p=__oracle_name: __oracle_getattr(__attr, __p)
        )
        __oracle_sys.modules[__oracle_name] = __oracle_mod
'''


def analyze_uncovered_lines(missing_lines: List[int], source_file: str) -> List[Dict[str, Any]]:
    """
    Analyze uncovered lines using AST to understand what code paths are not tested.
    Returns a list of uncovered code segments with context.
    """
    if not missing_lines or not source_file or not os.path.exists(source_file):
        return []

    try:
        with open(source_file, 'r', encoding='utf-8') as f:
            source_lines = f.readlines()
    except Exception:
        return []

    missing_set = set(missing_lines)
    uncovered_segments = []

    try:
        tree = ast.parse(''.join(source_lines), filename=source_file)
    except SyntaxError:
        return []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_start = getattr(node, 'lineno', 0)
            func_end = getattr(node, 'end_lineno', func_start)
            func_missing = [l for l in range(func_start, func_end + 1) if l in missing_set]
            if func_missing:
                body_lines = source_lines[max(0, func_start - 1):func_end]
                uncovered_segments.append({
                    "type": "function",
                    "name": node.name,
                    "start_line": func_start,
                    "end_line": func_end,
                    "missing_lines": func_missing,
                    "missing_count": len(func_missing),
                    "source": "".join(body_lines)
                })

        elif isinstance(node, ast.ClassDef):
            class_start = getattr(node, 'lineno', 0)
            class_end = getattr(node, 'end_lineno', class_start)
            class_missing = [l for l in range(class_start, class_end + 1) if l in missing_set]
            if class_missing:
                uncovered_segments.append({
                    "type": "class",
                    "name": node.name,
                    "start_line": class_start,
                    "end_line": class_end,
                    "missing_lines": class_missing,
                    "missing_count": len(class_missing)
                })

    return uncovered_segments


def classify_failure(output: str, is_js: bool = False) -> Dict[str, str]:
    """Categorize a failed test run so the next generation attempt receives a
    concise diagnosis instead of "the test failed, try again" (spec section 31).

    Returns {"category": <CATEGORY>, "detail": "<first relevant line>"}.
    """
    text = output or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    def _first_match(*needles: str) -> str:
        lowered = [ln for ln in lines]
        for ln in lowered:
            low = ln.lower()
            for n in needles:
                if n.lower() in low:
                    return ln[:300]
        return (lines[0][:300] if lines else "")

    checks = [
        ("TEST_DISCOVERY_ERROR",
         ("no tests ran", "no test files found", "collected 0 items",
          "no tests found", "0 tests found", "not ok 0")),
        ("MODULE_NOT_FOUND",
         ("cannot find module", "err_module_not_found", "module not found",
          "no module named", "cannot find package")),
        ("IMPORT_ERROR",
         ("cannot use import statement", "unexpected token 'export'",
          "require is not defined", "cannot use import.meta",
          "importerror", "attempted relative import")),
        ("SOURCE_EXECUTION_ERROR",
         ("err_unsupported_typescript_syntax", "unexpected token ':'")),
        ("TIMEOUT",
         ("timed out", "timeout", "etimedout")),
        ("MOCK_CONFIGURATION_ERROR",
         ("is not a constructor", "is not a function", "is not a function",
          "cannot read properties of undefined", "cannot read properties of null")),
        ("ASSERTION_FAILURE",
         ("assertionerror", "assert.ok", "expected values to be strictly",
          "expected ',' received", "to equal")),
        ("ENVIRONMENT_ERROR",
         ("command not found", "enoent", "is not recognized",
          "cannot find package 'node:test'", "eperm")),
        ("TYPE_ERROR",
         ("typeerror",)),
    ]
    for category, needles in checks:
        detail = _first_match(*needles)
        if detail:
            # _first_match returns the first line only when a needle matched OR
            # falls back to line 0; only accept real matches.
            low = detail.lower()
            if any(n.lower() in low for n in needles):
                return {"category": category, "detail": detail}
    return {"category": "SOURCE_EXECUTION_ERROR",
            "detail": (lines[0][:300] if lines else "test run failed")}


# ---------------------------------------------------------------------------
# JavaScript / TypeScript execution environment
# ---------------------------------------------------------------------------

# Node builtins that must never be stubbed (bare specifiers).
_NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "timers",
    "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib", "test", "sqlite",
})

_NODE_VERSION: Optional[tuple] = None


def _node_version() -> tuple:
    """(major, minor) of the installed node, cached. (0, 0) when unavailable."""
    global _NODE_VERSION
    if _NODE_VERSION is not None:
        return _NODE_VERSION
    try:
        out = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=10)
        m = re.match(r"v(\d+)\.(\d+)", (out.stdout or "").strip())
        _NODE_VERSION = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except Exception:  # noqa: BLE001
        _NODE_VERSION = (0, 0)
    return _NODE_VERSION


def _js_imports_of(source_text: str) -> List[Dict[str, Any]]:
    """Extract import bindings from JS/TS source (regex-level, no toolchain).

    Returns [{"specifier": "./x", "bare": False, "names": [...], "default": str|None,
              "namespace": str|None, "side_effect": bool}, ...]
    """
    results: List[Dict[str, Any]] = []
    seen = set()

    def _add(spec: str, names=None, default=None, namespace=None, side_effect=False):
        if not spec:
            return
        bare = not spec.startswith(".") and not spec.startswith("/") and not spec.startswith("node:")
        key = (spec, tuple(sorted(names or [])), default, namespace)
        if key in seen:
            return
        seen.add(key)
        results.append({
            "specifier": spec, "bare": bare, "names": list(names or []),
            "default": default, "namespace": namespace, "side_effect": side_effect,
        })

    # ESM: import / export ... from
    for m in re.finditer(
        r"^\s*import\s+(type\s+)?([^;'\"]*?)\s*from\s*['\"]([^'\"]+)['\"]",
        source_text, re.MULTILINE,
    ):
        clause, spec = m.group(2), m.group(3)
        default = None
        namespace = None
        names: List[str] = []
        clause = clause.strip()
        brace = re.search(r"\{([^}]*)\}", clause)
        before = clause.split("{")[0].strip().rstrip(",").strip()
        after = clause.split("}")[-1].strip().strip(",").strip() if "}" in clause else ""
        for ident in (before, after):
            if not ident:
                continue
            if ident.startswith("* as "):
                namespace = ident[5:].strip()
            elif re.fullmatch(r"[A-Za-z_$][\w$]*", ident):
                if default is None:
                    default = ident
        if brace:
            for part in brace.group(1).split(","):
                part = part.strip()
                if not part:
                    continue
                part = re.sub(r"^type\s+", "", part)
                if " as " in part:
                    part = part.split(" as ")[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", part):
                    names.append(part)
        _add(spec, names=names, default=default, namespace=namespace,
             side_effect=not (names or default or namespace))

    # Side-effect-only ESM imports: import './x'
    for m in re.finditer(r"^\s*import\s+['\"]([^'\"]+)['\"]", source_text, re.MULTILINE):
        _add(m.group(1), side_effect=True)

    # export ... from './x'
    for m in re.finditer(r"^\s*export\s+[^;'\"]*?from\s*['\"]([^'\"]+)['\"]", source_text, re.MULTILINE):
        _add(m.group(1), side_effect=True)

    # Dynamic import('./x')
    for m in re.finditer(r"import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", source_text):
        _add(m.group(1), side_effect=True)

    # CJS: require('x') / const { a } = require('x')
    for m in re.finditer(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", source_text):
        spec = m.group(1)
        # Try to recover destructured names just before this require call.
        prefix = source_text[max(0, m.start() - 200):m.start()]
        names = []
        destructure = re.search(r"(?:const|let|var)\s*\{([^}]*)\}\s*=\s*$", prefix, re.DOTALL)
        default = None
        if destructure:
            for part in destructure.group(1).split(","):
                part = part.strip()
                if not part:
                    continue
                if " as " in part:
                    part = part.split(" as ")[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", part):
                    names.append(part)
        else:
            assigned = re.search(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*$", prefix)
            if assigned:
                default = assigned.group(1)
        _add(spec, names=names, default=default, side_effect=not (names or default))

    return results


def _resolve_relative_import(importer_path: str, spec: str) -> Optional[str]:
    """Resolve a relative JS/TS specifier to a real file path (best effort)."""
    base = os.path.normpath(os.path.join(os.path.dirname(importer_path), spec))
    candidates = [base]
    root, ext = os.path.splitext(base)
    if ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        # TypeScript style './x.js' may actually be x.ts on disk.
        if ext == ".js":
            candidates.append(root + ".ts")
            candidates.append(root + ".tsx")
        candidates.append(root + ".ts" if ext == ".js" else root + ".js")
    else:
        for e in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
            candidates.append(base + e)
        for e in (".js", ".ts"):
            candidates.append(os.path.join(base, "index" + e))
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return None


_MAX_CLOSURE_FILES = 40


def _collect_js_closure(source_file: str) -> List[str]:
    """Source file + every relative-imported file it needs at runtime.

    Bounded so a pathological repo cannot explode the copy.
    """
    seen: Dict[str, None] = {}
    queue = [os.path.abspath(source_file)]
    while queue and len(seen) < _MAX_CLOSURE_FILES:
        current = queue.pop(0)
        if current in seen:
            continue
        seen[current] = None
        try:
            with open(current, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(512 * 1024)
        except OSError:
            continue
        for imp in _js_imports_of(text):
            if imp["bare"]:
                continue
            resolved = _resolve_relative_import(current, imp["specifier"])
            if resolved and resolved not in seen and len(seen) < _MAX_CLOSURE_FILES:
                queue.append(resolved)
    return list(seen.keys())


def _comment_out_decorators(text: str) -> str:
    """Comment decorator lines in place (line numbers preserved).

    Node's built-in TypeScript support cannot execute legacy decorators
    (@Injectable() etc). Commenting them out - always in a TEMPORARY execution
    copy, never the original - keeps every line number identical so the
    coverage report still maps 1:1 onto the real source (spec section 43).
    """
    lines = text.splitlines(keepends=True)
    out: List[str] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("@"):
            balance = 0
            while i < len(lines):
                ln = lines[i]
                nl = ""
                if ln.endswith("\n"):
                    nl = "\n"
                    ln = ln[:-1]
                balance += ln.count("(") + ln.count("[") + ln.count("{")
                balance -= ln.count(")") + ln.count("]") + ln.count("}")
                out.append("// " + ln.lstrip() + nl)
                i += 1
                if balance <= 0:
                    break
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


_STUB_DECORATOR_NAMES = frozenset({
    "Injectable", "Component", "Module", "Controller", "Inject", "Optional",
    "forwardRef", "Get", "Post", "Put", "Patch", "Delete", "Head", "Options",
    "Render", "Redirect", "HttpCode", "Header", "Headers", "Query", "Param",
    "Body", "Req", "Res", "UploadedFile", "UseGuards", "UsePipes",
    "UseInterceptors", "UseFilters", "Catch", "ExceptionFilter",
    "OnModuleInit", "OnModuleDestroy", "OnApplicationBootstrap",
    "PipeTransform", "CanActivate", "NestMiddleware", "UseMiddlewares",
    "Public", "SkipThrottle", "Throttle",
})

# Universal bare-import stub: callable WITHOUT new (decorator factory),
# constructable with new (class-ish, returns a Proxy instance that swallows
# unknown method calls), usable with extends (base class).
_STUB_JS_TEMPLATE = """\
// CodeOracle auto-stub for missing package '{pkg}' - the package is not
// present in the uploaded repository. Generated tests still mock behaviour.
function __hybrid(...args) {{
  if (new.target) {{
    const target = this;
    return new Proxy(target, {{
      get(t, prop) {{
        if (prop in t) {{ return t[prop]; }}
        if (prop === Symbol.toPrimitive) {{ return () => "[stub]"; }}
        if (prop === "toString") {{ return () => "[stub]"; }}
        if (prop === "valueOf") {{ return () => 0; }}
        return () => undefined;
      }}
    }});
  }}
  if (args.length === 1 && typeof args[0] === "function") {{ return args[0]; }}
  return (target) => target;
}}
function __fnStub(...args) {{ return undefined; }}
{exports}
"""


def _stub_export_line(name: str) -> str:
    if name in _STUB_DECORATOR_NAMES or (name[:1].isupper() if name else False):
        return f"exports.{name} = __hybrid; module.exports.{name} = __hybrid;"
    return f"exports.{name} = __fnStub; module.exports.{name} = __fnStub;"


def _stub_package_name(spec: str) -> str:
    """@scope/pkg/sub -> @scope/pkg ; pkg/sub -> pkg."""
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2])
    return parts[0]


def _stub_bare_imports(work_dir: str, scanned_files: List[str]) -> List[str]:
    """Create node_modules stubs for bare imports that cannot resolve.

    Only *missing* packages are stubbed (real ones - if the repository ships
    node_modules - are never touched). Stubs let the source module load so its
    real logic can execute; tests still mock the behaviour per spec section 16.
    """
    bindings: Dict[str, Dict[str, Any]] = {}

    for path in scanned_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(512 * 1024)
        except OSError:
            continue
        for imp in _js_imports_of(text):
            spec = imp["specifier"]
            if not imp["bare"]:
                continue
            pkg = _stub_package_name(spec)
            if pkg in _NODE_BUILTINS or pkg.split("/")[0] in _NODE_BUILTINS:
                continue
            entry = bindings.setdefault(spec, {"pkg": pkg, "names": set(), "default": False, "namespace": False})
            entry["names"].update(imp["names"])
            if imp["default"]:
                entry["default"] = True
            if imp["namespace"]:
                entry["namespace"] = True

    created: List[str] = []
    written_pkgs = set()
    for spec, entry in sorted(bindings.items()):
        pkg = entry["pkg"]
        pkg_dir = os.path.join(work_dir, "node_modules", *pkg.split("/"))
        subpath = spec[len(pkg):].lstrip("/")
        exports_lines = []
        for name in sorted(entry["names"]):
            exports_lines.append(_stub_export_line(name))
        if entry["default"]:
            exports_lines.append("module.exports.default = module.exports.default || __hybrid;")
        if entry["namespace"]:
            exports_lines.append("module.exports.default = module.exports.default || __hybrid;")
        body = _STUB_JS_TEMPLATE.format(
            pkg=spec,
            exports="\n".join(exports_lines) or "module.exports = __hybrid;",
        )
        try:
            if pkg not in written_pkgs and not os.path.isfile(os.path.join(pkg_dir, "package.json")):
                os.makedirs(pkg_dir, exist_ok=True)
                with open(os.path.join(pkg_dir, "package.json"), "w", encoding="utf-8") as f:
                    json.dump({"name": pkg, "version": "0.0.0", "main": "index.js"}, f)
                with open(os.path.join(pkg_dir, "index.js"), "w", encoding="utf-8") as f:
                    f.write(body)
                written_pkgs.add(pkg)
            if subpath:
                sub_file = os.path.join(pkg_dir, *subpath.split("/"))
                if not os.path.splitext(sub_file)[1]:
                    sub_file += ".js"
                if not os.path.isfile(sub_file):
                    os.makedirs(os.path.dirname(sub_file), exist_ok=True)
                    with open(sub_file, "w", encoding="utf-8") as f:
                        f.write(body)
            created.append(spec)
        except OSError:
            continue
    if created:
        logger.info("[TEST] stubbed missing packages: %s", ", ".join(created))
    return created


def _parse_lcov(lcov_text: str, source_file: str) -> Optional[Dict[str, Any]]:
    """Extract per-line data for source_file from an LCOV report.

    LCOV is emitted by node's built-in lcov reporter and contains exact
    `DA:<line>,<count>` records - the authority for what really executed.
    """
    source_norm = source_file.replace("\\", "/")
    source_base = os.path.basename(source_file)
    best: Optional[Dict[str, Any]] = None

    current: Dict[str, Any] = {"sf": "", "da": [], "fn": [], "fnda": [], "lf": 0, "lh": 0}
    records: List[Dict[str, Any]] = []

    for raw in (lcov_text or "").splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            current = {"sf": line[3:].strip(), "da": [], "fn": [], "fnda": [], "lf": 0, "lh": 0}
        elif line.startswith("DA:"):
            try:
                num, count = line[3:].split(",")[:2]
                current["da"].append((int(num), int(float(count))))
            except (ValueError, TypeError):
                pass
        elif line.startswith("FN:"):
            try:
                num, name = line[3:].split(",", 1)
                current["fn"].append((int(num), name.strip()))
            except (ValueError, TypeError):
                pass
        elif line.startswith("FNDA:"):
            try:
                count, name = line[5:].split(",", 1)
                current["fnda"].append((name.strip(), int(float(count))))
            except (ValueError, TypeError):
                pass
        elif line.startswith("LF:"):
            current["lf"] = int(line[3:] or 0)
        elif line.startswith("LH:"):
            current["lh"] = int(line[3:] or 0)
        elif line == "end_of_record":
            records.append(current)

    for rec in records:
        sf = rec["sf"].replace("\\", "/")
        if sf == source_norm or sf.endswith("/" + source_base) or os.path.basename(sf) == source_base:
            # Prefer the most exact match (full suffix) when several collide.
            if best is None or len(sf) > len(best["sf"]):
                best = rec

    if not best:
        return None

    covered = sorted(num for num, count in best["da"] if count > 0)
    missing = sorted(num for num, count in best["da"] if count == 0)
    total = len(best["da"])
    percent = round(len(covered) / total * 100, 1) if total else 0.0
    fn_exec = {name: count for name, count in best["fnda"]}
    fn_start = {name: num for num, name in best["fn"]}
    return {
        "coverage_percent": percent,
        "lines_total": total,
        "lines_covered": len(covered),
        "lines_missing": len(missing),
        "covered_lines": covered,
        "missing_lines": missing,
        "sf": best["sf"],
        "functions": [{"name": n, "start_line": fn_start.get(n, 0),
                       "execution_count": fn_exec.get(n, 0)} for n in fn_start],
    }


def _rewrite_relative_specifiers(text: str, file_path: str) -> str:
    """Append the real extension to relative import specifiers (temp copies only).

    ESM resolution (which node applies to ``export``-style TS/JS files) never
    tries extensions, while TypeScript sources are commonly written with
    bundler-style extensionless specifiers (``../utils/format``). Rewriting the
    specifier to the file that actually exists keeps line numbers intact so
    coverage still maps onto the original source.
    """
    def repl(m: "re.Match[str]") -> str:
        quote, spec, trail = m.group(1), m.group(2), m.group(3)
        base = spec.split("?")[0].split("#")[0]
        _root, ext = os.path.splitext(base)
        if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".node"):
            return m.group(0)
        resolved = _resolve_relative_import(file_path, spec)
        if resolved:
            new_ext = os.path.splitext(resolved)[1]
            return f"{quote}{base}{new_ext}{trail}"
        return m.group(0)

    return re.sub(r"(['\"])(\.\.?/[^'\"]+)(['\"])", repl, text)


def _js_uncovered_segments(missing_lines: List[int], source_file: str) -> List[Dict[str, Any]]:
    """Group contiguous missing lines with their source text for the brief."""
    if not missing_lines or not os.path.exists(source_file):
        return []
    try:
        with open(source_file, "r", encoding="utf-8", errors="replace") as f:
            src_lines = f.readlines()
    except OSError:
        return []
    missing = sorted(missing_lines)
    segments: List[Dict[str, Any]] = []
    run: List[int] = []
    for line in missing + [None]:
        if line is not None and (not run or line == run[-1] + 1):
            run.append(line)
            continue
        if run:
            start, end = run[0], run[-1]
            text = "".join(src_lines[max(0, start - 1):end])
            segments.append({
                "type": "lines", "name": f"{os.path.basename(source_file)}:{start}-{end}",
                "start_line": start, "end_line": end,
                "missing_lines": list(run), "missing_count": len(run),
                "source": text,
            })
            run = [line] if line is not None else []
    return segments


def _parse_coverage_table(output: str, source_file: str) -> Optional[Dict[str, Any]]:
    """Fallback parser for node's plain coverage table (no lcov reporter).

    Matches the source file's row by EXACT basename - never substring - so the
    generated test file's row can never be mistaken for the source. The table
    lists uncovered line ranges, but not which other lines are executable, so
    callers keep FILE scope (no invented per-function numbers).
    """
    source_base = os.path.basename(source_file)
    best: Optional[Dict[str, Any]] = None
    for raw in (output or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            line = line[1:].strip()
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        file_col = parts[0].replace("\\", "/")
        if os.path.basename(file_col) != source_base:
            continue
        if file_col in ("file", "all files"):
            continue
        try:
            pct = float(parts[1])
        except (ValueError, TypeError):
            continue
        missing: List[int] = []
        if len(parts) >= 5:
            for token in re.findall(r"(\d+)(?:-(\d+))?", parts[4]):
                start = int(token[0])
                end = int(token[1] or token[0])
                missing.extend(range(start, end + 1))
        # Prefer the longest matching path (most specific row).
        if best is None or len(file_col) > len(best["sf"]):
            best = {"sf": file_col, "coverage_percent": round(pct, 1), "missing": sorted(set(missing))}

    if best is None:
        return None

    missing_count = len(best["missing"])
    pct = best["coverage_percent"]
    if pct < 100.0 and missing_count:
        total = round(missing_count / (1 - pct / 100.0))
    else:
        total = missing_count if pct < 100.0 else 0
    best["lines_total"] = total
    best["lines_covered"] = max(0, total - missing_count)
    return best


def _js_coverage_result(
    test_code: str,
    source_file: str,
    test_filename: str = "test_generated.test.js",
) -> Dict[str, Any]:
    """Run node:test against a JS/TS source with real per-line coverage.

    Pipeline (spec section 6): write -> execute -> discover -> report.
    Uses the LCOV reporter for exact `DA:<line>,<count>` data so coverage can
    be scoped to the selected FUNCTION's lines, not the whole module.
    """
    empty_result = {
        "coverage_percent": 0,
        "passed": False,
        "output": "",
        "error": "Invalid or empty JavaScript test code",
        "lines_total": 0,
        "lines_covered": 0,
        "lines_missing": 0,
        "missing_lines": [],
        "covered_lines": [],
        "test_results": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0},
        "uncovered_segments": [],
        "functions_tested": 0,
        "functions_passed": 0,
        "executed": 0,
        "failure": {"category": "TEST_DISCOVERY_ERROR",
                    "detail": "Invalid or empty test code - no tests executed"},
        "coverage_scope": "FILE",
        "llm_unavailable": False,
    }

    if not _is_valid_test_code(test_code, is_js=True):
        return empty_result
    if not source_file or not os.path.exists(source_file):
        return {**empty_result, "error": f"Source file not found: {source_file}",
                "failure": {"category": "ENVIRONMENT_ERROR",
                            "detail": f"Source file not found: {source_file}"}}

    major, _minor = _node_version()
    if major and major < 22:
        return {**empty_result,
                "error": f"Node {major}.x cannot execute TypeScript or the "
                         f"coverage reporters CodeOracle requires (need Node >= 22)",
                "failure": {"category": "ENVIRONMENT_ERROR", "detail": f"node {major} too old"}}

    source_file = os.path.abspath(source_file)
    is_ts = source_file.endswith((".ts", ".tsx"))

    # ---- Build an isolated work dir preserving the file's relative layout --
    try:
        root = os.path.commonpath([os.path.dirname(source_file)]) or os.path.dirname(source_file)
    except ValueError:
        root = os.path.dirname(source_file)
    closure = _collect_js_closure(source_file)
    # Layout root: common ancestor of the closure (keeps ../ relatives intact).
    try:
        root = os.path.commonpath([os.path.dirname(p) for p in closure]) or root
    except ValueError:
        pass

    work_dir = os.path.join(tempfile.gettempdir(), f"oracle_js_{os.getpid()}")
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir, exist_ok=True)

    copied: List[str] = []
    rel_source = os.path.relpath(source_file, root)
    if rel_source.startswith(".."):
        rel_source = os.path.basename(source_file)
    for path in closure:
        if os.path.abspath(path) == source_file:
            rel = rel_source
        else:
            rel = os.path.relpath(path, root)
            if rel.startswith(".."):
                continue
        dest = os.path.join(work_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(2 * 1024 * 1024)
        except OSError:
            continue
        # Decorators: comment out in the TEMP copy, line numbers preserved.
        if re.search(r"^\s*@", text, re.MULTILINE):
            text = _comment_out_decorators(text)
        # Extensionless relative imports -> real file extension (ESM requires
        # explicit specifiers). Line numbers are preserved.
        text = _rewrite_relative_specifiers(text, path)
        with open(dest, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        copied.append(dest)

    rel_source_used = rel_source
    work_source = os.path.join(work_dir, rel_source_used)
    if not os.path.isfile(work_source):
        shutil.rmtree(work_dir, ignore_errors=True)
        return {**empty_result, "error": f"Failed to stage source file {rel_source}"}

    # ---- Test file lives NEXT TO the source so './module' resolves --------
    work_test = os.path.join(os.path.dirname(work_source), test_filename)
    # Specifier rewrite resolves against the source's directory (the test file
    # is staged beside it, so relative paths are identical).
    staged_test_code = _rewrite_relative_specifiers(
        test_code, os.path.join(os.path.dirname(source_file), "__oracle_stage__.js")
    )
    try:
        with open(work_test, "w", encoding="utf-8", newline="") as f:
            f.write(staged_test_code)
    except OSError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return {**empty_result, "error": f"Failed to write JS test file: {e}"}

    # ---- Stub bare imports that cannot resolve (missing packages only) ----
    _stub_bare_imports(work_dir, copied + [work_test])

    # ---- Execute with per-line coverage -----------------------------------
    lcov_path = os.path.join(work_dir, "oracle_lcov.info")
    cmd = ["node", "--test"]
    if is_ts or any(p.endswith((".ts", ".tsx")) for p in copied):
        cmd.append("--experimental-transform-types")
    cmd += [
        "--experimental-test-coverage",
        "--test-reporter=tap",
        "--test-reporter-destination=stdout",
        "--test-reporter=lcov",
        "--test-reporter-destination=" + lcov_path,
        os.path.relpath(work_test, work_dir),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=work_dir,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(work_dir, ignore_errors=True)
        return {**empty_result, "error": "Node test execution timed out",
                "failure": {"category": "TIMEOUT", "detail": "Node test execution timed out (60s)"}}
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(work_dir, ignore_errors=True)
        return {**empty_result, "error": f"Node test process error: {e}",
                "failure": {"category": "ENVIRONMENT_ERROR", "detail": str(e)}}

    output = (result.stdout or "") + "\n" + (result.stderr or "")

    # ---- Parse TAP for executed/passed/failed counts ----------------------
    test_results = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    pass_match = re.search(r"# pass (\d+)", output)
    fail_match = re.search(r"# fail (\d+)", output)
    skip_match = re.search(r"# skipped (\d+)", output)
    if pass_match:
        test_results["passed"] = int(pass_match.group(1))
    if fail_match:
        test_results["failed"] = int(fail_match.group(1))
    if skip_match:
        test_results["skipped"] = int(skip_match.group(1))
    executed = test_results["passed"] + test_results["failed"]

    # ---- Parse LCOV for EXACT per-line data of the source file ------------
    lcov_data: Optional[Dict[str, Any]] = None
    if os.path.isfile(lcov_path):
        try:
            with open(lcov_path, "r", encoding="utf-8", errors="replace") as f:
                lcov_data = _parse_lcov(f.read(), rel_source)
        except OSError:
            lcov_data = None
    if lcov_data is None:
        # Try matching on basename only (layout differences).
        if os.path.isfile(lcov_path):
            try:
                with open(lcov_path, "r", encoding="utf-8", errors="replace") as f:
                    lcov_data = _parse_lcov(f.read(), os.path.basename(source_file))
            except OSError:
                lcov_data = None
    table_missing: List[int] = []
    if lcov_data is None and executed > 0:
        # Node build without the lcov reporter: fall back to the plain table.
        # Line identity beyond "uncovered" is unknown, so per-line lists stay
        # empty and the number is honestly reported at FILE scope.
        table = _parse_coverage_table(output, source_file)
        if table is not None:
            table_missing = table["missing"]
            lcov_data = {
                "coverage_percent": table["coverage_percent"],
                "lines_total": table["lines_total"],
                "lines_covered": table["lines_covered"],
                "lines_missing": len(table_missing),
                "covered_lines": [],
                "missing_lines": [],
                "sf": table["sf"],
                "functions": [],
            }

    shutil.rmtree(work_dir, ignore_errors=True)

    failure: Optional[Dict[str, str]] = None
    if executed == 0:
        failure = classify_failure(output, is_js=True)
        if failure["category"] == "SOURCE_EXECUTION_ERROR":
            failure = {"category": "TEST_DISCOVERY_ERROR",
                       "detail": failure["detail"]}
        return {
            **empty_result,
            "coverage_percent": 0,
            "passed": False,
            "output": output.strip(),
            "error": output.strip() or "No tests were executed",
            "test_results": test_results,
            "executed": 0,
            "failure": failure,
            "llm_unavailable": False,
        }

    if lcov_data is None:
        # Tests ran but we could not map coverage onto the source file - this
        # is an instrumentation problem, reported honestly (spec section 42).
        # When nothing passed, the real cause (import/module failure) wins.
        if test_results["passed"] == 0:
            failure = classify_failure(output, is_js=True)
        else:
            failure = {"category": "SOURCE_EXECUTION_ERROR",
                       "detail": "coverage report does not contain the source file - "
                                 "the test may not import the module under test"}
        return {
            **empty_result,
            "coverage_percent": 0,
            "passed": False,
            "output": output.strip(),
            "error": failure["detail"],
            "test_results": test_results,
            "executed": executed,
            "functions_tested": executed,
            "functions_passed": test_results["passed"],
            "failure": failure,
            "llm_unavailable": False,
        }

    passed = result.returncode == 0 and test_results["failed"] == 0
    failure = None if passed else classify_failure(output, is_js=True)
    uncovered_segments = _js_uncovered_segments(
        lcov_data["missing_lines"] or table_missing, source_file
    )

    return {
        "coverage_percent": lcov_data["coverage_percent"],
        "passed": passed,
        "output": output.strip(),
        "error": None if passed else output.strip(),
        "lines_total": lcov_data["lines_total"],
        "lines_covered": lcov_data["lines_covered"],
        "lines_missing": lcov_data["lines_missing"],
        "missing_lines": lcov_data["missing_lines"],
        "covered_lines": lcov_data["covered_lines"],
        "test_results": test_results,
        "uncovered_segments": uncovered_segments,
        "functions_tested": executed,
        "functions_passed": test_results["passed"],
        "executed": executed,
        "failure": failure,
        "coverage_scope": "FILE",
        "target_functions": lcov_data.get("functions") or [],
        "llm_unavailable": False,
    }


def run_coverage_for_js_file(test_code: str, source_file: str, test_filename: str = "test_generated.test.js") -> Dict[str, Any]:
    """Run Node.js test runner with per-line coverage on generated JS/TS tests."""
    return _js_coverage_result(test_code, source_file, test_filename)


def run_coverage_for_file(
    test_code: str,
    source_file: str,
    test_filename: str = "",
    max_retries: int = 0
) -> Dict[str, Any]:
    """
    Run pytest (for Python) or Node test runner (for JS/TS) with coverage.
    Returns overall coverage percentage, pass/fail status, output, and uncovered lines.
    """
    ext = os.path.splitext(source_file)[1].lower() if source_file else ""
    is_js = ext in (".js", ".ts", ".jsx", ".tsx")

    if is_js:
        t_name = test_filename or f"test_{os.path.basename(source_file)}.test.js"
        return run_coverage_for_js_file(test_code, source_file, t_name)

    empty_result = {
        "coverage_percent": 0,
        "passed": False,
        "output": "",
        "error": "Invalid or empty Python test code",
        "lines_total": 0,
        "lines_covered": 0,
        "lines_missing": 0,
        "missing_lines": [],
        "covered_lines": [],
        "test_results": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0},
        "uncovered_segments": [],
        "functions_tested": 0,
        "functions_passed": 0,
        # Nothing ran - spec section 6: 0 executed is never a pass.
        "executed": 0,
        "failure": {"category": "TEST_DISCOVERY_ERROR",
                    "detail": "Invalid or empty test code - no tests executed"},
        "coverage_scope": "FILE",
        "llm_unavailable": False
    }

    if not _is_valid_test_code(test_code, is_js=False):
        if test_code and ("quota" in test_code.lower() or "unavailable" in test_code.lower() or "error" in test_code.lower()):
            return {
                **empty_result,
                "error": test_code.strip().lstrip("# ").strip(),
                # Generation-side problem (no suite produced), not an execution
                # failure - surfaced upstream as GENERATION_FAILURE.
                "failure": None,
                "llm_unavailable": True
            }
        return empty_result

    if not source_file or not os.path.exists(source_file):
        return {**empty_result, "error": f"Source file not found: {source_file}",
                "failure": {"category": "ENVIRONMENT_ERROR",
                            "detail": f"Source file not found: {source_file}"}}

    source_dir = os.path.dirname(source_file)
    test_dir = _get_test_dir()
    t_filename = test_filename or f"test_{os.path.basename(source_file)}"
    if not t_filename.endswith('.py'):
        t_filename += '.py'
    # Auto-stub third-party imports that are not installed here (uploaded
    # repositories never ship a virtualenv), and expose the project root so
    # package-qualified imports can resolve too.
    stub_imports = collect_python_imports(source_file, test_code)
    project_root = None
    try:
        import test_framework as _tf
        project_root = _tf.find_project_root(source_dir)
    except Exception:  # noqa: BLE001 - detection is best effort
        project_root = None
    test_path = _write_test_file(
        test_dir, t_filename, test_code, source_dir,
        extra_sys_paths=[project_root] if project_root else None,
        stub_imports=stub_imports,
    )

    if not test_path:
        return {**empty_result, "error": "Failed to write test file",
                "failure": {"category": "ENVIRONMENT_ERROR", "detail": "Failed to write test file"}}

    _cleanup_coverage_json(test_dir)

    coverage_json_path = os.path.join(test_dir, "coverage.json")
    cmd = [
        "pytest", test_path,
        f"--cov={source_dir}",
        f"--cov-report=json:{coverage_json_path}",
        "--tb=short", "-v"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=source_dir if source_dir else test_dir
        )
    except subprocess.TimeoutExpired:
        return {**empty_result, "error": "Pytest coverage run timed out",
                "failure": {"category": "TIMEOUT", "detail": "Pytest coverage run timed out (60s)"}}
    except Exception as e:
        return {**empty_result, "error": f"Pytest execution error: {str(e)}",
                "failure": {"category": "ENVIRONMENT_ERROR", "detail": str(e)}}

    output = result.stdout + result.stderr
    test_results = _parse_test_results(output)
    executed = (test_results.get("passed", 0) + test_results.get("failed", 0)
                + test_results.get("errors", 0))
    cov_data = _parse_coverage_json(test_dir, source_file)
    uncovered_segments = analyze_uncovered_lines(cov_data["missing_lines"], source_file)

    # A run where nothing executed is NEVER a pass (spec section 6), even if
    # pytest exits 0 (e.g. everything skipped or nothing collected).
    failure: Optional[Dict[str, str]] = None
    if executed == 0:
        passed = False
        failure = classify_failure(output, is_js=False)
        if failure["category"] == "SOURCE_EXECUTION_ERROR":
            failure = {"category": "TEST_DISCOVERY_ERROR", "detail": failure["detail"]}
    else:
        passed = result.returncode == 0 and test_results.get("failed", 0) == 0 \
            and test_results.get("errors", 0) == 0
        if not passed:
            failure = classify_failure(output, is_js=False)

    functions_tested = test_results.get("passed", 0) + test_results.get("failed", 0)
    functions_passed = test_results.get("passed", 0)

    return {
        "coverage_percent": cov_data["coverage_percent"],
        "passed": passed,
        "output": output,
        "error": None if passed else output,
        "lines_total": cov_data["lines_total"],
        "lines_covered": cov_data["lines_covered"],
        "lines_missing": cov_data["lines_missing"],
        "missing_lines": cov_data["missing_lines"],
        "covered_lines": cov_data["covered_lines"],
        "test_results": test_results,
        "uncovered_segments": uncovered_segments,
        "functions_tested": functions_tested,
        "functions_passed": functions_passed,
        "executed": executed,
        "failure": failure,
        "coverage_scope": "FILE",
        "llm_unavailable": False
    }


def run_coverage(test_code: str, source_file: str) -> Dict[str, Any]:
    """Run pytest with coverage on generated test code against source file."""
    return run_coverage_for_file(test_code, source_file, "test_oracle_generated.py")


def run_tests_against_refactor(test_code: str, refactored_code: str, is_js: bool = False, func_name_filter: Optional[str] = None) -> Dict[str, Any]:
    """
    Run unit tests against refactored code to verify functional correctness.
    Supports Python and JavaScript.
    
    Args:
        test_code: The test code to run
        refactored_code: The refactored code to test against
        is_js: Whether the code is JavaScript
        func_name_filter: Optional function name to filter tests (e.g., "calc_price" will run tests matching "test_calc_price*")
    """
    if not refactored_code or not test_code:
        return {"passed": False, "output": "No code or tests to verify"}

    tmp_dir = _get_test_dir()

    if is_js:
        refactored_path = os.path.join(tmp_dir, "oracle_refactored_module.js")
        test_path = os.path.join(tmp_dir, "test_oracle_refactored.test.js")

        try:
            with open(refactored_path, 'w', encoding='utf-8') as f:
                f.write(refactored_code)
            
            adj_test = re.sub(r"require\(['\"][^'\"]+['\"]\)", "require('./oracle_refactored_module.js')", test_code)
            with open(test_path, 'w', encoding='utf-8') as f:
                f.write(adj_test)

            res = subprocess.run(
                ["node", "--test", test_path],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=tmp_dir
            )
            return {"passed": res.returncode == 0, "output": res.stdout + res.stderr}
        except Exception as e:
            return {"passed": False, "output": str(e)}

    # Python
    refactored_path = os.path.join(tmp_dir, "oracle_refactored_module.py")
    test_path = os.path.join(tmp_dir, "test_oracle_refactored.py")

    if not _is_valid_test_code(test_code, is_js=False):
        return {"passed": False, "output": "Invalid test code for verification"}

    try:
        with open(refactored_path, 'w', encoding='utf-8') as f:
            f.write(refactored_code)
    except Exception as e:
        return {"passed": False, "output": f"Failed to write refactored module: {str(e)}"}

    lines = test_code.split('\n')
    adjusted_lines = []
    has_oracle_import = False

    for line in lines:
        stripped = line.strip()
        if 'oracle_refactored_module' in stripped:
            has_oracle_import = True
        adjusted_lines.append(line)

    if not has_oracle_import:
        insert_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('import ') or stripped.startswith('from '):
                insert_idx = i + 1
            elif stripped and not stripped.startswith('#') and not stripped.startswith('"""') and not stripped.startswith("'''"):
                break
        adjusted_lines.insert(insert_idx, 'import oracle_refactored_module')

    adjusted_test_code = '\n'.join(adjusted_lines)

    try:
        with open(test_path, 'w', encoding='utf-8') as f:
            f.write(adjusted_test_code)
    except Exception as e:
        return {"passed": False, "output": f"Failed to write test file: {str(e)}"}

    cmd = ["pytest", test_path, "--tb=short", "-q"]
    if func_name_filter:
        # Filter tests to only run those matching the function name
        # Test naming convention: test_<function_name>_<scenario>
        cmd.extend(["-k", func_name_filter])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=tmp_dir
        )
    except subprocess.TimeoutExpired:
        return {"passed": False, "output": "Test run timed out"}
    except Exception as e:
        return {"passed": False, "output": f"Subprocess error: {str(e)}"}

    output = result.stdout + result.stderr
    passed = result.returncode == 0

    return {"passed": passed, "output": output}
