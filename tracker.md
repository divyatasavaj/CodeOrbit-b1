# CodeOracle - Project Tracker

## Project Overview

**Name:** CodeOracle
**Type:** AI-Powered Legacy Code Explainer and Modernizer
**Tech Stack:** FastAPI + React (CDN) + Google Gemini API + Groq + MongoDB (with In-Memory Fallback)
**Status:** Complete & Production Ready — Multi-Language Support, Real Coverage Feedback Loop, Breaking Changes & Modern UI
**Last Updated:** Aug 14, 2026

---

## Architecture

```
┌─────────────────┐     HTTP      ┌─────────────────┐
│   Frontend      │ ◄───────────► │    Backend      │
│   (React CDN)   │               │    (FastAPI)    │
│   Port: None    │               │    Port: 8000   │
└─────────────────┘               └────────┬────────┘
                                           │
                              ┌────────────┼────────────┐
                              │            │            │
                        ┌─────▼─────┐ ┌────▼────┐ ┌────▼────┐
                        │  Parser   │ │   LLM   │ │  Graph  │
                        │  (AST/JS) │ │(Gemini) │ │(Mermaid)│
                        └───────────┘ └─────────┘ └─────────┘
```

---

## File Structure

```
codeoracle/
├── backend/
│   ├── main.py              ← FastAPI server, endpoints, background tasks
│   ├── parser.py            ← Python AST parser
│   ├── js_parser.py         ← JavaScript parser (esprima/regex fallback)
│   ├── llm.py               ← Google Gemini API integration (batching, fallback & backoff)
│   ├── graph.py             ← Dependency graph builder + Mermaid
│   ├── coverage_runner.py   ← Pytest + coverage runner with retry
│   ├── database.py          ← MongoDB Atlas connection + In-Memory fallback store
│   ├── .env                 ← Environment variables (MongoDB + Gemini)
│   └── requirements.txt     ← Python dependencies
├── frontend/
│   ├── index.html           ← Single-file React app (CDN imports)
│   └── ExplanationTab.jsx   ← Modular Explanation tab component
├── demo/
│   ├── sample_legacy.py     ← Demo legacy Python code
│   └── README.md            ← How to run the demo
└── tracker.md               ← This file
```

---

## Task Flow

### Phase 1: File Upload & Parsing
```
User uploads ZIP → POST /analyze → Extract ZIP → Find .py / .js files
                                                     ↓
                                           Parse with ast / js_parser
                                                     ↓
                                           Extract: functions, classes,
                                           imports, method bodies
```

### Phase 2: Analysis Pipeline (Background Task)
```
Parsed Files ──┬──► build_dependency_graph() ──► Mermaid diagram
               │
               ├──► explain_module_batch() ──► Function & Module explanations + Usage
               │
               ├──► generate_tests_batch() ──► pytest code
               │         ↓
               │    run_coverage_for_file() ──► Coverage percentage (retry if < 60%)
               │
               └──► refactor_batch() ──► Refactored code
                         ↓
                    run_tests_against_refactor() ──► Verification
```

