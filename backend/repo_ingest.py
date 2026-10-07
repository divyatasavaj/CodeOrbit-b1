"""
Safe repository ingestion: ZIP extraction with hard limits, ZIP-slip
protection, ignored-directory pruning, binary detection and source discovery.

Kept dependency-free (stdlib only) and deliberately defensive: a hostile or
broken archive degrades to "nothing to analyze" rather than an OOM or a
path-traversal write.
"""
import logging
import os
import zipfile
from typing import Any, Dict, List, Tuple

import config

logger = logging.getLogger("codeoracle.ingest")


class IngestError(Exception):
    """Raised for user-facing ingestion problems (bad zip, too large, empty)."""


def _is_skipped_dir(name: str) -> bool:
    if name in config.SKIP_DIRS:
        return True
    return any(name.endswith(suffix) for suffix in config.SKIP_DIR_SUFFIXES)


def _path_has_skipped_dir(rel_path: str) -> bool:
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    return any(_is_skipped_dir(p) for p in parts[:-1])


def is_supported_source(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext not in config.SOURCE_EXTENSIONS:
        return False
    name = os.path.basename(path)
    if name in config.SKIP_FILE_NAMES:
        return False
    if name.startswith("test_") or name.startswith("."):
        return False
    if ext == ".js" and name.endswith(".min.js"):
        return False
    return True


def is_binary_file(path: str, sample_size: int = 8192) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(sample_size)
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    if not chunk:
        return False
    # Heuristic: mostly non-text bytes -> binary.
    printable = sum(
        1 for b in chunk if b in (9, 10, 13) or 32 <= b < 127 or b >= 128
    )
    return printable / len(chunk) < 0.85


def safe_extract_zip(zip_path: str, dest_dir: str) -> Dict[str, Any]:
    """Extract a ZIP safely, honouring size/file limits. Returns stats."""
    stats: Dict[str, Any] = {
        "extracted_files": 0,
        "skipped_large": 0,
        "skipped_dirs": 0,
        "extracted_bytes": 0,
        "truncated": False,
    }
    dest_root = os.path.realpath(dest_dir)

    try:
        archive = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile as exc:
        raise IngestError("Invalid ZIP file format") from exc
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"Failed to read ZIP: {exc}") from exc

    with archive:
        members = archive.infolist()
        declared_total = sum(m.file_size for m in members)
        if declared_total > config.MAX_EXTRACTED_SIZE_BYTES:
            raise IngestError(
                f"Archive expands to over {config.MAX_EXTRACTED_SIZE_MB}MB "
                f"(limit exceeded) - refusing to extract."
            )

        written_bytes = 0
        for member in members:
            rel = member.filename.replace("\\", "/").lstrip("/")
            if not rel or rel.endswith("/"):
                continue
            if _path_has_skipped_dir(rel):
                stats["skipped_dirs"] += 1
                continue

            target = os.path.realpath(os.path.join(dest_root, rel))
            if not (target == dest_root or target.startswith(dest_root + os.sep)):
                logger.warning(f"Skipping potentially unsafe ZIP entry: {member.filename}")
                continue

            if member.file_size > config.MAX_FILE_SIZE_BYTES:
                stats["skipped_large"] += 1
                continue

            if stats["extracted_files"] >= config.MAX_FILES:
                stats["truncated"] = True
                logger.warning(f"Reached MAX_FILES ({config.MAX_FILES}); stopping extraction")
                break

            if written_bytes + member.file_size > config.MAX_EXTRACTED_SIZE_BYTES:
                stats["truncated"] = True
                logger.warning("Reached MAX_EXTRACTED_SIZE during extraction; stopping")
                break

            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                with archive.open(member) as src, open(target, "wb") as out:
                    while True:
                        chunk = src.read(1024 * 256)
                        if not chunk:
                            break
                        out.write(chunk)
                        written_bytes += len(chunk)
            except OSError as exc:
                logger.warning(f"Failed to extract {member.filename}: {exc}")
                continue

            stats["extracted_files"] += 1

    stats["extracted_bytes"] = written_bytes
    return stats


def find_source_files(root_dir: str) -> Tuple[List[str], Dict[str, int]]:
    """Walk an extracted tree and return analyzable source files + skip stats."""
    source_files: List[str] = []
    stats = {"skipped_binary": 0, "skipped_large": 0, "skipped_unsupported": 0, "truncated_files": 0}

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not _is_skipped_dir(d)]
        for name in files:
            filepath = os.path.join(root, name)
            if not is_supported_source(filepath):
                stats["skipped_unsupported"] += 1
                continue
            try:
                size = os.path.getsize(filepath)
            except OSError:
                continue
            if size > config.MAX_FILE_SIZE_BYTES:
                stats["skipped_large"] += 1
                continue
            if is_binary_file(filepath):
                stats["skipped_binary"] += 1
                continue
            source_files.append(filepath)
            if len(source_files) >= config.MAX_FILES:
                stats["truncated_files"] = 1
                logger.warning(f"Reached MAX_FILES ({config.MAX_FILES}); truncating file list")
                return source_files, stats

    return source_files, stats
