"""
Filesystem-backed persistent job store.

Jobs survive a normal backend reload because every job is materialised as a
directory tree under backend/cache/jobs/<job_id>/:

    metadata.json          job status, summary, progress, timings
    functions.json         normalized function registry
    graph.json             dependency graph
    explanations/<id>.json one file per analyzed function
    tests/<key>.json       on-demand test results (content keyed)
    refactors/<key>.json   on-demand refactor results (content keyed)

Writes are atomic (temp file + os.replace) and every read is defensive so a
corrupted file degrades to a cache miss instead of crashing the server.
"""
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("codeoracle.jobstore")

# Storage root. Defaults to the app directory for local development; a hosted
# deployment sets CODEORACLE_DATA_DIR to a writable mounted volume because the
# application directory is read-only on most PaaS platforms.
# May be imported before config.py, so load .env here as well; a process env
# var still wins because load_dotenv does not override existing values.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except Exception:  # pragma: no cover - dotenv ships with the app
    pass

_data_dir = os.environ.get("CODEORACLE_DATA_DIR", "").strip()
_ROOT = (Path(_data_dir) / "cache" / "jobs") if _data_dir else (Path(__file__).parent / "cache" / "jobs")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe(component: str) -> str:
    """Sanitize a single path component so job ids / keys cannot escape root."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(component or ""))
    return cleaned[:180] or "_"


def job_dir(job_id: str) -> Path:
    return _ROOT / _safe(job_id)


def _subdir(job_id: str, kind: str) -> Path:
    path = job_dir(job_id) / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, data: Any) -> bool:
    """Write JSON atomically; returns False instead of raising on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            json.dump(data, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as exc:  # noqa: BLE001 - never let cache IO crash the app
        logger.debug(f"Atomic write failed for {path}: {exc}")
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001 - corrupt file -> treat as missing
        logger.warning(f"Ignoring corrupt job-store file {path}: {exc}")
        return default


# --------------------------------------------------------------------------
# Core job documents
# --------------------------------------------------------------------------
def save_metadata(job_id: str, metadata: Dict[str, Any]) -> bool:
    return _atomic_write_json(job_dir(job_id) / "metadata.json", metadata)


def load_metadata(job_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(job_dir(job_id) / "metadata.json")


def save_functions(job_id: str, functions: List[Dict[str, Any]]) -> bool:
    return _atomic_write_json(job_dir(job_id) / "functions.json", functions)


def load_functions(job_id: str) -> List[Dict[str, Any]]:
    return _read_json(job_dir(job_id) / "functions.json", []) or []


def save_graph(job_id: str, graph: Dict[str, Any]) -> bool:
    return _atomic_write_json(job_dir(job_id) / "graph.json", graph)


def load_graph(job_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(job_dir(job_id) / "graph.json")


# --------------------------------------------------------------------------
# Per-function / per-key artifacts
# --------------------------------------------------------------------------
def save_explanation(job_id: str, function_id: str, explanation: Dict[str, Any]) -> bool:
    return _atomic_write_json(_subdir(job_id, "explanations") / f"{_safe(function_id)}.json", explanation)


def load_explanations(job_id: str) -> Dict[str, Dict[str, Any]]:
    directory = job_dir(job_id) / "explanations"
    results: Dict[str, Dict[str, Any]] = {}
    if not directory.exists():
        return results
    for path in directory.glob("*.json"):
        payload = _read_json(path)
        if isinstance(payload, dict):
            fid = payload.get("function_id") or path.stem
            results[str(fid)] = payload
    return results


def save_artifact(job_id: str, kind: str, key: str, payload: Dict[str, Any]) -> bool:
    return _atomic_write_json(_subdir(job_id, kind) / f"{_safe(key)}.json", payload)


def load_artifact(job_id: str, kind: str, key: str) -> Optional[Dict[str, Any]]:
    return _read_json(job_dir(job_id) / kind / f"{_safe(key)}.json")


def load_artifacts(job_id: str, kind: str) -> List[Dict[str, Any]]:
    directory = job_dir(job_id) / kind
    if not directory.exists():
        return []
    items = []
    for path in directory.glob("*.json"):
        payload = _read_json(path)
        if isinstance(payload, dict):
            items.append(payload)
    return items


def delete_artifact(job_id: str, kind: str, key: str) -> bool:
    """Remove one stored artifact. Returns False when it did not exist."""
    path = job_dir(job_id) / kind / f"{_safe(key)}.json"
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:  # noqa: BLE001 - deletion is best-effort
        logger.warning(f"Failed to delete job-store artifact {path}: {exc}")
        return False


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------
def delete_job(job_id: str) -> bool:
    directory = job_dir(job_id)
    if not directory.exists():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return True


def list_job_ids() -> List[str]:
    if not _ROOT.exists():
        return []
    return [p.name for p in _ROOT.iterdir() if p.is_dir()]


def cleanup_stale_jobs(max_age_hours: int = 24 * 7) -> int:
    """Delete job directories older than max_age_hours. Returns count removed."""
    if not _ROOT.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for path in _ROOT.iterdir():
        if not path.is_dir():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
