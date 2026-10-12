"""Crash-isolated, read-only libalpm analysis worker.

Run as a separate process (``python3 -I alpm_worker.py``): it reads one JSON request
on stdin and writes one JSON response on stdout. It depends only on pyalpm and the
standard library, so it can run in isolated mode without the cygnus package.

Why a separate process: pyalpm 0.12 with libalpm 16 segfaults while converting
package-conflict errors from ``prepare()`` (architecture §2.3). A crash here must
never take down the caller.

The real databases are never written: the worker uses a private temporary dbpath
whose ``local`` and ``sync`` entries are symlinks to the system databases (the
``checkupdates`` technique), so even the lock file lives in the temporary directory.

Operations:
  resolve    dry-run transaction: {"targets": [...], "remove": [...], "sysupgrade": bool}
  conflicts  version-aware conflicts between sync packages and the installed system
  satisfy    find installed and repository providers for dependency strings
  info       metadata of named sync/local packages
  search     repository packages whose name, description, provides or groups contain every search term
  installed  every installed package (reason, size, date, repository, update, unneeded dependency)
  details    one package's dependencies (and their state), optional dependencies, what needs it, files
  vercmp     compare pairs of version strings the way libalpm does
  sort_versions       order groups of version strings newest first, the way libalpm does
  installed_versions  name -> version of every installed package
  repo_names          the sorted names of every package in the repositories' lists
  removal_facts       for named packages: installed or not, why, and what installed packages need them
"""

import functools
import json
import re
import os
import sys
import tempfile

import pyalpm


def _pkg(p, installed=None):
    d = {
        "name": p.name,
        "version": p.version,
        "arch": p.arch,
        "repo": p.db.name if p.db is not None else None,
        "download_size": getattr(p, "download_size", None),
        "installed_size": p.isize,
        "depends": list(p.depends),
        "optdepends": list(p.optdepends),
        "provides": list(p.provides),
        "conflicts": list(p.conflicts),
        "replaces": list(p.replaces),
        "filename": getattr(p, "filename", None),
        "packager": p.packager,
    }
    if installed is not None:
        d["installed_version"] = installed.version if installed else None
    return d


def _handle(req, tmp):
    dbpath = req["dbpath"]
    os.symlink(os.path.join(dbpath, "local"), os.path.join(tmp, "local"))
    os.symlink(req.get("syncdir") or os.path.join(dbpath, "sync"), os.path.join(tmp, "sync"))
    h = pyalpm.Handle(req.get("root", "/"), tmp)
    h.arch = req["arch"]
    h.logfile = "/dev/null"
    for repo in req["repos"]:
        h.register_syncdb(repo, pyalpm.SIG_DATABASE_OPTIONAL)
    return h


def _find_sync(h, target):
    """Exact name (optionally 'repo/name'), else the first provider in repository order."""
    repo = None
    if "/" in target:
        repo, target = target.split("/", 1)
    dbs = [db for db in h.get_syncdbs() if repo is None or db.name == repo]
    for db in dbs:
        pkg = db.get_pkg(target)
        if pkg is not None:
            return pkg
    for db in dbs:
        sat = pyalpm.find_satisfier(db.pkgcache, target)
        if sat is not None:
            return sat
    return None


def _error(exc):
    args = list(exc.args) + [None, None, None]
    message, errno, data = args[0], args[1], args[2]
    if isinstance(data, list):
        data = [list(x) if isinstance(x, tuple) else x for x in data]
    return {"message": str(message), "errno": errno, "data": data}


