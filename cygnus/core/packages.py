"""Repository packages by name: search, the installed list, one package's details, and the checks made before an install or a removal
(the Packages page and `cygnus packages`).

Everything here only reads (through the crash-isolated libalpm worker). A change goes through the root helper's plans like every
other package change; this module only says, beforehand and in words, what that plan would refuse and why.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Callable

from cygnus.core.backends import pacman as pm
from cygnus.core.errors import CygnusError

# As the helper reads package names (cygnus/helper/actions.py PKG_NAME; a test keeps the two equal).
PKG_NAME = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]{0,99}\Z")
MAX_NAMES = 20  # packages in one install or removal asked for by name
MAX_BATCH = 200  # packages in one removal of everything no longer needed (the helper's own limit on a request)
MAX_TERMS = 8
MAX_TERM_LENGTH = 100
MIN_QUERY = 2


class PackagesError(CygnusError):
    """Something about the request, in words (a search that is too short, a package that is not there)."""


class PackageNotFound(PackagesError):
    """No package of that name is installed or offered by a repository."""


def query_terms(text: str) -> list[str]:
    """The words of a search box, lower-cased. A search is plain text: every word must be found (name, description, provides or
    group), and nothing is a pattern."""
    if not isinstance(text, str):
        raise PackagesError("the search must be text")
    terms = [t for t in text.lower().split() if t]
    if sum(len(t) for t in terms) < MIN_QUERY:
        raise PackagesError(f"type at least {MIN_QUERY} characters to search")
    if len(terms) > MAX_TERMS or any(len(t) > MAX_TERM_LENGTH for t in terms):
        raise PackagesError(f"search for at most {MAX_TERMS} words of up to {MAX_TERM_LENGTH} characters")
    return terms


def names_checked(names: Any, limit: int = MAX_NAMES) -> list[str]:
    """The package names of an install or removal: text, valid package names, no repeats, not too many."""
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise PackagesError("the package names must be a list of text")
    if not names:
        raise PackagesError("no package was named")
    unique = list(dict.fromkeys(names))
    if len(unique) > limit:
        raise PackagesError(f"at most {limit} packages at a time")
    for n in unique:
        if not PKG_NAME.match(n):
            raise PackagesError(f"{n[:60]!r} is not a package name Cygnus can work with")
    return unique


def _clean_name(name: str) -> str:
    """A name that came from an application's own metadata, safe to show: printable characters only, at most 100."""
    return "".join(c if c.isprintable() else " " for c in name)[:100]


def _managed_reason(name: str) -> str:
    return f"Cygnus manages this as {name}: remove it from its page under Applications, so that Cygnus's own list stays right."


def managed_packages(registry_path: str | None = None) -> dict[str, str]:
    """The packages Cygnus manages as applications (a converted .deb/.rpm, a package file, an AUR build): package name -> the
    application's name. Removing one of those from here would leave an application in Cygnus's list whose package is gone, so it is
    removed from its own page under Applications. When the list cannot be read the answer is empty (a removal is still checked by the
    helper; what is lost is only the hint)."""
    from cygnus.core.registry import db as regdb
    from cygnus.core.registry import open_registry

    out: dict[str, str] = {}
    try:
        reg = open_registry(registry_path)
        for r in regdb.list_installations(reg):
            for a in regdb.artifacts_of(reg, r["id"]):
                if a["kind"] == "package":
                    out[a["locator"]] = r["name"]
    except (sqlite3.Error, OSError, CygnusError):
        return {}
    return out


def lists_age(config: pm.PacmanConfig) -> dict[str, float | None]:
    """When pacman last refreshed the package lists (the sync databases), as epoch seconds, or None when there are none: {"refreshed"}.
    That is the date of the folder that holds them, which changes when a list is put there. The files' own dates are the mirror's
    (libalpm sets them from the server's Last-Modified), so they say when the mirror published, not when this computer looked.
    Searching reads these lists, so an old date means old results."""
    folder = os.path.join(config.dbpath, "sync")
    try:
        with os.scandir(folder) as entries:
            if not any(e.name.endswith(".db") and e.is_file(follow_symlinks=False) for e in entries):
                return {"refreshed": None}
        return {"refreshed": os.stat(folder).st_mtime}
    except OSError:
        return {"refreshed": None}


