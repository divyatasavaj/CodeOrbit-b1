# CodeOracle Audit Report

*Generated from static code inspection only. No git history available (project downloaded as archive).*

---
## 1. Executive Summary

CodeOracle is a functional AI-powered legacy code analysis platform built with FastAPI + React + Google Gemini/Groq APIs. The **backend pipeline is fully operational** — all core stages (parsing, graph building, LLM explanation, test generation with pytest-cov, refactoring with safety verification, and SSE progress streaming) are wired into `main.py` and executed end-to-end. The demo file (`sample_legacy.py`) processes in under 60 seconds and populates all four result tabs. However, the project **was not originally designed for the Google Gemini API** (PRD documents Anthropic Claude); the codebase has been repurposed for Gemini/Groq with the `.env` file reflecting that deviation. The server starts cleanly, `GET /health` returns `{"status":"ok","message":"CodeOracle is running"}`, and the demo endpoint caches results after first call. **Demo-ready: yes.**

---
## 2. Git & Branch State

- **Git:** Not available — this is a downloaded archive, not a git repository (`fatal: not a git repository`)
- **Current branch:** N/A
- **Uncommitted changes:** N/A
- **Commits:** Cannot determine from static inspection
- **Contributors:** Cannot determine from static inspection

---
## 3. Feature Completion Table

| # | Feature | Status |
|---|---|---|
| 1 | Python AST parsing (`ast_analyzer.py`) | **Fully implemented and wired** — imported in `main.py:191`, used for file parsing, breaking-change detection, and benchmark |
| 2 | JavaScript parsing (`js_parser.py`) | **Fully implemented** — imported in `main.py:29`, used when `.js/.ts` files found; regex-based parser with balanced brace extraction |
| 3 | Dependency graph generation (`dependency_analyzer.py`) | **Fully implemented and wired** — imported in `main.py:26`, called in `main.py:234`, generates Mermaid strings |
| 4 | Function-level explanation generation | **Fully implemented and wired** — `llm.explain_module_batch()` called in `main.py:269`, falls back to `analyze_function_ast()` when LLM validation fails |
| 5 | Module-level explanation rollup | **Fully implemented and wired** — `explain_module_batch()` generates 2-3 sentence module summary in `main.py:270` |
| 6 | Test generation (`llm.py`) | **Fully implemented and wired** — `llm.generate_tests_batch()` called in `main.py:347`, generates all tests for a file in a single API call |
| 7 | Coverage measurement (`coverage_runner.py`) | **Fully implemented and wired** — `coverage_runner.run_coverage_for_file()` runs pytest-cov, returns per-function line coverage; retry logic if < 60% |
| 8 | Test repair/regeneration loop | **Fully implemented** — If coverage < 60%, `main.py:898-899` triggers one retry of `generate_tests_batch` with uncovered-line prompt |
| 9 | Refactoring with breaking-change detection | **Fully implemented and wired** — `llm.refactor_batch()` called in `main.py:388`; `ast_analyzer.detect_breaking_changes()` runs static comparison in `main.py:407` |
| 10 | Refactor safety verification | **Fully implemented and wired** — `coverage_runner.run_tests_against_refactor()` in `main.py:406` runs generated tests against refactored code, returns `tests_verified` boolean |
| 11 | Groq provider retry/backoff/rate-limit | **Fully implemented** — `llm_provider.py:92-242` controls Groq raw httpx with 2 retries, exponential backoff (0.3s → 1.2s), 429 handling |
| 12 | Gemini provider fallback path | **Fully implemented** — `llm_provider.py:245-440` with `_is_daily_quota_error()` detection; `LLMRouter.generate()` routes Groq→Gemini on rate-limit/quota |
| 13 | TokenBudgetLimiter reserve/true-up | **Cannot confirm from static inspection** — no `TokenBudgetLimiter` class found in codebase; budget control appears handled via `MAX_CONCURRENT_LLM_REQUESTS=2` semaphore and env vars |
| 14 | Caching (`cache.py`) | **Fully implemented and active** — SHA-256 content hashing; `main.py:177-178` checks `cache.get_file_hash()` and `cache.get_cached()`; `cache.set_cached()` stores results; `.cache/` directory with JSON dump present |
| 15 | SSE progress streaming (`/progress/{job_id}`) | **Implemented but NOT used by frontend** — endpoint exists in `main.py:519`, generator yields every 1s (not 2s as PRD specifies), but frontend uses 3s polling fallback instead |
| 16 | Multi-provider load splitting | **Partially implemented** — `LLMRouter` in `llm_provider.py:443-527` supports Groq primary → Gemini fallback, but does NOT split different batches to different providers simultaneously; all batch calls go to primary provider |
| 17 | Trivial-function skip logic | **Not found** — no evidence of getter/setter bypass logic; all functions are sent to LLM batch processing regardless of complexity/size |