def op_resolve(h, req):
    local = h.get_localdb()
    missing = []
    t = h.init_transaction(needed=bool(req.get("needed", True)))
    try:
        for name in req.get("targets", []):
            pkg = _find_sync(h, name)
            if pkg is None:
                missing.append(name)
                continue
            t.add_pkg(pkg)
        for name in req.get("remove", []):
            pkg = local.get_pkg(name)
            if pkg is None:
                missing.append(name)
                continue
            t.remove_pkg(pkg)
        replacements = []
        if req.get("sysupgrade"):
            # pyalpm cannot answer libalpm's "replace X with Y?" question, so the dry run would silently
            # skip repository replacements that `pacman -Su --noconfirm` performs. Compute them here.
            replacements = compute_replacements(local.pkgcache, h.get_syncdbs(), pyalpm.vercmp)
            t.sysupgrade(False)
        if missing:
            return {"ok": False, "error": {"message": "target not found", "errno": None, "data": missing},
                    "kind": "target_not_found"}
        try:
            t.prepare()
        except pyalpm.error as exc:
            return {"ok": False, "error": _error(exc), "kind": "prepare_failed"}
        to_add = [_pkg(p, local.get_pkg(p.name) or False) for p in t.to_add]
        to_remove = [{"name": p.name, "version": p.version} for p in t.to_remove]
        added = {p["name"] for p in to_add}
        removed = {p["name"] for p in to_remove}
        replacers = []
        for r in replacements:
            if r["install"] not in added:
                sp = _find_sync(h, f"{r['repo']}/{r['install']}")
                if sp is not None:
                    d = _pkg(sp, False)
                    d["replaces_installed"] = r["remove"]
                    to_add.append(d)
                    replacers.append(sp)
            if r["remove"] not in removed:
                victim = local.get_pkg(r["remove"])
                to_remove.append({"name": r["remove"], "version": victim.version if victim else None,
                                  "replaced_by": r["install"]})
        if replacers:
            # libalpm never saw the replacers, so it did not resolve THEIR dependencies either: do it here, so that
            # what the user approves lists every package pacman will install.
            gone = removed | {r["remove"] for r in replacements}
            try:
                pulled = replacer_dependencies(list(local.pkgcache), list(t.to_add), gone, replacers,
                                               lambda dep: _find_sync(h, dep), pyalpm.find_satisfier)
            except UnresolvableDependency as exc:
                return {"ok": False, "error": {"message": str(exc), "errno": None, "data": [exc.dependency]},
                        "kind": "prepare_failed"}
            for pkg, needed_by in pulled:
                d = _pkg(pkg, local.get_pkg(pkg.name) or False)
                d["pulled_in_by"] = needed_by
                to_add.append(d)
        return {"ok": True, "to_add": to_add, "to_remove": to_remove}
    finally:
        t.release()


class UnresolvableDependency(Exception):
    def __init__(self, package: str, dependency: str):
        super().__init__(f"{package} needs {dependency}, which no repository provides")
        self.dependency = dependency


def replacer_dependencies(local_pkgs, added, removed, replacers, find_provider, satisfier) -> list[tuple]:
    """The packages that installing `replacers` pulls in on top of what the transaction already holds, as
    (package, needed_by) pairs, in the order pacman would resolve them. `removed` are names that will be gone."""
    state = {p.name: p for p in local_pkgs if p.name not in removed}
    state.update({p.name: p for p in added})
    state.update({p.name: p for p in replacers})
    pulled: list[tuple] = []
    queue = list(replacers)
    while queue:
        pkg = queue.pop(0)
        for dep in pkg.depends:
            if satisfier(list(state.values()), dep) is not None:
                continue
            provider = find_provider(dep)
            if provider is None:
                raise UnresolvableDependency(pkg.name, dep)
            if any(provider.name == q.name for q, _ in pulled):
                continue  # already pulled in, and still not enough: pacman reports that when it runs
            state[provider.name] = provider
            pulled.append((provider, pkg.name))
            queue.append(provider)
    return pulled


_DEP = re.compile(r"^([^<>=]+)(?:(<=|>=|<|>|=)(.+))?$")