def _ask(config: pm.PacmanConfig, request: dict[str, Any], worker: Callable | None) -> dict[str, Any]:
    try:
        answer = (worker or pm.run_worker)(config, request)  # looked up now, so a test (or a caller) can swap it
    except pm.WorkerCrash as crash:
        raise PackagesError(f"the package databases could not be read ({crash})") from crash
    if not answer.get("ok"):
        raise PackagesError(str((answer.get("error") or {}).get("message") or "the package databases could not be read"))
    return answer


def search(text: str, *, config: pm.PacmanConfig | None = None, worker: Callable | None = None) -> dict[str, Any]:
    """Repository packages matching every word of `text`, best match first: {"terms", "results", "total", "truncated", "lists"}."""
    terms = query_terms(text)
    config = config or pm.read_config()
    answer = _ask(config, {"op": "search", "terms": terms}, worker)
    return {"terms": terms, "results": answer["results"], "total": answer["total"], "truncated": answer["truncated"],
            "lists": lists_age(config)}


def installed_versions(*, config: pm.PacmanConfig | None = None, worker: Callable | None = None) -> dict[str, str]:
    """name -> version of every installed package (one cheap question; for a page that only needs to know what is installed)."""
    versions = _ask(config or pm.read_config(), {"op": "installed_versions"}, worker)["versions"]
    if not isinstance(versions, dict):
        raise PackagesError("the package databases gave an answer Cygnus did not expect")
    return {str(k): str(v) for k, v in versions.items()}


def repo_names(*, config: pm.PacmanConfig | None = None, worker: Callable | None = None) -> frozenset[str]:
    """The name of every package in the repositories' lists (one cheap question; for a page that only needs to know what Arch already has)."""
    names = _ask(config or pm.read_config(), {"op": "repo_names"}, worker)["names"]
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise PackagesError("the package databases gave an answer Cygnus did not expect")
    return frozenset(names)


def installed(*, config: pm.PacmanConfig | None = None, worker: Callable | None = None) -> dict[str, Any]:
    """Every installed package: {"packages", "updates", "lists"}; each says whether it is protected and whether Cygnus manages it as an
    application. `updates` is how many have a newer version in the repositories."""
    config = config or pm.read_config()
    packages = _ask(config, {"op": "installed"}, worker)["packages"]
    managed = managed_packages()  # one read of the application list for all of them
    for p in packages:
        p["protected"] = pm.is_protected(p["name"], config)
        p["managed"] = p["name"] in managed  # a package Cygnus manages as an application: removed from its own page, never from here
        p["odd_name"] = not PKG_NAME.match(p["name"])  # a name the helper does not take (a capital letter...): pacman can remove it, Cygnus cannot
    return {"packages": packages, "updates": sum(1 for p in packages if p["update"]), "lists": lists_age(config)}


def why_not(info: dict[str, Any], config: pm.PacmanConfig) -> dict[str, str | None]:
    """Why a package cannot be installed or removed from here (None when it can), as a plain sentence each."""
    name = info["name"]
    valid = bool(PKG_NAME.match(name))
    install = remove = None
    if info.get("installed"):
        compared = info.get("version_cmp")  # the installed version against the repository's: below 0 older, 0 the same, above 0 newer
        if not info.get("repo"):
            install = "It is installed, but no repository offers it."
        elif compared is not None and compared > 0:
            install = "The installed version is newer than the one the repository offers (a build of your own, or a repository that went back)."
        elif compared is None and info.get("installed_version") == info.get("version") or compared == 0:
            install = "It is already installed."
        else:
            install = "A newer version is available: update the whole system on the Updates page."
        if not valid:
            remove = "Cygnus does not work with packages whose name looks like this."
        elif pm.is_protected(name, config):
            remove = "This is a protected system package: Cygnus never removes it."
        elif info.get("managed_as"):
            remove = _managed_reason(info["managed_as"])
        elif info.get("required_by") or info.get("required_by_more"):
            more = f" and {info['required_by_more']} more" if info.get("required_by_more") else ""
            remove = "Other packages need it: " + ", ".join(info["required_by"][:12]) + more + "."
    else:
        remove = "It is not installed."
        if not info.get("repo"):
            install = "No repository offers it."
        elif not valid:
            install = "Cygnus does not work with packages whose name looks like this."
    return {"install": install, "remove": remove}


