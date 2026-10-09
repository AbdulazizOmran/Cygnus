"""Minimal HTTPS client: TLS only, timeouts, size caps, no redirects to plain HTTP."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

import requests
from urllib3.util import parse_url

from cygnus import __version__
from cygnus.core.errors import CygnusError

USER_AGENT = f"Cygnus/{__version__} (+https://github.com/AbdulazizOmran/Cygnus)"
DEFAULT_LIMIT = 4 * 1024 * 1024


class HttpError(CygnusError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body  # the start of what the server said with an error status (a service's own explanation)


MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)


class _NoAuth(requests.auth.AuthBase):
    """Asking for authentication explicitly stops `requests` from looking in ~/.netrc: what the person keeps there for a host is
    never sent along with a page request."""

    def __call__(self, r):
        return r


def _checked_host(url: str) -> str:
    """The host this address really leads to, or an error for an address that two parsers could read differently. A check on the
    host is only worth anything when it is made on the host that is connected to: `https://evil.example\\@vendor.example/` reads as
    vendor.example to one parser and is sent to evil.example by the one `requests` uses."""
    if any(ord(c) <= 32 or c in "\\\x7f\x85" for c in url):
        raise HttpError(f"refusing an address with odd characters: {url[:100]!r}")
    try:
        parts = urlparse(url)
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise HttpError(f"refusing an address with a user name in it: {url[:100]!r}")
        connects = parse_url(requests.Request("GET", url).prepare().url)
    except (ValueError, requests.RequestException, LookupError) as exc:
        raise HttpError(f"refusing an address that cannot be read: {url[:100]!r}") from exc
    host = (parts.hostname or "").lower()
    if not host or host != (connects.host or "").lower().strip("[]"):
        raise HttpError(f"refusing an address that is read two ways: {url[:100]!r}")
    return host


def _within(seconds: float, work, what: str):
    """Runs `work` and gives up after `seconds` in all, however slowly the other side trickles (a socket timeout only limits each
    wait, so a server that sends a byte every few seconds would otherwise keep a request for ever)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = work()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True, name="cygnus-http")
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise HttpError(f"{what} is responding too slowly")
    if "error" in box:
        raise box["error"]
    return box["value"]


@contextmanager
def _open(url: str, *, headers: dict[str, str], timeout: float, host_ok=None) -> Iterator[requests.Response]:
    """GET `url`, following redirects ourselves: the address of every hop is checked BEFORE it is requested, so
    a redirect to plain HTTP (or, when `host_ok` is given, to a host it does not accept) is never contacted."""
    for _ in range(MAX_REDIRECTS + 1):
        if not url.startswith("https://"):
            raise HttpError(f"redirected to a non-HTTPS URL: {url[:100]}")
        host = _checked_host(url)
        if host_ok is not None and not host_ok(host):
            raise HttpError(f"redirected to a host that is not allowed: {url[:100]}")
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=False, auth=_NoAuth())
        location = resp.headers.get("Location")
        if resp.status_code in _REDIRECTS and location:
            resp.close()
            try:
                url = urljoin(url, location)
            except ValueError:
                raise HttpError(f"redirected to an address that cannot be read: {location[:100]!r}") from None
            continue
        with resp:
            yield resp
        return
    raise HttpError("too many redirects")


def get(url: str, *, limit: int = DEFAULT_LIMIT, timeout: float = 20.0, headers: dict[str, str] | None = None,
        range_end: int | None = None, host_ok=None) -> bytes:
    if not url.startswith("https://"):
        raise HttpError(f"refusing non-HTTPS URL: {url}")
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    if range_end is not None:
        hdrs["Range"] = f"bytes=0-{range_end}"
    def fetch() -> bytes:
        with _open(url, headers=hdrs, timeout=timeout, host_ok=host_ok) as resp:
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

    try:
        return _within(max(timeout * 6, 60), fetch, url)
    except requests.RequestException as exc:
        raise HttpError(f"network error for {url}: {exc}") from exc


def get_json(url: str, **kw: Any) -> Any:
    try:
        return json.loads(get(url, **kw))
    except ValueError as exc:
        raise HttpError(f"invalid JSON from {url}") from exc


def post_json(url: str, payload: Any, *, limit: int = DEFAULT_LIMIT, timeout: float = 20.0,
              headers: dict[str, str] | None = None, allow_loopback_http: bool = False) -> Any:
    """POST JSON and read a JSON answer. `headers` carries things like an API key (never put one in the address).
    Plain HTTP is refused, except, when asked for, to this computer itself (a local model server)."""
    if not url.startswith("https://") and not (allow_loopback_http and _is_loopback_http(url)):
        raise HttpError(f"refusing non-HTTPS URL: {url}")
    _checked_host(url)
    # UTF-8 as it is, not \\uXXXX escapes: six times smaller for non-Latin text, and what a service's size limit is written for.
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")
    local = url.startswith("http://")  # a key sent to a local server must not be handed to a proxy on the way
    hdrs = {"User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8", **(headers or {})}

    def fetch() -> bytes:
        with requests.post(url, data=body, headers=hdrs, timeout=timeout, allow_redirects=False, stream=True, auth=_NoAuth(),
                           proxies={"http": None, "https": None} if local else None) as resp:
            if resp.status_code >= 400:
                try:
                    said = resp.raw.read(2000, decode_content=True).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001 - the explanation is a bonus
                    said = ""
                raise HttpError(f"HTTP {resp.status_code} for {url}", resp.status_code, said)
            chunks, total = [], 0
            deadline = time.monotonic() + max(timeout * 6, 60)
            for chunk in resp.iter_content(65536):  # decoded (decompressed) chunks, capped as they arrive
                total += len(chunk)
                if total > limit:
                    raise HttpError(f"response from {url} exceeds {limit} bytes")
                if time.monotonic() > deadline:
                    raise HttpError(f"{url} is responding too slowly")
                chunks.append(chunk)
            return b"".join(chunks)

    try:
        raw = _within(max(timeout * 6, 60), fetch, url)
    except requests.RequestException as exc:
        raise HttpError(f"network error for {url}: {exc}") from exc
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:  # not JSON, or nested so deeply that reading it would overflow the stack
        raise HttpError(f"invalid JSON from {url}") from exc


def _is_loopback_http(url: str) -> bool:
    parts = urlparse(url)
    return parts.scheme == "http" and (parts.hostname or "") in ("localhost", "127.0.0.1", "::1") and not parts.username


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
