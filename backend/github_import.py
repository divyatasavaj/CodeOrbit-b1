"""Import a PUBLIC GitHub repository so it can be analyzed by the exact same
pipeline that handles uploaded ZIP files.

    GitHub URL -> validate -> fetch archive -> safe extract -> existing pipeline

Only github.com is ever contacted. ``owner``/``repo``/``ref`` are validated
against a strict charset and the archive URL is *built from those parts* (never
from the raw user string), so the endpoint can never be turned into a generic
"download any URL" / SSRF primitive.

No GitHub credentials are required or requested. An optional ``GITHUB_TOKEN``
environment variable is honoured server-side only - never logged, echoed, or
stored - so private repositories can be enabled later without changing this
interface. The archive is streamed to disk under the same size limits as a
manual upload; nothing is buffered whole in memory.
"""
import hashlib
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

import config

logger = logging.getLogger("codeoracle.github")

GITHUB_API = "https://api.github.com"
GITHUB_CODELOAD = "https://codeload.github.com"
_USER_AGENT = "CodeOracle/2.0"

# Hosts we may legitimately reach (including after redirects).
_ALLOWED_HOSTS = {
    "github.com",
    "www.github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
}
_GITHUB_HOSTS = {"github.com", "www.github.com"}

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}$")
_BRANCH_SEGMENTS = {"tree", "blob"}

# Stable, machine-readable error codes -> the frontend maps these to copy.
ERR_INVALID_URL = "invalid_github_url"
ERR_NOT_FOUND = "github_not_found"
ERR_PRIVATE = "github_private"
ERR_RATE_LIMITED = "github_rate_limited"
ERR_UNAVAILABLE = "github_unavailable"
ERR_DOWNLOAD_FAILED = "github_download_failed"
ERR_TOO_LARGE = "github_too_large"
ERR_EMPTY = "github_empty"

INVALID_URL_MESSAGE = "Please enter a valid GitHub repository URL."


class GithubImportError(Exception):
    """User-facing GitHub import failure carrying a stable ``code``."""

    def __init__(self, message: str, code: str = ERR_DOWNLOAD_FAILED):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# URL parsing / validation
# --------------------------------------------------------------------------
def parse_github_url(raw_url: str) -> Tuple[str, str, Optional[str]]:
    """Normalize a GitHub repository URL into ``(owner, repo, ref)``.

    Accepts ``https://github.com/owner/repo``, an optional trailing slash, a
    ``.git`` suffix, a missing scheme, and an optional ``/tree/<ref>`` selector.
    Every other form is rejected.
    """
    if not raw_url or not str(raw_url).strip():
        raise GithubImportError(INVALID_URL_MESSAGE, ERR_INVALID_URL)

    value = str(raw_url).strip()
    if "://" in value:
        scheme, rest = value.split("://", 1)
        if scheme.lower() not in ("http", "https"):
            raise GithubImportError(INVALID_URL_MESSAGE, ERR_INVALID_URL)
    else:
        rest = value

    # Strip credentials, query string and fragment before splitting the path.
    rest = rest.split("#", 1)[0].split("?", 1)[0]
    rest = rest.split("@", 1)[-1]
    parts = [p for p in rest.split("/") if p]
    if len(parts) < 3:
        raise GithubImportError(INVALID_URL_MESSAGE, ERR_INVALID_URL)

    if parts[0].lower() not in _GITHUB_HOSTS:
        raise GithubImportError(INVALID_URL_MESSAGE, ERR_INVALID_URL)

    owner, repo = parts[1], parts[2]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]

    ref: Optional[str] = None
    if len(parts) > 3:
        # Only /tree/<ref> (or /blob/<ref>) is meaningful; /issues, /pull, ...
        # are not repository URLs.
        if parts[3] not in _BRANCH_SEGMENTS or len(parts) < 5:
            raise GithubImportError(INVALID_URL_MESSAGE, ERR_INVALID_URL)
        ref = "/".join(parts[4:])

    if not _NAME_RE.match(owner) or not _NAME_RE.match(repo):
        raise GithubImportError(INVALID_URL_MESSAGE, ERR_INVALID_URL)
    if ref is not None and not _REF_RE.match(ref):
        raise GithubImportError(INVALID_URL_MESSAGE, ERR_INVALID_URL)

    return owner, repo, ref


def _headers() -> Dict[str, str]:
    """Request headers; the token (if any) is read from the environment only."""
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"}
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(connect=10.0, read=config.GITHUB_TIMEOUT, write=30.0, pool=10.0),
        follow_redirects=True,
    )


def _as_error(exc: httpx.HTTPError, action: str) -> GithubImportError:
    logger.warning("[GITHUB] %s failed: %s", action, exc)
    return GithubImportError("Unable to reach GitHub right now. Please try again later.", ERR_UNAVAILABLE)


