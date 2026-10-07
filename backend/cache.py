"""
Smart caching system.

Two layers:
  * File/AST cache  - keyed by file content hash (``get_cached``/``set_cached``).
  * Operation cache - content based, keyed by
        SHA256(source_code + operation + prompt_version + model [+ function_id])
    used for per-function explanations, generated tests and refactors so a
    change to one function only invalidates that function's cached result.

Results persist to ``backend/.cache`` as JSON and are also kept in a bounded
in-memory LRU so repeated reads stay fast.
"""
import hashlib
import json
import logging
import os
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("codeoracle")

_cache_dir = Path(__file__).parent / ".cache"
_cache_dir.mkdir(exist_ok=True)

_MEMORY_CACHE_MAX = int(os.environ.get("CODEORACLE_CACHE_MEMORY_MAX", "2000"))
_memory_cache: "OrderedDict[str, Any]" = OrderedDict()


def _remember(key: str, value: Any) -> None:
    _memory_cache[key] = value
    _memory_cache.move_to_end(key)
    while len(_memory_cache) > _MEMORY_CACHE_MAX:
        _memory_cache.popitem(last=False)


def _plain_key(content: str, analysis_type: str, model: str = "", prompt_version: str = "v1") -> str:
    key_data = f"{content}:{analysis_type}:{model}:{prompt_version}"
    return hashlib.sha256(key_data.encode("utf-8", "replace")).hexdigest()


def source_hash(source_code: str) -> str:
    return hashlib.sha256((source_code or "").encode("utf-8", "replace")).hexdigest()


def operation_cache_key(
    source_code: str,
    operation: str,
    prompt_version: str,
    model: str,
    function_id: str = "",
) -> str:
    """Content-addressed key: identical source+operation+prompt+model hits."""
    payload = f"{operation}:{prompt_version}:{model}:{function_id}:{source_hash(source_code)}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------
# Generic (file/AST) cache - unchanged public contract
# --------------------------------------------------------------------------
def get_cached(analysis_type: str, content: str, model: str = "") -> Optional[Any]:
    key = _plain_key(content, analysis_type, model)
    if key in _memory_cache:
        _memory_cache.move_to_end(key)
        return _memory_cache[key]

    cache_file = _cache_dir / f"{key}.json"
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            _remember(key, data)
            return data
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Ignoring corrupt cache entry {cache_file}: {exc}")

    return None


def set_cached(analysis_type: str, content: str, result: Any, model: str = "") -> None:
    key = _plain_key(content, analysis_type, model)
    _remember(key, result)
    _write_disk(key, result)


def _write_disk(key: str, result: Any) -> None:
    cache_file = _cache_dir / f"{key}.json"
    fd = None
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(_cache_dir))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp_path, cache_file)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to write cache: {exc}")
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


# --------------------------------------------------------------------------
# Operation cache (content based, per function)
# --------------------------------------------------------------------------
def get_operation_cached(
    operation: str,
    source_code: str,
    prompt_version: str,
    model: str,
    function_id: str = "",
) -> Optional[Any]:
    key = operation_cache_key(source_code, operation, prompt_version, model, function_id)
    if key in _memory_cache:
        _memory_cache.move_to_end(key)
        return _memory_cache[key]
    cache_file = _cache_dir / "ops" / f"{operation}-{key}.json"
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            _remember(key, data)
            return data
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Ignoring corrupt operation cache {cache_file}: {exc}")
    return None


def set_operation_cached(
    operation: str,
    source_code: str,
    prompt_version: str,
    model: str,
    result: Any,
    function_id: str = "",
) -> None:
    key = operation_cache_key(source_code, operation, prompt_version, model, function_id)
    _remember(key, result)
    ops_dir = _cache_dir / "ops"
    ops_dir.mkdir(exist_ok=True)
    cache_file = ops_dir / f"{operation}-{key}.json"
    fd = None
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(ops_dir))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp_path, cache_file)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to write operation cache: {exc}")
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


def get_file_hash(filepath: str) -> str:
    """Get SHA-256 hash of file content."""
    try:
        with open(filepath, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return ""


def invalidate_operation(operation: str, source_code: str, prompt_version: str, model: str, function_id: str = "") -> bool:
    """Remove a single operation cache entry. Returns True if a file was removed."""
    key = operation_cache_key(source_code, operation, prompt_version, model, function_id)
    _memory_cache.pop(key, None)
    cache_file = _cache_dir / "ops" / f"{operation}-{key}.json"
    if cache_file.exists():
        try:
            cache_file.unlink()
            return True
        except OSError:
            return False
    return False


def clear_cache() -> int:
    """Clear all cached files. Returns number of files removed."""
    count = 0
    for cache_file in _cache_dir.rglob("*.json"):
        try:
            cache_file.unlink()
            count += 1
        except Exception:
            pass
    _memory_cache.clear()
    return count


def get_cache_stats() -> dict:
    """Get cache statistics."""
    files = list(_cache_dir.glob("*.json"))
    ops_files = list((_cache_dir / "ops").glob("*.json")) if (_cache_dir / "ops").exists() else []
    return {
        "memory_entries": len(_memory_cache),
        "disk_entries": len(files),
        "operation_entries": len(ops_files),
        "cache_dir": str(_cache_dir),
    }
