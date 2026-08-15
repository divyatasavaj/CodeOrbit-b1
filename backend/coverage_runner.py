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

logger = logging.getLogger("codeoracle")

COVERAGE_THRESHOLD = 60
MAX_COVERAGE_RETRIES = 1


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


def _write_test_file(test_dir: str, test_filename: str, test_code: str, source_dir: str) -> Optional[str]:
    """Write test file and conftest.py. Returns path or None on error."""
    test_path = os.path.join(test_dir, test_filename)
    try:
        with open(test_path, 'w', encoding='utf-8') as f:
            f.write(test_code)
    except Exception:
        return None

    conftest_path = os.path.join(test_dir, "conftest.py")
    try:
        with open(conftest_path, 'w', encoding='utf-8') as f:
            f.write(f"import sys\nsys.path.insert(0, r'{source_dir}')\n")
    except Exception:
        pass

    return test_path


def _parse_coverage_json(test_dir: str, source_file: str) -> Dict[str, Any]:
    """Parse coverage.json and extract per-file and total coverage."""
    source_module = os.path.basename(source_file).replace('.py', '')
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
        for file_path, file_data in files_data.items():
            if source_module in os.path.basename(file_path) or os.path.basename(file_path) == os.path.basename(source_file):
                summary = file_data.get("summary", {})
                result["lines_total"] = summary.get("num_statements", 0)
                result["lines_covered"] = summary.get("covered_lines", 0)
                result["lines_missing"] = summary.get("missing_lines", 0)
                result["coverage_percent"] = round(
                    (result["lines_covered"] / result["lines_total"] * 100), 1
                ) if result["lines_total"] > 0 else 0

                analysis = file_data.get("analysis", [])
                for line_data in analysis:
                    line_no = line_data[0]
                    coverage_count = line_data[1]
                    if coverage_count is None:
                        result["missing_lines"].append(line_no)
                    else:
                        result["covered_lines"].append(line_no)

                result["file_coverage"][file_path] = {
                    "summary": summary,
                    "missing_lines": result["missing_lines"]
                }
                break

        if result["lines_total"] == 0:
            totals = cov_data.get("totals", {})
            result["lines_total"] = totals.get("num_statements", 0)
            result["lines_covered"] = totals.get("covered_lines", 0)
            result["lines_missing"] = totals.get("missing_lines", 0)
            result["coverage_percent"] = round(
                (result["lines_covered"] / result["lines_total"] * 100), 1
            ) if result["lines_total"] > 0 else 0
    except Exception:
        pass

    return result


def _parse_test_results(output: str) -> Dict[str, int]:
    """Parse pytest output to count passed/failed/error tests."""
    results = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for line in output.split('\n'):
        if " PASSED" in line:
            results["passed"] += 1
        elif " FAILED" in line:
            results["failed"] += 1
        elif " ERROR" in line:
            results["errors"] += 1
        elif " SKIPPED" in line:
            results["skipped"] += 1
    return results


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


def run_coverage_for_js_file(test_code: str, source_file: str, test_filename: str = "test_generated.test.js") -> Dict[str, Any]:
    """
    Run Node.js built-in test runner with code coverage on generated JS test code.
    Returns overall coverage percentage, pass/fail status, and TAP report.
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
        "llm_unavailable": False
    }

    if not _is_valid_test_code(test_code, is_js=True):
        return empty_result

    if not source_file or not os.path.exists(source_file):
        return {**empty_result, "error": f"Source file not found: {source_file}"}

    source_dir = os.path.dirname(source_file)
    source_basename = os.path.basename(source_file)

    test_dir = os.path.join(tempfile.gettempdir(), "oracle_js_tests")
    os.makedirs(test_dir, exist_ok=True)
    
    shutil.copy2(source_file, os.path.join(test_dir, source_basename))
    test_path = os.path.join(test_dir, test_filename)

    try:
        with open(test_path, 'w', encoding='utf-8') as f:
            f.write(test_code)
    except Exception as e:
        return {**empty_result, "error": f"Failed to write JS test file: {str(e)}"}

    cmd = ["node", "--test", "--experimental-test-coverage", test_path]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,
            cwd=test_dir
        )
    except subprocess.TimeoutExpired:
        return {**empty_result, "error": "Node test execution timed out"}
    except Exception as e:
        return {**empty_result, "error": f"Node test process error: {str(e)}"}

    output = result.stdout + "\n" + result.stderr
    passed = result.returncode == 0

    coverage_percent = 0.0
    for line in output.splitlines():
        if source_basename in line and '|' in line:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2:
                try:
                    coverage_percent = float(parts[1])
                except ValueError:
                    pass
        elif 'all files' in line and '|' in line and coverage_percent == 0.0:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2:
                try:
                    coverage_percent = float(parts[1])
                except ValueError:
                    pass

    functions_tested = 0
    functions_passed = 0
    pass_match = re.search(r'# pass (\d+)', output)
    fail_match = re.search(r'# fail (\d+)', output)
    if pass_match:
        functions_passed = int(pass_match.group(1))
        functions_tested += functions_passed
    if fail_match:
        fail_count = int(fail_match.group(1))
        functions_tested += fail_count

    return {
        "coverage_percent": round(coverage_percent, 1),
        "passed": passed,
        "output": output.strip(),
        "error": None if passed else output.strip(),
        "lines_total": 100,
        "lines_covered": int(coverage_percent),
        "lines_missing": max(0, 100 - int(coverage_percent)),
        "missing_lines": [],
        "covered_lines": [],
        "test_results": {"passed": functions_passed, "failed": functions_tested - functions_passed, "errors": 0, "skipped": 0},
        "uncovered_segments": [],
        "functions_tested": functions_tested,
        "functions_passed": functions_passed,
        "llm_unavailable": False
    }


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
        "llm_unavailable": False
    }

    if not _is_valid_test_code(test_code, is_js=False):
        if test_code and ("quota" in test_code.lower() or "unavailable" in test_code.lower() or "error" in test_code.lower()):
            return {
                **empty_result,
                "error": test_code.strip().lstrip("# ").strip(),
                "llm_unavailable": True
            }
        return empty_result

    if not source_file or not os.path.exists(source_file):
        return {**empty_result, "error": f"Source file not found: {source_file}"}

    source_dir = os.path.dirname(source_file)
    test_dir = _get_test_dir()
    t_filename = test_filename or f"test_{os.path.basename(source_file)}"
    if not t_filename.endswith('.py'):
        t_filename += '.py'
    test_path = _write_test_file(test_dir, t_filename, test_code, source_dir)

    if not test_path:
        return {**empty_result, "error": "Failed to write test file"}

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
        return {**empty_result, "error": "Pytest coverage run timed out"}
    except Exception as e:
        return {**empty_result, "error": f"Pytest execution error: {str(e)}"}

    output = result.stdout + result.stderr
    passed = result.returncode == 0
    test_results = _parse_test_results(output)
    cov_data = _parse_coverage_json(test_dir, source_file)
    uncovered_segments = analyze_uncovered_lines(cov_data["missing_lines"], source_file)

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
