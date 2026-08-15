"""
Smart caching system with SHA-256 content hashing.
Caches file parsing, AST results, LLM responses, and more.
"""
import os
import json
import hashlib
import logging
from typing import Any, Optional
from pathlib import Path

logger = logging.getLogger("codeoracle")

_cache_dir = Path(__file__).parent / ".cache"
_cache_dir.mkdir(exist_ok=True)

_memory_cache = {}


def _get_cache_key(content: str, analysis_type: str, model: str = "", prompt_version: str = "v1") -> str:
    """Generate SHA-256 cache key from content and metadata."""
    key_data = f"{content}:{analysis_type}:{model}:{prompt_version}"
    return hashlib.sha256(key_data.encode()).hexdigest()


def get_cached(analysis_type: str, content: str, model: str = "") -> Optional[Any]:
    """Retrieve cached result if available."""
    cache_key = _get_cache_key(content, analysis_type, model)
    
    if cache_key in _memory_cache:
        return _memory_cache[cache_key]
    
    cache_file = _cache_dir / f"{cache_key}.json"
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _memory_cache[cache_key] = data
            return data
        except Exception:
            pass
    
    return None


def set_cached(analysis_type: str, content: str, result: Any, model: str = "") -> None:
    """Store result in cache."""
    cache_key = _get_cache_key(content, analysis_type, model)
    
    _memory_cache[cache_key] = result
    
    cache_file = _cache_dir / f"{cache_key}.json"
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to write cache: {e}")


def get_file_hash(filepath: str) -> str:
    """Get SHA-256 hash of file content."""
    try:
        with open(filepath, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return ""


def clear_cache() -> int:
    """Clear all cached files. Returns number of files removed."""
    count = 0
    for cache_file in _cache_dir.glob("*.json"):
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
    return {
        "memory_entries": len(_memory_cache),
        "disk_entries": len(files),
        "cache_dir": str(_cache_dir)
    }
