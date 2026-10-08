"""
Lightweight, one-time test-environment detection for CodeOracle.

Reads a handful of project configuration files ONLY (package.json, tsconfig,
pytest config, ...). It never executes tests, never calls the LLM, and is
invoked lazily on the first on-demand test generation for a job - so initial
repository analysis speed is completely unaffected (spec sections 26/36/46).

Result shape follows spec section 45:

    {
      "language": "typescript",
      "framework": "nestjs",
      "testRunner": "jest",
      "coverageTool": "istanbul",
      "moduleSystem": "esm",
      "importStyle": "esm",
      "decorators": true,
      "testCommand": "npm test",
      "configFiles": ["package.json"]
    }
"""
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("codeoracle")

# Never read more than this many bytes of any config file.
_MAX_CONFIG_BYTES = 256 * 1024

# Walk-up limit when searching for the project root from the source files.
_MAX_ROOT_WALK = 6

_JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_TS_EXTS = (".ts", ".tsx")


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(_MAX_CONFIG_BYTES)
    except OSError:
        return ""


def _read_json(path: str) -> Optional[Any]:
    text = _read_text(path)
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # package.json sometimes carries trailing commas / comments in the
        # wild; strip those two tolerantly before giving up.
        stripped = re.sub(r",\s*([}\]])", r"\1", text)
        stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.MULTILINE)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None


