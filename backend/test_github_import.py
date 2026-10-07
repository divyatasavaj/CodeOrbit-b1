"""Tests for GitHub repository import (URL validation + fetch error handling).

Fully offline: the HTTP layer is stubbed, so these run without network access
and without touching GitHub. Run with:

    python -m pytest test_github_import.py -q
"""
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import github_import as gh


# --------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("https://github.com/facebook/react", ("facebook", "react", None)),
    ("https://github.com/user/project/", ("user", "project", None)),
    ("https://github.com/user/project.git", ("user", "project", None)),
    ("http://github.com/user/project", ("user", "project", None)),
    ("github.com/user/project", ("user", "project", None)),
    ("https://www.github.com/user/project", ("user", "project", None)),
    ("https://github.com/user/project/tree/main", ("user", "project", "main")),
    ("https://github.com/user/project/tree/master/", ("user", "project", "master")),
    ("https://github.com/user/project/tree/feature/nested", ("user", "project", "feature/nested")),
    ("  https://github.com/user/project  ", ("user", "project", None)),
    ("https://github.com/divyatasavaj/CodeOrbit-b1", ("divyatasavaj", "CodeOrbit-b1", None)),
])
def test_valid_urls(url, expected):
    assert gh.parse_github_url(url) == expected


@pytest.mark.parametrize("url", [
    "",
    "   ",
    "not-a-url",
    "https://github.com/",
    "https://github.com/user",
    "https://google.com",
    "https://example.com/user/repository",
    "https://gitlab.com/user/repository",
    "javascript:alert(1)",
    "https://github.com/user/repository/issues",
    "https://github.com/user/repository/pull/12",
    "ftp://github.com/user/repository",
    "file:///etc/passwd",
])
def test_invalid_urls(url):
    with pytest.raises(gh.GithubImportError) as exc:
        gh.parse_github_url(url)
    assert exc.value.code == gh.ERR_INVALID_URL
    assert exc.value.message == gh.INVALID_URL_MESSAGE


# --------------------------------------------------------------------------
# Download / fetch failure handling (stubbed HTTP)
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, chunks=(), url="https://codeload.github.com/u/r/zip/x"):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self._chunks = chunks
        self.url = httpx.URL(url)

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_bytes(self, size=1024):
        for chunk in self._chunks:
            yield chunk


class _CM:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *exc):
        return False


class FakeClient:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        return self.response

    def stream(self, method, url, headers=None):
        return _CM(self.response)


def _patch(monkeypatch, response):
    monkeypatch.setattr(gh, "_client", lambda: FakeClient(response))


def test_repository_not_found(monkeypatch):
    _patch(monkeypatch, FakeResponse(status_code=404))
    with pytest.raises(gh.GithubImportError) as exc:
        gh.fetch_repository("user", "missing")
    assert exc.value.code == gh.ERR_NOT_FOUND


def test_private_repository(monkeypatch):
    _patch(monkeypatch, FakeResponse(status_code=200, json_data={
        "full_name": "user/secret", "private": True, "default_branch": "main", "size": 10,
    }))
    with pytest.raises(gh.GithubImportError) as exc:
        gh.fetch_repository("user", "secret")
    assert exc.value.code == gh.ERR_PRIVATE
    assert "private" in exc.value.message.lower()


def test_rate_limited(monkeypatch):
    _patch(monkeypatch, FakeResponse(status_code=429))
    with pytest.raises(gh.GithubImportError) as exc:
        gh.fetch_repository("user", "repo")
    assert exc.value.code == gh.ERR_RATE_LIMITED


def test_github_5xx(monkeypatch):
    _patch(monkeypatch, FakeResponse(status_code=503))
    with pytest.raises(gh.GithubImportError) as exc:
        gh.fetch_repository("user", "repo")
    assert exc.value.code == gh.ERR_UNAVAILABLE


def test_timeout_is_reported_as_unavailable(monkeypatch):
    def boom():
        raise httpx.ConnectTimeout("timed out")

    class TimeoutClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(gh, "_client", lambda: TimeoutClient())
    with pytest.raises(gh.GithubImportError) as exc:
        gh.fetch_repository("user", "repo")
    assert exc.value.code == gh.ERR_UNAVAILABLE


