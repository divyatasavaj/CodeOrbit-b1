"""
CodeOracle - Optimized Backend Pipeline
FastAPI server with modular architecture for multi-language legacy codebase analysis.
Supports Python & JavaScript/TypeScript, real pytest/node coverage, breaking-change detection, and caching.
"""
import os
import uuid
import zipfile
import tempfile
import shutil
import logging
import asyncio
import json
import re
import time
import traceback
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, File, UploadFile, Form, BackgroundTasks, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse, Response

import cache
import config
import job_store
import chatbot_api
import ai_pipeline
import repo_ingest
import github_import
import function_registry
import ast_analyzer
import context_builder
import dependency_analyzer
import llm_provider
import coverage_runner
import performance_monitor
import js_parser
import llm
import test_framework
from database import get_jobs_collection, close_connection


def is_trivial_function(func_body: str) -> bool:
    """Detect simple getter/setter/init functions that can be explained
    without LLM calls, avoiding unnecessary API costs for boilerplate code.

    A function is considered trivial if it has very few body statements
    (1-2) and matches common patterns:
    - Getter: returns self.attribute
    - Init: sets self.attribute fields (2-3 assignments)
    - Setter: sets self.attribute from parameter
    - Simple return: returns a computed value
    - Single-condition validator: if <condition>: raise <Error>(...)
    - Pure delegation: return self.helper(...)
    - String-formatting wrapper: return f"..." or return "...%s..." % ...
    """
    if not func_body or func_body.strip().startswith('#'):
        return False

    lines = func_body.strip().split('\n')
    if not lines:
        return False

    # The first line is the def header, rest is function body
    def_line = lines[0]
    body_lines = lines[1:]

    # Strip whitespace from each body line and filter out empties/comments/pass
    stripped_body = [l.strip() for l in body_lines if l.strip()]
    effective_body_lines = [
        l for l in stripped_body
        if not l.startswith('#') and l != 'pass'
    ]

    # Must have very few body statements to qualify
    if len(effective_body_lines) > 2:
        return False

    # Concatenate body for pattern matching
    body_text = '\n'.join(effective_body_lines)

    # Check for branching constructs in the body (if/for/while/try)
    # Allow single if-raise pattern (validator)
    if_raise_pattern = bool(re.search(r'if\s+.+:\s*raise\s+\w+\(.*\)', body_text))
    # Check for other branching
    has_other_branching = bool(re.search(r'\b(elif|else|for|while|try):', body_text))
    has_if_not_raise = bool(re.search(r'if\s+.+:', body_text)) and not if_raise_pattern
    has_branching = has_other_branching or has_if_not_raise
    if has_branching:
        return False

    # Concatenate body for pattern matching
    body_text = '\n'.join(effective_body_lines)

    # Pattern classification based on body content

    # 1. Getter: has "return self.X" somewhere in body
    is_getter = bool(re.search(r'return\s+self\.\w+', body_text))

    # 2. Init pattern: has self. assignments (2-3 common in __init__),
    #    total body lines <= 3
    self_assignments = len(re.findall(r'self\.\w+\s*=', body_text))
    is_init = self_assignments >= 1 and self_assignments <= 3 and len(effective_body_lines) <= 3

    # 3. Simple return: just "return <expr>" with no self. reference
    is_simple_return = (
        len(effective_body_lines) == 1
        and re.match(r'return\s+.+', effective_body_lines[0])
        and not re.search(r'self\.', effective_body_lines[0])
    )

    # 4. Setter: only self. assignments, very few lines
    is_setter = (
        self_assignments == len(effective_body_lines)
        and len(effective_body_lines) <= 2
        and all(re.match(r'self\.\w+\s*=', l) for l in effective_body_lines)
    )

    # 5. One-liner: single return statement
    is_one_liner = (
        len(effective_body_lines) == 1
        and re.match(r'return\s+.+', effective_body_lines[0])
        and not is_getter
    )

    # 6. Single-condition validator: if <cond>: raise <Error>(...)
    is_validator = (
        len(effective_body_lines) <= 2
        and if_raise_pattern
        and not has_other_branching
    )

    # 7. Pure delegation: return self.helper(...) or return helper(...)
    is_delegation = (
        len(effective_body_lines) == 1
        and re.match(r'return\s+(self\.\w+|\w+)\(.*\)', effective_body_lines[0])
        and not is_getter
    )

    # 8. String-formatting wrapper: return f"..." or return "...%s..." % ...
    is_string_wrapper = (
        len(effective_body_lines) == 1
        and re.match(r'return\s+(f?["\'].*\{.*\}.*["\']|f?["\'].*%.*["\'])\s*%?', effective_body_lines[0])
    )

    # Track newly classified trivial functions for review
    is_trivial = (
        is_getter or is_init or is_simple_return or is_setter or is_one_liner
        or is_validator or is_delegation or is_string_wrapper
    )
    
    if is_trivial:
        # Log for review
        func_name_match = re.search(r'def\s+(\w+)', func_body)
        func_name = func_name_match.group(1) if func_name_match else "unknown"
        category = []
        if is_getter: category.append("getter")
        if is_init: category.append("init")
        if is_simple_return: category.append("simple_return")
        if is_setter: category.append("setter")
        if is_one_liner: category.append("one_liner")
        if is_validator: category.append("validator")
        if is_delegation: category.append("delegation")
        if is_string_wrapper: category.append("string_wrapper")
        logger.info(f"TRIVIAL FUNCTION DETECTED: {func_name} [{', '.join(category)}]")

    return is_trivial


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("codeoracle")

