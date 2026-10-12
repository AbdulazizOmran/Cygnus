"""AppImageHub (appimage.github.io): the community directory of AppImages that people have listed, read as a catalogue for Discover.

The directory publishes one JSON file. Everything in it was written by whoever listed the program, so it is read as hostile data: every field is
type-checked and cut, an entry becomes a record only when it names a GitHub project with a valid owner and repository name, and a program that
lives somewhere else (GitLab, SourceForge, a web page) is left out because Cygnus could not check its download address the way it does GitHub's.
No address from the file is ever followed: the download is looked up through GitHub's release answer for "owner/repo" when the person presses
the button (cygnus.core.backends.release_feeds), never from a link in this file.

What listing means: AppImageHub does not vouch for a program. Cygnus has not looked at it either. It is a way to FIND an AppImage; the Install page
then analyses the file, and an AppImage runs with all of your user's rights."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from cygnus.core.backends import release_feeds as rf
from cygnus.core.errors import CygnusError
from cygnus.core.util import http
from cygnus.core.util.text import plain

ID = "appimagehub"
FEED = "https://appimage.github.io/feed.json"
MAX_FEED = 24 * 1024 * 1024
MAX_ITEMS = 20_000
MAX_NAME, MAX_SUMMARY, MAX_DEVELOPER = 120, 300, 120
_CATEGORY = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,29}\Z")


def host_ok(host: str) -> bool:
    return host == "appimage.github.io"


def _repo_of(item: dict[str, Any]) -> str | None:
    """"owner/repo" of the GitHub project the entry names, or None when it names none that is valid."""
    links = item.get("links")
    for link in links if isinstance(links, list) else []:
        if isinstance(link, dict) and link.get("type") == "GitHub" and isinstance(link.get("url"), str):
            owner, slash, name = link["url"].partition("/")
            if slash and rf._OWNER.fullmatch(owner) and rf._REPO.fullmatch(name) and not name.endswith(".git"):
                return f"{owner}/{name}"
    return None


def _shown_name(name: str, repo: str) -> str:
    """The directory names a program by its file name ("-_ICQ", "Dead_Rising_2"): underscores are read as spaces, and what is left of a name that has
    no letter or digit in it is the repository's name."""
    text = plain(name.replace("_", " "), MAX_NAME).strip(" -")
    return text if any(c.isalnum() for c in text) else repo.partition("/")[2]


def record_of(item: Any) -> dict[str, Any] | None:
    """One catalogue record from one entry of the file, or None."""
    if not isinstance(item, dict):
        return None
    repo = _repo_of(item)
    name = item.get("name")
    if repo is None or not isinstance(name, str):
        return None
    description = item.get("description")
    categories = item.get("categories")
    authors = item.get("authors")
    first = next((a.get("name") for a in authors if isinstance(a, dict) and isinstance(a.get("name"), str)), "") if isinstance(authors, list) else ""
    return {"repo": repo, "name": _shown_name(name, repo),
            "summary": plain(description, MAX_SUMMARY) if isinstance(description, str) else "",
            "categories": [c for c in categories if isinstance(c, str) and _CATEGORY.fullmatch(c)][:12] if isinstance(categories, list) else [],
            "developer": plain(first, MAX_DEVELOPER)}


def parse(data: Any) -> list[dict[str, Any]]:
    """The records of the directory's file (one per GitHub project; the first entry wins when a project is listed twice)."""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise CygnusError("AppImageHub's list was not in a form Cygnus understands")
    seen: set[str] = set()
    out = []
    for item in items[:MAX_ITEMS]:
        record = record_of(item)
        if record is not None and record["repo"].lower() not in seen:
            seen.add(record["repo"].lower())
            out.append(record)
    return out


def clean(records: Any) -> list[dict[str, Any]]:
    """Records read back from the cache folder, checked again as strictly as when they were made (the file is ours, but it is only a file)."""
    out, seen = [], set()
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict) or not all(isinstance(rec.get(k), str) for k in ("repo", "name", "summary", "developer")):
            continue
        owner, _, name = rec["repo"].partition("/")
        if not (rf._OWNER.fullmatch(owner) and rf._REPO.fullmatch(name)) or rec["repo"].lower() in seen:
            continue
        seen.add(rec["repo"].lower())
        cats = rec.get("categories")
        out.append({"repo": rec["repo"], "name": plain(rec["name"], MAX_NAME), "summary": plain(rec["summary"], MAX_SUMMARY),
                    "categories": [c for c in cats if isinstance(c, str) and _CATEGORY.fullmatch(c)][:12] if isinstance(cats, list) else [],
                    "developer": plain(rec["developer"], MAX_DEVELOPER)})
    return out


def fetch(progress: Callable[[str], None] = lambda _: None, *, get: Callable[..., bytes] = http.get) -> list[dict[str, Any]]:
    """Download and read the directory's file (about two megabytes)."""
    import json

    progress("Reading AppImageHub's list…")
    try:
        data = json.loads(get(FEED, limit=MAX_FEED, host_ok=host_ok))
    except (ValueError, RecursionError) as exc:
        raise CygnusError("AppImageHub's list was not valid") from exc
    return parse(data)
