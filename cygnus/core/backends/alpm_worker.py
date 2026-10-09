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
"""

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


OPS = {"load": op_load, "resolve": op_resolve, "conflicts": op_conflicts, "satisfy": op_satisfy, "info": op_info,
       "outdated": op_outdated, "local_conflicts": op_local_conflicts}


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
