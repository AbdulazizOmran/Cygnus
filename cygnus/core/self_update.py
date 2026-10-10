"""Is there a newer Cygnus? Cygnus does not update itself (its package contains the administrator helper, and a download of it
could only be trusted as far as the network and a GitHub account), but it can say that a newer release exists, once, and where to
get it. Nothing is downloaded, installed or changed here.

The question is asked of GitHub's list of releases (never "the latest release": that address skips pre-releases, and every release
so far is one), only when the person checks for updates or the background check runs, and the answer is kept so the Updates page
can show it without asking again."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from cygnus import __version__
from cygnus.core import paths
from cygnus.core.backends import vendor_feeds
from cygnus.core.errors import CygnusError
from cygnus.core.util import http
from cygnus.core.util.fs import atomic_write

RELEASES_URL = "https://api.github.com/repos/AbdulazizOmran/Cygnus/releases?per_page=15"
PAGE_PREFIX = "https://github.com/AbdulazizOmran/Cygnus/releases/"
MAX_ANSWER = 512 * 1024
_TAG = re.compile(r"v?([0-9]+(?:\.[0-9]+){1,3})")  # ASCII digits only: \d would also take digits of other scripts


def cache_path() -> Path:
    return paths.cache_dir() / "cygnus-update.json"


def newest(releases: object) -> dict[str, Any] | None:
    """The highest numbered release in GitHub's list: {"version", "url", "prerelease"}. Drafts, tags that are not plain version numbers
    and addresses that are not this project's own release pages are ignored (what the answer says is data, never trusted further)."""
    if not isinstance(releases, list):
        return None
    best: dict[str, Any] | None = None
    for release in releases[:100]:
        if not isinstance(release, dict) or release.get("draft") is True:
            continue
        tag = release.get("tag_name")
        match = _TAG.fullmatch(tag) if isinstance(tag, str) else None
        if match is None:
            continue
        version = match.group(1)
        url = release.get("html_url")
        if not isinstance(url, str) or not url.startswith(PAGE_PREFIX) or len(url) > 200 or any(c.isspace() or ord(c) < 33 for c in url):
            url = PAGE_PREFIX
        if best is None or vendor_feeds.vercmp(version, best["version"]) > 0:
            best = {"version": version, "url": url, "prerelease": release.get("prerelease") is True}
    return best


def _describe(found: dict[str, Any], checked_at: str) -> dict[str, Any]:
    return {"ok": True, "current": __version__, "latest": found["version"], "url": found["url"], "prerelease": found["prerelease"],
            "newer": vendor_feeds.vercmp(found["version"], __version__) > 0, "checked_at": checked_at}


def _cache() -> dict[str, Any]:
    try:
        raw = json.loads(cache_path().read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember(**changes: Any) -> None:
    data = {**_cache(), **changes}
    try:
        cache_path().parent.mkdir(parents=True, exist_ok=True)
        atomic_write(cache_path(), json.dumps(data).encode())
    except OSError:
        pass  # not remembering is not worth failing a check for


def check(*, fetch: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Ask GitHub, remember a good answer, and say what it was. A failed check changes nothing that was remembered but the note that
    it failed, and can never break whatever else is being checked (whatever goes wrong, it is an answer that says so)."""
    try:
        data = (fetch or http.get_json)(RELEASES_URL, limit=MAX_ANSWER, timeout=20.0, headers={"Accept": "application/vnd.github+json"},
                                        host_ok=lambda host: host == "api.github.com")
        found = newest(data)
    except Exception as exc:  # noqa: BLE001 - a network error, an odd answer, a stack overflow on a hostile one: none may escape
        message = str(exc)[:200] if isinstance(exc, CygnusError) else f"the answer could not be read ({type(exc).__name__})"
        _remember(error=message)
        return {"ok": False, "error": message}
    if found is None:
        message = "GitHub's list of releases was not in the expected form"
        _remember(error=message)
        return {"ok": False, "error": message}
    result = _describe(found, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    _remember(latest=found, checked_at=result["checked_at"], error=None)
    return result


def last_known() -> dict[str, Any] | None:
    """The last good answer (no network), judged against the version installed NOW: after an update the notice is gone. "last_error"
    says why the most recent attempt failed, if it did. None when nothing has been asked yet."""
    raw = _cache()
    error = raw.get("error") if isinstance(raw.get("error"), str) else None
    found = raw.get("latest")
    if not (isinstance(found, dict) and isinstance(found.get("version"), str) and _TAG.fullmatch(found["version"])):
        return {"ok": False, "newer": False, "error": error, "last_error": error} if error else None
    url = found.get("url")
    clean = {"version": found["version"], "url": url if isinstance(url, str) and url.startswith(PAGE_PREFIX) else PAGE_PREFIX,
             "prerelease": found.get("prerelease") is True}
    return {**_describe(clean, str(raw.get("checked_at") or "")), "last_error": error}