def _literal_match(pkg, dep: str, vercmp) -> bool:
    """libalpm's _alpm_depcmp_literal: name and version of the package itself, never its provides."""
    m = _DEP.match(dep.strip())
    if not m or m.group(1) != pkg.name:
        return False
    if not m.group(2):
        return True
    c = vercmp(pkg.version, m.group(3))
    return {"=": c == 0, "<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0}[m.group(2)]


def compute_replacements(local_pkgs, sync_dbs, vercmp) -> list[dict]:
    """The replacements `pacman -Su` performs (libalpm sync.c: check_replacers / alpm_sync_sysupgrade):
    for each installed package, repositories are searched in order, and the first one that either
    replaces it (literal match) or carries it under its own name decides."""
    replaces_by_name = []
    for db in sync_dbs:
        index: dict[str, list] = {}
        for sp in db.pkgcache:
            for dep in sp.replaces:
                m = _DEP.match(dep.strip())
                if m:
                    index.setdefault(m.group(1), []).append((sp, dep))
        replaces_by_name.append((db, index))
    out, seen = [], set()
    for lp in local_pkgs:
        for db, index in replaces_by_name:
            replacers = [sp for sp, dep in index.get(lp.name, []) if sp.name != lp.name and _literal_match(lp, dep, vercmp)]
            if replacers:
                if lp.name not in seen:
                    out.append({"install": replacers[0].name, "remove": lp.name, "repo": db.name})
                    seen.add(lp.name)
                break
            if db.get_pkg(lp.name) is not None:
                break
    return out


def op_conflicts(h, req):
    """Version-aware conflict detection without libalpm's (crashing) conflict path."""
    local = h.get_localdb()
    installed = list(local.pkgcache)
    new_pkgs = [p for p in (_find_sync(h, n) for n in req["names"]) if p is not None]
    new_names = {p.name for p in new_pkgs}
    removing = set(req.get("remove", []))
    candidates = [p for p in installed if p.name not in new_names and p.name not in removing]
    found = []
    for new in new_pkgs:
        for dep in new.conflicts:
            for inst in candidates:  # every installed package that satisfies the rule, not just the first
                if pyalpm.find_satisfier([inst], dep) is not None:
                    found.append({"new": new.name, "installed": inst.name, "rule": dep, "declared_by": new.name})
        for inst in candidates:
            for dep in inst.conflicts:
                if pyalpm.find_satisfier([new], dep) is not None:
                    found.append({"new": new.name, "installed": inst.name, "rule": dep, "declared_by": inst.name})
    # Conflicts between two packages that would both be newly installed.
    for i, a in enumerate(new_pkgs):
        for b in new_pkgs[i + 1:]:
            for x, y in ((a, b), (b, a)):
                for dep in x.conflicts:
                    if pyalpm.find_satisfier([y], dep) is not None:
                        found.append({"new": x.name, "installed": None, "other_new": y.name, "rule": dep,
                                      "declared_by": x.name})
    unique = {json.dumps(f, sort_keys=True): f for f in found}
    return {"ok": True, "conflicts": list(unique.values())}


def op_outdated(h, req):
    """Installed packages whose sync version is newer (what `pacman -Qu` reports)."""
    out = []
    for p in h.get_localdb().pkgcache:
        for db in h.get_syncdbs():
            sp = db.get_pkg(p.name)
            if sp is not None:
                if pyalpm.vercmp(sp.version, p.version) > 0:
                    out.append({"name": p.name, "installed": p.version, "available": sp.version, "repo": db.name})
                break
    return {"ok": True, "outdated": out}


def op_local_conflicts(h, req):
    """Conflicts between a local package (described by its metadata) and the installed system."""
    local = h.get_localdb()
    installed = [p for p in local.pkgcache if p.name != req["name"]]
    found = []
    for dep in req.get("conflicts", []):
        for inst in installed:
            if pyalpm.find_satisfier([inst], dep) is not None:
                found.append({"installed": inst.name, "rule": dep, "declared_by": req["name"]})
    provided = [req["name"]] + [p.split("=")[0] for p in req.get("provides", [])]
    for inst in installed:
        for dep in inst.conflicts:
            name = dep.split("<")[0].split(">")[0].split("=")[0]
            if name in provided:
                found.append({"installed": inst.name, "rule": dep, "declared_by": inst.name})
    current = local.get_pkg(req["name"])
    cmp = pyalpm.vercmp(req["version"], current.version) if current is not None and req.get("version") else None
    return {"ok": True, "conflicts": found, "installed_version": current.version if current else None,
            "version_cmp": cmp}


def op_satisfy(h, req):
    local = h.get_localdb()
    installed = list(local.pkgcache)
    out = {}
    for dep in req["deps"]:
        inst = pyalpm.find_satisfier(installed, dep)
        repo_hit = None
        for db in h.get_syncdbs():
            sat = pyalpm.find_satisfier(db.pkgcache, dep)
            if sat is not None:
                repo_hit = {"name": sat.name, "version": sat.version, "repo": db.name}
                break
        out[dep] = {"installed": inst.name if inst else None, "repo": repo_hit}
    return {"ok": True, "satisfiers": out}


def op_info(h, req):
    local = h.get_localdb()
    out = {}
    for name in req["names"]:
        sync = _find_sync(h, name)
        loc = local.get_pkg(name)
        out[name] = {
            "sync": _pkg(sync) if sync else None,
            "local": _pkg(loc) if loc else None,
            "local_reason": None if loc is None else ("explicit" if loc.reason == 0 else "dependency"),
        }
    return {"ok": True, "packages": out}


def op_load(h, req):
    """What libalpm itself reads from package files: the facts pacman will act on."""
    out = []
    for path in req["paths"]:
        p = h.load_pkg(path)
        out.append({"path": path, "name": p.name, "version": p.version, "arch": p.arch, "desc": p.desc or "",
                    "depends": list(p.depends), "conflicts": list(p.conflicts), "provides": list(p.provides),
                    "replaces": list(p.replaces), "has_scriptlet": bool(p.has_scriptlet)})
    return {"ok": True, "packages": out}


# -- browsing: search, the installed list and one package's details (read-only, bounded) ------------------------------------------

SEARCH_LIMIT = 200  # results of one search (the answer says how many matched in all)
MAX_TERMS = 8
MAX_TERM_LENGTH = 100
LIST_LIMIT = 200  # entries of any one list in a details answer
FILES_LIMIT = 3000  # file names of an installed package (linux-firmware has tens of thousands)
TEXT_LIMIT = 1000  # one description, URL or dependency string
_VERSION_OPERATORS = re.compile(r"[<>=]")


def _text(value, limit=None):
    """Text from a package database, safe to show anywhere: cut to `limit`, and every character that is not printable (an escape
    sequence a terminal would obey, a direction mark that moves text around) replaced by a space."""
    limit = TEXT_LIMIT if limit is None else limit
    text = "" if value is None else str(value)
    text = "".join(c if c.isprintable() else " " for c in text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _capped(items, limit=None):
    """The first `limit` entries as text (LIST_LIMIT by default), and how many more there were."""
    limit = LIST_LIMIT if limit is None else limit
    items = list(items)
    return [_text(x) for x in items[:limit]], max(0, len(items) - limit)


def _provided_name(dep):
    return _VERSION_OPERATORS.split(dep, 1)[0]


def _reason(pkg):
    return "explicit" if pkg.reason == 0 else "dependency"


def search_rank(name, desc, provides, groups, terms):
    """How well a package matches every search term (smaller is better), or None when some term is nowhere in it. Terms are
    plain text, never patterns (libalpm's own search takes regular expressions, which a search box must not hand over)."""
    n, d = name.lower(), (desc or "").lower()
    provided = [_provided_name(x).lower() for x in provides]
    grouped = [g.lower() for g in groups]
    for t in terms:
        if not (t in n or t in d or any(t in x for x in provided) or any(t in g for g in grouped)):
            return None
    if len(terms) == 1 and n == terms[0]:
        return 0
    if all(t in n for t in terms):
        return 1 if n.startswith(terms[0]) else 2
    if all(t in n or any(t in x for x in provided) for t in terms):
        return 3
    return 4


def search_packages(sync_dbs, local, terms, vercmp, limit=SEARCH_LIMIT):
    """Repository packages matching every term, best first, one entry per name (the first repository that has it wins, as for
    `pacman -S`)."""
    seen, hits = set(), []
    for db in sync_dbs:
        for p in db.pkgcache:
            if p.name in seen:
                continue
            seen.add(p.name)
            rank = search_rank(p.name, p.desc, p.provides, p.groups, terms)
            if rank is not None:
                hits.append((rank, len(p.name), p.name, db.name, p))
    hits.sort(key=lambda h: h[:3])
    out = []
    for _, _, _, repo, p in hits[:limit]:
        have = local.get_pkg(p.name)
        out.append({"name": _text(p.name, 200), "version": _text(p.version, 200), "repo": _text(repo, 100), "desc": _text(p.desc, 300),
                    "installed_version": _text(have.version, 200) if have else None,
                    "reason": _reason(have) if have else None,
                    "update": bool(have and vercmp(p.version, have.version) > 0)})
    return {"results": out, "total": len(hits), "truncated": len(hits) > limit}


def installed_packages(local, sync_dbs, vercmp):
    """Every installed package. `unneeded` is what `pacman -Qdt` lists: installed as a dependency and needed by no installed package
    any more (a package that is only an optional dependency of another counts as unneeded). `repo` is None for a package no
    repository has (installed from a file or the AUR)."""
    out = []
    for p in local.pkgcache:
        sync = next((sp for sp in (db.get_pkg(p.name) for db in sync_dbs) if sp is not None), None)
        repo = next((db.name for db in sync_dbs if db.get_pkg(p.name) is not None), None)
        entry = {"name": _text(p.name, 200), "version": _text(p.version, 200), "desc": _text(p.desc, 300), "reason": _reason(p),
                 "size": p.isize, "installed": p.installdate, "repo": _text(repo, 100) if repo else None,
                 "update": bool(sync is not None and vercmp(sync.version, p.version) > 0)}
        if entry["update"]:
            entry["update_to"] = _text(sync.version, 200)
        if p.reason != 0:
            entry["unneeded"] = not p.compute_requiredby()
        out.append(entry)
    out.sort(key=lambda e: e["name"])
    return out


def _dependency_state(dep, installed, sync_packages, satisfier):
    have = satisfier(installed, dep)
    if have is not None:
        return {"dep": _text(dep), "state": "installed", "by": _text(have.name, 200)}
    for repo, pkgs in sync_packages():
        offered = satisfier(pkgs, dep)
        if offered is not None:
            return {"dep": _text(dep), "state": "available", "by": _text(offered.name, 200), "repo": _text(repo, 100)}
    return {"dep": _text(dep), "state": "missing"}


def package_details(name, repo, local, sync_dbs, satisfier, vercmp=None):
    """What the details panel shows about one package, by exact name: the repository's entry (if it has one) and the installed one
    (if there is one). Lists are capped and say how much was left out."""
    sync = None
    for db in sync_dbs:
        if repo is None or db.name == repo:
            sync = db.get_pkg(name)
            if sync is not None:
                break
    have = local.get_pkg(name)
    if sync is None and have is None:
        return {"found": False}
    p = sync or have  # the metadata of the repository's entry when there is one: it is what an install or update would bring
    installed = list(local.pkgcache)
    cache: list = []

    def sync_packages():
        if not cache:
            cache.extend((db.name, list(db.pkgcache)) for db in sync_dbs)
        return cache

    depends = [_dependency_state(d, installed, sync_packages, satisfier) for d in list(p.depends)[:LIST_LIMIT]]
    optional = []
    for entry in list(p.optdepends)[:LIST_LIMIT]:
        dep, _, why = entry.partition(": ")
        optional.append({"dep": _text(dep), "why": _text(why),
                         "installed": satisfier(installed, dep) is not None})
    out = {
        "found": True, "name": _text(p.name, 200), "version": _text(p.version, 200), "arch": _text(p.arch, 50), "desc": _text(p.desc),
        "url": _text(p.url),
        "licenses": _capped(p.licenses)[0], "groups": _capped(p.groups)[0], "packager": _text(p.packager, 200),
        "build_date": p.builddate, "repo": _text(sync.db.name, 100) if sync is not None and sync.db is not None else None,
        "package_size": getattr(sync, "size", None) if sync is not None else None,
        "installed_size": p.isize, "depends": depends, "depends_more": max(0, len(p.depends) - LIST_LIMIT),
        "optdepends": optional, "optdepends_more": max(0, len(p.optdepends) - LIST_LIMIT),
        "installed": have is not None, "installed_version": _text(have.version, 200) if have else None,
        "provides": _capped(p.provides)[0], "conflicts": _capped(p.conflicts)[0], "replaces": _capped(p.replaces)[0],
    }
    out["depends_missing"] = sum(1 for d in depends if d["state"] == "missing")
    if have is not None and sync is not None and vercmp is not None:
        out["version_cmp"] = vercmp(have.version, sync.version)  # the installed version against the repository's: below 0 older, 0 the same, above 0 newer
    if have is not None:
        required, required_more = _capped(have.compute_requiredby())
        optional_for, optional_more = _capped(have.compute_optionalfor())
        files = [f[0] for f in have.files]
        out.update({"reason": _reason(have), "install_date": have.installdate, "required_by": required,
                    "required_by_more": required_more, "optional_for": optional_for, "optional_for_more": optional_more,
                    "files": [_text(f, 4096) for f in files[:FILES_LIMIT]], "files_total": len(files),
                    "has_install_script": bool(have.has_scriptlet)})
    return out


def op_search(h, req):
    given = req.get("terms")
    if not isinstance(given, list) or not all(isinstance(t, str) for t in given):
        return {"ok": False, "error": {"message": "terms must be a list of text"}}
    terms = [t.lower() for t in given if t.strip()]
    if not terms or len(terms) > MAX_TERMS or any(len(t) > MAX_TERM_LENGTH for t in terms):
        return {"ok": False, "error": {"message": f"search for 1 to {MAX_TERMS} words of at most {MAX_TERM_LENGTH} characters"}}
    return {"ok": True, **search_packages(h.get_syncdbs(), h.get_localdb(), terms, pyalpm.vercmp)}


def op_installed(h, req):
    return {"ok": True, "packages": installed_packages(h.get_localdb(), h.get_syncdbs(), pyalpm.vercmp)}


MAX_PAIRS = 5000


def op_vercmp(h, req):
    pairs = req.get("pairs")
    if (not isinstance(pairs, list) or len(pairs) > MAX_PAIRS
            or not all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, str) for v in p) for p in pairs)):
        return {"ok": False, "error": {"message": f"pairs must be at most {MAX_PAIRS} pairs of version texts"}}
    return {"ok": True, "results": [pyalpm.vercmp(a, b) for a, b in pairs]}


