"""Minimal HTTPS client: TLS only, timeouts, size caps, no redirects to plain HTTP."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urljoin

import requests

from cygnus import __version__
from cygnus.core.errors import CygnusError

USER_AGENT = f"Cygnus/{__version__} (+https://github.com/AbdulazizOmran/Cygnus)"
DEFAULT_LIMIT = 4 * 1024 * 1024


class HttpError(CygnusError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)


@contextmanager
def _open(url: str, *, headers: dict[str, str], timeout: float) -> Iterator[requests.Response]:
    """GET `url`, following redirects ourselves: the address of every hop is checked BEFORE it is requested, so
    a redirect to plain HTTP is never contacted (and its body never read)."""
    for _ in range(MAX_REDIRECTS + 1):
        if not url.startswith("https://"):
            raise HttpError(f"redirected to a non-HTTPS URL: {url[:100]}")
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=False)
        location = resp.headers.get("Location")
        if resp.status_code in _REDIRECTS and location:
            resp.close()
            url = urljoin(url, location)
            continue
        with resp:
            yield resp
        return
    raise HttpError("too many redirects")


def get(url: str, *, limit: int = DEFAULT_LIMIT, timeout: float = 20.0, headers: dict[str, str] | None = None,
        range_end: int | None = None) -> bytes:
    if not url.startswith("https://"):
        raise HttpError(f"refusing non-HTTPS URL: {url}")
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    if range_end is not None:
        hdrs["Range"] = f"bytes=0-{range_end}"
    try:
        with _open(url, headers=hdrs, timeout=timeout) as resp:
            if resp.status_code >= 400:
                raise HttpError(f"HTTP {resp.status_code} for {url}", resp.status_code)
            chunks, total = [], 0
            deadline = time.monotonic() + max(timeout * 6, 60)  # whole-request deadline, not per read
            for chunk in resp.iter_content(65536):
                if time.monotonic() > deadline:
                    raise HttpError(f"{url} is responding too slowly")
                total += len(chunk)
                if total > limit and range_end is None:
                    raise HttpError(f"response from {url} exceeds {limit} bytes")
                chunks.append(chunk)
                if range_end is not None and total > range_end:
                    break
            return b"".join(chunks)
    except requests.RequestException as exc:
        raise HttpError(f"network error for {url}: {exc}") from exc


def get_json(url: str, **kw: Any) -> Any:
    try:
        return json.loads(get(url, **kw))
    except ValueError as exc:
        raise HttpError(f"invalid JSON from {url}") from exc


def post_json(url: str, payload: Any, *, limit: int = DEFAULT_LIMIT, timeout: float = 20.0) -> Any:
    if not url.startswith("https://"):
        raise HttpError(f"refusing non-HTTPS URL: {url}")
    try:
        with requests.post(url, json=payload, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                           allow_redirects=False, stream=True) as resp:
            if resp.status_code >= 400:
                raise HttpError(f"HTTP {resp.status_code} for {url}", resp.status_code)
            chunks, total = [], 0
            deadline = time.monotonic() + max(timeout * 6, 60)
            for chunk in resp.iter_content(65536):  # decoded (decompressed) chunks, capped as they arrive
                total += len(chunk)
                if total > limit:
                    raise HttpError(f"response from {url} exceeds {limit} bytes")
                if time.monotonic() > deadline:
                    raise HttpError(f"{url} is responding too slowly")
                chunks.append(chunk)
    except requests.RequestException as exc:
        raise HttpError(f"network error for {url}: {exc}") from exc
    try:
        return json.loads(b"".join(chunks))
    except ValueError as exc:
        raise HttpError(f"invalid JSON from {url}") from exc


def download(url: str, dest_dir, *, expected_sha256: str | None = None, limit: int = 4 * 1024**3,
             timeout: float = 60.0, progress=None):
    """Download over HTTPS into dest_dir (same filesystem as the final location), verifying SHA-256.

    Returns the path of the verified file. A partial or mismatching download is deleted.
    """
    import hashlib
    import os
    from pathlib import Path

    if not url.startswith("https://"):
        raise HttpError(f"refusing non-HTTPS URL: {url}")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "download"
    name = "".join(c if c.isalnum() or c in "._-+" else "_" for c in name)[:200]
    final = dest_dir / name
    part = dest_dir / f".{name}.part"
    h = hashlib.sha256()
    total = 0
    try:
        with _open(url, headers={"User-Agent": USER_AGENT}, timeout=timeout) as resp:
            if resp.status_code >= 400:
                raise HttpError(f"HTTP {resp.status_code} for {url}", resp.status_code)
            size = int(resp.headers.get("Content-Length") or 0) or None
            deadline = time.monotonic() + max(timeout * 60, 3600)
            part.unlink(missing_ok=True)  # a left-over (or a planted link: only the link goes) ...
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)  # ...never written through
            with os.fdopen(fd, "wb") as out:
                for chunk in resp.iter_content(1 << 20):
                    if time.monotonic() > deadline:
                        raise HttpError("the download is taking too long")
                    total += len(chunk)
                    if total > limit:
                        raise HttpError("download exceeds the size limit")
                    h.update(chunk)
                    out.write(chunk)
                    if progress:
                        progress(total, size)
                out.flush()
                os.fsync(out.fileno())
        digest = h.hexdigest()
        if expected_sha256 and digest != expected_sha256.lower():
            raise HttpError(f"checksum mismatch for {name}: got {digest[:12]}…, expected {expected_sha256[:12]}…")
        os.replace(part, final)
        return final
    except requests.RequestException as exc:
        raise HttpError(f"network error for {url}: {exc}") from exc
    except OSError as exc:
        raise HttpError(f"could not save the download: {exc.strerror or exc}") from exc
    finally:
        part.unlink(missing_ok=True)
