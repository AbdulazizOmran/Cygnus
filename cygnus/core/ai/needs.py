"""Finding what else an application needs (architecture §14).

The model is given public documentation as DATA and asked for a list of components, each with a quote from the document that
says so. What comes back is never trusted: ordinary code keeps an item only if its quote really is in the document it names,
and only in one of a few shapes (an Arch package that exists, membership of the `input` group, a service to enable, a store
page for a browser extension, or a note). Packages are looked up in the real repositories and the AUR. Every kept item is
labelled as an AI suggestion, and nothing is done until the person approves it, through the usual helper plan."""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from cygnus.core.ai import prompt, sources
from cygnus.core.ai.text import plain
from cygnus.core.errors import CygnusError

MAX_ITEMS = 12
MAX_CONSIDERED = 30
MIN_QUOTE, MAX_QUOTE = 12, 300
TOKEN_LIFETIME_S = 30 * 60
STORES = frozenset({"addons.mozilla.org", "chromewebstore.google.com", "chrome.google.com", "microsoftedge.microsoft.com"})
_PACKAGE = re.compile(r"[a-z0-9@_+][a-z0-9@._+-]{0,99}")


def unit_ok(unit: object) -> bool:
    """A unit name the helper itself would accept (it is the helper's own rule, so no suggestion is offered that it would refuse)."""
    from cygnus.helper.actions import UNIT_NAME

    return isinstance(unit, str) and UNIT_NAME.fullmatch(unit) is not None


class NeedsError(CygnusError):
    pass


@dataclass
class Suggestion:
    token: str
    name: str
    relation: str  # "required" | "optional"
    why: str
    kind: str  # "package" | "aur" | "group" | "service" | "extension" | "info"
    target: str  # package, group, unit or address; "" for a note
    source: str  # where a package was found: the repository, or "AUR"
    citation_url: str
    quote: str
    satisfied: str = ""  # non-empty when there is nothing to do, and why
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Result:
    suggestions: list[Suggestion]
    left_out: list[str]  # why some suggestions were dropped (plain text)
    pages: list[str]  # the documents that were read
    provider: str


@dataclass(frozen=True)
class AppInfo:
    name: str
    app_id: str
    fmt: str
    homepages: list[str]


# -- the suggestions waiting for an answer: kept here, so that what is acted on is what was checked, never what a page sends back
_STORE: dict[str, tuple[float, Suggestion]] = {}
_LOCK = threading.Lock()


def remember(items: list[Suggestion]) -> None:
    now = time.monotonic()
    with _LOCK:
        for token in [t for t, (when, _s) in _STORE.items() if now - when > TOKEN_LIFETIME_S]:
            del _STORE[token]
        for item in items:
            _STORE[item.token] = (now, item)


def recall(token: object) -> Suggestion:
    with _LOCK:
        entry = _STORE.get(token) if isinstance(token, str) else None
    if entry is None or time.monotonic() - entry[0] > TOKEN_LIFETIME_S:
        raise NeedsError("this suggestion has expired; ask again")
    return entry[1]


# -- the checks ------------------------------------------------------------------------------------------------------------
def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _plain(value: object, limit: int) -> str:
    """Text from the model, made safe to show: a single line, no control or invisible characters, cut to size."""
    return plain(value, limit)


def _cited(citation: object, pages: list[sources.Page]) -> tuple[str, str] | None:
    """(address, quote) when the quote really is in the document it names. The whole quote (up to 600 characters) must be there;
    only the first 300 are kept for showing."""
    if not isinstance(citation, dict):
        return None
    url, quote = citation.get("url"), _plain(citation.get("quote"), 600)
    if not isinstance(url, str) or len(quote) < MIN_QUOTE:
        return None
    page = next((p for p in pages if p.url == url), None)
    if page is None or _squash(quote) not in _squash(page.text):
        return None
    return page.url, quote[:MAX_QUOTE]


def _exact(info: dict | None, key: str, name: str) -> dict | None:
    """The sync/local entry of a lookup, only when it is the package of that very name: a lookup also answers for a package that
    merely PROVIDES the name (bash provides sh), which is not what the page asked for."""
    entry = (info or {}).get(key)
    return entry if isinstance(entry, dict) and entry.get("name") == name else None


