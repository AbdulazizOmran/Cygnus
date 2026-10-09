"""Find existing installations of an application (for health checks and adopting existing apps).

Read-only. AppImages are found in registered application folders, in running processes'
APPIMAGE environment (own processes only), and in autostart / menu entries that point at them.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from cygnus.core.desktop import entry as desktop_entry
from cygnus.core.detect import detect_file
from cygnus.core.errors import CygnusError
from cygnus.core.manifest.schema import Manifest
from cygnus.core.models import PackageFormat
from cygnus.core.recovery.model import Issue, IssueSeverity, Resolution, SafetyClass
from cygnus.core.util import proc


@dataclass(slots=True, kw_only=True)
class FoundInstall:
    format: str  # appimage | flatpak | pacman
    source_id: str | None  # matching manifest source, if known
    path: str | None = None
    flatpak_ref: str | None = None
    installation: str | None = None
    package: str | None = None
    version: str | None = None
    running: bool = False
    referenced_by: list[str] = field(default_factory=list)  # autostart / menu entries pointing at it
    facts: dict = field(default_factory=dict)


def _exec_target(exec_line: str) -> str | None:
    try:
        argv = shlex.split(exec_line)
    except ValueError:
        return None
    return argv[0] if argv else None


def running_appimages() -> dict[str, list[int]]:
    """APPIMAGE paths of running processes you own -> PIDs."""
    out: dict[str, list[int]] = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            data = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue
        for var in data.split(b"\0"):
            if var.startswith(b"APPIMAGE="):
                out.setdefault(var[9:].decode("utf-8", "replace"), []).append(int(pid))
    return out


def entry_references(dirs: Iterable[Path]) -> dict[str, list[str]]:
    """Map executable paths named in desktop/autostart entries -> the entry files naming them."""
    refs: dict[str, list[str]] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.desktop"):
            try:
                de = desktop_entry.parse(f.read_text(errors="replace")[:262144])
            except OSError:
                continue
            target = _exec_target(de.get("Exec") or "")
            if target and target.startswith("/"):
                refs.setdefault(target, []).append(str(f))
    return refs


def _appimage_candidates(app_dirs: Iterable[Path], running: dict[str, list[int]],
                         refs: dict[str, list[str]]) -> set[str]:
    paths = set(running) | {p for p in refs if p.lower().endswith(".appimage")}
    for d in app_dirs:
        if not d.is_dir():
            continue
        for p in list(d.glob("*.AppImage")) + list(d.glob("*.appimage")) + list(d.glob("*/*.AppImage")):
            paths.add(str(p))
    return {p for p in paths if os.path.isfile(p)}


def offline_installs(manifest: Manifest, registry, cands=None) -> list[FoundInstall]:
    """AppImages Cygnus manages for this application whose storage location is not connected right now. They
    cannot be found by looking in folders, but they are not missing: they are offline (spec 6.4)."""
    from cygnus.core.registry import db as regdb
    from cygnus.core.storage import locations

    place = {loc.id: loc for loc in registry.list_locations()}
    found = []
    for row in regdb.list_installations(registry):
        loc = place.get(row["location_id"])
        if row["format"] != "appimage" or row["app_id"] != manifest.application.id or loc is None:
            continue
        resolved = locations.resolve(loc, cands)
        if not resolved.online:
            found.append(FoundInstall(format="appimage", source_id=None, path=row["source"].get("path"),
                                      version=row["version"],
                                      facts={"offline": True, "location": loc.label, "reason": resolved.reason}))
    return found


def find_installs(manifest: Manifest, *, app_dirs: Iterable[Path] = (), flatpak_env=None) -> list[FoundInstall]:
    app = manifest.application
    found: list[FoundInstall] = []
    # Flatpak
    if flatpak_env is not None and app.flatpak_ids:
        for inst, ref in flatpak_env.installed_refs():
            if ref.get_name() in app.flatpak_ids and ref.format_ref().startswith("app/"):
                src = next((s.id for s in manifest.sources if s.format.startswith("flatpak")), None)
                found.append(FoundInstall(format="flatpak", source_id=src, flatpak_ref=ref.format_ref(),
                                          installation=flatpak_env.inst_id(inst), version=ref.get_appdata_version(),
                                          facts={"origin": ref.get_origin()}))
    # pacman
    for pkg in app.package_names:
        res = proc.run(["pacman", "-Q", "--", pkg], timeout=15)
        if res.ok and res.stdout.split()[:1] == [pkg]:  # exact name, not a provider
            src = next((s.id for s in manifest.sources if s.package == pkg), None)
            found.append(FoundInstall(format="pacman", source_id=src, package=pkg, version=res.stdout.split()[-1]))
    # AppImages
    home = Path.home()
    running = running_appimages()
    refs = entry_references([home / ".config/autostart", home / ".local/share/applications"])
    app_src = next((s.id for s in manifest.sources if s.format == "appimage"), None)
    for path in sorted(_appimage_candidates(app_dirs, running, refs)):
        try:
            cand = detect_file(path)
        except CygnusError:
            continue
        if cand.format is not PackageFormat.APPIMAGE:
            continue
        ids = {cand.identity.get("desktop_id"), cand.identity.get("appstream_id")}
        if not ids & {*app.desktop_ids, *app.appstream_ids, app.id}:
            continue
        found.append(FoundInstall(format="appimage", source_id=app_src, path=path, version=cand.version,
                                  running=path in running, referenced_by=refs.get(path, []),
                                  facts={"update_info": cand.metadata.get("update_info"),
                                         # present, not verified: verification is a separate, explicit step
                                         "signature_present": cand.metadata.get("signature_present")}))
    return found


def duplicate_issue(manifest: Manifest, installs: list[FoundInstall]) -> Issue | None:
    if len(installs) < 2:
        return None
    labels = []
    for i in installs:
        where = i.path or i.flatpak_ref or i.package
        state = "running" if i.running else "not running"
        labels.append(f"{i.format} ({where}, {state})")
    conflict = next((c.reason for c in manifest.conflicts
                     if {i.source_id for i in installs} >= set(c.between)), None)
    return Issue(
        code="DUPLICATE_INSTALL", severity=IssueSeverity.DEGRADED,
        title=f"{manifest.application.name} is installed {len(installs)} times",
        explanation="; ".join(labels) + (f". {conflict}" if conflict else ""),
        facts={"installs": [i.format for i in installs]},
        resolutions=[Resolution(id="keep-one", title="Keep one copy…",
                                explanation="Choose which copy to keep; Cygnus removes the others (your data is "
                                            "kept unless you choose otherwise).",
                                safety=SafetyClass.APPROVAL, recommended=True)])