def test_repository_too_large_from_metadata(monkeypatch, tmp_path):
    huge_kb = (config.MAX_ZIP_SIZE_BYTES // 1024) + 1
    _patch(monkeypatch, FakeResponse(status_code=200, json_data={
        "full_name": "user/big", "private": False, "default_branch": "main", "size": huge_kb,
    }))
    with pytest.raises(gh.GithubImportError) as exc:
        gh.fetch_repository("user", "big")
    assert exc.value.code == gh.ERR_TOO_LARGE


def test_repository_too_large_from_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "fetch_repository", lambda o, r: {"full_name": "u/r", "default_branch": "main"})
    _patch(monkeypatch, FakeResponse(
        status_code=200,
        headers={"content-length": str(config.MAX_ZIP_SIZE_BYTES + 10)},
    ))
    dest = str(tmp_path / "r.zip")
    with pytest.raises(gh.GithubImportError) as exc:
        gh.download_repository_zip("u", "r", None, dest)
    assert exc.value.code == gh.ERR_TOO_LARGE
    assert not os.path.exists(dest)


def test_repository_too_large_while_streaming(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "fetch_repository", lambda o, r: {"full_name": "u/r", "default_branch": "main"})
    chunk = b"x" * (1024 * 1024)
    _patch(monkeypatch, FakeResponse(status_code=200, chunks=[chunk] * 3))
    monkeypatch.setattr(config, "MAX_ZIP_SIZE_BYTES", 2 * 1024 * 1024)
    dest = str(tmp_path / "r.zip")
    with pytest.raises(gh.GithubImportError) as exc:
        gh.download_repository_zip("u", "r", None, dest)
    assert exc.value.code == gh.ERR_TOO_LARGE


def test_empty_archive(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "fetch_repository", lambda o, r: {"full_name": "u/r", "default_branch": "main"})
    _patch(monkeypatch, FakeResponse(status_code=200, chunks=[]))
    with pytest.raises(gh.GithubImportError) as exc:
        gh.download_repository_zip("u", "r", None, str(tmp_path / "r.zip"))
    assert exc.value.code == gh.ERR_EMPTY


def test_unexpected_host_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "fetch_repository", lambda o, r: {"full_name": "u/r", "default_branch": "main"})
    _patch(monkeypatch, FakeResponse(status_code=200, chunks=[b"data"], url="https://evil.example.com/x.zip"))
    with pytest.raises(gh.GithubImportError) as exc:
        gh.download_repository_zip("u", "r", None, str(tmp_path / "r.zip"))
    assert exc.value.code == gh.ERR_DOWNLOAD_FAILED


def test_successful_download_writes_archive(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "fetch_repository", lambda o, r: {"full_name": "u/r", "default_branch": "trunk"})
    _patch(monkeypatch, FakeResponse(status_code=200, chunks=[b"PK\x03\x04", b"payload"]))
    dest = tmp_path / "r.zip"
    meta = gh.download_repository_zip("u", "r", None, str(dest))
    assert dest.read_bytes() == b"PK\x03\x04payload"
    assert meta["ref"] == "trunk"
    assert meta["downloaded_bytes"] == 11  # 4-byte + 7-byte chunks


def test_branch_url_uses_ref(monkeypatch, tmp_path):
    captured = {}

    def fake_stream(url, dest_path):
        captured["url"] = url
        with open(dest_path, "wb") as fh:
            fh.write(b"PK\x03\x04")
        return 4

    monkeypatch.setattr(gh, "fetch_repository", lambda o, r: {"full_name": "u/r", "default_branch": "main"})
    monkeypatch.setattr(gh, "_stream_to_file", fake_stream)
    gh.download_repository_zip("u", "r", "develop", str(tmp_path / "r.zip"))
    assert captured["url"].endswith("/zip/refs/heads/develop")


# --------------------------------------------------------------------------
# Revision identity
# --------------------------------------------------------------------------
def test_source_digest_is_stable_and_content_sensitive(tmp_path):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    first = gh.compute_source_digest(str(tmp_path))
    assert first and first == gh.compute_source_digest(str(tmp_path))

    (tmp_path / "b.py").write_text("def b():\n    return 3\n", encoding="utf-8")
    assert gh.compute_source_digest(str(tmp_path)) != first


def test_source_digest_ignores_non_source_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    baseline = gh.compute_source_digest(str(tmp_path))
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert gh.compute_source_digest(str(tmp_path)) == baseline


def test_headers_include_token_only_when_present(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert "Authorization" not in gh._headers()
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    headers = gh._headers()
    assert headers["Authorization"] == "Bearer secret-token"