### Phase 3: Results Delivery
```
GET /results/{job_id} ──► Return JSON with:
                          ├── summary (files, functions, coverage, languages)
                          ├── explanation (per-file, per-function, usage)
                          ├── graph (nodes, edges, mermaid)
                          ├── tests (code, coverage, pass/fail)
                          └── refactor (code, changes, verified)
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | / | Serve frontend |
| GET | /health | Health check |
| POST | /analyze | Upload ZIP for analysis |
| GET | /results/{job_id} | Get analysis results |
| GET | /progress/{job_id} | SSE progress stream |
| GET | /demo | Run demo analysis |
| GET | /demo/file | Get demo file content |

---

## Frontend Tabs

| Tab | Content |
|-----|---------|
| Explanation | Module summaries + searchable cards with Explanation, Usage, Purpose, I/O & Risks |
| Dependency Graph | Mermaid.js rendered call graph |
| Generated Tests | pytest code with coverage badges |
| Refactored Code | Side-by-side original vs refactored + breaking changes table |

---

## Key Design Decisions

1. **Google Gemini API:** LLM calls use Gemini with multi-model fallback, batching, and exponential backoff retry.
2. **MongoDB Atlas with In-Memory Safe Fallback:** Jobs and results stored in MongoDB with automatic fallback to memory dict if database is unreachable.
3. **Multi-language:** Python + JavaScript parsing with esprima fallback.
4. **No Auth:** Single-user demo application.
5. **CDN React:** No build step needed, just open HTML file.
6. **Semaphore & Batching:** Batch requests per file to eliminate 429 rate limit issues.
7. **Background Tasks:** FastAPI BackgroundTasks for non-blocking analysis.
8. **Graceful Errors:** Pipeline-level catch-all, every LLM call wrapped in try/except.
9. **Coverage Retry:** If coverage < 60%, retry test generation once targeting uncovered lines.
10. **SSE Progress:** Real-time progress updates via Server-Sent Events.

---

## LLM Prompts

### explain_function / explain_module_batch
- 5-part breakdown: Purpose, Function Explanation, Practical Usage, Input/Output, Risks

### generate_tests / generate_tests_batch
- pytest code only, no explanation, proper module imports

### refactor_function / refactor_batch
- Python code block + JSON breaking changes array

---

## Progress Log

| Step | File | Status |
|------|------|--------|
| 1 | Project structure | Done |
| 2 | requirements.txt | Done |
| 3 | parser.py | Done |
| 4 | graph.py | Done |
| 5 | llm.py (Gemini API) | Done |
| 6 | coverage_runner.py | Done |
| 7 | main.py (endpoints & pipeline) | Done |
| 8 | frontend/index.html (React CDN app) | Done |
| 9 | demo/sample_legacy.py | Done |
| 10 | demo/README.md | Done |
| 11 | tracker.md | Done |
| 12 | MongoDB integration (database.py, .env, main.py) | Done |
| 13 | Pipeline-level catch-all error handling (main.py) | Done |
| 14 | LLM retry logic + Gemini API multi-model fallback (llm.py) | Done |
| 15 | Refactor safety verification (coverage_runner.py) | Done |
| 16 | Coverage retry logic (coverage_runner.py, main.py) | Done |
| 17 | /demo and /demo/file endpoints (main.py) | Done |
| 18 | SSE progress endpoint (main.py) | Done |
| 19 | JavaScript parsing (js_parser.py, main.py) | Done |
| 20 | Bug fix: Skip coverage when test code is invalid | Done — Aug 14 |
| 21 | Bug fix: Clean coverage.json before pytest run | Done — Aug 14 |
| 22 | Bug fix: Use overall line coverage instead of per-function average | Done — Aug 14 |
| 23 | Warning fix: Add /demo endpoint | Done — Aug 14 |
| 24 | Warning fix: Add copy button for code blocks | Done — Aug 14 |
| 25 | Warning fix: Remove builtins from graph | Done — Aug 14 |
| 26 | Warning fix: Fix empty ZIP error message | Done — Aug 14 |
| 27 | Warning fix: Validate FALLBACK_MODELS | Done — Aug 14 |
| 28 | Batch test generation (1 API call per file) | Done — Aug 14 |
| 29 | Batch explanation (1 API call per file) | Done — Aug 14 |
| 30 | Batch refactoring (1 API call per file) | Done — Aug 14 |
| 31 | Fix coverage calculation (per-file, not per-function) | Done — Aug 14 |
| 32 | Fix test import (correct module name) | Done — Aug 14 |
| 33 | Add conftest.py for sys.path injection | Done — Aug 14 |
| 34 | Handle 429 quota errors gracefully | Done — Aug 14 |
| 35 | ExplanationTab dedicated Explanation & Usage overview | Done — Aug 14 |
| 36 | Safe database fallback for offline MongoDB | Done — Aug 14 |

---

## Member A Tasks (Backend Pipeline & Reliability)

| Task | Size | Priority | Status | Notes |
|------|------|----------|--------|-------|
| LLM retry logic + Gemini response-shape audit | M | Critical | Done | 3 retries, exponential backoff, multi-model fallback |
| Pipeline-level catch-all error handling | S | Critical | Done | try/except with logging |
| Coverage retry logic | M | Critical | Done | Retry once if < 60% |
| Refactor safety verification | M | Critical | Done | Improved import adjustment |
| SSE progress endpoint | M | Cut-safe | Done | GET /progress/{job_id} |
| Re-add /demo and /demo/file endpoints | S | Cut-safe | Done | File content endpoint + cached demo |
| JavaScript parsing | L | Cut-safe | Done | esprima + regex fallback |
| Full audit - Bug fixes (3 critical) | L | Critical | Done | Coverage skip, cleanup, overall calc |
| Full audit - Warning fixes (7) | M | High | Done | Copy buttons, builtins, error msgs |
| Testing & Coverage Pipeline Fix | L | Critical | Done | Batch generation, real coverage |

---

## Testing & Coverage Pipeline Fix Summary

### Problem
- Gemini API was making 8-16 requests per analysis (one per function)
- Coverage was always 0% due to incorrect calculation
- Tests were importing wrong module name

### Solution
- **Batch test generation**: 1 API call generates tests for ALL functions in a file
- **Batch explanations**: 1 API call explains ALL functions in a file
- **Batch refactoring**: 1 API call refactors ALL functions in a file
- **Fixed coverage**: pytest-cov runs ONCE per file, returns actual coverage
- **Fixed imports**: Tests import correct module name (e.g., `sample_legacy`)
- **Graceful error handling**: 429 quota errors handled without crashing

### Results
| Metric | Before | After |
|--------|--------|-------|
| Gemini requests | 8-16 | 3 |
| Coverage | 0% | 88% |
| Tests generated | 0 | 28 |
| Tests passed | 0 | 28 |

---

## Performance Optimization Summary (v2.0)

### Architecture Changes
- **Modular design**: Split into cache, llm_provider, batch_processor, ast_analyzer, context_builder, dependency_analyzer, performance_monitor
- **Provider abstraction**: LLMProvider interface with Gemini implementation
- **Smart caching**: SHA-256 content hashing for file parsing, AST, LLM responses
- **Dynamic batching**: Batch size calculated based on token limits
- **Controlled concurrency**: MAX_CONCURRENT_LLM_REQUESTS environment variable
- **Performance monitoring**: Timing for all pipeline stages

### New Modules
| Module | Purpose |
|--------|---------|
| cache.py | SHA-256 caching for AST, LLM responses |
| llm_provider.py | LLM abstraction (Gemini, extensible) |
| batch_processor.py | Dynamic batch sizing, concurrency control |
| ast_analyzer.py | Enhanced AST with complexity analysis |
| context_builder.py | Smart context generation for LLM |
| dependency_analyzer.py | Static dependency graph (no LLM) |
| performance_monitor.py | Pipeline timing and metrics |
| benchmark.py | Performance benchmarking script |

### Benchmark Results (demo/sample_legacy.py)
| Metric | Value |
|--------|-------|
| Files | 1 |
| Lines | 167 |
| Functions | 7 |
| Classes | 2 |
| Methods | 9 |
| AST nodes | 851 |
| AST analysis time | 0.004s |
| Graph build time | <0.001s |
| Total static analysis | 0.005s |

### Performance Improvements
- **AST-first analysis**: Static analysis before LLM calls
- **Dependency graph**: Built from AST, no LLM needed
- **Caching**: Repeated analyses use cached results
- **Batch processing**: Dynamic batch sizing based on token limits
- **Concurrency control**: Configurable concurrent LLM requests

### Configuration
```bash
# Environment variables
LLM_PROVIDER=groq
GROQ_API_KEY=your_groq_key
GROQ_MODEL=llama-3.1-8b-instant
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-3.5-flash-lite
MAX_CONCURRENT_LLM_REQUESTS=3
```

### LLM Provider Architecture (Added: 2026-08-13)
```
LLMRouter
  ├── GroqProvider (PRIMARY)
  │     └── llama-3.1-8b-instant
  └── GeminiProvider (FALLBACK)
        └── gemini-3.5-flash-lite