app = FastAPI(title="CodeOracle", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

jobs: Dict[str, Any] = {}

# Hot in-memory job cache bound. Jobs beyond this many live entries (oldest
# completed first) are evicted and transparently reloaded from the job store
# on demand - keeps RAM bounded no matter how many analyses run (spec 16/33).
MAX_LIVE_JOBS = max(2, int(os.environ.get("CODEORACLE_MAX_LIVE_JOBS", "8")))


def _evict_stale_jobs(keep: str) -> None:
    """Evict oldest finished jobs so the in-memory cache stays bounded."""
    if len(jobs) <= MAX_LIVE_JOBS:
        return

    def _is_active(jid: str) -> bool:
        st = ai_pipeline.get_state(jid)
        if st is not None and st.task is not None and not st.task.done():
            return True
        return (jobs.get(jid) or {}).get("status") == "processing" and not (jobs.get(jid) or {}).get("structural_ready")

    def _created(jid: str) -> float:
        try:
            return float((jobs.get(jid) or {}).get("created_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    evictable = [
        jid for jid in list(jobs)
        if jid != keep and not _is_active(jid)
        and (jobs.get(jid) or {}).get("status") in ("complete", "error", "cancelled")
    ]
    evictable.sort(key=_created)
    for jid in evictable:
        if len(jobs) <= MAX_LIVE_JOBS:
            break
        jobs.pop(jid, None)
        logger.info(f"[JOBS] evicted cached job {jid} (reloads from store on demand)")

# Metadata keys mirrored to the filesystem job store. Everything else (graph,
# registry, explanation, tests, refactor) lives in its own file so the tiny
# metadata document stays cheap to rewrite on every progress tick.
_PERSISTED_META_KEYS = {
    "status", "ai_status", "progress", "message", "summary", "performance",
    "analysis_progress", "structural", "structural_ready", "pipeline",
    "extract_dir", "source_files", "languages", "failed_functions",
    "created_at", "updated_at", "ai_error",
    "source_type", "repository_url", "repository", "owner", "branch", "source_digest",
}


def _local_module_summary(filename: str) -> str:
    """Deterministic, LLM-free module summary (static info stays local)."""
    return (
        f"Module {filename}: functions discovered during static analysis, "
        f"with AI explanations generated progressively."
    )


def _persist_metadata(job_id: str) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    meta = {k: v for k, v in job.items() if k in _PERSISTED_META_KEYS}
    meta["job_id"] = job_id
    meta["updated_at"] = time.time()
    job_store.save_metadata(job_id, meta)


def update_job(job_id: str, data: Dict[str, Any], upsert: bool = False, persist: bool = True):
    """Update job state in memory, on disk, and (best-effort) MongoDB."""
    if job_id not in jobs:
        jobs[job_id] = {}
    jobs[job_id].update(data)
    if persist:
        _persist_metadata(job_id)
    try:
        jobs_col = get_jobs_collection()
        if jobs_col is not None:
            jobs_col.update_one({"_id": job_id}, {"$set": data}, upsert=upsert)
    except Exception as e:
        logger.debug(f"MongoDB update notice for job {job_id}: {e}")


def _compose_explanation(explanation_map: Dict[str, Any], registry_funcs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rebuild the frontend explanation array from persisted per-function data."""
    groups: Dict[str, Dict[str, Any]] = {}
    ordered_ids: List[str] = []
    seen = set()
    for func in registry_funcs or []:
        fname = func.get("filename") or "unknown"
        groups.setdefault(fname, {"filename": fname, "module_summary": _local_module_summary(fname), "functions": []})
        fid = func.get("id")
        if fid and fid not in seen:
            seen.add(fid)
            ordered_ids.append(fid)
    for fid, entry in (explanation_map or {}).items():
        if not isinstance(entry, dict):
            continue
        fname = entry.get("filename") or "unknown"
        grp = groups.setdefault(fname, {"filename": fname, "module_summary": _local_module_summary(fname), "functions": []})
        grp["functions"].append({
            "name": entry.get("name"),
            "function_id": fid,
            "ai_status": entry.get("ai_status"),
            "explanation": entry.get("explanation"),
        })
        seen.add(fid)
    # Registry functions never persisted (cancelled mid-pipeline / AI disabled)
    # still belong in the list; the functions endpoint fills their static
    # explanation lazily from job["registry"].
    registry_by_id = {f.get("id"): f for f in registry_funcs or [] if isinstance(f, dict)}
    for fid in ordered_ids:
        if fid in seen:
            continue
        func = registry_by_id.get(fid) or {}
        fname = func.get("filename") or "unknown"
        grp = groups.setdefault(fname, {"filename": fname, "module_summary": _local_module_summary(fname), "functions": []})
        grp["functions"].append({
            "name": func.get("qualified_name") or func.get("name"),
            "function_id": fid,
            "ai_status": "static",
            "explanation": None,
        })
    for grp in groups.values():
        grp["functions"].sort(key=lambda f: f.get("name") or "")
    return list(groups.values())


def _load_job_from_store(job_id: str) -> Dict[str, Any]:
    """Rebuild a full job from the filesystem store (survives restarts)."""
    meta = job_store.load_metadata(job_id)
    if not meta:
        return None
    job: Dict[str, Any] = dict(meta)
    funcs = job_store.load_functions(job_id)
    if funcs:
        job["registry"] = funcs
    graph = job_store.load_graph(job_id)
    if graph:
        job["graph"] = graph
    exps = job_store.load_explanations(job_id)
    if exps:
        job["explanation"] = _compose_explanation(exps, funcs)
    tests = job_store.load_artifacts(job_id, "tests")
    if tests:
        job["tests"] = sorted(tests, key=lambda t: t.get("name") or "")
    refactors = job_store.load_artifacts(job_id, "refactors")
    if refactors:
        job["refactor"] = sorted(refactors, key=lambda r: r.get("name") or "")
    jobs[job_id] = job
    return job


def get_job(job_id: str) -> Dict[str, Any]:
    """Retrieve a job from the hot memory cache, disk store, or MongoDB."""
    job = jobs.get(job_id)
    if job is not None:
        return job
    job = _load_job_from_store(job_id)
    if job is not None:
        return job
    try:
        jobs_col = get_jobs_collection()
        if jobs_col is not None:
            doc = jobs_col.find_one({"_id": job_id})
            if doc:
                jobs[job_id] = doc
                return doc
    except Exception as e:
        logger.debug(f"MongoDB query notice for job {job_id}: {e}")
    return None


# Repository chatbot routes (see chatbot_api). The existing job resolver is
# injected so the chatbot reuses this lookup/authorization path instead of
# duplicating repository storage, and so access checks stay in one place.
chatbot_api.configure(get_job)
app.include_router(chatbot_api.router)


def cleanup_stale_extract_dirs(max_age_hours: int = 24):
    """Remove leftover analysis directories from previous runs."""
    tmp_root = tempfile.gettempdir()
    cutoff = time.time() - max_age_hours * 3600
    try:
        for name in os.listdir(tmp_root):
            if not name.startswith("oracle_"):
                continue
            path = os.path.join(tmp_root, name)
            try:
                if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
                    logger.info(f"Cleaned up stale analysis dir: {path}")
            except OSError:
                pass
    except OSError:
        pass


def _reconcile_interrupted_jobs():
    """Repair jobs left mid-flight by a restart so they never appear stuck."""
    for job_id in job_store.list_job_ids():
        try:
            meta = job_store.load_metadata(job_id) or {}
            status = meta.get("status")
            ai_status = str(meta.get("ai_status") or "")
            if status == "processing":
                job_store.save_metadata(job_id, {**meta, "status": "error", "message": "Analysis interrupted by a backend restart. Please re-run."})
            elif status == "complete" and ai_status in (ai_pipeline.STATUS_AI_QUEUED, ai_pipeline.STATUS_AI_GENERATING):
                job_store.save_metadata(job_id, {**meta, "ai_status": "ai_interrupted"})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Reconcile skipped for {job_id}: {e}")


@app.on_event("startup")
async def startup_db():
    """Initialize MongoDB connection on startup and repair leftover state."""
    try:
        get_jobs_collection()
    except Exception as e:
        logger.warning(f"MongoDB startup connection warning: {e}")
    cleanup_stale_extract_dirs()
    try:
        removed = job_store.cleanup_stale_jobs()
        if removed:
            logger.info(f"Cleaned up {removed} stale job directories")
        _reconcile_interrupted_jobs()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Job store startup warning: {e}")


@app.on_event("shutdown")
async def shutdown_db():
    """Close MongoDB connection on shutdown."""
    try:
        close_connection()
    except Exception as e:
        logger.warning(f"MongoDB shutdown warning: {e}")


SKIP_DIRS = {
    "__pycache__", "node_modules", ".git", "dist", "build",
    ".venv", "venv", "coverage", ".pytest_cache", "target",
    "vendor", ".tox", ".mypy_cache", ".eggs", "*.egg-info"
}

SUPPORTED_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx"}

# Bump when the analysis result shape changes so old cached jobs (in memory,
# on disk, or in MongoDB) are transparently re-analyzed rather than served stale.
PIPELINE_VERSION = 4


@app.get("/")
async def serve_index():
    index_file = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "CodeOracle API is running"}


@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "CodeOracle is running", "version": "2.0.0"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=b"", media_type="image/x-icon", status_code=204)


def find_source_files(root_dir: str) -> List[str]:
    """Find supported source files, ignoring unnecessary directories.

    Thin wrapper over repo_ingest so limits/binary detection stay in one place.
    """
    files, _stats = repo_ingest.find_source_files(root_dir)
    return files


def _progress_from(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "completed": payload.get("completed", 0),
        "total": payload.get("total", 0),
        "failed": payload.get("failed", 0),
        "cached": payload.get("cached", 0),
        "trivial": payload.get("trivial", 0),
    }


def _merge_explanation_entries(job_id: str, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge newly completed per-function explanations into the job's groups."""
    job = jobs.setdefault(job_id, {})
    groups = job.get("explanation")
    if not isinstance(groups, list):
        groups = []
    by_file = {g.get("filename"): g for g in groups}
    for entry in entries:
        fname = entry.get("filename") or "unknown"
        grp = by_file.get(fname)
        if grp is None:
            grp = {"filename": fname, "module_summary": _local_module_summary(fname), "functions": []}
            groups.append(grp)
            by_file[fname] = grp
        funcs = grp.setdefault("functions", [])
        name = entry.get("name")
        funcs[:] = [f for f in funcs if f.get("name") != name]
        funcs.append({
            "name": name,
            "function_id": entry.get("function_id"),
            "ai_status": entry.get("ai_status"),
            "explanation": entry.get("explanation"),
        })
        funcs.sort(key=lambda f: f.get("name") or "")
    job["explanation"] = groups
    return groups


def _ai_hooks(job_id: str) -> Dict[str, Any]:
    """Bridge ai_pipeline progress into the job record (and thus SSE/UI)."""

    def on_started(payload: Dict[str, Any]) -> None:
        update_job(job_id, {
            "ai_status": ai_pipeline.STATUS_AI_GENERATING,
            "analysis_progress": _progress_from(payload),
            "ai_stats": {
                "total": payload.get("total"),
                "cached": payload.get("cached"),
                "trivial": payload.get("trivial"),
                "queued": payload.get("queued"),
                "batches": payload.get("batches"),
                "batch_size": payload.get("batch_size"),
                "concurrency": payload.get("concurrency"),
            },
        }, persist=False)

    def on_progress(snapshot: Dict[str, Any]) -> None:
        update_job(job_id, {
            "analysis_progress": _progress_from(snapshot),
            "ai_status": snapshot.get("ai_status") or ai_pipeline.STATUS_AI_GENERATING,
        }, persist=False)

    def on_batch(payload: Dict[str, Any]) -> None:
        groups = _merge_explanation_entries(job_id, payload.get("functions") or [])
        update_job(job_id, {
            "explanation": groups,
            "analysis_progress": _progress_from(payload),
            "ai_status": ai_pipeline.STATUS_AI_PARTIAL,
        }, persist=False)

    def on_complete(payload: Dict[str, Any]) -> None:
        job = jobs.get(job_id) or {}
        summary = dict(job.get("summary") or {})
        completed = payload.get("completed", 0)
        failed = payload.get("failed", 0)
        summary["ai_explained"] = max(completed - failed, 0)
        summary["ai_failed"] = failed
        summary["ai_cached"] = payload.get("cached", 0)
        perf = dict(job.get("performance") or {})
        started_at = payload.get("started_at")
        finished_at = payload.get("finished_at")
        first_result_at = payload.get("first_result_at")
        if started_at and finished_at:
            perf["ai_total"] = round(finished_at - started_at, 2)
        if started_at and first_result_at:
            perf["first_ai_result"] = round(first_result_at - started_at, 2)
        perf["ai_llm_calls"] = payload.get("llm_calls", 0)
        perf["ai_batches"] = payload.get("batches_total", 0)
        perf["ai_avg_batch_size"] = payload.get("avg_batch_size", 0)
        perf["ai_max_concurrency"] = payload.get("max_concurrency", 0)
        update_job(job_id, {
            "status": "complete",
            "ai_status": payload.get("status") or ai_pipeline.STATUS_AI_COMPLETE,
            "analysis_progress": _progress_from(payload),
            "failed_functions": payload.get("failed_functions", []),
            "summary": summary,
            "performance": perf,
            "progress": "Analysis complete",
        })

    def on_error(message: str) -> None:
        update_job(job_id, {
            "status": "complete",
            "ai_status": ai_pipeline.STATUS_AI_FAILED,
            "ai_error": message,
        })

    return {
        "started": on_started,
        "progress": on_progress,
        "batch": on_batch,
        "complete": on_complete,
        "error": on_error,
        "is_trivial": is_trivial_function,
    }


def _parse_source_files(source_files: List[str], job_id: str, monitor) -> Any:
    """Parse every source file (AST / JS parser) with cache lookups.

    Runs in a worker thread from ``run_analysis`` so the event loop stays
    responsive during large-repository analysis (spec section 22).
    """
    parsed_files: List[Dict[str, Any]] = []
    languages = set()
    classes_found = 0

    for source_file in source_files:
        if ai_pipeline.is_cancelled(job_id):
            break
        try:
            file_hash = cache.get_file_hash(source_file)
            cached = cache.get_cached("ast_analysis", file_hash)

            if cached:
                monitor.record_cache_hit()
                analysis_dict = cached
            else:
                monitor.record_cache_miss()
                if source_file.endswith(".py"):
                    analysis = ast_analyzer.analyze_file(source_file)
                    analysis_dict = analysis.to_dict()
                    analysis_dict["raw_source"] = analysis.source
                elif source_file.endswith((".js", ".ts", ".jsx", ".tsx")):
                    analysis_dict = js_parser.parse_javascript_file(source_file)
                    if "error" in analysis_dict:
                        continue
                else:
                    continue
                cache.set_cached("ast_analysis", file_hash, analysis_dict)

            if source_file.endswith(".py"):
                languages.add("Python")
            else:
                languages.add("JavaScript")

            analysis_dict.setdefault("filepath", source_file)
            classes_found += len(analysis_dict.get("classes", []) or [])
            parsed_files.append(analysis_dict)
        except Exception as file_err:
            logger.warning(f"Skipping {source_file}: {file_err}")
            continue

    return parsed_files, languages, classes_found


def _build_explanation_groups(registry: List[Any]) -> Any:
    """Structural (LLM-free) explanation groups for the UI.

    Entries carry the deterministic static explanation so cards render
    immediately. Runs in a worker thread from ``run_analysis`` - over a
    50K-LOC registry this CPU pass used to stall the event loop inline
    (spec sections 22 and 29).
    """
    groups_by_file: Dict[str, Dict[str, Any]] = {}
    file_function_counts: Dict[str, int] = {}
    source_files_map: Dict[str, str] = {}
    for func in registry:
        source_files_map.setdefault(func.filename, func.source_file)
        file_function_counts[func.filename] = file_function_counts.get(func.filename, 0) + 1
        grp = groups_by_file.get(func.filename)
        if grp is None:
            grp = {
                "filename": func.filename,
                "module_summary": _local_module_summary(func.filename),
                "functions": [],
            }
            groups_by_file[func.filename] = grp
        grp["functions"].append({
            "name": func.qualified_name,
            "function_id": func.id,
            "ai_status": "static",
            "explanation": llm.normalize_explanation_item(
                llm.analyze_function_ast(
                    {
                        "display_name": func.qualified_name,
                        "name": func.name,
                        "args": func.args,
                        "body": func.source_code,
                    },
                    func.filename,
                )
            ),
        })
    explanation_groups = list(groups_by_file.values())
    for grp in explanation_groups:
        grp["functions"].sort(key=lambda f: f.get("name") or "")
    return explanation_groups, file_function_counts, source_files_map


async def run_analysis(job_id: str, extract_dir: str):
    """Two-phase analysis.

    Phase 1 (here) is pure static analysis: file scan, AST parse, function
    registry, dependency graph and metrics. It makes NO LLM calls and returns
    as fast as possible. Phase 2 (AI explanations) is scheduled as an
    independent background task so the job is usable immediately.

    Every CPU/IO-heavy phase runs in a worker thread: the event loop must
    keep serving health checks, SSE streams and status polls throughout
    (spec section 22).
    """
    monitor = performance_monitor.PerformanceMonitor()
    monitor.start()
    structural_start = time.time()
    analysis_succeeded = False

    try:
        update_job(job_id, {
            "status": "processing",
            "ai_status": ai_pipeline.STATUS_AI_QUEUED,
            "progress": "Scanning archive...",
            "performance": {},
            "created_at": time.time(),
        }, upsert=True)

        if ai_pipeline.is_cancelled(job_id):
            update_job(job_id, {"status": "complete", "ai_status": ai_pipeline.STATUS_AI_CANCELLED,
                                "message": "Analysis cancelled"})
            return

        with monitor.timer("zip_extraction"):
            source_files, ingest_stats = await asyncio.to_thread(
                repo_ingest.find_source_files, extract_dir
            )

        if not source_files:
            update_job(job_id, {
                "status": "error",
                "message": "No analyzable source files found. Upload a ZIP containing Python or JavaScript files.",
            })
            return

        update_job(job_id, {"progress": f"Parsing {len(source_files)} files..."})

        with monitor.timer("file_parsing"):
            parsed_files, languages, classes_found = await asyncio.to_thread(
                _parse_source_files, source_files, job_id, monitor
            )

        with monitor.timer("registry_build"):
            registry = await asyncio.to_thread(function_registry.build_registry, parsed_files)
        monitor.metrics.files_analyzed = len(parsed_files)
        monitor.metrics.functions_found = len(registry)

        update_job(job_id, {"progress": f"Building dependency graph ({len(registry)} functions)..."})

        with monitor.timer("dependency_graph"):
            graph_filter = dependency_analyzer.GraphFilter(
                max_nodes=config.MAX_GRAPH_NODES,
                cluster_by_file=True,
                max_external_nodes=20,
            )

            def _build_graph():
                dependency_graph = dependency_analyzer.build_dependency_graph(
                    parsed_files, max_nodes=config.MAX_GRAPH_NODES, graph_filter=graph_filter
                )
                return dependency_graph.to_dict(), dependency_analyzer.get_graph_stats(dependency_graph)

            graph_dict, graph_stats = await asyncio.to_thread(_build_graph)

        explanation_groups, file_function_counts, source_files_map = await asyncio.to_thread(
            _build_explanation_groups, registry
        )

        parsed_count = len(parsed_files)

        # parsed_files / raw_source are only needed until the registry and
        # graph exist - release the bulk source text before returning so a
        # 100K-LOC repository does not keep every file body resident (spec 16).
        del parsed_files

        structural_counts = {
            "files": parsed_count,
            "functions": len(registry),
            "classes": classes_found,
            "graph_nodes": graph_stats.get("total_nodes", len(graph_dict.get("nodes", []))),
            "graph_edges": graph_stats.get("total_edges", len(graph_dict.get("edges", []))),
            "file_function_counts": file_function_counts,
        }
        structural_time = time.time() - structural_start
        monitor.stop()
        ai_enabled = config.AI_ANALYSIS_ENABLED and len(registry) > 0
        language_list = sorted(languages) if languages else ["Python"]

        update_job(job_id, {
            "status": "ai_generating" if ai_enabled else "complete",
            "ai_status": ai_pipeline.STATUS_AI_QUEUED if ai_enabled else ai_pipeline.STATUS_AI_DISABLED,
            "progress": "Structural analysis complete - generating AI explanations...",
            "structural_ready": True,
            "structural": structural_counts,
            "functions_found": len(registry),
            "files_found": parsed_count,
            "summary": {
                "files_analyzed": parsed_count,
                "functions_found": len(registry),
                "classes_found": classes_found,
                "graph_nodes": structural_counts["graph_nodes"],
                "graph_edges": structural_counts["graph_edges"],
                "avg_coverage": 0,
                "breaking_changes": 0,
                "languages": language_list,
            },
            "graph": graph_dict,
            "explanation": explanation_groups,
            "tests": [],
            "refactor": [],
            "analysis_progress": {"completed": 0, "total": len(registry), "failed": 0, "cached": 0, "trivial": 0},
            "performance": {**monitor.get_metrics(), "structural_total": round(structural_time, 2), "ingest": ingest_stats},
            "pipeline": PIPELINE_VERSION,
            "extract_dir": extract_dir,
            "source_files": source_files_map,
            "languages": language_list,
        })

        job_store.save_functions(job_id, function_registry.registry_to_job_functions(registry))
        job_store.save_graph(job_id, graph_dict)
        logger.info(
            f"[ANALYSIS] job={job_id} structural_complete files={parsed_count} "
            f"functions={len(registry)} classes={classes_found} "
            f"graph_nodes={structural_counts['graph_nodes']} graph_edges={structural_counts['graph_edges']} "
            f"duration={structural_time:.2f}s"
        )
        ai_pipeline.publish(job_id, {
            "type": "structural_complete",
            "duration": round(structural_time, 2),
            **structural_counts,
        })

        analysis_succeeded = True

        if ai_pipeline.is_cancelled(job_id):
            update_job(job_id, {"status": "complete", "ai_status": ai_pipeline.STATUS_AI_CANCELLED})
            ai_pipeline.publish(job_id, {"type": "analysis_cancelled", "total": len(registry), "completed": 0})
            return

        if ai_enabled:
            ai_pipeline.schedule(job_id, registry, _ai_hooks(job_id))
        else:
            update_job(job_id, {"progress": "Analysis complete"})

    except Exception as e:
        error_msg = f"Pipeline error: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_msg)
        update_job(job_id, {"status": "error", "message": str(e)})
    finally:
        if analysis_succeeded:
            # Keep extracted sources on disk for on-demand test/refactor
            # generation; only drop the uploaded ZIP to free space.
            try:
                zip_path = os.path.join(extract_dir, "upload.zip")
                if os.path.isfile(zip_path):
                    os.remove(zip_path)
            except OSError:
                pass
        else:
            shutil.rmtree(extract_dir, ignore_errors=True)


@app.post("/analyze")
async def analyze_codebase(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only ZIP files are supported")

    job_id = str(uuid.uuid4())
    update_job(job_id, {
        "status": "processing",
        "ai_status": ai_pipeline.STATUS_AI_QUEUED,
        "progress": "Uploading...",
        "source_type": "upload",
    }, upsert=True)

    extract_dir = os.path.join(tempfile.gettempdir(), f"oracle_{job_id}")
    os.makedirs(extract_dir, exist_ok=True)
    zip_path = os.path.join(extract_dir, "upload.zip")

    # Stream to disk in chunks so a large upload is never fully buffered in RAM.
    size = 0
    try:
        with open(zip_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_ZIP_SIZE_BYTES:
                    message = f"ZIP exceeds the {config.MAX_ZIP_SIZE_MB}MB upload limit"
                    update_job(job_id, {"status": "error", "message": message})
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    return JSONResponse({"job_id": job_id, "status": "error", "message": message}, status_code=413)
                out.write(chunk)
    except OSError as e:
        shutil.rmtree(extract_dir, ignore_errors=True)
        update_job(job_id, {"status": "error", "message": f"Upload failed: {e}"})
        return JSONResponse({"job_id": job_id, "status": "error", "message": f"Upload failed: {e}"}, status_code=500)

    if size == 0:
        update_job(job_id, {"status": "error", "message": "ZIP file is empty"})
        shutil.rmtree(extract_dir, ignore_errors=True)
        return JSONResponse({"job_id": job_id, "status": "error", "message": "ZIP file is empty"})

    # Extraction is CPU/IO heavy on big archives - keep it off the event loop
    # so health checks, SSE and status polls stay responsive during uploads.
    try:
        await asyncio.to_thread(repo_ingest.safe_extract_zip, zip_path, extract_dir)
    except repo_ingest.IngestError as e:
        update_job(job_id, {"status": "error", "message": str(e)})
        shutil.rmtree(extract_dir, ignore_errors=True)
        return JSONResponse({"job_id": job_id, "status": "error", "message": str(e)})
    except Exception as e:  # noqa: BLE001
        update_job(job_id, {"status": "error", "message": f"Failed to extract ZIP: {e}"})
        shutil.rmtree(extract_dir, ignore_errors=True)
        return JSONResponse({"job_id": job_id, "status": "error", "message": str(e)})

    _evict_stale_jobs(job_id)
    background_tasks.add_task(run_analysis, job_id, extract_dir)

    return {"job_id": job_id, "status": "processing"}


@app.post("/analyze/github")
async def analyze_github_repository(background_tasks: BackgroundTasks, payload: Dict[str, Any] = Body(...)):
    """Import a PUBLIC GitHub repository and run the standard analysis pipeline.

    The remote archive is fetched into the same temporary directory layout a
    manual upload uses, extracted with the same ZIP-slip-safe extractor, then
    handed to ``run_analysis`` - i.e. identical structural + AI analysis, no
    parallel pipeline. No GitHub credentials are requested or stored.
    """
    repo_url = (
        payload.get("repo_url")
        or payload.get("url")
        or payload.get("repository_url")
        or ""
    ).strip()

    try:
        owner, repo, ref = github_import.parse_github_url(repo_url)
    except github_import.GithubImportError as exc:
        logger.info("[GITHUB] repository_import_rejected reason=%s", exc.code)
        raise HTTPException(status_code=400, detail=exc.message)

    canonical_url = f"https://github.com/{owner}/{repo}"
    if ref:
        canonical_url += f"/tree/{ref}"

    job_id = str(uuid.uuid4())
    extract_dir = os.path.join(tempfile.gettempdir(), f"oracle_{job_id}")
    os.makedirs(extract_dir, exist_ok=True)
    zip_path = os.path.join(extract_dir, "upload.zip")

    logger.info("[GITHUB] job=%s repository_import_started repository=%s/%s url=%s", job_id, owner, repo, canonical_url)

    # --- fetch (off the event loop; streamed to disk with a hard size cap) ---
    try:
        meta = await asyncio.to_thread(
            github_import.download_repository_zip, owner, repo, ref, zip_path
        )
    except github_import.GithubImportError as exc:
        logger.warning("[GITHUB] job=%s repository_import_failed reason=%s", job_id, exc.code)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return JSONResponse(
            {"job_id": job_id, "status": "error", "error_code": exc.code, "message": exc.message},
            status_code=_github_error_status(exc.code),
        )
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the client
        logger.error("[GITHUB] job=%s repository_import_failed unexpected=%s", job_id, exc, exc_info=True)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return JSONResponse(
            {"job_id": job_id, "status": "error", "error_code": github_import.ERR_DOWNLOAD_FAILED,
             "message": "Unable to download this repository. Please try again."},
            status_code=502,
        )

    logger.info("[GITHUB] download_completed job=%s repository=%s size=%s", job_id, meta.get("full_name"), meta.get("downloaded_bytes"))

    # --- extract with the same safe extractor used by uploads (off the loop) ---
    try:
        ingest_stats = await asyncio.to_thread(repo_ingest.safe_extract_zip, zip_path, extract_dir)
    except repo_ingest.IngestError as exc:
        logger.warning("[GITHUB] job=%s extraction_failed reason=%s", job_id, exc)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return JSONResponse(
            {"job_id": job_id, "status": "error", "error_code": github_import.ERR_DOWNLOAD_FAILED,
             "message": "Unable to download this repository. Please try again."},
            status_code=400,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[GITHUB] job=%s extraction_failed unexpected=%s", job_id, exc, exc_info=True)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return JSONResponse(
            {"job_id": job_id, "status": "error", "error_code": github_import.ERR_DOWNLOAD_FAILED,
             "message": "Unable to download this repository. Please try again."},
            status_code=502,
        )

    source_digest = await asyncio.to_thread(github_import.compute_source_digest, extract_dir)
    logger.info("[GITHUB] extraction_completed job=%s files=%s digest=%s",
                job_id, ingest_stats.get("extracted_files"), (source_digest or "")[:12])

    update_job(job_id, {
        "status": "processing",
        "ai_status": ai_pipeline.STATUS_AI_QUEUED,
        "progress": "Preparing repository...",
        "source_type": "github",
        "repository_url": canonical_url,
        "repository": meta.get("full_name") or f"{owner}/{repo}",
        "owner": owner,
        "branch": meta.get("ref") or meta.get("default_branch"),
        "source_digest": source_digest,
    }, upsert=True)

    logger.info("[GITHUB] analysis_started job=%s repository=%s", job_id, meta.get("full_name"))
    _evict_stale_jobs(job_id)
    background_tasks.add_task(run_analysis, job_id, extract_dir)

    return {
        "job_id": job_id,
        "status": "processing",
        "source_type": "github",
        "repository": meta.get("full_name") or f"{owner}/{repo}",
        "owner": owner,
        "branch": meta.get("ref") or meta.get("default_branch"),
        "repository_url": canonical_url,
    }


def _github_error_status(code: str) -> int:
    """Map a stable GitHub error code onto an appropriate HTTP status."""
    return {
        github_import.ERR_INVALID_URL: 400,
        github_import.ERR_NOT_FOUND: 404,
        github_import.ERR_PRIVATE: 403,
        github_import.ERR_TOO_LARGE: 413,
        github_import.ERR_RATE_LIMITED: 429,
        github_import.ERR_UNAVAILABLE: 502,
    }.get(code, 502)


@app.get("/results/{job_id}")
async def get_results(job_id: str):
    """Return the job. During AI generation this already includes the full
    structural payload (graph, file groups, counts) plus whatever explanations
    are ready, so the UI never has to wait for 100% completion."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("status") == "processing":
        return {
            "status": "processing",
            "progress": job.get("progress", "Processing..."),
            "functions_found": job.get("functions_found"),
            "files_found": job.get("files_found"),
        }

    return job


@app.get("/progress/{job_id}")
async def stream_progress(job_id: str):
    """Backward-compatible coarse progress SSE stream."""
    async def event_generator():
        while True:
            job = get_job(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break

            status = job.get("status", "processing")
            payload = {
                "status": status,
                "progress": job.get("progress", "Processing..."),
                "ai_status": job.get("ai_status"),
                "analysis_progress": job.get("analysis_progress"),
                "structural_ready": job.get("structural_ready", False),
                "functions_found": job.get("functions_found"),
                "files_found": job.get("files_found"),
            }
            yield f"data: {json.dumps(payload)}\n\n"

            if status in ("complete", "error", "cancelled"):
                yield f"data: {json.dumps({'status': status, 'done': True})}\n\n"
                break

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


def _sse(event: Dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


_TERMINAL_EVENTS = ("analysis_completed", "analysis_error", "analysis_cancelled")


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str):
    """Typed progressive event stream: structural completion, batch progress,
    per-result payloads, completion/error/cancellation."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        queue = ai_pipeline.subscribe(job_id)
        try:
            current = get_job(job_id) or {}
            if current.get("structural_ready"):
                yield _sse({
                    "type": "structural_complete",
                    "job_id": job_id,
                    **(current.get("structural") or {}),
                })
            ai = ai_pipeline.get_ai_status(job_id)
            if ai:
                yield _sse({"type": "progress", "job_id": job_id, **ai})

            status = current.get("status")
            if status == "error":
                yield _sse({"type": "analysis_error", "job_id": job_id,
                            "message": current.get("message", "Analysis failed")})
                return
            if status in ("complete", "cancelled"):
                yield _sse({
                    "type": "analysis_cancelled" if current.get("ai_status") == ai_pipeline.STATUS_AI_CANCELLED else "analysis_completed",
                    "job_id": job_id,
                    "status": current.get("ai_status"),
                    "failed_functions": current.get("failed_functions", []),
                    **(ai or {}),
                })
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    job_now = get_job(job_id) or {}
                    if job_now.get("status") in ("complete", "error", "cancelled"):
                        break
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)
                if event.get("type") in _TERMINAL_EVENTS:
                    break
        finally:
            ai_pipeline.unsubscribe(job_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.get("/jobs/{job_id}/status")
async def job_status(job_id: str):
    """Lightweight polling status for structural + AI progress.

    Carries everything the UI needs to render the results shell WITHOUT ever
    fetching the full /results payload (graph + all explanations), which is
    multi-megabyte on large repositories (spec sections 2, 9 and 27)."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    ai = ai_pipeline.get_ai_status(job_id) or {}
    structural = job.get("structural") or {}
    summary = job.get("summary") or {}
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "ai_status": job.get("ai_status") or ai.get("ai_status"),
        "structural_ready": bool(job.get("structural_ready")),
        "structural": structural,
        "progress": job.get("progress"),
        "message": job.get("message"),
        "functions_found": job.get("functions_found") or structural.get("functions"),
        "files_found": job.get("files_found") or structural.get("files"),
        "summary": summary,
        "source_type": job.get("source_type"),
        "repository_url": job.get("repository_url"),
        "repository": job.get("repository"),
        "branch": job.get("branch"),
        "languages": job.get("languages") or summary.get("languages") or [],
        "tests": job.get("tests") or [],
        "refactor": job.get("refactor") or [],
        "analysis_progress": job.get("analysis_progress") or _progress_from(ai),
        "failed_functions": job.get("failed_functions") or ai.get("failed_functions") or [],
        "ai_stats": job.get("ai_stats") or {},
        "performance": job.get("performance") or {},
        "file_counts": structural.get("file_function_counts") or {},
    }


@app.get("/jobs/{job_id}/functions")
async def job_functions(
    job_id: str,
    offset: int = 0,
    limit: int = 100,
    search: str = "",
    include_explanation: int = 1,
):
    """Paginated function window (the only way the UI should read functions).

    Order matches the classic explanation array (files in discovery order,
    functions sorted by name) so pagination is stable across calls. Server-side
    search keeps the client from downloading tens of thousands of entries to
    filter a handful (spec sections 2, 10 and 26)."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    query = (search or "").strip().lower()

    if not job.get("structural_ready"):
        return {
            "job_id": job_id,
            "status": job.get("status"),
            "pending": True,
            "total": 0,
            "offset": 0,
            "limit": limit,
            "query": search or "",
            "groups": [],
        }

    refs: List[Any] = []
    for grp in job.get("explanation") or []:
        fname = grp.get("filename") or "unknown"
        for fn in grp.get("functions") or []:
            refs.append((fname, fn))

    if query:
        refs = [
            (fname, fn) for (fname, fn) in refs
            if query in (fn.get("name") or "").lower() or query in fname.lower()
        ]

    total = len(refs)
    window = refs[offset:offset + limit]

    if any(fn.get("explanation") is None for _, fn in window):
        # Entries without a stored explanation (reload gap for cancelled /
        # LLM-disabled jobs): fill the static explanation for just this page.
        registry_by_id = {
            r.get("id"): r for r in job.get("registry") or [] if isinstance(r, dict)
        }
        for _, fn in window:
            if fn.get("explanation") is None:
                reg = registry_by_id.get(fn.get("function_id"))
                if reg:
                    fn["explanation"] = llm.normalize_explanation_item(
                        llm.analyze_function_ast(
                            {
                                "display_name": reg.get("qualified_name") or reg.get("name"),
                                "name": reg.get("name"),
                                "args": reg.get("args") or [],
                                "body": reg.get("source_code") or "",
                            },
                            reg.get("filename"),
                        )
                    )
                    fn.setdefault("ai_status", "static")

    try:
        include = int(include_explanation) != 0
    except (TypeError, ValueError):
        include = True
    out_groups: Dict[str, Dict[str, Any]] = {}
    loaded = 0
    for fname, fn in window:
        grp = out_groups.get(fname)
        if grp is None:
            grp = {
                "filename": fname,
                "module_summary": _local_module_summary(fname),
                "functions": [],
            }
            out_groups[fname] = grp
        item = {
            "name": fn.get("name"),
            "function_id": fn.get("function_id"),
            "ai_status": fn.get("ai_status"),
        }
        if include:
            item["explanation"] = fn.get("explanation")
        grp["functions"].append(item)
        loaded += 1

    return {
        "job_id": job_id,
        "status": job.get("status"),
        "total": total,
        "offset": offset,
        "limit": limit,
        "loaded": loaded,
        "query": search or "",
        "groups": list(out_groups.values()),
    }


@app.get("/jobs/{job_id}/functions/{function_id}/explanation")
async def job_function_explanation(job_id: str, function_id: str):
    """Lazy single-function explanation (spec sections 5 and 19).

    The paginated window normally carries explanations, but rows can arrive
    status-only via progress streams; the UI fetches text on expand."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    for grp in job.get("explanation") or []:
        for fn in grp.get("functions") or []:
            if fn.get("function_id") == function_id:
                if fn.get("explanation") is None:
                    reg = next(
                        (r for r in job.get("registry") or []
                         if isinstance(r, dict) and r.get("id") == function_id),
                        None,
                    )
                    if reg:
                        fn["explanation"] = llm.normalize_explanation_item(
                            llm.analyze_function_ast(
                                {
                                    "display_name": reg.get("qualified_name") or reg.get("name"),
                                    "name": reg.get("name"),
                                    "args": reg.get("args") or [],
                                    "body": reg.get("source_code") or "",
                                },
                                reg.get("filename"),
                            )
                        )
                        fn.setdefault("ai_status", "static")
                return {
                    "function_id": function_id,
                    "name": fn.get("name"),
                    "ai_status": fn.get("ai_status"),
                    "explanation": fn.get("explanation"),
                }

    stored = job_store.load_explanations(job_id).get(function_id)
    if stored:
        return {
            "function_id": function_id,
            "name": stored.get("name"),
            "ai_status": stored.get("ai_status"),
            "explanation": stored.get("explanation"),
        }
    raise HTTPException(status_code=404, detail="Function not found")


@app.get("/jobs/{job_id}/graph")
async def job_graph(job_id: str,
                    max_nodes: int = 250,
                    node_types: Optional[str] = None,
                    files: Optional[str] = None,
                    min_degree: int = 0,
                    max_external: int = 20,
                    cluster: Optional[bool] = None):
    """Dependency-graph payload for a job.

    With no filter arguments this returns the stored graph, loaded lazily from
    the job store so the payload is only fetched when the graph tab opens.

    When filter arguments are supplied and the original sources are still on
    disk the graph is rebuilt with the requested node type / file / degree
    filters and file-based clusters.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    has_filter = bool(node_types or files or min_degree > 0) or max_external != 20 \
        or cluster is not None or max_nodes != 250

    if has_filter:
        extract_dir = job.get("extract_dir")
        if extract_dir and os.path.isdir(extract_dir):
            source_paths, _ = repo_ingest.find_source_files(extract_dir)
            parsed_files = []
            for source_file in source_paths:
                try:
                    file_hash = cache.get_file_hash(source_file)
                    cached = cache.get_cached("ast_analysis", file_hash)
                    if cached:
                        parsed_files.append(cached)
                    elif source_file.endswith(".py"):
                        parsed_files.append(ast_analyzer.analyze_file(source_file).to_dict())
                    elif source_file.endswith((".js", ".ts", ".jsx", ".tsx")):
                        parsed_files.append(js_parser.parse_javascript_file(source_file))
                except Exception:
                    continue

            graph_filter = dependency_analyzer.GraphFilter(
                max_nodes=max_nodes,
                node_types=set(node_types.split(",")) if node_types else None,
                files=set(files.split(",")) if files else None,
                min_degree=min_degree,
                max_external_nodes=max_external,
                cluster_by_file=(cluster if cluster is not None else True),
            )
            dependency_graph = await asyncio.to_thread(
                dependency_analyzer.build_dependency_graph,
                parsed_files,
                max_nodes=max_nodes,
                graph_filter=graph_filter,
            )
            return dependency_graph.to_dict()

    graph = job.get("graph")
    if not graph:
        graph = await asyncio.to_thread(job_store.load_graph, job_id)
        if graph:
            job["graph"] = graph
    return graph or {"nodes": [], "edges": []}


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Request cancellation. In-flight LLM requests finish; queued batches stop."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    ai_pipeline.request_cancel(job_id)
    update_job(job_id, {"cancel_requested": True})
    ai_pipeline.publish(job_id, {"type": "cancel_requested", "job_id": job_id})
    return {"job_id": job_id, "status": "cancelling"}


_generation_locks: Dict[str, asyncio.Lock] = {}


def get_generation_lock(job_id: str) -> asyncio.Lock:
    """Serialize on-demand generation per job so concurrent requests don't
    overwrite each other's appended results."""
    if job_id not in _generation_locks:
        _generation_locks[job_id] = asyncio.Lock()
    return _generation_locks[job_id]


def require_complete_job(job_id: str) -> Dict[str, Any]:
    """A job is usable once STRUCTURAL analysis is done - on-demand tests and
    refactors do not need to wait for every AI explanation to finish."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.get("structural_ready") and job.get("status") not in ("complete",):
        raise HTTPException(status_code=409, detail="Structural analysis is still running")
    return job


def load_job_function(job: Dict[str, Any], function_name: str, filename: str):
    """Re-parse a source file retained from a completed job and locate the
    requested function (or class method).

    Returns (func_dict, source_file, source_code).
    """
    source_files = job.get("source_files") or {}
    source_file = source_files.get(filename)
    if not source_file or not os.path.isfile(source_file):
        raise HTTPException(status_code=410, detail="Source files for this analysis are no longer available on the server. Please re-upload the archive.")

    file_hash = cache.get_file_hash(source_file)
    analysis_dict = cache.get_cached("ast_analysis", file_hash)
    if not analysis_dict:
        if source_file.endswith(".py"):
            analysis = ast_analyzer.analyze_file(source_file)
            analysis_dict = analysis.to_dict()
            analysis_dict["raw_source"] = analysis.source
        elif source_file.endswith((".js", ".ts", ".jsx", ".tsx")):
            analysis_dict = js_parser.parse_javascript_file(source_file)
            if "error" in analysis_dict:
                raise HTTPException(status_code=500, detail=f"Could not parse {filename}: {analysis_dict['error']}")
        else:
            raise HTTPException(status_code=415, detail=f"Unsupported file type: {filename}")

    funcs = []
    for func in analysis_dict.get("functions", []):
        f = dict(func)
        f["source_file"] = source_file
        f["filename"] = analysis_dict.get("filename", filename)
        f["display_name"] = f.get("name", "")
        funcs.append(f)
    for cls in analysis_dict.get("classes", []):
        for method in cls.get("methods", []):
            m = dict(method)
            m["source_file"] = source_file
            m["filename"] = analysis_dict.get("filename", filename)
            m["class_name"] = cls.get("name", "")
            m["display_name"] = f"{cls.get('name', '')}.{method.get('name', '')}"
            funcs.append(m)

    match = next((f for f in funcs if (f.get("display_name") or f.get("name", "")) == function_name), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Function '{function_name}' not found in {filename}")

    if source_file.endswith(".py") and analysis_dict.get("raw_source"):
        source_code = analysis_dict["raw_source"]
    else:
        try:
            with open(source_file, "r", encoding="utf-8") as fh:
                source_code = fh.read()
        except OSError:
            source_code = ""
    return match, source_file, source_code


# ---------------------------------------------------------------------------
# Per-function generation context (spec sections 8-10, 13)
# ---------------------------------------------------------------------------
# The model must see the COMPLETE target function, its class, and the file's
# real imports - never a truncated copy of the whole file. Everything here is
# pure text slicing: no execution, no LLM, no coverage, so initial analysis
# speed is unaffected (these helpers only run on-demand).

def _function_slice(source_code: str, func: Dict[str, Any]) -> str:
    """Exact source text of the function, preferring real line numbers."""
    body = func.get("body") or ""
    start = func.get("lineno")
    if start:
        lines = (source_code or "").splitlines()
        end = func.get("end_lineno")
        if not end:
            end = start + max(len(body.splitlines()) - 1, 0)
        if 1 <= start <= len(lines):
            text = "\n".join(lines[start - 1:min(end, len(lines))])
            if text.strip():
                return text
    return body


def _file_import_lines(source_code: str, is_js: bool) -> List[str]:
    """Collect the module's import statements (top of file) for the prompt."""
    imports: List[str] = []
    in_doc = False
    doc_marker = ""
    for raw in (source_code or "").splitlines()[:150]:
        stripped = raw.strip()
        if not stripped:
            continue
        if not is_js and stripped.startswith("#"):
            continue
        if is_js and stripped.startswith("//"):
            continue
        if not imports and not in_doc and stripped[:3] in ('"""', "'''"):
            marker = stripped[:3]
            rest = stripped[3:]
            if rest.endswith(marker) and len(rest) >= 3:
                continue  # one-line module docstring
            in_doc = True
            doc_marker = marker
            continue
        if in_doc:
            if doc_marker in stripped:
                in_doc = False
            continue
        if is_js:
            if (stripped.startswith("import ")
                    or re.match(r"^(const|let|var)\s+.*=\s*require\(", stripped)
                    or (stripped.startswith("export ") and " from " in stripped)):
                imports.append(stripped)
                continue
            # First real code line ends the import block.
            break
        if stripped.startswith(("import ", "from ")):
            imports.append(stripped)
            continue
        break  # python: def/class/@ decorator ends the import block
    return imports[:40]


def _class_source_slice(source_code: str, class_name: str, is_js: bool) -> str:
    """Source text of the containing class (constructor + sibling methods)."""
    if not class_name or not source_code:
        return ""
    lines = source_code.splitlines()
    if is_js:
        pat = re.compile(
            rf"^\s*(export\s+)?(default\s+)?(abstract\s+)?class\s+{re.escape(class_name)}\b"
        )
        start = next((i for i, l in enumerate(lines) if pat.match(l)), None)
        if start is None:
            return ""
        depth, seen, end = 0, False, len(lines) - 1
        for i in range(start, len(lines)):
            depth += lines[i].count("{") - lines[i].count("}")
            if "{" in lines[i]:
                seen = True
            if seen and depth <= 0:
                end = i
                break
        return "\n".join(lines[start:end + 1])[:6000]
    pat = re.compile(rf"^class\s+{re.escape(class_name)}\b")
    start = next((i for i, l in enumerate(lines) if pat.match(l)), None)
    if start is None:
        return ""
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if line[0] not in (" ", "\t") and not line.startswith("#"):
            end = i
            break
    return "\n".join(lines[start:end])[:6000]


def _import_example_for(source_file: str, framework: Dict[str, Any], is_js: bool) -> str:
    """The exact import statement a generated test should use for this file."""
    module = os.path.splitext(os.path.basename(source_file))[0]
    ext = os.path.splitext(source_file)[1]
    if not is_js:
        return f"import {module}"
    # The test file is staged NEXT TO the source, with its real extension.
    spec = f"./{module}{ext}"
    if (framework.get("moduleSystem") or "cjs") == "esm":
        return f"import {{ /* named exports */ }} from '{spec}'"
    return f"const {{ /* named exports */ }} = require('{spec}')"


def build_test_context(
    func: Dict[str, Any],
    source_file: str,
    source_code: str,
    framework: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the complete, untruncated context for one function's test suite."""
    is_js = source_file.endswith((".js", ".ts", ".jsx", ".tsx"))
    framework = framework or {}
    return {
        "func": func,
        "is_js": is_js,
        "source_file": source_file,
        "source_ext": os.path.splitext(source_file)[1].lower(),
        "source_basename": os.path.basename(source_file),
        "module": os.path.splitext(os.path.basename(source_file))[0],
        "function_source": _function_slice(source_code, func),
        "class_source": _class_source_slice(
            source_code, func.get("class_name") or "", is_js
        ),
        "import_lines": _file_import_lines(source_code, is_js),
        "framework": framework,
        "import_example": _import_example_for(source_file, framework, is_js),
        "target": config.MIN_TEST_COVERAGE,
        "language": framework.get("language") or ("javascript" if is_js else "python"),
    }


def compute_function_coverage(func: Dict[str, Any], cov_result: Dict[str, Any]):
    """Scope file-level coverage down to the target function's line range.

    Test generation is per-function now, so the card should report coverage of
    that function's executable lines, not the whole file. Returns a dict with
    coverage fields, or None when the range can't be determined.
    """
    start = func.get("lineno")
    if not start:
        return None
    end = func.get("end_lineno")
    if not end:
        body_lines = len((func.get("body") or "").splitlines())
        end = start + max(body_lines - 1, 0)

    covered_lines = cov_result.get("covered_lines") or []
    missing_lines = cov_result.get("missing_lines") or []
    in_range_cov = [ln for ln in covered_lines if start <= ln <= end]
    in_range_missing = [ln for ln in missing_lines if start <= ln <= end]
    total = len(in_range_cov) + len(in_range_missing)
    if total == 0:
        return None
    return {
        "coverage_percent": round(len(in_range_cov) / total * 100, 1),
        "lines_total": total,
        "lines_covered": len(in_range_cov),
        "lines_missing": len(in_range_missing),
        "missing_lines": sorted(in_range_missing)
    }



# ---------------------------------------------------------------------------
# Minimum measured coverage gate for generated tests
# ---------------------------------------------------------------------------
# Every suite that is reported back as a successful result must reach
# ``config.MIN_TEST_COVERAGE`` of *measured* line coverage. Coverage always
# comes from a real pytest/node run of the coverage tool; the helpers below
# never synthesize, round up, or hardcode a percentage.

_DUMMY_STRING_HINTS = (
    "name", "text", "msg", "message", "label", "title", "key", "path",
    "filename", "reason", "currency", "desc", "status",
)
_DUMMY_SEQ_HINTS = (
    "items", "lst", "list", "arr", "array", "values", "rows", "entries",
    "records", "args", "kwargs",
)
_DUMMY_INT_HINTS = (
    "count", "qty", "quantity", "num", "number", "size", "index", "idx",
    "stock", "age", "limit", "length", "total", "n", "i",
)
_DUMMY_FLOAT_HINTS = (
    "price", "amount", "rate", "ratio", "score", "weight", "value",
    "percent", "discount", "tax",
)


def _dummy_value(param: str) -> str:
    """Pick a harmless placeholder argument for a synthesized smoke test."""
    name = (param or "").lower()
    if any(hint in name for hint in _DUMMY_SEQ_HINTS):
        return "[]"
    if name in ("mapping", "options", "config", "params", "data"):
        return "{}"
    if any(hint in name for hint in _DUMMY_STRING_HINTS):
        return '"value"'
    if any(hint in name for hint in _DUMMY_FLOAT_HINTS):
        return "1.0"
    if any(hint in name for hint in _DUMMY_INT_HINTS):
        return "1"
    return "1"


def _build_smoke_test(func: Dict[str, Any], source_file: str) -> Optional[str]:
    """Build a runnable smoke test that really imports and calls a trivial function.

    Trivial getters/setters/``__init__`` bodies skip the LLM for speed, but
    their coverage still has to be *measured*. This test genuinely executes the
    function so the coverage tool reports a real number; when it lands below the
    target the normal improvement loop takes over.

    Returns None for anything we cannot build safely (e.g. JavaScript).
    """
    if not source_file.endswith(".py"):
        return None

    raw_name = (func.get("name") or "").strip()
    if not raw_name or raw_name.startswith("<"):
        return None

    args = [a for a in (func.get("args") or []) if a not in ("self", "cls")]
    module = os.path.splitext(os.path.basename(source_file))[0]
    arg_str = ", ".join(_dummy_value(a) for a in args)
    class_name = (func.get("class_name") or "").strip()

    if class_name:
        qualified = f"{module}.{class_name}"
        if raw_name in ("__init__", "__new__"):
            call = (
                f"    obj = {qualified}.__new__({qualified})\n"
                f"    {qualified}.{raw_name}(obj{', ' if arg_str else ''}{arg_str})"
            )
        else:
            call = f"    obj = {qualified}.__new__({qualified})\n    obj.{raw_name}({arg_str})"
    else:
        call = f"    {module}.{raw_name}({arg_str})"

    test_name = (raw_name.strip("_") or "target").replace(".", "_")
    return (
        f"# Auto-generated smoke test for '{raw_name}'.\n"
        f"# Coverage for this suite is measured by the coverage tool, never assumed.\n"
        f"import {module}\n\n\n"
        f"def test_{test_name}_smoke():\n"
        f"    \"\"\"Execute {raw_name} so its lines are genuinely exercised.\"\"\"\n"
        f"{call}\n"
    )


def _uncovered_brief(cov_result: Dict[str, Any]) -> str:
    """Describe what the current suite missed, using only real coverage output."""
    current = coverage_runner.measured_coverage(cov_result)
    passed = bool(cov_result.get("passed"))
    header = (
        f"Current measured coverage: {current:g}%\n"
        f"Required minimum coverage: {config.MIN_TEST_COVERAGE:g}%\n"
        f"The previous suite currently {'passes' if passed else 'FAILS'} its test run."
    )
    parts: List[str] = []
    # Diagnosis first: the LLM must see WHY the run failed (spec section 31),
    # not just which lines were uncovered.
    failure = cov_result.get("failure") or {}
    executed = int(cov_result.get("executed") or 0)
    if failure.get("category"):
        detail = (failure.get("detail") or "").strip()[:600]
        if executed <= 0:
            parts.append(
                f"DIAGNOSIS: NO tests were executed. Category {failure['category']}: {detail}"
            )
        else:
            parts.append(
                f"DIAGNOSIS: the suite ran ({executed} test(s)) but FAILED. "
                f"Category {failure['category']}: {detail}"
            )
    missing = sorted({
        int(line) for line in (cov_result.get("missing_lines") or [])
        if isinstance(line, (int, float))
    })
    if missing:
        parts.append("Uncovered source lines: " + ", ".join(str(l) for l in missing[:60]))
    for segment in (cov_result.get("uncovered_segments") or [])[:5]:
        source = (segment.get("source") or "").strip()
        if source:
            parts.append(
                f"Uncovered {segment.get('type')} `{segment.get('name')}` "
                f"(lines {segment.get('start_line')}-{segment.get('end_line')}):\n{source[:900]}"
            )
    failed = (cov_result.get("test_results") or {}).get("failed") or 0
    if failed:
        parts.append(f"{failed} test(s) failed on the previous run - keep the passing cases but fix those.")
    if not parts:
        parts.append("The function's remaining branches and edge cases are not exercised yet.")
    return header + "\n\n" + "\n\n".join(parts)


def _cov_rank(cov_result: Dict[str, Any]) -> tuple:
    """Ordering for picking the best *real* suite.

    A suite that passes AND reaches the measured target always wins; otherwise
    the highest measured coverage wins (best actual result), with passing as a
    tie-breaker. Percentages are read from the coverage tool, never adjusted.
    """
    meets = coverage_runner.meets_min_coverage(cov_result)
    passed = bool(cov_result.get("passed"))
    exercises = cov_result.get("exercises_target") is not False
    success = meets and passed and exercises
    return (
        1 if success else 0,
        1 if exercises else 0,
        coverage_runner.measured_coverage(cov_result),
        1 if passed else 0,
    )


def _suite_success(cov_result: Dict[str, Any]) -> bool:
    """Success = tests executed successfully AND measured coverage >= target.

    A suite that never invokes its target function can never be a success,
    however high the (import-only) coverage number looks.
    """
    if not cov_result:
        return False
    if cov_result.get("exercises_target") is False:
        return False
    return coverage_runner.meets_min_coverage(cov_result) and bool(cov_result.get("passed"))


def _test_references_function(test_code: str, func: Dict[str, Any]) -> bool:
    """Placeholder gate (spec sections 7, 40): the suite must invoke its target.

    A suite of ``assert True`` / typeof-only checks can import the module and
    even cover the ``def`` line while never executing the function - for
    trivial one-liners that fakes 100%. Requiring a real call site closes it.
    """
    if not test_code:
        return False
    name = (func.get("name") or "").strip()
    if not name or name.startswith("<"):
        return True
    try:
        if re.search(rf"\b{re.escape(name)}\s*\(", test_code):
            return True
        cls = (func.get("class_name") or "").strip()
        if cls and re.search(
            rf"(new\s+{re.escape(cls)}\b\s*\(|{re.escape(cls)}\.__new__\s*\()", test_code
        ):
            return True  # constructor tests may only construct, never name __init__
        if cls and re.search(rf"\b{re.escape(cls)}\s*\(", test_code):
            # A plain constructor call (`InventoryItem(...)`) implicitly runs
            # __init__ / the constructor; naming it explicitly is not required.
            return True
    except re.error:  # pragma: no cover - re.escape cannot fail, belt and braces
        return name in test_code
    return False


def _unusable_brief(func: Dict[str, Any]) -> str:
    """Diagnosis for a suite that never executed its target function."""
    name = func.get("name") or "the target function"
    return (
        "DIAGNOSIS: the previous test suite never executed the target function "
        f"'{name}' (or could not run at all), so it measured no real coverage.\n"
        "REQUIRED FIX:\n"
        f"- Import the module and CALL `{name}` directly with real arguments - "
        "every test must reach it.\n"
        "- Remove placeholder tests (assert True, typeof-only checks).\n"
        "- Assert real expected values for every branch of the function."
    )


def compute_test_status(cov_result: Any) -> str:
    """Map a measured (or failed) run onto the spec section 32 status codes.

    Precedence: GENERATION -> EXECUTION (nothing ran) -> TEST_FAILURE (ran and
    failed) -> TARGET_NOT_REACHED (passed but below target) -> SUCCESS.
    """
    if not isinstance(cov_result, dict):
        return "GENERATION_FAILURE"
    if cov_result.get("generation_failed") or cov_result.get("llm_unavailable"):
        return "GENERATION_FAILURE"
    if cov_result.get("exercises_target") is False:
        # The suite ran but never invoked the target function: the generated
        # tests themselves are the failure, not the execution.
        return "GENERATION_FAILURE"
    executed = int(cov_result.get("executed") or 0)
    if executed <= 0:
        return "EXECUTION_FAILURE"
    if not cov_result.get("passed"):
        return "TEST_FAILURE"
    if coverage_runner.meets_min_coverage(cov_result):
        return "SUCCESS"
    return "TARGET_NOT_REACHED"


def _entry_executed(entry: Dict[str, Any]) -> int:
    """Tests executed for a stored entry (handles pre-executed-field entries)."""
    if isinstance(entry.get("executed"), int):
        return entry["executed"]
    tr = entry.get("test_results") or {}
    try:
        return int(tr.get("passed") or 0) + int(tr.get("failed") or 0) + int(tr.get("errors") or 0)
    except (TypeError, ValueError):
        return 0


def _cached_test_entry_usable(entry: Any) -> bool:
    """Reuse a cached suite only while it still satisfies the coverage gate.

    Entries produced before the gate existed (or that never reached the target
    even after the full improvement budget) are regenerated once and re-cached,
    so the cache self-heals without bumping the prompt version. A recorded
    best-effort result is accepted so an unreachable target is not retried on
    every request.
    """
    if not isinstance(entry, dict) or not entry.get("test_code"):
        return False
    try:
        cached_cov = float(entry.get("coverage_percent") or 0)
    except (TypeError, ValueError):
        cached_cov = 0.0
    # Valid cached result: reached the configured minimum AND its tests passed.
    if cached_cov >= config.MIN_TEST_COVERAGE and bool(entry.get("passed")):
        return True
    return bool(entry.get("improvement_exhausted"))


async def _measure_suite(test_code: str, source_file: str, func: Dict[str, Any]) -> Dict[str, Any]:
    """Run a suite and scope the *measured* coverage down to the target function.

    FUNCTION scope is the default result (spec section 4): the gate applies to
    the target function's own lines; the whole-file number stays available as
    ``file_coverage_percent``. When the function's range can't be mapped (e.g.
    no line data), the honest FILE-scope number is reported instead - never a
    guessed value.
    """
    cov_result = await asyncio.to_thread(
        coverage_runner.run_coverage_for_file,
        test_code,
        source_file,
        f"test_{os.path.basename(source_file)}",
    )
    file_percent = cov_result.get("coverage_percent", 0)
    fn_cov = compute_function_coverage(func, cov_result)
    # Placeholder gate: did the suite actually invoke the target function?
    exercises = _test_references_function(test_code, func)
    if fn_cov:
        return {**cov_result, **fn_cov,
                "coverage_scope": "FUNCTION",
                "file_coverage_percent": file_percent,
                "exercises_target": exercises}
    return {**cov_result,
            "coverage_scope": "FILE",
            "file_coverage_percent": file_percent,
            "exercises_target": exercises}


async def _generate_with_min_coverage(
    func: Dict[str, Any],
    source_file: str,
    source_code: str,
    seed_test_code: Optional[str] = None,
    ctx: Optional[Dict[str, Any]] = None,
) -> tuple:
    """Generate tests, then improve them until *measured* coverage clears the minimum.

        generate -> run -> measure -> below target? -> feed uncovered lines back
                    to the LLM -> run -> measure -> ... (bounded budget)

    The suite with the highest *real* measured coverage is returned, so callers
    always report an actual result - never a synthesized or rounded-up number.
    Suites that never invoke the target function (placeholders) can be measured
    but can never count as success. Returns ``(test_code, cov_result, attempts)``.
    """
    is_js = source_file.endswith((".js", ".ts", ".jsx", ".tsx"))
    budget = config.TEST_MAX_ATTEMPTS
    target = config.MIN_TEST_COVERAGE
    func_name = func.get("name") or "function"
    logger.info(
        "[TEST] generation_started function=%s target=%s%% max_attempts=%s scope=FUNCTION",
        func_name, f"{target:g}", budget,
    )

    def _log_attempt(attempt_no: int, cov: Dict[str, Any]) -> None:
        logger.info(
            "[TEST] function=%s attempt=%s/%s coverage=%s%% file_coverage=%s%% "
            "target=%s%% passed=%s target_met=%s exercises=%s",
            func_name, attempt_no, budget,
            coverage_runner.measured_coverage(cov),
            cov.get("file_coverage_percent"), f"{target:g}",
            bool(cov.get("passed")), coverage_runner.meets_min_coverage(cov),
            cov.get("exercises_target"),
        )

    # ---- initial suite (complete context, no truncated source) ----------
    if seed_test_code is not None:
        test_code = seed_test_code
    elif ctx is not None:
        test_code = await llm.generate_function_tests(ctx)
    else:
        test_code = await llm.generate_tests_batch([func], source_code, source_file)
    if not test_code or test_code.startswith("# Error generating tests"):
        detail = test_code.split(":", 1)[1].strip() if test_code.startswith("# Error generating tests:") else ""
        raise ValueError("Test generation returned no usable tests" + (f" - {detail}" if detail else ""))

    best_code = test_code
    best_cov: Optional[Dict[str, Any]] = None
    attempts = 1
    if coverage_runner._is_valid_test_code(test_code, is_js=is_js):
        best_cov = await _measure_suite(test_code, source_file, func)
        _log_attempt(attempts, best_cov)
        if best_cov.get("exercises_target") is False:
            logger.info(
                "[TEST] initial suite never called %s (placeholder gate)", func_name
            )
    else:
        logger.info("[TEST] initial suite is not runnable for %s", func_name)

    # Keep improving while the suite has not both passed and reached the
    # measured minimum. Bounded by the configured attempt budget - never an
    # infinite loop - and stops the moment the target is genuinely met.
    while attempts < budget and not _suite_success(best_cov):
        if best_cov is None or best_cov.get("exercises_target") is False:
            brief = _unusable_brief(func)
            if best_cov is not None:
                brief = brief + "\n\n" + _uncovered_brief(best_cov)
        else:
            brief = _uncovered_brief(best_cov)
        try:
            if ctx is not None:
                improved = await llm.generate_tests_for_coverage(
                    func, brief, source_file, ctx=ctx
                )
            else:
                improved = await llm.generate_tests_for_coverage(
                    func, brief, source_file
                )
        except llm.QuotaExhaustedError as exc:
            # No capacity to improve: keep the best measured suite instead of
            # burning the remaining attempts on calls that cannot succeed.
            logger.warning("[TEST] stopping improvement for %s - %s", func_name, exc)
            break
        except Exception as exc:  # noqa: BLE001 - keep the best measured result
            logger.warning(f"[TEST] improvement attempt failed for {func_name}: {exc}")
            break
        if not improved or not coverage_runner._is_valid_test_code(improved, is_js=is_js):
            attempts += 1
            logger.info(
                "[TEST] attempt %s/%s produced unusable tests for %s",
                attempts, budget, func_name,
            )
            continue

        candidate_cov = await _measure_suite(improved, source_file, func)
        attempts += 1
        _log_attempt(attempts, candidate_cov)
        if candidate_cov.get("exercises_target") is False:
            logger.info(
                "[TEST] attempt %s never called %s (placeholder gate)",
                attempts, func_name,
            )
        if best_cov is None or _cov_rank(candidate_cov) > _cov_rank(best_cov):
            best_code, best_cov = improved, candidate_cov

    if best_cov is None:
        # Every produced suite was syntax-garbage - report an honest
        # no-execution result instead of inventing coverage (spec section 6).
        best_cov = {
            "coverage_percent": 0,
            "passed": False,
            "executed": 0,
            "lines_total": 0,
            "lines_covered": 0,
            "lines_missing": 0,
            "missing_lines": [],
            "uncovered_segments": [],
            "test_results": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "failure": {"category": "TEST_DISCOVERY_ERROR",
                        "detail": "generated tests were never runnable"},
            "coverage_scope": "FILE",
            "file_coverage_percent": 0,
            "exercises_target": False,
            "generation_failed": True,
        }

    logger.info(
        "[TEST] generation_completed function=%s best_coverage=%s%% file_coverage=%s%% "
        "attempts=%s target_met=%s passed=%s status=%s",
        func_name, coverage_runner.measured_coverage(best_cov),
        best_cov.get("file_coverage_percent"), attempts,
        coverage_runner.meets_min_coverage(best_cov), bool(best_cov.get("passed")),
        compute_test_status(best_cov),
    )
    return best_code, best_cov, attempts

# Per-(job, file, function) locks: identical simultaneous requests for the
# same function share one generation instead of duplicating the LLM call.
_function_locks: Dict[str, asyncio.Lock] = {}


def get_function_lock(job_id: str, filename: str, function_name: str) -> asyncio.Lock:
    if len(_function_locks) > 5000:
        _function_locks.clear()
    key = f"{job_id}:{filename}:{function_name}"
    lock = _function_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _function_locks[key] = lock
    return lock


def _function_id_for(func: Dict[str, Any], source_file: str) -> str:
    return function_registry.make_function_id(
        source_file,
        func.get("display_name") or func.get("name", ""),
        func.get("body", "") or "",
    )


def _tests_cache_version(framework: Dict[str, Any]) -> str:
    """Cache version = base version + detected environment (spec section 24).

    A suite generated under one framework/module system is never reused under
    another (e.g. jest+esm vs node:test+cjs produce different imports).
    """
    framework = framework or {}
    return (
        f"{config.TESTS_CACHE_VERSION}|{framework.get('language', '')}"
        f"-{framework.get('testRunner', '')}-{framework.get('moduleSystem', '')}"
    )


def _generation_failure_entry(
    function_name: str,
    filename: str,
    error: str,
    framework: Dict[str, Any],
    func_id: str,
) -> Dict[str, Any]:
    """Honest entry for a run where no suite could be produced (spec section 32)."""
    return {
        "name": function_name,
        "filename": filename,
        "test_code": "",
        "coverage_percent": 0,
        "passed": False,
        "test_output": "",
        "error": error,
        "lines_total": 0,
        "lines_covered": 0,
        "lines_missing": 0,
        "missing_lines": [],
        "test_results": {},
        "uncovered_segments": [],
        "min_coverage": config.MIN_TEST_COVERAGE,
        "coverage_target": config.MIN_TEST_COVERAGE,
        "meets_min_coverage": False,
        "coverage_target_met": False,
        "coverage_attempts": 0,
        "improvement_exhausted": False,
        "status": "GENERATION_FAILURE",
        "functionId": func_id,
        "attempts": 0,
        "executed": 0,
        "coverage_scope": "FILE",
        "file_coverage_percent": 0,
        "failure_category": "GENERATION_FAILURE",
        "failure_detail": error,
        "coverage": 0,
        "coverageTarget": config.MIN_TEST_COVERAGE,
        "coverageTargetMet": False,
        "testsPassed": False,
        "framework": framework,
        "cached": False,
    }


@app.post("/generate/tests/{job_id}")
async def generate_tests_on_demand(job_id: str, payload: Dict[str, Any] = Body(...)):
    """Generate unit tests for a single function on demand (one LLM call,
    content-cached and de-duplicated)."""
    job = require_complete_job(job_id)
    function_name = (payload.get("function_name") or "").strip()
    filename = (payload.get("filename") or "").strip()
    if not function_name or not filename:
        raise HTTPException(status_code=422, detail="function_name and filename are required")

    # User-triggered work: take priority over any running bulk analysis.
    llm.mark_interactive()

    func, source_file, source_code = load_job_function(job, function_name, filename)
    func_id = _function_id_for(func, source_file)

    async with get_function_lock(job_id, filename, function_name):
        # Framework detection is lazy (first request for this job) and folded
        # into the cache key, so a suite generated under one environment is
        # never reused under another (spec sections 24, 45).
        fw = test_framework.get_job_framework(job)
        cache_ver = _tests_cache_version(fw)
        fw_summary = {
            "language": fw.get("language"),
            "testRunner": fw.get("testRunner"),
            "framework": fw.get("framework"),
        }
        cached_entry = cache.get_operation_cached(
            "tests", func.get("body", ""), cache_ver, config.TESTGEN_CACHE_ID, func_id
        )
        if _cached_test_entry_usable(cached_entry):
            entry = {**cached_entry, "name": function_name, "filename": filename, "cached": True}
            # Backfill gate metadata for entries cached before these fields
            # existed so the UI always receives the same response contract.
            entry["coverage_target"] = config.MIN_TEST_COVERAGE
            if "meets_min_coverage" not in entry:
                try:
                    _cached_cov = float(entry.get("coverage_percent") or 0)
                except (TypeError, ValueError):
                    _cached_cov = 0.0
                entry["meets_min_coverage"] = _cached_cov >= config.MIN_TEST_COVERAGE
            entry.setdefault("coverage_attempts", 1)
            entry.setdefault("improvement_exhausted", False)
            entry["coverage_target_met"] = bool(entry["meets_min_coverage"])
            # Spec section 47 response contract (camelCase aliases).
            entry.setdefault("functionId", func_id)
            entry.setdefault("attempts", entry.get("coverage_attempts", 1))
            entry.setdefault("coverage", entry.get("coverage_percent", 0))
            entry.setdefault("coverageTarget", config.MIN_TEST_COVERAGE)
            entry.setdefault("coverageTargetMet", entry["coverage_target_met"])
            entry.setdefault("testsPassed", bool(entry.get("passed")))
            entry.setdefault("framework", fw_summary)
            if not entry.get("status"):
                entry["status"] = compute_test_status({
                    "executed": _entry_executed(entry),
                    "passed": entry.get("passed"),
                    "coverage_percent": entry.get("coverage_percent"),
                })
        else:
            # Trivial bodies get a real smoke test as the starting point instead
            # of an assumed percentage; everything then goes through the same
            # generate -> measure -> improve loop with the complete function
            # context (never a truncated source file).
            seed = None
            if is_trivial_function(func.get("body", "")):
                seed = _build_smoke_test(func, source_file)
            ctx = build_test_context(func, source_file, source_code, fw)

            entry = None
            try:
                test_code, cov_result, attempts = await _generate_with_min_coverage(
                    func, source_file, source_code, seed, ctx=ctx
                )
            except ValueError as exc:
                # Generation failed outright: report the status honestly in the
                # job (spec section 32) instead of a bare HTTP error, and do
                # NOT cache the failure so the next request retries.
                logger.warning(f"[TEST] generation failed for {function_name}: {exc}")
                cov_result, attempts, test_code = None, 0, ""
                entry = _generation_failure_entry(
                    function_name, filename, str(exc), fw_summary, func_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"On-demand test generation failed for {function_name}: {exc}")
                cov_result, attempts, test_code = None, 0, ""
                entry = _generation_failure_entry(
                    function_name, filename,
                    f"Test generation failed: {exc}", fw_summary, func_id,
                )

            if cov_result is not None:
                meets_min = coverage_runner.meets_min_coverage(cov_result)
                tests_passed = bool(cov_result.get("passed"))
                status = compute_test_status(cov_result)
                failure = cov_result.get("failure") or {}
                logger.info(
                    f"[TEST] job={job_id} function={function_name} status={status} "
                    f"measured={coverage_runner.measured_coverage(cov_result)}% "
                    f"file={cov_result.get('file_coverage_percent')}% "
                    f"scope={cov_result.get('coverage_scope')} "
                    f"target={config.MIN_TEST_COVERAGE:g}% attempts={attempts} "
                    f"passed={tests_passed} target_met={meets_min}"
                )

                entry = {
                    "name": function_name,
                    "filename": filename,
                    "test_code": test_code,
                    "coverage_percent": cov_result.get("coverage_percent", 0),
                    "passed": tests_passed,
                    "test_output": cov_result.get("output", ""),
                    "error": cov_result.get("error"),
                    "lines_total": cov_result.get("lines_total", 0),
                    "lines_covered": cov_result.get("lines_covered", 0),
                    "lines_missing": cov_result.get("lines_missing", 0),
                    "missing_lines": cov_result.get("missing_lines", []),
                    "test_results": cov_result.get("test_results", {}),
                    "uncovered_segments": cov_result.get("uncovered_segments", []),
                    # Coverage-gate metadata (all derived from the measured run).
                    "min_coverage": config.MIN_TEST_COVERAGE,
                    "coverage_target": config.MIN_TEST_COVERAGE,
                    "meets_min_coverage": meets_min,
                    "coverage_target_met": meets_min,
                    "coverage_attempts": attempts,
                    "improvement_exhausted": not (meets_min and tests_passed),
                    # Spec sections 32/34/47: status + execution detail + contract.
                    "status": status,
                    "functionId": func_id,
                    "attempts": attempts,
                    "executed": cov_result.get("executed", 0),
                    "coverage_scope": cov_result.get("coverage_scope", "FILE"),
                    "file_coverage_percent": cov_result.get("file_coverage_percent"),
                    "exercises_target": cov_result.get("exercises_target"),
                    "failure_category": failure.get("category"),
                    "failure_detail": failure.get("detail"),
                    "coverage": cov_result.get("coverage_percent", 0),
                    "coverageTarget": config.MIN_TEST_COVERAGE,
                    "coverageTargetMet": meets_min,
                    "testsPassed": tests_passed,
                    "framework": fw_summary,
                    "cached": False,
                }
                cache.set_operation_cached(
                    "tests", func.get("body", ""), cache_ver, config.TESTGEN_CACHE_ID, entry, func_id
                )

    async with get_generation_lock(job_id):
        job = get_job(job_id) or job
        tests = [t for t in (job.get("tests") or []) if t.get("name") != function_name]
        tests.append(entry)
        coverages = [t.get("coverage_percent", 0) or 0 for t in tests]
        summary = dict(job.get("summary") or {})
        summary["avg_coverage"] = round(sum(coverages) / len(coverages), 1) if coverages else 0
        update_job(job_id, {"tests": tests, "summary": summary}, persist=False)
        job_store.save_artifact(job_id, "tests", func_id, entry)
    return entry


@app.post("/generate/refactor/{job_id}")
async def refactor_on_demand(job_id: str, payload: Dict[str, Any] = Body(...)):
    """Refactor a single function on demand (one LLM call, content-cached)."""
    job = require_complete_job(job_id)
    function_name = (payload.get("function_name") or "").strip()
    filename = (payload.get("filename") or "").strip()
    if not function_name or not filename:
        raise HTTPException(status_code=422, detail="function_name and filename are required")

    func, source_file, _ = load_job_function(job, function_name, filename)
    is_js = source_file.endswith((".js", ".ts", ".jsx", ".tsx"))
    original_code = func.get("body", "")
    func_id = _function_id_for(func, source_file)

    # User-triggered work: take priority over any running bulk analysis.
    llm.mark_interactive()

    if is_trivial_function(original_code):
        entry = {
            "name": function_name,
            "filename": filename,
            "original_code": original_code,
            "refactored_code": original_code,
            "breaking_changes": [],
            "tests_verified": True,
            "cached": True,
        }
    else:
        async with get_function_lock(job_id, filename, function_name):
            cached_ref = cache.get_operation_cached(
                "refactor", original_code, config.PROMPT_VERSION_REFACTOR, config.MODEL, func_id
            )
            if isinstance(cached_ref, dict) and cached_ref.get("refactored_code"):
                refactor_code = cached_ref["refactored_code"]
                merged_changes = cached_ref.get("breaking_changes") or []
                was_cached = True
            else:
                try:
                    results = await llm.refactor_batch([func])
                except Exception as e:
                    logger.warning(f"On-demand refactor failed for {function_name}: {e}")
                    raise HTTPException(status_code=502, detail=f"Refactoring failed: {e}")
                matched = results[0] if results else None
                if not matched:
                    raise HTTPException(status_code=502, detail="LLM returned no refactored code for this function")

                refactor_code = matched.get("refactored_code") or original_code
                static_breaking = ast_analyzer.detect_breaking_changes(func, refactor_code, is_js=is_js)
                merged_changes = list(static_breaking)
                for lc in matched.get("breaking_changes", []):
                    if not any(sc.get("change") == lc.get("change") for sc in static_breaking):
                        merged_changes.append(lc)
                was_cached = False
                cache.set_operation_cached(
                    "refactor", original_code, config.PROMPT_VERSION_REFACTOR, config.MODEL,
                    {"refactored_code": refactor_code, "breaking_changes": merged_changes}, func_id,
                )

        tests_verified = None
        current = get_job(job_id) or job
        test_entry = next((t for t in (current.get("tests") or []) if t.get("name") == function_name), None)
        if test_entry and test_entry.get("test_code"):
            try:
                verified = coverage_runner.run_tests_against_refactor(
                    test_entry.get("test_code", ""), refactor_code,
                    is_js=is_js, func_name_filter=func.get("name", "")
                )
                tests_verified = verified.get("passed", False)
            except Exception as e:
                logger.warning(f"Refactor verification failed for {function_name}: {e}")

        entry = {
            "name": function_name,
            "filename": filename,
            "original_code": original_code,
            "refactored_code": refactor_code,
            "breaking_changes": merged_changes,
            "tests_verified": tests_verified,
            "cached": was_cached,
        }

    async with get_generation_lock(job_id):
        job = get_job(job_id) or job
        refactor_list = [r for r in (job.get("refactor") or []) if r.get("name") != function_name]
        refactor_list.append(entry)
        summary = dict(job.get("summary") or {})
        summary["breaking_changes"] = sum(len(r.get("breaking_changes") or []) for r in refactor_list)
        update_job(job_id, {"refactor": refactor_list, "summary": summary}, persist=False)
        job_store.save_artifact(job_id, "refactors", func_id, entry)
    return entry


@app.get("/demo")
async def run_demo(background_tasks: BackgroundTasks):
    """Run analysis on the demo sample file."""
    demo_job = get_job("demo")
    if (
        demo_job
        and demo_job.get("status") == "complete"
        and demo_job.get("pipeline") == PIPELINE_VERSION
        and demo_job.get("structural_ready")
        and demo_job.get("graph")
    ):
        return demo_job

    demo_file = Path(__file__).parent.parent / "demo" / "sample_legacy.py"
    if not demo_file.exists():
        raise HTTPException(status_code=404, detail="Demo file not found")

    job_id = "demo"
    update_job(job_id, {"status": "processing", "progress": "Running demo analysis..."}, upsert=True)

    extract_dir = os.path.join(tempfile.gettempdir(), "oracle_demo")
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)
    
    shutil.copy2(str(demo_file), os.path.join(extract_dir, "sample_legacy.py"))
    
    _evict_stale_jobs("demo")
    background_tasks.add_task(run_analysis, "demo", extract_dir)
    
    return {"job_id": "demo", "status": "processing"}


@app.get("/demo/file")
async def get_demo_file():
    """Return the raw demo Python file content."""
    demo_file = Path(__file__).parent.parent / "demo" / "sample_legacy.py"
    if not demo_file.exists():
        raise HTTPException(status_code=404, detail="Demo file not found")
    
    content = demo_file.read_text(encoding="utf-8")
    return {"filename": "sample_legacy.py", "content": content}


@app.get("/cache/stats")
async def get_cache_stats():
    """Get cache statistics."""
    return cache.get_cache_stats()


@app.post("/cache/clear")
async def clear_cache():
    """Clear all cached data."""
    count = cache.clear_cache()
    return {"cleared": count}


@app.get("/benchmark")
async def run_benchmark():
    """Run benchmark on demo file."""
    import time
    
    demo_file = Path(__file__).parent.parent / "demo" / "sample_legacy.py"
    if not demo_file.exists():
        raise HTTPException(status_code=404, detail="Demo file not found")
    
    start_time = time.time()
    
    analysis = ast_analyzer.analyze_file(str(demo_file))
    graph_res = dependency_analyzer.build_dependency_graph([analysis.to_dict()])
    
    elapsed = time.time() - start_time
    
    return {
        "file": demo_file.name,
        "lines": analysis.line_count,
        "functions": len(analysis.functions),
        "classes": len(analysis.classes),
        "ast_nodes": analysis.ast_nodes,
        "graph_nodes": len(graph_res.nodes),
        "graph_edges": len(graph_res.edges),
        "analysis_time": round(elapsed, 2)
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