def op_details(h, req):
    name, repo = req.get("name"), req.get("repo")
    if not isinstance(name, str) or not name or not (repo is None or isinstance(repo, str)):
        return {"ok": False, "error": {"message": "name must be text"}}
    return {"ok": True, **package_details(name, repo, h.get_localdb(), h.get_syncdbs(), pyalpm.find_satisfier, pyalpm.vercmp)}


MAX_VERSIONS = 50_000


def op_sort_versions(h, req):
    groups = req.get("groups")
    if (not isinstance(groups, list) or not all(isinstance(g, list) and all(isinstance(v, str) for v in g) for g in groups)
            or sum(len(g) for g in groups) > MAX_VERSIONS):
        return {"ok": False, "error": {"message": f"groups must be lists of version texts, at most {MAX_VERSIONS} in all"}}
    key = functools.cmp_to_key(pyalpm.vercmp)
    return {"ok": True, "orders": [sorted(g, key=key, reverse=True) for g in groups]}


def op_installed_versions(h, req):
    return {"ok": True, "versions": {p.name: p.version for p in h.get_localdb().pkgcache}}


def op_repo_names(h, req):
    return {"ok": True, "names": sorted({p.name for db in h.get_syncdbs() for p in db.pkgcache})}


MAX_FACT_NAMES = 500