def validate(answer: object, pages: list[sources.Page], *, packages: Callable[[list[str]], dict[str, dict]],
             aur: Callable[[list[str]], set[str] | None], in_group: Callable[[str], bool]) -> tuple[list[Suggestion], list[str]]:
    """The suggestions that survive the checks, and a plain reason for each one that did not."""
    if not isinstance(answer, dict) or not isinstance(answer.get("components"), list):
        raise NeedsError("the AI's answer was not in the form that was asked for")
    from cygnus.helper.actions import ALLOWED_GROUPS

    drafts: list[dict[str, Any]] = []
    left_out: list[str] = []

    def drop(label: str, why: str) -> None:
        if len(left_out) < 10:
            left_out.append(f"{label or 'an item'}: {why}")

    for raw in answer["components"][:MAX_CONSIDERED]:
        if not isinstance(raw, dict) or not isinstance(raw.get("action"), dict):
            drop("", "it was not in the expected form")
            continue
        name = _plain(raw.get("name"), 80)
        action = raw["action"]
        kind = action.get("kind")
        cite = _cited(raw.get("citation"), pages)
        if not name:
            drop("", "it had no name")
        elif cite is None:
            drop(name, "its quote was not found in the document it named")
        elif kind not in ("package", "group", "service", "extension", "info"):
            drop(name, "it asked for something Cygnus does not do")
        else:
            drafts.append({"name": name, "relation": "required" if raw.get("relation") == "required" else "optional",
                           "why": _plain(raw.get("why"), 300), "action": action, "kind": kind, "cite": cite})

    package_names = sorted({d["action"].get("name") for d in drafts
                            if d["kind"] == "package" and isinstance(d["action"].get("name"), str)
                            and _PACKAGE.fullmatch(d["action"]["name"])})
    found = packages(package_names) if package_names else {}
    wanted_aur = [n for n in package_names if not _exact(found.get(n), "sync", n)]
    in_aur = aur(wanted_aur) if wanted_aur else set()  # None: the AUR could not be asked

    kept: list[Suggestion] = []
    seen: set[tuple[str, str]] = set()
    for d in drafts:
        action, kind, name = d["action"], d["kind"], d["name"]
        target, source, satisfied, notes = "", "", "", []
        if kind == "package":
            pkg = action.get("name")
            if not isinstance(pkg, str) or not _PACKAGE.fullmatch(pkg):
                drop(name, "it did not give a usable package name")
                continue
            info = found.get(pkg) or {}
            sync = _exact(info, "sync", pkg)
            if sync:
                kind, target, source = "package", pkg, sync.get("repo") or "repositories"
                satisfied = "already installed" if _exact(info, "local", pkg) else ""
            elif in_aur is None:
                drop(name, f"{pkg} is not in your repositories and the AUR could not be asked just now")
                continue
            elif pkg in in_aur:
                kind, target, source = "aur", pkg, "AUR"
                notes.append("This is a community package: you review its build files before anything is built.")
            else:
                drop(name, f"no package called {pkg} exists in your repositories or the AUR")
                continue
        elif kind == "group":
            group = action.get("name")
            if not isinstance(group, str) or group not in ALLOWED_GROUPS:
                drop(name, f"Cygnus can only add you to: {', '.join(sorted(ALLOWED_GROUPS))}")
                continue
            target, satisfied = group, ("you are already in that group" if in_group(group) else "")
        elif kind == "service":
            unit = action.get("unit")
            if not unit_ok(unit):
                drop(name, "it did not give a usable service name")
                continue
            target = unit
            notes.append("The service belongs to a package: install that package first.")
        elif kind == "extension":
            url = sources.clean_url(action.get("url"))
            if url is None or sources._host(url) not in STORES:
                drop(name, "its address is not a browser extension store page Cygnus knows")
                continue
            target = url
        else:  # info
            if not d["why"]:
                drop(name, "a note needs a sentence")
                continue
        if (kind, target) in seen and target:
            continue
        seen.add((kind, target))
        kept.append(Suggestion(secrets.token_hex(12), name, d["relation"], d["why"], kind, target, source, d["cite"][0], d["cite"][1],
                               satisfied, notes))
    kept.sort(key=lambda s: (s.satisfied != "", s.relation != "required", s.name.lower()))
    return kept[:MAX_ITEMS], left_out


# -- the real lookups (replaced in tests) -------------------------------------------------------------------------------------
def host_packages(names: list[str]) -> dict[str, dict]:
    from cygnus.core.backends import pacman as pm

    return pm.run_worker(pm.read_config(), {"op": "info", "names": names})["packages"]


def host_aur(names: list[str]) -> set[str] | None:
    from cygnus.core.backends import aur

    try:
        return set(aur.info(names))
    except CygnusError:
        return None  # the AUR cannot be reached: said so, not "does not exist"


def host_in_group(group: str) -> bool:
    import grp

    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return False
    return gid in os.getgroups()


def discover(app: AppInfo, provider: Any, *, fetch: Callable[..., list[sources.Page]] = sources.fetch_pages,
             packages: Callable[[list[str]], dict[str, dict]] = host_packages, aur: Callable[[list[str]], set[str] | None] = host_aur,
             in_group: Callable[[str], bool] = host_in_group, progress: Callable[[str], None] = lambda _: None) -> Result:
    """Read the application's documentation, ask the model, check what it says."""
    progress("Reading the application's public documentation…")
    pages = fetch(app.homepages)
    progress(f"Asking {provider.name}…")
    answer = provider.ask_needs(app, pages)
    progress("Checking the suggestions against your repositories…")
    kept, left_out = validate(answer, pages, packages=packages, aur=aur, in_group=in_group)
    remember(kept)
    return Result(kept, left_out, [p.url for p in pages], provider.name)