```

- **Primary**: Groq (fast, free tier)
- **Fallback**: Gemini (quota-aware)
- **QuotaExhaustedError**: Immediate stop (no retry loops)
- **Provider selection**: Configurable via `LLM_PROVIDER` env var

### Configuration
```bash
# Environment variables
LLM_PROVIDER=groq
GROQ_API_KEY=your_groq_key
GROQ_MODEL=llama-3.1-8b-instant
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-3.5-flash-lite
MAX_LLM_RETRIES=2
MAX_CONCURRENT_LLM_REQUESTS=2
LLM_RETRY_BASE_DELAY=0.3
LLM_RETRY_MAX_DELAY=2.0
```

---

## Best Version Changes (Aug 14, 2026)

### Critical Bug Fixes
- **Mermaid syntax**: Fixed missing space in `dependency_analyzer.py:201` (`-->` → `--> `)
- **Coverage calculation**: Fixed `overall_coverage` being overwritten instead of averaged in `main.py:401-402`
- **Tailwind class**: Fixed invalid `bg-gray-750` → `bg-gray-700` in `frontend/index.html:321,331`

### Dead Code Removed
- **parser.py**: 106 lines removed (superseded by `ast_analyzer.py`)
- **graph.py**: 80 lines removed (superseded by `dependency_analyzer.py`)
- **batch_processor import**: Removed unused import from `main.py:26`
- **Empty timer**: Removed `with monitor.timer("coverage"): pass` from `main.py:388-389`
- **Unused import**: Removed `import re` from `coverage_runner.py:5`

### Code Quality Improvements
- **Inline imports moved to module level**: `re`, `random` in `llm_provider.py`; `json` in `context_builder.py`
- **Unused imports removed**: `Set`, `Tuple` in `ast_analyzer.py`; `Tuple` in `dependency_analyzer.py`; `Optional` in `context_builder.py`
- **Unused variables removed**: `last_status`, `attempt_elapsed` in `llm_provider.py`; `attempt_elapsed` in `gemini_provider.py`

### LLM Provider Optimization
- **Raw httpx**: Bypassed Groq SDK auto-retry (was waiting 29-44s on 429)
- **Controlled retry**: Our own retry with 0.3-1.2s delays (max 2 retries)
- **Concurrency semaphore**: Max 2 concurrent LLM requests
- **Structured logging**: `[LLM]` format with provider, latency, status, retry count

### Performance Results
| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| Single request | 43s | 1.76s | 96% faster |
| 5 concurrent (wall) | 71s | 5.64s | 92% faster |
| SDK auto-retry wait | 29-44s | 0 | Eliminated |
| Our retry delay | N/A | 0.3-1.2s | Controlled |

---

## File Inventory (Best Version)

| File | Lines | Purpose |
|------|-------|---------|
| main.py | ~725 | FastAPI server, analysis pipeline |
| llm_provider.py | ~540 | Groq + Gemini providers, router |
| ast_analyzer.py | ~295 | AST analysis, complexity |
| dependency_analyzer.py | ~228 | Dependency graph, mermaid |
| coverage_runner.py | ~365 | pytest-cov runner |
| js_parser.py | ~194 | JavaScript parsing |
| batch_processor.py | ~163 | Batch processing |
| context_builder.py | ~163 | LLM prompt context |
| performance_monitor.py | ~156 | Pipeline timing |
| cache.py | ~89 | SHA-256 caching |
| frontend/index.html | ~520 | React CDN app |

---

## PS-06 Changes (Aug 14, 2026)

### Coverage Feedback Loop
- **Max iterations**: 2 (configurable)
- **Target coverage**: 60%
- **Process**: Generate → Execute → Measure → Analyze uncovered → Generate targeted → Execute again
- **Duplicate prevention**: Skips iteration if >50% test names are duplicates

### Uncovered-Line Analysis
- **AST-based**: Parses coverage.json to find missing lines
- **Segment analysis**: Identifies uncovered functions/classes with source code
- **Targeted generation**: Sends uncovered segments to LLM for targeted test generation

### Rich Function Context
- **AST-derived**: Parameters, return values, complexity, control flow
- **Exceptions raised**: Identifies raise statements
- **Global variables**: Tracks which globals are used
- **Function calls**: Lists called functions for dependency context

### LLM Unavailable Handling
- **Clear distinction**: UI shows "Coverage unavailable" vs "0% coverage"
- **Error messages**: Shows actual reason (quota exhausted, rate limited)
- **No fake coverage**: Never reports fake 0% as if tests failed

### Function-to-Test Mapping
- **Stable IDs**: Uses function name + filename for mapping
- **No reuse**: Each function gets its own test entry
- **Correct display**: Tests shown under correct function

### Regression Test Results
| Project | LOC | Functions | Coverage | Time |
|---------|-----|-----------|----------|------|
| math_utils | 30 | 6 | 86.7% | 1.82s |
| student_grade | 60 | 10 | 100.0% | 5.19s |
| data_processor | 45 | 3 | LLM unavailable | 5.09s |

### Performance
- **Baseline**: 4.04s
- **Current**: 2.89s average
- **Regression**: None (-28.5% improvement)


to run
 start D:\CodeOrbit-b1\frontend\index.html

 uvicorn main:app --host 0.0.0.0 --port 8001