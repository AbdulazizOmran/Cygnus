"""AUR: RPC v5 client and dependency-graph resolution (architecture §10.2). Read-only."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

from cygnus.core.recovery.model import Action, Issue, IssueSeverity, Resolution, SafetyClass, explain_only
from cygnus.core.util import http

RPC = "https://aur.archlinux.org/rpc/v5"
_DEP_NAME = re.compile(r"^([^<>=:]+)")


def dep_name(dep: str) -> str:
    m = _DEP_NAME.match(dep.strip())
    return m.group(1).strip() if m else dep.strip()


def info(names: list[str], fetch_json: Callable[..., Any] = http.get_json) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(names), 100):  # keep URLs short
        chunk = names[i : i + 100]
        url = RPC + "/info?" + "&".join(f"arg[]={quote(n)}" for n in chunk)
        data = fetch_json(url)
        if data.get("type") == "error":
            raise http.HttpError(f"AUR error: {data.get('error')}")
        out.update({r["Name"]: r for r in data.get("results", [])})
    return out


def search(term: str, by: str = "name", fetch_json: Callable[..., Any] = http.get_json) -> list[dict[str, Any]]:
    data = fetch_json(f"{RPC}/search/{quote(term)}?by={by}")
    return data.get("results", []) if data.get("type") != "error" else []


def provider(name: str, fetch_json: Callable[..., Any] = http.get_json) -> str | None:
    """Most popular AUR package that provides `name` (deterministic: popularity, then name)."""
    results = search(name, by="provides", fetch_json=fetch_json)
    if not results:
        return None
    return sorted(results, key=lambda r: (-(r.get("Popularity") or 0), r.get("Name", "")))[0]["Name"]


@dataclass(slots=True, kw_only=True)
class AurPlan:
    targets: list[str]
    build_order: list[str] = field(default_factory=list)  # package bases, dependencies first
    packages: dict[str, dict[str, Any]] = field(default_factory=dict)
    repo_deps: list[str] = field(default_factory=list)
    repo_makedeps: list[str] = field(default_factory=list)
    unresolvable: list[dict[str, str]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


def resolve(targets: list[str], *, satisfy: Callable[[list[str]], dict[str, dict[str, Any]]],
            fetch_info: Callable[[list[str]], dict[str, dict[str, Any]]] = info, include_check: bool = True,
            now: float | None = None,
            find_provider: Callable[[str], str | None] | None = None) -> AurPlan:
    """Resolve AUR targets into a build order.

    satisfy(deps) -> {dep: {"installed": name|None, "repo": {...}|None}} (pacman worker 'satisfy').
    """
    plan = AurPlan(targets=list(targets))
    pending = list(targets)
    seen: set[str] = set()
    edges: dict[str, set[str]] = {}
    requested_by: dict[str, str] = {}
    repo_deps: set[str] = set()
    repo_make: set[str] = set()
    alias: dict[str, str] = {}  # dependency name -> the AUR package that provides it
    while pending:
        batch = [n for n in dict.fromkeys(pending) if n not in seen]
        pending = []
        if not batch:
            break
        seen.update(batch)
        found = fetch_info(batch)
        for name in batch:
            if name not in found:
                provider = find_provider(name) if find_provider else None
                if provider:
                    # e.g. 'libbar' satisfied by AUR 'libbar-git' through its provides (which may be
                    # one of the requested packages itself)
                    alias[name] = provider
                    requested_by.setdefault(provider, requested_by.get(name, "?"))
                    for deps in edges.values():
                        if name in deps:
                            deps.discard(name)
                            deps.add(provider)
                    if provider not in seen:
                        pending.append(provider)
                    continue
                plan.unresolvable.append({"dependency": name,
                                          "needed_by": "requested" if name in targets else requested_by.get(name, "?")})
        for name, pkg in found.items():
            plan.packages[name] = pkg
            runtime = list(pkg.get("Depends") or [])
            build = list(pkg.get("MakeDepends") or []) + (list(pkg.get("CheckDepends") or []) if include_check else [])
            deps = runtime + build
            sat = satisfy(deps) if deps else {}
            edges.setdefault(name, set())
            for dep in deps:
                s = sat.get(dep, {})
                if s.get("installed"):
                    continue
                if s.get("repo"):
                    (repo_deps if dep in runtime else repo_make).add(s["repo"]["name"])
                    continue
                dname = alias.get(dep_name(dep), dep_name(dep))
                edges[name].add(dname)
                requested_by.setdefault(dname, name)
                if dname not in seen:
                    pending.append(dname)
    # Unresolvable dependency names that are not AUR packages either.
    known = set(plan.packages)
    for name, deps in edges.items():
        for d in deps:
            if d not in known and not any(u["dependency"] == d for u in plan.unresolvable):
                plan.unresolvable.append({"dependency": d, "needed_by": name})
        edges[name] = {d for d in deps if d in known}
    plan.repo_deps = sorted(repo_deps)
    plan.repo_makedeps = sorted(repo_make - repo_deps)
    # AUR git repositories (and builds) are per package *base*: collapse split packages.
    base_of = {n: (p.get("PackageBase") or n) for n, p in plan.packages.items()}
    base_edges: dict[str, set[str]] = {}
    for name, deps in edges.items():
        b = base_of.get(name, name)
        base_edges.setdefault(b, set()).update(base_of.get(d, d) for d in deps if base_of.get(d, d) != b)
    plan.build_order = _toposort(base_edges, plan.issues)
    _quality_issues(plan, now or time.time())
    if plan.unresolvable:
        plan.issues.append(Issue(
            code="PKG_DEP_UNRESOLVABLE", severity=IssueSeverity.BLOCKER,
            title="Some dependencies exist neither in your repositories nor in the AUR",
            explanation="; ".join(f"{u['needed_by']} needs {u['dependency']}" for u in plan.unresolvable),
            facts={"unresolvable": plan.unresolvable},
            resolutions=[explain_only("explain", "Cannot build", "The package cannot be built without them.")]))
    return plan


def _toposort(edges: dict[str, set[str]], issues: list[Issue]) -> list[str]:
    order, state = [], {}

    def visit(n: str, stack: list[str]) -> None:
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            cycle = stack[stack.index(n):] + [n]
            issues.append(Issue(code="AUR_DEPENDENCY_CYCLE", severity=IssueSeverity.BLOCKER,
                                title="Circular AUR dependencies", explanation=" → ".join(cycle),
                                resolutions=[explain_only("explain", "Cannot build", "The AUR packages depend on "
                                                                                       "each other in a loop.")]))
            return
        state[n] = 1
        for d in sorted(edges.get(n, ())):
            visit(d, stack + [n])
        state[n] = 2
        order.append(n)

    for n in sorted(edges):
        visit(n, [])
    return order


def _quality_issues(plan: AurPlan, now: float) -> None:
    for name, pkg in plan.packages.items():
        if pkg.get("OutOfDate"):
            since = time.strftime("%Y-%m-%d", time.gmtime(pkg["OutOfDate"]))
            plan.issues.append(Issue(code="AUR_OUT_OF_DATE", severity=IssueSeverity.NOTICE,
                                     title=f"{name} is flagged out of date (since {since})",
                                     explanation="The AUR maintainer has not yet updated it to the latest "
                                                 "upstream version.", facts={"package": name}))
        if not pkg.get("Maintainer"):
            plan.issues.append(Issue(code="AUR_ORPHANED", severity=IssueSeverity.NOTICE,
                                     title=f"{name} has no maintainer",
                                     explanation="Orphaned AUR packages receive no updates or fixes.",
                                     facts={"package": name}))
        if pkg.get("LastModified") and now - pkg["LastModified"] < 2 * 86400:
            plan.issues.append(Issue(code="AUR_RECENTLY_CHANGED", severity=IssueSeverity.NOTICE,
                                     title=f"{name} was changed in the last two days",
                                     explanation="Review the build script carefully: recent changes have had "
                                                 "little community scrutiny.", facts={"package": name}))
    plan.issues.append(Issue(
        code="AUR_REVIEW_REQUIRED", severity=IssueSeverity.NOTICE,
        title="AUR packages are community-made build scripts",
        explanation="Each build script (PKGBUILD) runs on your computer and the result is installed as root. "
                    "Cygnus will show every script for review before building.",
        resolutions=[Resolution(id="review", title="Review the build scripts", safety=SafetyClass.APPROVAL,
                                explanation="Required before building.", recommended=True,
                                actions=[Action(kind="aur.review", params={"pkgbases": plan.build_order})])]))