def find_project_root(start_dir: str) -> Optional[str]:
    """Walk up from start_dir looking for a project manifest. Cheap."""
    current = os.path.abspath(start_dir or "")
    for _ in range(_MAX_ROOT_WALK):
        for name in ("package.json", "pyproject.toml", "pytest.ini", "setup.py", "setup.cfg"):
            if os.path.isfile(os.path.join(current, name)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def project_root_for_files(source_files: Any) -> Optional[str]:
    """Derive the project root from a job's source_files map/list."""
    paths: List[str] = []
    if isinstance(source_files, dict):
        paths = [p for p in source_files.values() if isinstance(p, str)]
    elif isinstance(source_files, (list, tuple)):
        paths = [p for p in source_files if isinstance(p, str)]
    if not paths:
        return None
    try:
        common = os.path.commonpath([os.path.dirname(os.path.abspath(p)) for p in paths])
    except ValueError:
        common = os.path.dirname(os.path.abspath(paths[0]))
    root = find_project_root(common)
    if root:
        return root
    return common


def _detect_python(root: Optional[str], source_files: Any) -> Dict[str, Any]:
    config_files: List[str] = []
    deps_text = ""
    framework = "unittest"
    coverage_tool = "coverage.py"

    roots = [root] if root else []
    # Also honour manifests sitting next to individual source files.
    for p in (source_files or {}).values() if isinstance(source_files, dict) else []:
        d = os.path.dirname(p)
        if d and d not in roots:
            roots.append(d)

    for base in roots:
        for name in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                config_files.append(name)
                deps_text += "\n" + _read_text(path)
        req = os.path.join(base, "requirements.txt")
        if os.path.isfile(req):
            config_files.append("requirements.txt")
            deps_text += "\n" + _read_text(req)
        if os.path.isfile(os.path.join(base, "conftest.py")):
            config_files.append("conftest.py")

    lowered = deps_text.lower()
    if (
        "[tool.pytest" in lowered
        or "pytest" in lowered
        or "pytest.ini" in " ".join(config_files)
        or "conftest.py" in config_files
    ):
        framework = "pytest"
    if "pytest-cov" not in lowered and "pytest-cov" not in deps_text:
        coverage_tool = "coverage.py"

    return {
        "language": "python",
        "framework": "python",
        "testRunner": framework,
        "coverageTool": coverage_tool,
        "moduleSystem": "none",
        "importStyle": "python",
        "decorators": False,
        "testCommand": f"{framework} (run by CodeOracle)" if framework == "pytest" else "python -m unittest",
        "configFiles": sorted(set(config_files)),
    }


def _detect_js(root: Optional[str], source_files: Any) -> Dict[str, Any]:
    config_files: List[str] = []
    pkg: Dict[str, Any] = {}
    tsconfig_text = ""

    pkg_path = os.path.join(root, "package.json") if root else ""
    if pkg_path and os.path.isfile(pkg_path):
        loaded = _read_json(pkg_path)
        if isinstance(loaded, dict):
            pkg = loaded
            config_files.append("package.json")

    tsconfig_path = os.path.join(root, "tsconfig.json") if root else ""
    if tsconfig_path and os.path.isfile(tsconfig_path):
        tsconfig_text = _read_text(tsconfig_path)
        config_files.append("tsconfig.json")

    # Any config file that names a runner counts as evidence.
    if root:
        for name in ("jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs",
                     "vitest.config.ts", "vitest.config.js", "vite.config.ts", "vitest.config.mjs",
                     ".babelrc", "babel.config.js", "karma.conf.js"):
            if os.path.isfile(os.path.join(root, name)):
                config_files.append(name)

    deps: Dict[str, Any] = {}
    deps.update(pkg.get("dependencies") or {})
    deps.update(pkg.get("devDependencies") or {})

    # Language: manifest hint first, then actual uploaded files.
    has_ts_config = bool(tsconfig_text)
    has_ts_dep = "typescript" in deps
    has_ts_file = False
    values = source_files.values() if isinstance(source_files, dict) else (source_files or [])
    for p in values:
        if isinstance(p, str) and p.endswith(_TS_EXTS):
            has_ts_file = True
            break
    language = "typescript" if (has_ts_config or has_ts_dep or has_ts_file) else "javascript"

    # Test runner / framework detection (spec section 11).
    if "@nestjs/testing" in deps or "@nestjs/core" in deps or "@nestjs/common" in deps:
        framework = "nestjs"
    elif "react" in deps:
        framework = "react"
    else:
        framework = "javascript"

    if "vitest" in deps or any(c.startswith("vitest") for c in config_files):
        runner = "vitest"
    elif "jest" in deps or "ts-jest" in deps or any(c.startswith("jest") for c in config_files):
        runner = "jest"
    elif "mocha" in deps:
        runner = "mocha"
    else:
        runner = "node:test"

    coverage_tool = "istanbul" if runner == "jest" else ("v8" if runner in ("vitest", "node:test") else "v8")

    module_system = "cjs"
    if (pkg.get("type") or "").lower() == "module":
        module_system = "esm"
    elif language == "typescript" and tsconfig_text:
        m = re.search(r'"module"\s*:\s*"(esnext|es2020|es2022|es2015|nodenext|node16)"', tsconfig_text)
        if m:
            module_system = "esm"

    decorators = bool(re.search(r'"experimentalDecorators"\s*:\s*true', tsconfig_text))

    scripts = pkg.get("scripts") or {}
    test_cmd = scripts.get("test") or ("npm test" if pkg else f"{runner} (run by CodeOracle)")

    return {
        "language": language,
        "framework": framework,
        "testRunner": runner,
        "coverageTool": coverage_tool,
        "moduleSystem": module_system,
        # What the *generated test file* should use (CodeOracle executes with
        # the standalone node:test runner; see _import_rules_for).
        "importStyle": module_system,
        "decorators": decorators,
        "testCommand": test_cmd,
        "configFiles": sorted(set(config_files)),
    }


def detect_test_framework(root: Optional[str], source_files: Any) -> Dict[str, Any]:
    """Detect the repository's test environment from config files alone.

    Cheap: a handful of small file reads, no subprocesses, no network, no LLM.
    """
    has_py = False
    has_js = False
    values = []
    if isinstance(source_files, dict):
        values = list(source_files.values())
    elif isinstance(source_files, (list, tuple)):
        values = list(source_files)
    for p in values:
        if not isinstance(p, str):
            continue
        if p.endswith(".py"):
            has_py = True
        elif p.endswith(_JS_EXTS):
            has_js = True

    # Python manifests win for .py files; JS manifest for JS/TS repos.
    if has_py and not has_js:
        result = _detect_python(root, source_files)
    elif has_js and not has_py:
        result = _detect_js(root, source_files)
    else:
        # Mixed repo: report per the majority, runners are per-file anyway.
        result = _detect_js(root, source_files) if has_js else _detect_python(root, source_files)

    logger.info(
        "[TEST] framework detected language=%s runner=%s framework=%s module=%s configs=%s",
        result["language"], result["testRunner"], result["framework"],
        result["moduleSystem"], ",".join(result["configFiles"]) or "-",
    )
    return result


def get_job_framework(job: Dict[str, Any]) -> Dict[str, Any]:
    """Return (and lazily cache on the job) its detected test framework.

    Called ONLY from the on-demand test-generation flow - never during
    initial analysis.
    """
    if not isinstance(job, dict):
        return detect_test_framework(None, None)
    cached = job.get("test_framework")
    if isinstance(cached, dict) and cached.get("testRunner"):
        return cached
    root = project_root_for_files(job.get("source_files"))
    fw = detect_test_framework(root, job.get("source_files"))
    try:
        job["test_framework"] = fw
    except Exception:  # noqa: BLE001 - best effort cache only
        pass
    return fw