def op_removal_facts(h, req):
    names = req.get("names")
    if not isinstance(names, list) or len(names) > MAX_FACT_NAMES or not all(isinstance(n, str) for n in names):
        return {"ok": False, "error": {"message": f"names must be at most {MAX_FACT_NAMES} texts"}}
    local = h.get_localdb()
    out = {}
    for name in names:
        p = local.get_pkg(name)
        if p is None:
            out[name] = {"installed": False}
            continue
        needed_by, more = _capped(p.compute_requiredby())
        out[name] = {"installed": True, "reason": _reason(p), "required_by": needed_by, "required_by_more": more}
    return {"ok": True, "packages": out}


OPS = {"load": op_load, "resolve": op_resolve, "conflicts": op_conflicts, "satisfy": op_satisfy, "info": op_info,
       "outdated": op_outdated, "local_conflicts": op_local_conflicts, "search": op_search, "installed": op_installed,
       "details": op_details, "vercmp": op_vercmp, "sort_versions": op_sort_versions, "installed_versions": op_installed_versions,
       "removal_facts": op_removal_facts, "repo_names": op_repo_names}


def main():
    req = json.load(sys.stdin)
    fn = OPS.get(req.get("op"))
    if fn is None:
        json.dump({"ok": False, "error": {"message": f"unknown op {req.get('op')!r}"}}, sys.stdout)
        return 2
    with tempfile.TemporaryDirectory(prefix="cygnus-alpm-") as tmp:
        h = _handle(req, tmp)
        result = fn(h, req)
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
