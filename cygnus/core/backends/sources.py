"""Find where an application can come from: repositories, AUR, Flathub, curated manifests.

Used by the format advisor ("prefer a native or vendor-supported format") and by the
DEB/RPM policy engine. Network lookups are best-effort: a failing source is reported, not fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from cygnus.core.backends import aur
from cygnus.core.manifest.catalog import LoadedManifest
from cygnus.core.util import http

FLATHUB_SEARCH = "https://flathub.org/api/v2/search"


@dataclass(slots=True, kw_only=True)
class Alternative:
    kind: str  # installed | manifest-source | manifest-component | repo | aur | flathub
    name: str
    label: str
    vendor_supported: bool
    relocatable: bool
    note: str = ""
    facts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "label": self.label, "vendor_supported": self.vendor_supported,
                "relocatable": self.relocatable, "note": self.note, **self.facts}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def from_manifest(loaded: LoadedManifest) -> list[Alternative]:
    out = []
    for src in loaded.manifest.sources:
        relocatable = src.format in ("appimage", "flatpak-bundle", "flatpak-remote", "tarball")
        out.append(Alternative(
            kind="manifest-source", name=src.id, label=src.label or src.format,
            vendor_supported=src.vendor_supported and loaded.can_drive_actions, relocatable=relocatable,
            note=src.notes or "",
            facts={"format": src.format, "recommended_when": src.recommended_when,
                   "known_issues": src.known_issues, "trust": loaded.trust_level}))
    return out


def from_manifest_components(package: str, catalog: dict[str, LoadedManifest]) -> list[Alternative]:
    """Vendor packages that trusted manifests install as a component of an application, e.g. the
    WhatPulse pcap service, matched by the package name their own health probe checks for."""
    out = []
    for loaded in catalog.values():
        if not loaded.can_drive_actions:
            continue
        for comp in loaded.manifest.components:
            if not any(p.probe == "pacman_installed" and p.name == package for p in comp.verify):
                continue
            action = comp.platform_actions.get("arch") or comp.action
            if action is None or action.kind not in ("pacman.install_local", "pacman.install_repo"):
                continue
            app = loaded.manifest.application
            facts = {"component": comp.id, "app_id": app.id, "action": action.kind, "trust": loaded.trust_level}
            if action.kind == "pacman.install_local":
                facts.update(url=action.url, sha256=action.sha256)
            out.append(Alternative(
                kind="manifest-component", name=package,
                label=f"{app.vendor.name}'s Arch package of {package}",
                vendor_supported=True, relocatable=False,
                note=f"Published by the vendor for Arch Linux; Cygnus installs it as the '{comp.name}' component "
                     f"of {app.name}.", facts=facts))
    return out


def find(name: str, *, app_name: str | None = None, appstream_id: str | None = None,
         repo_info: Callable[[list[str]], dict[str, Any]] | None = None,
         aur_info: Callable[[list[str]], dict[str, Any]] | None = None,
         flathub_search: Callable[[str], list[dict[str, Any]]] | None = None,
         manifest: LoadedManifest | None = None, catalog: dict[str, LoadedManifest] | None = None,
         ) -> tuple[list[Alternative], list[str]]:
    """Return (alternatives, warnings). `repo_info` is the pacman worker's 'info' op."""
    found: list[Alternative] = []
    warnings: list[str] = []
    if manifest is not None:
        found += from_manifest(manifest)
    if catalog is not None:
        found += from_manifest_components(name, catalog)
    candidates = list(dict.fromkeys([name, f"{name}-bin"]))

    if repo_info is not None:
        try:
            info = repo_info(candidates)
            for n in candidates:
                local = (info.get(n) or {}).get("local")
                if local:
                    found.append(Alternative(kind="installed", name=local["name"],
                                             label=f"{local['name']} {local['version']}, already installed",
                                             vendor_supported=False, relocatable=False,
                                             note="An Arch package of it is already installed; this file is not needed.",
                                             facts={"version": local["version"]}))
                sync = (info.get(n) or {}).get("sync")
                if sync:
                    found.append(Alternative(kind="repo", name=sync["name"],
                                             label=f"{sync['name']} from your {sync['repo']} repository",
                                             vendor_supported=False, relocatable=False,
                                             note="Official distribution package; updated with your system.",
                                             facts={"repo": sync["repo"], "version": sync["version"]}))
        except Exception as exc:  # noqa: BLE001 - best-effort
            warnings.append(f"repository lookup failed: {exc}")

    try:
        aur_found = (aur_info or aur.info)(candidates)
        for n in candidates:
            pkg = aur_found.get(n)
            if pkg:
                stale = " (flagged out of date)" if pkg.get("OutOfDate") else ""
                found.append(Alternative(kind="aur", name=n, label=f"{n} from the AUR{stale}",
                                         vendor_supported=False, relocatable=False,
                                         note="Community build script; reviewed before building.",
                                         facts={"version": pkg.get("Version"), "votes": pkg.get("NumVotes"),
                                                "out_of_date": bool(pkg.get("OutOfDate"))}))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"AUR lookup failed: {exc}")

    search = flathub_search or _flathub_search
    try:
        hits = search(app_name or name)
        target = _norm(app_name or name)

        def score(h: dict[str, Any]) -> tuple[int, int] | None:
            """Best match first: the exact AppStream id, then a verified publisher; a mere name
            match from an unverified lookalike never beats either."""
            app_id = h.get("app_id") or ""
            exact = bool(appstream_id) and app_id == appstream_id
            named = _norm(h.get("name", "")) == target or _norm(app_id.rsplit(".", 1)[-1]) == target
            if not (exact or named):
                return None
            return (0 if exact else 1, 0 if h.get("verification_verified") else 1)

        scored = [(sc, i, h) for i, h in enumerate(hits[:20]) if (sc := score(h)) is not None]
        if scored:
            _, _, h = min(scored, key=lambda t: (t[0], t[1]))
            app_id = h.get("app_id") or ""
            verified = bool(h.get("verification_verified"))
            found.append(Alternative(kind="flathub", name=app_id, label=f"{h.get('name', app_id)} on Flathub"
                                     + (" (verified publisher)" if verified else ""),
                                     vendor_supported=verified, relocatable=True,
                                     note="Sandboxed; can be stored on your HDD.",
                                     facts={"verified": verified}))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Flathub lookup failed: {exc}")
    return rank(found), warnings


def _flathub_search(query: str) -> list[dict[str, Any]]:
    return http.post_json(FLATHUB_SEARCH, {"query": query}).get("hits", [])


def rank(alts: list[Alternative], *, want_relocatable: bool = False) -> list[Alternative]:
    """Deterministic preference: already installed > vendor-supported > native repo > Flathub > AUR,
    relocatable first if wanted."""
    order = {"manifest-source": 0, "manifest-component": 0, "repo": 1, "flathub": 2, "aur": 3}

    trust_rank = {"curated": 0, "vendor-signed": 0}

    def key(a: Alternative) -> tuple:
        untrusted_manifest = (a.kind in ("manifest-source", "manifest-component")
                              and trust_rank.get(a.facts.get("trust"), 1) != 0)
        return (a.kind != "installed", untrusted_manifest, not a.vendor_supported,
                (not a.relocatable) if want_relocatable else 0, order.get(a.kind, 9),
                bool(a.facts.get("out_of_date")))

    return sorted(alts, key=key)