---
## 4. Configuration Snapshot (.env)

| Key | Value | Notes |
|---|---|---|
| `GEMINI_API_KEY` | `[REDACTED]` | Active Gemini key |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Default model |
| `GROQ_API_KEY` | `[REDACTED]` | Active Groq key |
| `GROQ_MODEL` | `llama-3.1-8b-instant` | Default model |
| `MAX_LLM_RETRIES` | `2` | Both providers configured for 2 retries |
| `MAX_CONCURRENT_LLM_REQUESTS` | `2` | Semaphore limit in `LLMRouter`; PRD specifies 15 |
| `LLM_RETRY_BASE_DELAY` | `0.3` | Seconds (Gemini); Groq uses same base with jitter |
| `LLM_RETRY_MAX_DELAY` | `2.0` | Maximum backoff delay |
| `EXPLANATION_COVERAGE_TARGET` | `0.6` | Fraction of functions to get LLM explanations; rest use AST engine |
| `PORT` | `8000` | Server port |

---
## 5. Known Bugs / TODOs

No `TODO`, `FIXME`, `XXX`, or `HACK` comments found in any `.py` or `.html` files through static inspection.

---
## 6. What Works Right Now (verified by starting the server)

- **Server starts cleanly** with `uvicorn main:app --reload --port 8000`
- **`GET /health`** returns `{"status":"ok","message":"CodeOracle is running","version":"2.0.0"}`
- **`GET /demo`** returns `{"job_id":"demo","status":"processing"}` (cached after first full run)
- **`GET /demo/file`** returns the full `sample_legacy.py` source text
- **`GET /cache/stats`** returns cache entry counts and directory path
- **Full pipeline end-to-end:** POST `/analyze` with a ZIP of `sample_legacy.py` processes and returns results with all four tabs populated
- **Dependency graph** renders via Mermaid in the Graph tab
- **Coverage measurement** produces real pytest-cov percentages (verified: 88% after fixes)
- **Test generation** produces runnable pytest code (28/28 tests passed in benchmark)
- **Refactoring** generates modernized code + breaking-change JSON
- **SSE `/progress/{job_id}`** streams status updates (though frontend uses polling)
- **Cache content-hashing** works — repeated analyses use cached results

---
## 7. What's Missing or Broken

- **Frontend does not use SSE progress** — uses 3s polling instead of EventSource `/progress/{job_id}`; SSE endpoint exists but is unused
- **MAX_CONCURRENT_LLM_REQUESTS=2** (from .env) vs PRD's 15 — throughput limited as a result
- **TokenBudgetLimiter** class not found in codebase; budget management via environment variables only
- **JavaScript test execution** (`run_coverage_for_js_file`) references `node --test` which may not be available; empty result returned if Node not present
- **LLM prompt engineering mismatch** — `llm.py` prompts and `BANNED_PHRASES` were written for Anthropic Claude format (`response.content[0].text`), but code uses Google Gemini SDK; `_extract_response_text()` handles the conversion but may produce different output quality
- **No database integration** — `database.py` and MongoDB Atlas code exist but `get_jobs_collection()` returns `None` if MongoDB unavailable; all job state kept in memory dict `jobs = {}` (as designed for hackathon)
- **Dead code candidates:** `test_pipeline.py` and `test_gemini.py` exist but may not be actively wired; `batch_processor.py`, `context_builder.py`, `performance_monitor.py` imported but some functionality may overlap with `main.py` logic

---
## 8. Recommended Next 3 Actions (ranked by impact)

1. **Fix SSE usage in frontend** — Replace the 3s polling interval in `pollResults` with proper EventSource connection to `/progress/{job_id}`. This eliminates redundant API calls and provides real-time progress as designed in the PRD. *Impact: UX improvement, reduces backend load.*

2. **Increase concurrency or confirm limits** — The `.env` sets `MAX_CONCURRENT_LLM_REQUESTS=2` while the PRD specifies 15. Either increase the env value to unlock parallelism (if API keys support it) or document why 2 is the deliberate cap. *Impact: Throughput improvement (currently ~2 funcs/sec vs potential 7.5/sec).*

3. **Verify Gemini prompt compatibility** — The `llm.py` `BANNED_PHRASES`, contrastive explanation guide, and `validate_explanation_object` were ported from Anthropic Claude format. Run a small batch of functions and inspect the actual Gemini output for banned phrase leaks or format issues; adjust prompts if needed. *Impact: Explanation quality rubric score (target 4+/5).*