def details(name: str, repo: str | None = None, *, config: pm.PacmanConfig | None = None,
            worker: Callable | None = None) -> dict[str, Any]:
    """One package, by exact name: its metadata, dependencies and their state, what needs it, its files (installed packages), and
    `blocked` = why it cannot be installed / removed from here, if so."""
    if not isinstance(name, str) or not name or len(name) > 200:
        raise PackagesError("the package name must be text")
    if repo is not None and not isinstance(repo, str):
        raise PackagesError("the repository must be text")
    config = config or pm.read_config()
    answer = _ask(config, {"op": "details", "name": name, "repo": repo}, worker)
    if not answer.get("found"):
        raise PackageNotFound(f"no package called {name[:60]!r} is installed or offered by your repositories")
    answer.pop("ok", None)
    answer["protected"] = pm.is_protected(answer["name"], config)
    managed = managed_packages().get(answer["name"]) if answer.get("installed") else None
    answer["managed_as"] = _clean_name(managed) if managed else None
    answer["blocked"] = why_not(answer, config)
    answer["lists"] = lists_age(config)
    return answer


def check_install(names: Any, *, config: pm.PacmanConfig | None = None, worker: Callable | None = None) -> list[str]:
    """The names to install, after checking each is offered by a repository and is not installed yet; says which and why otherwise."""
    wanted = names_checked(names)
    config = config or pm.read_config()
    problems = []
    for n in wanted:
        try:
            reason = details(n, config=config, worker=worker)["blocked"]["install"]
        except PackageNotFound:
            reason = "No repository offers it."
        if reason:
            problems.append(f"{n}: {reason}")
    if problems:
        raise PackagesError(" ".join(problems))
    return wanted


def check_removal(names: Any, *, config: pm.PacmanConfig | None = None, worker: Callable | None = None, limit: int = MAX_NAMES) -> list[str]:
    """The names to remove, after checking each is installed, not protected, not a package Cygnus manages as an application, and not
    needed by a package that stays; says which and why otherwise. Packages removed together may need each other. One question to the
    worker answers for all of them."""
    wanted = names_checked(names, limit)
    config = config or pm.read_config()
    facts = _ask(config, {"op": "removal_facts", "names": wanted}, worker)["packages"]
    managed = managed_packages()
    problems = []
    for n in wanted:
        f = facts.get(n) or {"installed": False}
        if not f["installed"]:
            problems.append(f"{n}: It is not installed.")
        elif pm.is_protected(n, config):
            problems.append(f"{n}: This is a protected system package: Cygnus never removes it.")
        elif n in managed:
            problems.append(f"{n}: " + _managed_reason(_clean_name(managed[n])))
        else:
            outside = [r for r in f["required_by"] if r not in wanted]
            if f.get("required_by_more") or outside:  # more dependents than the list holds can never all be among the ones removed too
                more = f" and {f['required_by_more']} more" if f.get("required_by_more") else ""
                problems.append(f"{n}: Other packages need it: {', '.join((outside or f['required_by'])[:12])}{more}.")
    if problems:
        raise PackagesError(" ".join(problems))
    return wanted


# The helper refuses with these; they are recognised here so that the page can say what to do instead of showing a code.
_UPDATE_FIRST = "PKG_PARTIAL_UPGRADE_RISK"
_NOT_BY_CYGNUS = "were not installed by Cygnus"


def refusal(message: str) -> str | None:
    """Which known refusal of the helper's plan this is: "update_first" (installing now would be a partial upgrade),
    "not_installed_by_cygnus" (removing a package Cygnus did not install needs an explicit confirmation), or None."""
    if _UPDATE_FIRST in message:
        return "update_first"
    if _NOT_BY_CYGNUS in message:
        return "not_installed_by_cygnus"
    return None


REFUSAL_TEXT = {
    "update_first": ("Your system has updates waiting, and installing a new package now could break it (on Arch-based systems the whole "
                     "system is updated together, never in parts). Update the system first, on the Updates page; then install this."),
    "not_installed_by_cygnus": ("Cygnus did not install this package. Removing a package that is part of your system can break things: "
                                "go on only if you know you want it gone."),
}
