"""Deterministic tests for the min-coverage generation loop (spec section 30).

Monkeypatches the LLM + measurement layers so attempt sequences are scripted,
then asserts attempt counts, best-result selection, exact-65% acceptance, and
that no value is ever faked. Run: python test_cov_gate.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config          # noqa: E402
import coverage_runner  # noqa: E402
import main             # noqa: E402
import llm              # noqa: E402
import test_framework as tf  # noqa: E402

PASS = 0
FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS {name}" + (f" [{extra}]" if extra != "" else ""))
    else:
        FAIL += 1
        print(f"FAIL {name}" + (f" [{extra}]" if extra != "" else ""))


def cov(pct, passed=True):
    return {
        "coverage_percent": pct,
        "passed": passed,
        "executed": 1,
        "missing_lines": [],
        "uncovered_segments": [],
        "test_results": {"passed": 1 if passed else 0, "failed": 0 if passed else 1},
    }


class FakeLLM:
    def __init__(self):
        self.initial_calls = 0
        self.improve_calls = 0
        self.last_brief = ""
        self.ctx_seen = None
        self.function_ctx_calls = 0

    async def generate_tests_batch(self, funcs, source_code, source_file):
        self.initial_calls += 1
        return "INITIAL_SUITE"

    async def generate_function_tests(self, ctx):
        self.function_ctx_calls += 1
        self.ctx_seen = ctx
        return "INITIAL_SUITE"

    async def generate_tests_for_coverage(self, func, uncovered_lines, source_file="", ctx=None):
        self.improve_calls += 1
        self.last_brief = uncovered_lines
        self.ctx_seen = ctx
        return f"IMPROVED_SUITE_{self.improve_calls}"


def run_loop(seq, seed=None, ctx=None, func=None):
    """Run _generate_with_min_coverage against a scripted measurement sequence."""
    fake_llm = FakeLLM()
    state = {"n": 0}

    async def fake_measure(test_code, source_file, f):
        idx = min(state["n"], len(seq) - 1)
        state["n"] += 1
        item = seq[idx]
        return item if isinstance(item, dict) else cov(item)

    real = (main._measure_suite, llm.generate_tests_batch,
            llm.generate_function_tests, llm.generate_tests_for_coverage)
    main._measure_suite = fake_measure
    llm.generate_tests_batch = fake_llm.generate_tests_batch
    llm.generate_function_tests = fake_llm.generate_function_tests
    llm.generate_tests_for_coverage = fake_llm.generate_tests_for_coverage
    try:
        code, best, attempts = asyncio.run(
            main._generate_with_min_coverage(
                func or {"name": "fn", "body": "def fn(): pass", "args": []},
                "mod.py", "def fn(): pass", seed, ctx=ctx,
            )
        )
    finally:
        (main._measure_suite, llm.generate_tests_batch,
         llm.generate_function_tests, llm.generate_tests_for_coverage) = real
    return code, best, attempts, fake_llm


FUNC = {"name": "fn", "body": "def fn():\n    pass", "args": []}

# ---------------------------------------------------------------- defaults
check("default MIN_TEST_COVERAGE is 65", config.MIN_TEST_COVERAGE == 65.0, config.MIN_TEST_COVERAGE)
check("default TEST_MAX_ATTEMPTS is 3", config.TEST_MAX_ATTEMPTS == 3, config.TEST_MAX_ATTEMPTS)

# ------------------------------------------------- boundary: exactly 65 valid
check("meets_min(65.0) is True (>= not >)", coverage_runner.meets_min_coverage(cov(65.0)))
check("meets_min(64.9) is False", not coverage_runner.meets_min_coverage(cov(64.9)))
check("meets_min(42) is False", not coverage_runner.meets_min_coverage(cov(42)))
check("meets_min(0) is False", not coverage_runner.meets_min_coverage(cov(0)))

# ------------------------------------------------------- Case 1: first try
code, best, attempts, f = run_loop([cov(70)])
check("Case1 attempts == 1", attempts == 1, attempts)
check("Case1 no second LLM call", f.improve_calls == 0, f.improve_calls)
check("Case1 returns initial suite", code == "INITIAL_SUITE")
check("Case1 coverage 70 returned", best["coverage_percent"] == 70)
check("Case1 target met", coverage_runner.meets_min_coverage(best))

# ------------------------------------------- Case 2: first below, second ok
code, best, attempts, f = run_loop([cov(48), cov(69)])
check("Case2 attempts == 2", attempts == 2, attempts)
check("Case2 improvement call made", f.improve_calls == 1, f.improve_calls)
check("Case2 returns attempt 2 code", code == "IMPROVED_SUITE_1")
check("Case2 coverage 69 (not faked)", best["coverage_percent"] == 69)
check("Case2 target met", coverage_runner.meets_min_coverage(best))
check("Case2 brief has current coverage", "Current measured coverage: 48%" in f.last_brief, f.last_brief.splitlines()[0])
check("Case2 brief has required minimum", "Required minimum coverage: 65%" in f.last_brief)

# --------------------------------- Case 3: all attempts below -> best actual
code, best, attempts, f = run_loop([cov(40), cov(51), cov(61)])
check("Case3 attempts == 3 (budget)", attempts == 3, attempts)
check("Case3 improvement calls == 2", f.improve_calls == 2, f.improve_calls)
check("Case3 returns best attempt (61)", best["coverage_percent"] == 61, best["coverage_percent"])
check("Case3 target NOT met", not coverage_runner.meets_min_coverage(best))
check("Case3 value not faked to 65", best["coverage_percent"] != 65)

# ------------------------------------ Case 4: exactly 65 succeeds, no retry
code, best, attempts, f = run_loop([cov(65)])
check("Case4 attempts == 1", attempts == 1, attempts)
check("Case4 no improvement call", f.improve_calls == 0, f.improve_calls)
check("Case4 target met at exactly 65", coverage_runner.meets_min_coverage(best))

# ------------------------- Case 5: coverage ok but tests FAIL -> keep trying
code, best, attempts, f = run_loop(
    [cov(80, passed=False), cov(70, passed=True)]
)
check("Case5 retries despite coverage 80 (tests failed)", attempts == 2, attempts)
check("Case5 final suite passes", bool(best["passed"]))
check("Case5 best is the passing suite", code == "IMPROVED_SUITE_1")
check("Case5 passing coverage 70", best["coverage_percent"] == 70)

# ------------- Case 5b: all candidates failing -> no success claimed
code, best, attempts, f = run_loop([cov(90, passed=False), cov(95, passed=False), cov(70, passed=False)])
check("Case5b budget exhausted at 3", attempts == 3, attempts)
check("Case5b best kept as highest measured", best["coverage_percent"] == 95, best["coverage_percent"])
check("Case5b not a suite success", not main._suite_success(best))

# ------------------- Best-result selection: keep 61 over later 57 (spec 9)
code, best, attempts, f = run_loop([cov(43), cov(61), cov(57)])
check("Best-of picks 61 not 57", best["coverage_percent"] == 61, best["coverage_percent"])
check("Best-of code is attempt 2", code == "IMPROVED_SUITE_1")

# ------------------- Seed path (trivial smoke tests) skips initial LLM call
code, best, attempts, f = run_loop([cov(100)], seed="SMOKE_SUITE")
check("Seed: initial LLM skipped", f.initial_calls == 0, f.initial_calls)
check("Seed: attempt 1 measured", attempts == 1)
check("Seed: 100% -> no improve", f.improve_calls == 0)

# ------------------------------------------- Loop never exceeds the budget
code, best, attempts, f = run_loop([cov(10), cov(10), cov(10), cov(10), cov(10)])
check("Hard cap at TEST_MAX_ATTEMPTS", attempts == config.TEST_MAX_ATTEMPTS, attempts)

# ------------------------------------------- Cache validation (spec 23)
usable = main._cached_test_entry_usable
check("cache: cov72 + passed -> usable", usable({"test_code": "x", "coverage_percent": 72, "passed": True}))
check("cache: cov54 + passed -> rejected", not usable({"test_code": "x", "coverage_percent": 54, "passed": True}))
check("cache: cov72 + FAILED -> rejected", not usable({"test_code": "x", "coverage_percent": 72, "passed": False}))
check("cache: below-target but exhausted -> accepted (no infinite retry)",
      usable({"test_code": "x", "coverage_percent": 54, "passed": True, "improvement_exhausted": True}))
check("cache: no test_code -> rejected", not usable({"coverage_percent": 99, "passed": True}))
check("cache: missing fields -> rejected", not usable(None))

# ------------------------------------- Cache key includes target (spec 22)
k_plain = cache_key = __import__("cache").operation_cache_key("src", "tests", config.PROMPT_VERSION_TESTS, config.MODEL, "f1")
k_gated = __import__("cache").operation_cache_key("src", "tests", config.TESTS_CACHE_VERSION, config.MODEL, "f1")
check("cache key differs once target is folded in", k_plain != k_gated)
check("TESTS_CACHE_VERSION encodes target", f"mc{int(config.MIN_TEST_COVERAGE)}" in config.TESTS_CACHE_VERSION,
      config.TESTS_CACHE_VERSION)

# ------------------------------------------------- Frontend parity constant
try:
    import re
    src = open(os.path.join(os.path.dirname(__file__), "..", "frontend", "src", "components", "TestsTab.jsx"),
               encoding="utf-8").read()
    m = re.search(r"COVERAGE_THRESHOLD\s*=\s*(\d+)", src)
    check("frontend threshold mirrors backend 65", m is not None and int(m.group(1)) == int(config.MIN_TEST_COVERAGE),
          m.group(1) if m else "missing")
except FileNotFoundError:
    print("SKIP frontend threshold check (path not found)")

# ------------------------------------- Status codes (spec sections 32/34)
st = main.compute_test_status
check("status: executed+passed+target -> SUCCESS",
      st({"executed": 2, "passed": True, "coverage_percent": 70}) == "SUCCESS")
check("status: passed but below target -> TARGET_NOT_REACHED",
      st({"executed": 1, "passed": True, "coverage_percent": 40}) == "TARGET_NOT_REACHED")
check("status: ran but failed -> TEST_FAILURE",
      st({"executed": 3, "passed": False, "coverage_percent": 80}) == "TEST_FAILURE")
check("status: nothing executed -> EXECUTION_FAILURE",
      st({"executed": 0, "passed": False, "coverage_percent": 0}) == "EXECUTION_FAILURE")
check("status: llm unavailable -> GENERATION_FAILURE",
      st({"llm_unavailable": True}) == "GENERATION_FAILURE")
check("status: never called target -> GENERATION_FAILURE",
      st({"executed": 3, "passed": True, "coverage_percent": 100,
          "exercises_target": False}) == "GENERATION_FAILURE")
check("status: generation flag beats execution",
      st({"executed": 0, "generation_failed": True}) == "GENERATION_FAILURE")
check("status: non-dict -> GENERATION_FAILURE", st(None) == "GENERATION_FAILURE")

# ------------------------- Placeholder gate (spec sections 7/40)
gate = main._test_references_function
check("gate: real call passes",
      gate("import g\nassert g.classify(3) == 'pos'", {"name": "classify"}))
check("gate: assert True rejected",
      not gate("def test_x():\n    assert True", {"name": "classify"}))
check("gate: typeof-only check rejected",
      not gate("test('x', () => assert.ok(typeof src.classify === 'function'));",
               {"name": "classify"}))
check("gate: empty code rejected", not gate("", {"name": "classify"}))
check("gate: method call via object passes",
      gate("const s = new Svc();\ns.getDashboard('week');",
           {"name": "getDashboard", "class_name": "Svc"}))
check("gate: python constructor via __new__ passes",
      gate("import m\nobj = m.Thing.__new__(m.Thing)",
           {"name": "__init__", "class_name": "Thing"}))
check("gate: js constructor via new passes",
      gate("const s = new Svc({});", {"name": "constructor", "class_name": "Svc"}))
check("gate: no name -> conservative pass", gate("anything()", {"name": ""}))

# ---------------- Placeholder suites can never claim success (spec 7)
seq = [{**cov(66), "exercises_target": False},
       {**cov(70), "exercises_target": False},
       {**cov(72), "exercises_target": False}]
code, best, attempts, f = run_loop(seq)
check("placeholder suite never succeeds (budget spent)", attempts == 3, attempts)
check("placeholder coverage still measured honestly", best["coverage_percent"] == 72)
check("placeholder result status GENERATION_FAILURE",
      st(best) == "GENERATION_FAILURE")

seq = [{**cov(30), "exercises_target": False}, {**cov(70), "exercises_target": True}]
code, best, attempts, f = run_loop(seq)
check("exercising suite wins and stops the loop", attempts == 2, attempts)
check("fixed suite status SUCCESS", st(best) == "SUCCESS")

# ---------------- Diagnosis flows into the improvement brief (spec 31)
brief = main._uncovered_brief({
    "coverage_percent": 12, "passed": False, "executed": 0,
    "failure": {"category": "MODULE_NOT_FOUND", "detail": "Cannot find module './x'"},
    "missing_lines": [3, 4], "uncovered_segments": [], "test_results": {},
})
check("brief carries failure category", "DIAGNOSIS" in brief and "MODULE_NOT_FOUND" in brief)
check("brief says nothing executed", "NO tests were executed" in brief)
brief2 = main._uncovered_brief({
    "coverage_percent": 50, "passed": False, "executed": 4,
    "failure": {"category": "ASSERTION_FAILURE", "detail": "assert 1 == 2"},
    "missing_lines": [9], "uncovered_segments": [], "test_results": {"failed": 1},
})
check("brief says suite failed when it ran",
      "FAILED" in brief2 and "ASSERTION_FAILURE" in brief2)
check("brief keeps uncovered lines", "Uncovered source lines: 9" in brief2)
check("unusable brief demands a real call", "CALL" in main._unusable_brief({"name": "f"}))

# ---------------- Context builder: complete, untruncated (spec 8-10)
PY_SRC = '''import os
import json


def top_level(a):
    return a + 1


class Widget:
    def __init__(self, size):
        self.size = size

    def render(self):
        return f"size={self.size}"
'''
ctx = main.build_test_context(
    {"name": "render", "class_name": "Widget", "lineno": 13,
     "body": 'def render(self):\n        return f"size={self.size}"'},
    "/repo/pkg/widget.py", PY_SRC,
    {"language": "python", "testRunner": "pytest", "moduleSystem": "none"},
)
check("ctx: function source is the exact slice",
      ctx["function_source"] == '    def render(self):\n        return f"size={self.size}"',
      repr(ctx["function_source"]))
check("ctx: class context includes ctor + target",
      "def __init__" in ctx["class_source"] and "def render" in ctx["class_source"])
check("ctx: import lines collected", ctx["import_lines"] == ["import os", "import json"],
      ctx["import_lines"])
check("ctx: python import example", ctx["import_example"] == "import widget")
check("ctx: target folded in", ctx["target"] == config.MIN_TEST_COVERAGE)

fctx = main.build_test_context(
    {"name": "top_level", "lineno": 5,
     "body": "def top_level(a):\n    return a + 1"},
    "/r/m.py", PY_SRC,
)
check("ctx: one-liner-accurate function slice",
      fctx["function_source"] == "def top_level(a):\n    return a + 1",
      repr(fctx["function_source"]))

JS_SRC = '''const path = require('path');
const { helper } = require('./helper.js');

function compute(x) {
  return x * 2;
}
module.exports = { compute };
'''
jctx = main.build_test_context(
    {"name": "compute", "lineno": 4,
     "body": "function compute(x) {\n  return x * 2;\n}"},
    "/repo/src/calc.js", JS_SRC,
    {"language": "javascript", "testRunner": "node:test", "moduleSystem": "cjs"},
)
check("ctx: js require imports collected",
      jctx["import_lines"] == ["const path = require('path');",
                               "const { helper } = require('./helper.js');"],
      jctx["import_lines"])
check("ctx: cjs import example", jctx["import_example"] == "const { /* named exports */ } = require('./calc.js')",
      jctx["import_example"])
check("ctx: js function slice spans the real body",
      jctx["function_source"] == "function compute(x) {\n  return x * 2;\n}",
      repr(jctx["function_source"]))
ectx = main.build_test_context(
    {"name": "run", "lineno": 1, "body": "export function run() {}"},
    "/r/analytics.service.ts", "export function run(): void {}\n",
    {"language": "typescript", "testRunner": "jest", "moduleSystem": "esm"},
)
check("ctx: esm import example uses .ts extension",
      ectx["import_example"] == "import { /* named exports */ } from './analytics.service.ts'",
      ectx["import_example"])

# ---------------- _measure_suite keeps file percent + FUNCTION scope
_real_run = coverage_runner.run_coverage_for_file


def _fake_run(test_code, source_file, test_filename=""):
    return {"coverage_percent": 60, "passed": True, "executed": 1,
            "covered_lines": [1, 2], "missing_lines": [3, 4, 5],
            "test_results": {"passed": 1}, "uncovered_segments": [],
            "failure": None}


coverage_runner.run_coverage_for_file = _fake_run
try:
    res = asyncio.run(main._measure_suite(
        "def test_x():\n    assert True", "m.py",
        {"name": "target", "lineno": 1, "end_lineno": 2, "body": "x"}))
finally:
    coverage_runner.run_coverage_for_file = _real_run
check("measure: scope is FUNCTION", res.get("coverage_scope") == "FUNCTION", res.get("coverage_scope"))
check("measure: file percent preserved", res.get("file_coverage_percent") == 60)
check("measure: function percent scoped to range", res.get("coverage_percent") == 100.0,
      res.get("coverage_percent"))
check("measure: placeholder suite flagged", res.get("exercises_target") is False)

# ---------------- Framework detection is lazy + cached (spec 11/45/46)
job = {"source_files": {"m.py": os.path.join(os.environ.get("TEMP", "/tmp"), "proj", "m.py")}}
fw1 = tf.get_job_framework(job)
check("job framework: python detected", fw1.get("language") == "python", fw1)
check("job framework: cached on the job", job.get("test_framework") is fw1)
js_job = {"source_files": {"a.ts": os.path.join(os.environ.get("TEMP", "/tmp"), "p", "a.ts")}}
fw_js = tf.get_job_framework(js_job)
check("job framework: ts defaults to node:test", fw_js.get("testRunner") == "node:test", fw_js)

# ---------------- Cache key includes framework (spec 24)
v1 = main._tests_cache_version({"language": "python", "testRunner": "pytest", "moduleSystem": "none"})
v2 = main._tests_cache_version({"language": "typescript", "testRunner": "jest", "moduleSystem": "esm"})
check("cache version contains base version", config.TESTS_CACHE_VERSION in v1)
check("cache version differs per environment", v1 != v2)
check("cache version stable for same environment", v1 == main._tests_cache_version(
    {"language": "python", "testRunner": "pytest", "moduleSystem": "none"}))

# ---------------- Context flows through the generation loop
code, best, attempts, f = run_loop([cov(40), cov(70)],
                                   ctx={"func": {}, "is_js": False, "target": 65})
check("ctx path: initial used generate_function_tests", f.function_ctx_calls == 1)
check("ctx path: improvement receives ctx", f.improve_calls == 1 and f.ctx_seen is not None)
code, best, attempts, f = run_loop([cov(70)])
check("legacy path: batch generator still used when no ctx",
      f.function_ctx_calls == 0 and f.initial_calls == 1)

# ---------------- Mock-mode suites really execute targets (spec 7)
mock_py = llm._mock_test_code(
    [{"name": "classify", "args": ["n"], "class_name": ""}], "/x/gated.py")
check("mock py: calls the target", "gated.classify(" in mock_py)
check("mock py: no placeholder asserts", "assertTrue" not in mock_py and "assert True" not in mock_py)
mock_cls = llm._mock_test_code(
    [{"name": "render", "args": [], "class_name": "Widget"}], "/x/widget.py")
check("mock py: class method via module-qualified __new__",
      "widget.Widget.__new__" in mock_cls and "obj.render(" in mock_cls, mock_cls)
mock_js = llm._mock_test_code(
    [{"name": "run", "args": [], "class_name": "Svc"}], "/x/job.js")
check("mock js: constructs + calls", "new src.Svc(" in mock_js and "instance.run(" in mock_js)

# ---------------- Smoke tests reference module-qualified classes
smoke = main._build_smoke_test(
    {"name": "render", "class_name": "Widget", "args": []}, "/r/widget.py")
check("smoke: module-qualified class", "widget.Widget.__new__" in smoke, smoke)
check("smoke: passes the placeholder gate",
      main._test_references_function(smoke, {"name": "render"}))

print(f"\n===== {PASS}/{PASS + FAIL} passed =====")
sys.exit(0 if FAIL == 0 else 1)
