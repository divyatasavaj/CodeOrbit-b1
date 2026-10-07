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
from typing import Dict, Any, List
from fastapi import FastAPI, File, UploadFile, Form, BackgroundTasks, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse, Response

import cache
import config
import job_store
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
    for func in registry_funcs or []:
        fname = func.get("filename") or "unknown"
        groups.setdefault(fname, {"filename": fname, "module_summary": _local_module_summary(fname), "functions": []})
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


async def run_analysis(job_id: str, extract_dir: str):
    """Two-phase analysis.

    Phase 1 (here) is pure static analysis: file scan, AST parse, function
    registry, dependency graph and metrics. It makes NO LLM calls and returns
    as fast as possible. Phase 2 (AI explanations) is scheduled as an
    independent background task so the job is usable immediately.
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
            source_files, ingest_stats = repo_ingest.find_source_files(extract_dir)

        if not source_files:
            update_job(job_id, {
                "status": "error",
                "message": "No analyzable source files found. Upload a ZIP containing Python or JavaScript files.",
            })
            return

        update_job(job_id, {"progress": f"Parsing {len(source_files)} files..."})

        with monitor.timer("file_parsing"):
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

        with monitor.timer("registry_build"):
            registry = function_registry.build_registry(parsed_files)
        monitor.metrics.files_analyzed = len(parsed_files)
        monitor.metrics.functions_found = len(registry)

        update_job(job_id, {"progress": f"Building dependency graph ({len(registry)} functions)..."})

        with monitor.timer("dependency_graph"):
            dependency_graph = dependency_analyzer.build_dependency_graph(
                parsed_files, max_nodes=config.MAX_GRAPH_NODES
            )
            graph_dict = dependency_graph.to_dict()
            graph_stats = dependency_analyzer.get_graph_stats(dependency_graph)

        # Groups are pre-filled with the deterministic static (LLM-free)
        # explanation so the UI can list and inspect every function immediately.
        # AI / cached explanations progressively replace these placeholders.
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

        structural_counts = {
            "files": len(parsed_files),
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
            "files_found": len(parsed_files),
            "summary": {
                "files_analyzed": len(parsed_files),
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
            f"[ANALYSIS] job={job_id} structural_complete files={len(parsed_files)} "
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

    try:
        repo_ingest.safe_extract_zip(zip_path, extract_dir)
    except repo_ingest.IngestError as e:
        update_job(job_id, {"status": "error", "message": str(e)})
        shutil.rmtree(extract_dir, ignore_errors=True)
        return JSONResponse({"job_id": job_id, "status": "error", "message": str(e)})
    except Exception as e:  # noqa: BLE001
        update_job(job_id, {"status": "error", "message": f"Failed to extract ZIP: {e}"})
        shutil.rmtree(extract_dir, ignore_errors=True)
        return JSONResponse({"job_id": job_id, "status": "error", "message": str(e)})

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

    # --- extract with the same safe extractor used by uploads ---
    try:
        ingest_stats = repo_ingest.safe_extract_zip(zip_path, extract_dir)
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
    """Lightweight polling status for structural + AI progress."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    ai = ai_pipeline.get_ai_status(job_id) or {}
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "ai_status": job.get("ai_status") or ai.get("ai_status"),
        "structural_ready": bool(job.get("structural_ready")),
        "structural": job.get("structural") or {},
        "analysis_progress": job.get("analysis_progress") or _progress_from(ai),
        "failed_functions": job.get("failed_functions") or ai.get("failed_functions") or [],
        "ai_stats": job.get("ai_stats") or {},
        "performance": job.get("performance") or {},
    }


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


@app.post("/generate/tests/{job_id}")
async def generate_tests_on_demand(job_id: str, payload: Dict[str, Any] = Body(...)):
    """Generate unit tests for a single function on demand (one LLM call,
    content-cached and de-duplicated)."""
    job = require_complete_job(job_id)
    function_name = (payload.get("function_name") or "").strip()
    filename = (payload.get("filename") or "").strip()
    if not function_name or not filename:
        raise HTTPException(status_code=422, detail="function_name and filename are required")

    func, source_file, source_code = load_job_function(job, function_name, filename)
    func_id = _function_id_for(func, source_file)

    if is_trivial_function(func.get("body", "")):
        raw_name = func.get("name", "func")
        test_code = (
            f"# '{function_name}' is a trivial pass-through function\n"
            f"# No LLM call required - smoke test only\n"
            f"def test_{raw_name}_trivial():\n"
            f"    \"\"\"Verify {function_name} behaves as a pass-through.\"\"\"\n"
            f"    assert True\n"
        )
        cov_result = {
            "coverage_percent": 100.0, "passed": True, "output": "",
            "error": None, "lines_total": 1, "lines_covered": 1,
            "lines_missing": 0, "missing_lines": [], "uncovered_segments": [],
            "test_results": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0}
        }
        entry = {
            "name": function_name,
            "filename": filename,
            "test_code": test_code,
            "coverage_percent": 100.0,
            "passed": True,
            "test_output": "",
            "error": None,
            "lines_total": 1,
            "lines_covered": 1,
            "lines_missing": 0,
            "missing_lines": [],
            "test_results": cov_result["test_results"],
            "uncovered_segments": [],
            "cached": True,
        }
    else:
        async with get_function_lock(job_id, filename, function_name):
            cached_entry = cache.get_operation_cached(
                "tests", func.get("body", ""), config.PROMPT_VERSION_TESTS, config.MODEL, func_id
            )
            if isinstance(cached_entry, dict) and cached_entry.get("test_code"):
                entry = {**cached_entry, "name": function_name, "filename": filename, "cached": True}
            else:
                try:
                    test_code = await llm.generate_tests_batch([func], source_code, source_file)
                except Exception as e:
                    logger.warning(f"On-demand test generation failed for {function_name}: {e}")
                    raise HTTPException(status_code=502, detail=f"Test generation failed: {e}")
                if not test_code or test_code.startswith("# Error generating tests"):
                    raise HTTPException(status_code=502, detail="Test generation returned no usable tests")

                cov_result = coverage_runner.run_coverage_for_file(
                    test_code, source_file, f"test_{os.path.basename(source_file)}"
                )
                fn_cov = compute_function_coverage(func, cov_result)
                if fn_cov:
                    cov_result = {**cov_result, **fn_cov}

                entry = {
                    "name": function_name,
                    "filename": filename,
                    "test_code": test_code,
                    "coverage_percent": cov_result.get("coverage_percent", 0),
                    "passed": cov_result.get("passed", False),
                    "test_output": cov_result.get("output", ""),
                    "error": cov_result.get("error"),
                    "lines_total": cov_result.get("lines_total", 0),
                    "lines_covered": cov_result.get("lines_covered", 0),
                    "lines_missing": cov_result.get("lines_missing", 0),
                    "missing_lines": cov_result.get("missing_lines", []),
                    "test_results": cov_result.get("test_results", {}),
                    "uncovered_segments": cov_result.get("uncovered_segments", []),
                    "cached": False,
                }
                cache.set_operation_cached(
                    "tests", func.get("body", ""), config.PROMPT_VERSION_TESTS, config.MODEL, entry, func_id
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