# --------------------------------------------------------------------------
# Repository metadata (single request, keeps GitHub API usage minimal)
# --------------------------------------------------------------------------
def fetch_repository(owner: str, repo: str) -> Dict[str, Any]:
    """Resolve a public repository's default branch and basic size."""
    try:
        with _client() as client:
            response = client.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=_headers())
    except httpx.HTTPError as exc:
        raise _as_error(exc, "metadata request")

    if response.status_code == 404:
        raise GithubImportError(
            f"Repository '{owner}/{repo}' not found.", ERR_NOT_FOUND
        )
    if response.status_code in (401, 403, 429):
        raise GithubImportError(
            "GitHub request limit reached. Please try again later.", ERR_RATE_LIMITED
        )
    if response.status_code >= 500:
        raise GithubImportError("Unable to reach GitHub right now. Please try again later.", ERR_UNAVAILABLE)
    if response.status_code != 200:
        raise GithubImportError(
            f"GitHub returned an unexpected status ({response.status_code}).", ERR_DOWNLOAD_FAILED
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise GithubImportError("GitHub returned a malformed response.", ERR_DOWNLOAD_FAILED) from exc

    if data.get("private"):
        raise GithubImportError(
            "This repository is private. CodeOracle currently supports public GitHub repositories only.",
            ERR_PRIVATE,
        )

    size_kb = data.get("size") or 0
    if size_kb and size_kb * 1024 > config.MAX_ZIP_SIZE_BYTES:
        raise GithubImportError(
            f"Repository is too large ({size_kb // 1024}MB; limit {config.MAX_ZIP_SIZE_MB}MB).",
            ERR_TOO_LARGE,
        )

    return {
        "full_name": data.get("full_name") or f"{owner}/{repo}",
        "default_branch": data.get("default_branch") or "main",
        "size_kb": size_kb,
    }


# --------------------------------------------------------------------------
# Archive download
# --------------------------------------------------------------------------
def _archive_urls(owner: str, repo: str, ref: Optional[str], default_branch: str) -> List[str]:
    """Candidate codeload URLs, most specific first (no API call involved)."""
    base = f"{GITHUB_CODELOAD}/{owner}/{repo}/zip"
    if ref:
        return [f"{base}/refs/heads/{ref}", f"{base}/{ref}"]
    return [f"{base}/refs/heads/{default_branch}"]


def download_repository_zip(owner: str, repo: str, ref: Optional[str], dest_path: str) -> Dict[str, Any]:
    """Stream the repository source archive into ``dest_path`` (a ZIP file)."""
    meta = fetch_repository(owner, repo)
    candidates = _archive_urls(owner, repo, ref, meta["default_branch"])

    last_error: Optional[GithubImportError] = None
    for candidate in candidates:
        try:
            written = _stream_to_file(candidate, dest_path)
        except GithubImportError as exc:
            last_error = exc
            continue
        meta.update({
            "archive_url": candidate,
            "ref": ref or meta["default_branch"],
            "downloaded_bytes": written,
        })
        logger.info("[GITHUB] repo=%s ref=%s bytes=%s", meta["full_name"], meta["ref"], written)
        return meta

    raise last_error or GithubImportError(
        "Unable to download this repository. Please try again.", ERR_DOWNLOAD_FAILED
    )


def _stream_to_file(url: str, dest_path: str) -> int:
    written = 0
    limit = config.MAX_ZIP_SIZE_BYTES
    try:
        with _client() as client:
            with client.stream("GET", url, headers=_headers()) as response:
                if response.status_code == 404:
                    raise GithubImportError(
                        "Repository or branch not found.", ERR_NOT_FOUND
                    )
                if response.status_code in (401, 403, 429):
                    raise GithubImportError(
                        "GitHub request limit reached. Please try again later.", ERR_RATE_LIMITED
                    )
                if response.status_code >= 500:
                    raise GithubImportError(
                        "Unable to reach GitHub right now. Please try again later.", ERR_UNAVAILABLE
                    )
                if response.status_code >= 400:
                    raise GithubImportError(
                        f"GitHub returned an unexpected status ({response.status_code}).",
                        ERR_DOWNLOAD_FAILED,
                    )

                if response.url.host not in _ALLOWED_HOSTS:
                    logger.warning("[GITHUB] refused unexpected archive host: %s", response.url.host)
                    raise GithubImportError(
                        "Unable to download this repository. Please try again.", ERR_DOWNLOAD_FAILED
                    )

                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise GithubImportError(
                        f"Repository is too large ({int(declared) // (1024 * 1024)}MB; limit {config.MAX_ZIP_SIZE_MB}MB).",
                        ERR_TOO_LARGE,
                    )

                with open(dest_path, "wb") as handle:
                    for chunk in response.iter_bytes(256 * 1024):
                        written += len(chunk)
                        if written > limit:
                            raise GithubImportError(
                                f"Repository is too large (limit {config.MAX_ZIP_SIZE_MB}MB).",
                                ERR_TOO_LARGE,
                            )
                        handle.write(chunk)
    except httpx.HTTPError as exc:
        raise _as_error(exc, "archive download")

    if written == 0:
        raise GithubImportError("GitHub returned an empty archive.", ERR_EMPTY)
    return written


# --------------------------------------------------------------------------
# Revision identity
# --------------------------------------------------------------------------
def compute_source_digest(extract_dir: str) -> str:
    """Deterministic digest of the analyzed sources (content, not commit metadata).

    GitHub's zip endpoint does not expose the commit SHA without an extra API
    request, and this project deliberately keeps GitHub API usage minimal. A
    sorted digest of (relative path, file content) is revision-accurate for
    cache identity: identical sources hash identically, any source change
    changes the digest. Per-function content hashing in ``cache``/``ai_pipeline``
    independently guarantees correct per-result reuse.
    """
    digest = hashlib.sha256()
    try:
        files, _ = repo_ingest.find_source_files(extract_dir)
    except Exception:  # noqa: BLE001 - identity must never break the import
        return ""
    for path in sorted(files):
        try:
            with open(path, "rb") as handle:
                content_hash = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            continue
        digest.update(os.path.relpath(path, extract_dir).replace("\\", "/").encode("utf-8", "replace"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


import repo_ingest  # noqa: E402  (kept last: repo_ingest imports config only)
