"""Engine façade used by the GUI. Plain Python (no Qt) so it can be unit-tested directly.

Every public function returns JSON-serializable dicts. Each call opens its own registry
connection, so functions are safe to run on worker threads.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from cygnus.core import inventory, paths, planner
from cygnus.core import progress as progress_mod
from cygnus.core.detect import detect_file
from cygnus.core.errors import CygnusError
from cygnus.core import preferences
from cygnus.core.health.engine import InstallContext, dismissible, evaluate
from cygnus.core.manifest import catalog
from cygnus.core.models import PackageFormat
from cygnus.core.recovery.model import Issue, IssueSeverity, explain_only
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation
from cygnus.core.storage import locations
from cygnus.core.storage.discovery import scan
from cygnus.core.util.fs import open_regular

SYMBOLS = {"ok": "✓", "optional_unavailable": "○", "missing_component": "⚠", "likely_failing": "?",
           "broken": "✗", "offline": "⏏", "unknown": "?", "dismissed": "–"}


def _issue(i: Issue) -> dict[str, Any]:
    return {"code": i.code, "severity": i.severity.value, "title": i.title, "explanation": i.explanation,
            "evidence": i.evidence[:5],
            "resolutions": [{"id": r.id, "title": r.title, "explanation": r.explanation, "safety": r.safety.value,
                             "privilege": r.privilege.value, "recommended": r.recommended} for r in i.resolutions]}


def _registry(path: str | None = None):
    return open_registry(path)


def _system_location(reg) -> StorageLocation:
    for loc in reg.list_locations():
        if loc.location_class == "system":
            return loc
    return StorageLocation(id="system", label="SSD", fs_uuid=None, fs_type="", location_class="system")


# -- read side ---------------------------------------------------------------------------------------------
def list_apps(registry_path: str | None = None) -> list[dict[str, Any]]:
    from cygnus.gui import fixes

    fixes.settle_late_commits()  # a commit that timed out may have finished since
    reg = _registry(registry_path)
    locs = {l.id: l.label for l in reg.list_locations()}
    out = []
    for r in regdb.list_installations(reg):
        icon = next((Path(a["locator"]).stem for a in regdb.artifacts_of(reg, r["id"]) if a["kind"] == "icon"), None)
        if r["format"] == "flatpak":
            icon = r["source"].get("ref", "").split("/")[1] if r["source"].get("ref") else None
        out.append({"installation_id": r["id"], "app_id": r["app_id"], "name": r["name"], "format": r["format"],
                    "icon": icon or ("package-x-generic" if r["format"] in ("pacman", "aur") else "application-x-executable"),
                    "version": r["version"] or "", "origin": r["origin"],
                    "location": locs.get(r["location_id"], "SSD" if r["format"] != "appimage" else ""),
                    "path": r["source"].get("path", ""), "update": r["update_provider"].get("type", "none")})
    return out


def storage(registry_path: str | None = None) -> dict[str, Any]:
    from cygnus.core.ops import flatpak_ops

    reg = _registry(registry_path)
    try:
        flatpak_ops.ensure_relocation_links()  # self-heal links that `flatpak repair` removes
    except CygnusError:
        pass  # reported by the health check / doctor
    cands = scan()
    registered = []
    for loc in reg.list_locations():
        rv = locations.resolve(loc, cands)
        try:
            apps = str(locations.apps_dir(loc, rv)) if rv.online else ""
        except CygnusError as exc:
            apps = f"({exc})"
        registered.append({"id": loc.id, "label": loc.label, "class": loc.location_class, "fs_type": loc.fs_type,
                           "online": rv.online, "path": rv.path or "", "reason": rv.reason or "",
                           "default": loc.is_default, "apps_dir": apps,
                           "appimages": bool(loc.capabilities.get("exec_allowed")) or loc.location_class == "system",
                           "flatpak": planner.location_supports(loc, "flatpak")[0]})
    candidates = [{"name": c.display_name, "mount": c.canonical_mount or "", "fs_type": c.fs_type,
                   "class": c.location_class.value, "free": c.free_bytes, "size": c.size_bytes,  # None = not known (0 = full)
                   "eligible": c.eligible, "reason": c.ineligible_reason or "", "notes": c.notes,
                   "rotational": c.rotational} for c in cands]
    return {"locations": registered, "candidates": candidates}


def check_app(query: str, registry_path: str | None = None) -> dict[str, Any]:
    """Health of every installed copy of an application known to the curated catalog."""
    from cygnus.core.backends import flatpak as fb

    cat = catalog.bundled_manifests()
    q = query.lower()
    loaded = next((m for k, m in cat.items() if q in (k.lower(), m.manifest.application.name.lower())), None)
    if loaded is None:
        return {"known": False, "query": query}
    try:
        env = fb.FlatpakEnv()
        fp_apps = {a["ref"]: a for a in fb.installed_apps_with_runtime(env)}
    except CygnusError:
        env, fp_apps = None, {}
    reg = _registry(registry_path)
    dirs = []
    for loc in reg.list_locations():
        rv = locations.resolve(loc)
        if rv.online:
            try:
                dirs.append(locations.apps_dir(loc, rv))
            except CygnusError:
                pass
    installs = inventory.find_installs(loaded.manifest, app_dirs=dirs, flatpak_env=env)
    installs += inventory.offline_installs(loaded.manifest, reg)  # on a drive that is not connected right now
    reports = []
    for inst in installs:
        if inst.format == "appimage":
            ctx = InstallContext(source_id=inst.source_id, format="appimage", payload_path=inst.path,
                                 location_online=not inst.facts.get("offline"))
        elif inst.format == "flatpak":
            info = fp_apps.get(inst.flatpak_ref, {})
            ctx = InstallContext(source_id=inst.source_id, format="flatpak", flatpak_ref=inst.flatpak_ref,
                                 runtime_eol=info.get("runtime_eol"), runtime_installed=info.get("runtime_installed"))
        else:
            ctx = InstallContext(source_id=inst.source_id, format="pacman", package=inst.package)
        h = evaluate(loaded.manifest, ctx, trusted=loaded.can_drive_actions,
                     dismissed=preferences.dismissed(loaded.manifest.application.id))
        reports.append({
            "format": inst.format, "where": inst.path or inst.flatpak_ref or inst.package, "running": inst.running,
            "version": inst.version or "", "overall": h.overall.value, "symbol": SYMBOLS[h.overall.value],
            "runtime_eol": bool(inst.format == "flatpak" and fp_apps.get(inst.flatpak_ref, {}).get("runtime_eol")),
            "features": [{"id": f.id, "name": f.name, "status": f.status.value,
                          "symbol": "○" if f.optional and f.status.value == "optional_unavailable"
                          else SYMBOLS[f.status.value], "explanation": f.explanation, "optional": f.optional,
                          "dismissed_components": [c.component.id for c in f.components
                                                   if c.status.value == "dismissed"]}
                         for f in h.features],
            "issues": [{**_issue(i), "component": i.facts.get("component"),
                        "dismissible": bool(i.facts.get("component"))
                        and dismissible(loaded.manifest, i.facts["component"])} for i in h.issues]})
    dup = inventory.duplicate_issue(loaded.manifest, installs)
    return {"known": True, "app_id": loaded.manifest.application.id, "name": loaded.manifest.application.name,
            "vendor": loaded.manifest.application.vendor.name, "trust": loaded.trust_level, "installs": reports,
            "duplicate": _issue(dup) if dup else None}


# -- analysis / plans ----------------------------------------------------------------------------------------
def analyse(path: str, location_label: str | None, registry_path: str | None = None, *,
            progress: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    reg = _registry(registry_path)
    before = _file_signature(path)
    cand = detect_file(path)
    manifest = catalog.find_for({**cand.identity, "name": cand.name or ""}, catalog.bundled_manifests())
    locs = reg.list_locations()
    system = _system_location(reg)
    target = next((l for l in locs if l.label.lower() == (location_label or "").lower()), None) \
        or reg.default_location() or system
    base = {"file": path, "format": cand.format.value, "name": cand.name or Path(path).name,
            "version": cand.version or "", "summary": cand.summary or "", "target": target.label,
            "findings": [{"code": f.code, "severity": f.severity.value, "message": f.message} for f in cand.findings],
            "trust": manifest.trust_level if manifest else "unverified"}
    if cand.format is PackageFormat.APPIMAGE:
        from cygnus.core.backends import appimage as ab

        sig = None
        pinned = next((s.verification.openpgp_fingerprint for s in manifest.manifest.sources
                       if s.format == "appimage" and s.verification.openpgp_fingerprint), None) if manifest else None
        if pinned:
            sig = ab.verify_signature(Path(cand.source), pinned).status
        extra = ab.prerequisite_issues(Path(cand.source))
        if not any(l.id == target.id for l in locs):  # only the built-in stand-in for "this computer's drive"
            extra = [Issue(code="STORAGE_NOT_SET_UP", severity=IssueSeverity.BLOCKER,
                           title="Choose where applications are stored first",
                           explanation="An AppImage needs a folder to live in, and no storage location is set up "
                                       "for it yet. Open Storage and add your drive (your home folder works), then "
                                       "come back here.",
                           resolutions=[explain_only("add-storage", "Add a storage location",
                                                     "On the Storage page, add the drive or folder where you want "
                                                     "applications to live, then analyse the file again.")]),
                     *extra]
        plan = planner.plan_appimage(cand, target, system, manifest=manifest, signature_status=sig,
                                     extra_issues=extra, health_probe=_probe_component)
    elif cand.format is PackageFormat.FLATPAK_BUNDLE:
        from cygnus.core.backends import flatpak as fb

        from cygnus.core.ops import flatpak_ops

        user_loc = _user_flatpak_location(reg)
        _, installation, _, _ = planner.flatpak_installation(target, system, user_loc)
        env = fb.FlatpakEnv(target=flatpak_ops.installation_for(installation["kind"]))
        alts = [{"id": s.id, "format_label": s.label or s.format} for s in manifest.manifest.sources
                if s.format == "appimage"] if manifest else []
        runtime_issues = fb.analyse_bundle(cand, env, alternatives=alts)
        plan = planner.plan_flatpak(cand, target, system, runtime_issues=runtime_issues, runtime_bytes=_runtime_bytes(runtime_issues), manifest=manifest,
                                    user_install_location=user_loc, health_probe=_probe_component)
    elif cand.format is PackageFormat.LOCAL_PKG:
        from cygnus.core.backends import pacman as pm

        issues = pm.analyse_local_package(cand)
        analysis = type("A", (), {"to_add": [{"name": cand.name, "installed_size": cand.installed_size}],
                                  "issues": issues})()
        plan = planner.plan_system_package(cand.name, analysis, target, system, manifest=manifest,
                                           source_label="Local package", health_probe=_probe_component)
    elif cand.format in (PackageFormat.DEB, PackageFormat.RPM):
        # The checksum the person confirms must be of the very bytes that were analysed: taken before, checked after,
        # so a file replaced in the meantime is refused instead of being shown with the new file's checksum.
        digest = file_sha256(str(cand.source))
        if _file_signature(path) != before:  # swapped between being recognised and being summed
            raise CygnusError("the file changed while Cygnus was opening it, so what it found no longer applies; "
                              "please open the file again")
        verdict = _foreign_verdict(cand, progress)
        if file_sha256(str(cand.source)) != digest:
            raise CygnusError("the file changed while Cygnus was checking it, so what it found no longer applies; "
                              "please open the file again")
        return {**base, "kind": "foreign", "strategy": verdict.strategy, "verdict": verdict.summary,
                # when the person may go ahead after reading, the page explains that itself: a red "not converted" box
                # above an offer to convert would contradict it
                "issues": [_issue(i) for i in verdict.issues
                           if not (verdict.scripts_acknowledgeable and i.code == "FOREIGN_SCRIPTS_UNRECOGNISED")],
                "placements": [], "components": [],
                "depends": verdict.repo_dependencies, "installable": verdict.strategy == "convert",
                # packages only some parts of the program want: offered, never installed unasked
                "optional": [{"package": o["package"], "libraries": o["libraries"][:6],
                              "files": [Path(f).name for f in o["files"][:4]]} for o in verdict.optional_dependencies],
                # install scripts Cygnus could not read: the person may convert anyway after reading what they contain
                "additions": verdict.additions,  # what Cygnus will add itself (menu icon, command link): listed before you confirm
                "installable_after_review": verdict.scripts_acknowledgeable,
                "scripts": {"unreadable": verdict.scripts.unknown[:8], "mentions": verdict.script_mentions,
                            "effects": verdict.script_effects,
                            "texts": verdict.script_sources} if verdict.scripts_acknowledgeable else None,
                "sha256": digest}  # what the user is shown belongs to exactly this file
    else:
        return {**base, "kind": "info", "issues": [], "placements": [], "components": [], "installable": False,
                "verdict": "Cygnus can show this file's details; installing this kind of file is not supported yet."}
    return {**base, "kind": "plan", "placements": [asdict(p) for p in plan.placements],
            "components": [{"id": c.id, "name": c.name, "relation": c.relation, "privilege": c.privilege.value,
                            "already_present": c.already_present, "note": c.note} for c in plan.components],
            "runtime": plan.runtime or "", "usage": plan.usage_by_location(), "notes": plan.notes,
            "issues": [_issue(i) for i in plan.issues], "blocked": plan.blocked,
            "installable": not plan.blocked and cand.format in (PackageFormat.APPIMAGE, PackageFormat.LOCAL_PKG,
                                                                PackageFormat.FLATPAK_BUNDLE)}


def _runtime_bytes(issues) -> int | None:
    """Installed size of the runtime the plan will add, from the remote it will come from."""
    for i in issues:
        source = (i.facts or {}).get("install_from")
        if i.code in ("FP_RUNTIME_MISSING", "FP_RUNTIME_EOL") and source:
            return (i.facts.get("remotes", {}).get(source["remote"]) or {}).get("installed_size") or None
    return None


def _probe_component(comp) -> bool:
    from cygnus.core.health.engine import _component_health
    from cygnus.core.health.probes import run_probe

    return _component_health(comp, run_probe).status.value == "ok"


def _user_flatpak_location(reg) -> str | None:
    from cygnus.core.ops import flatpak_ops

    target = flatpak_ops.relocation_state().relocated_to
    if target is None:
        return None
    best = None
    for loc in reg.list_locations():
        rv = locations.resolve(loc)
        # Real path containment (/mnt/data2/x is not inside /mnt/data); the deepest location wins.
        if rv.online and rv.path and Path(target).is_relative_to(rv.path) \
                and (best is None or len(rv.path) > len(best[1])):
            best = (loc.id, rv.path)
    return best[0] if best else "elsewhere"


# -- write side (user scope) --------------------------------------------------------------------------------
def install_appimage(path: str, location_label: str, progress: Callable[[str], None]) -> dict[str, Any]:
    from cygnus.core.ops import appimage_ops

    reg = _registry()
    cand = detect_file(path)
    if cand.format is not PackageFormat.APPIMAGE:
        raise CygnusError("not an AppImage")
    manifest = catalog.find_for({**cand.identity, "name": cand.name or ""}, catalog.bundled_manifests())
    key = appimage_ops.app_key_for(cand, manifest.manifest.application.id if manifest else None)
    loc = next((l for l in reg.list_locations() if l.label.lower() == location_label.lower()), None)
    if loc is None:
        raise CygnusError(f"no storage location named {location_label!r}; add one on the Storage page first")
    rv = locations.resolve(loc)
    if not rv.online:
        raise CygnusError(f"{loc.label} is not available: {rv.reason}")
    if rv.path and str(Path(cand.source)).startswith(rv.path.rstrip("/") + "/"):
        steps = appimage_ops.plan_adopt(cand, app_key=key, location=loc, manifest=manifest)  # already there
        kind = "adopt"
    else:
        steps = appimage_ops.plan_install(cand, app_key=key, location=loc, apps_dir=locations.apps_dir(loc, rv),
                                          manifest=manifest)
        kind = "install"
    report = appimage_ops.Ops(reg).executor().run(
        kind, steps, progress=progress_mod.steps(progress, numbered=True))
    return {"ok": report.state == "succeeded", "state": report.state, "error": report.error or ""}


def uninstall(installation_id: str, delete_file: bool, progress: Callable[[str], None]) -> dict[str, Any]:
    from cygnus.core.ops import plan_uninstall

    reg = _registry()
    row = reg.conn.execute("SELECT format, application_id FROM installation WHERE id=?", (installation_id,)).fetchone()
    if row is None:
        raise CygnusError("unknown installation")
    steps = plan_uninstall(reg, installation_id, remove_payload=delete_file)
    report = executor_for(reg).run(
        "uninstall", steps, app_id=row[1],
        progress=progress_mod.steps(progress, numbered=True))
    return {"ok": report.state == "succeeded", "state": report.state, "error": report.error or "",
            "kept": report.kept}


def add_location(path: str, label: str, make_default: bool) -> dict[str, Any]:
    reg = _registry()
    loc, result = locations.register(reg, path, label, default=make_default)
    return {"ok": True, "id": loc.id, "class": loc.location_class,
            "appimages": bool(result and result.supports_appimages), "flatpak": bool(result and result.supports_flatpak)}


_FETCH_LOCK = threading.Lock()  # one update download at a time: two would share the same folder and spoil each other


def _fresh_updates_folder() -> Path:
    """An empty folder of our own for one update download, with the earlier ones removed (they are big). Nothing is ever
    deleted through a link: a folder that is a link, or something else than a plain folder, is refused."""
    import shutil
    import tempfile

    root = paths.cache_dir() / "downloads"
    for part in (root, root / "updates"):
        if part.is_symlink() or (part.exists() and not part.is_dir()):
            raise CygnusError(f"{part} is not a plain folder, so Cygnus will not put or remove files there")
    updates_dir = root / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    for old in updates_dir.iterdir():
        if old.is_symlink() or not old.is_dir():
            old.unlink(missing_ok=True)  # a stray file or link of ours: the link itself goes, never what it points at
        else:
            shutil.rmtree(old, ignore_errors=True)
    return Path(tempfile.mkdtemp(prefix="dl-", dir=updates_dir))


def fetch_converted_update(installation_id: str, progress: Callable[[str], None], *, check_kw: dict | None = None,
                           download=None) -> dict[str, Any]:
    """Download the newer version of a program Cygnus converted. The file is only fetched (over HTTPS, checked against the
    checksum the vendor publishes where there is one), never converted or installed here: the person opens it on the Install
    page, where it is analysed and confirmed like any file they chose, and it replaces the old copy."""
    from cygnus.core import updates
    from cygnus.core.util import http

    if not _FETCH_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "Another update is still being downloaded. Wait for it to finish."}
    try:
        reg = _registry()
        row = next((r for r in regdb.list_installations(reg) if r["id"] == installation_id), None)
        if row is None or not updates.is_converted(row):
            raise CygnusError("this is not a program Cygnus converted from a vendor's package")
        progress("Checking for the newest version…")
        st = updates.check(row, **(check_kw or {}))
        updates.record(reg, st)
        if st.status != updates.AVAILABLE:
            return {"ok": False, "error": f"No update to download: {st.detail}"}
        url = st.facts.get("download_url") or ""
        if not url.startswith("https://"):
            return {"ok": False, "error": "the vendor's package list gave no usable download address"}
        folder = _fresh_updates_folder()
        name = url.rsplit("/", 1)[-1]
        progress(f"Downloading {name}…")
        path = (download or http.download)(url, folder, expected_sha256=st.facts.get("expected_sha256"),
                                           progress=_download_progress(progress, name))
        return {"ok": True, "path": str(path), "file_url": Path(path).as_uri(), "version": st.available or "",
                "name": row["name"], "checked": bool(st.facts.get("expected_sha256"))}
    except OSError as exc:  # a full disk, a folder that cannot be written: said in words, not as a raw error
        raise CygnusError(f"the update could not be saved: {exc.strerror or exc}") from exc
    finally:
        _FETCH_LOCK.release()


def _file_signature(path: str) -> tuple[int, int, int] | None:
    """What identifies the file as it is now (its place on the disk, size and modification time): a file replaced or
    rewritten in between two looks at it does not have the same one."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with os.fdopen(open_regular(path), "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def install_flatpak_bundle(path: str, location_label: str | None, progress: Callable[[str], None],
                           registry_path: str | None = None) -> dict[str, Any]:
    """Install a .flatpak bundle: system installation on the system drive, otherwise the user
    installation stored on the chosen drive (relocated on first use, architecture §7.2). The runtime
    is resolved for that installation first (§9); the bundle itself never adds repositories."""
    from cygnus.core.executor import Executor
    from cygnus.core.ops import flatpak_ops

    reg = _registry(registry_path)
    cand = detect_file(path)
    if cand.format is not PackageFormat.FLATPAK_BUNDLE:
        raise CygnusError("not a Flatpak bundle")
    attention: list[str] = []
    where, kind, inst = _prepare_flatpak_installation(reg, location_label, progress, attention)
    executor = Executor(reg, dict(flatpak_ops.HANDLERS))
    app_ref = cand.identity.get("flatpak_ref") or ""
    steps = flatpak_ops.plan_bundle_install(inst, Path(path), app_ref, cand.metadata.get("runtime"))
    report = executor.run("flatpak-install", steps, app_id=cand.identity.get("flatpak_id"),
                          progress=progress_mod.steps(progress))
    if report.state != "succeeded":
        undone = " Everything done so far was undone." if report.state == "rolled_back" else ""
        return {"ok": False, "error": (report.error or "the installation failed") + undone}
    manifest = catalog.find_for({**cand.identity, "name": cand.name or ""}, catalog.bundled_manifests())
    app_id = manifest.manifest.application.id if manifest else cand.identity.get("flatpak_id", cand.name)
    try:
        _record_flatpak(reg, app_id=app_id, name=cand.name or app_id, trust=manifest.trust_level if manifest else "unverified",
                        source={"ref": app_ref, "installation": kind, "bundle": path,
                                "origin_url": cand.metadata.get("origin_url")},
                        version=cand.version, location_id=_registered_id(reg, where),
                        provider="flatpak-remote" if cand.metadata.get("origin_url") else "manual",
                        locator=f"{kind}:{app_ref}")
    except Exception as exc:  # noqa: BLE001 - the app IS installed: say that, rather than "failed"
        return {"ok": False, "error": _installed_but_unrecorded(cand.name or app_id, exc)}
    progress("Installed.")
    return {"ok": True, "installation": kind, "operation": report.op_id,
            "steps": [st.description for st in steps], "attention": " ".join(attention)}


def _record_flatpak(reg, *, app_id: str, name: str, trust: str, source: dict[str, Any], version: str | None,
                    location_id: str | None, provider: str, locator: str) -> str:
    """Register an installed Flatpak (application, installation and its ref) in one transaction."""
    with reg.transaction() as c:
        regdb.add_application(reg, app_id=app_id, display_name=name, trust_level=trust, within=c)
        iid = regdb.add_installation(reg, app_id=app_id, fmt="flatpak", source=source, version=version,
                                     location_id=location_id, origin="installed", update_provider={"type": provider},
                                     within=c)
        regdb.add_artifact(reg, installation_id=iid, kind="flatpak_ref", locator=locator, within=c)
    return iid


def _installed_but_unrecorded(name: str, exc: Exception) -> str:
    return (f"{name} was installed, but Cygnus could not record it ({exc}), so it will not show up under "
            "Applications. Run the same installation again to record it, or remove it with Flatpak.")


def _registered_id(reg, loc) -> str | None:
    """The location's id if it is registered (the system drive may only be an implicit default)."""
    return loc.id if any(l.id == loc.id for l in reg.list_locations()) else None


def _flatpak_destination(reg, location_label: str | None):
    """(location, installation kind) a Flatpak chosen for `location_label` really goes to (planner rules)."""
    system = _system_location(reg)
    chosen = next((l for l in reg.list_locations() if l.label.lower() == (location_label or "").lower()), None) \
        or reg.default_location() or system
    where, installation, _, _ = planner.flatpak_installation(chosen, system, _user_flatpak_location(reg))
    return where, installation["kind"]


def _prepare_flatpak_installation(reg, location_label: str | None, progress: Callable[[str], None],
                                  attention: list[str] | None = None):
    """Make sure the chosen installation exists where it should (relocating the user installation to
    its drive on first use, architecture §7.2) and return (location, kind, installation). If the move left an old
    folder behind because it changed while it was being moved, `attention` says where (and `progress` too)."""
    from cygnus.core.executor import Executor
    from cygnus.core.ops import flatpak_ops

    where, kind = _flatpak_destination(reg, location_label)
    if kind == "system":
        progress("Using the system Flatpak installation (Flatpak may ask for authorization)…")
    else:
        rv = locations.resolve(where)
        if not rv.online:
            raise CygnusError(f"{where.label} is not available: {rv.reason}")
        steps = flatpak_ops.plan_relocation(Path(rv.path) / ".cygnus-flatpak-user", fs_uuid=where.fs_uuid)
        if steps:
            progress(f"Moving Flatpak's application storage to {where.label}…")
            report = Executor(reg, dict(flatpak_ops.HANDLERS)).run("flatpak-relocate", steps)
            if report.state != "succeeded":
                raise CygnusError(f"could not prepare {where.label} for Flatpak: {report.error}")
            if report.kept:
                note = ("Flatpak's storage was moved, but " + report.kept_reasons.get(report.kept[0], "an old folder was kept")
                        + ": " + ", ".join(report.kept) + ". Cygnus did not delete it; copy over anything you need "
                        "from it.")
                progress(note)
                if attention is not None:
                    attention.append(note)
        flatpak_ops.user_dir().mkdir(parents=True, exist_ok=True)
    return where, kind, flatpak_ops.installation_for(kind)


def configured_flatpak_remotes() -> dict[str, str]:
    """{repository address (see launch.normal_url): remote name} for the remotes that are set up and enabled,
    so a .flatpakref can be matched to one of them. Never adds anything."""
    from cygnus.core.backends import flatpak as fb

    from cygnus.gui.launch import normal_url

    out: dict[str, str] = {}
    try:
        for _inst, remote in fb.FlatpakEnv().remotes():
            if not remote.get_disabled() and remote.get_url():
                out.setdefault(normal_url(remote.get_url()), remote.get_name())
    except Exception:  # noqa: BLE001 - no Flatpak: nothing is configured
        return {}
    return out


def parse_flatpak_spec(spec: str) -> tuple[str, str, str | None]:
    """"[remote:]app.id[//branch]" -> (remote, app id, branch); the remote defaults to flathub."""
    import re

    remote, _, rest = spec.rpartition(":") if ":" in spec else ("flathub", "", spec)
    app_id, _, branch = rest.partition("//")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+", app_id) \
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", remote) \
            or (branch and not re.fullmatch(r"[A-Za-z0-9_.-]+", branch)):
        raise CygnusError(f"not a Flatpak application id: {spec!r} (expected e.g. flathub:org.example.App)")
    return remote, app_id, branch or None


def analyse_flatpak_ref(spec: str, location_label: str | None, registry_path: str | None = None) -> dict[str, Any]:
    from cygnus.core.backends import flatpak as fb
    from cygnus.core.ops import flatpak_ops

    reg = _registry(registry_path)
    remote, app_id, branch = parse_flatpak_spec(spec)
    system = _system_location(reg)
    where, kind = _flatpak_destination(reg, location_label)
    target = next((l for l in reg.list_locations() if l.label.lower() == (location_label or "").lower()), None) \
        or reg.default_location() or system
    env = fb.FlatpakEnv(target=flatpak_ops.installation_for(kind))
    cand, _ = flatpak_ops.remote_app(env, remote, app_id, branch)
    manifest = catalog.find_for({**cand.identity, "name": cand.name or ""}, catalog.bundled_manifests())
    issues = fb.analyse_bundle(cand, env)
    plan = planner.plan_flatpak(cand, target, system, runtime_issues=issues, runtime_bytes=_runtime_bytes(issues), manifest=manifest,
                                user_install_location=_user_flatpak_location(reg), health_probe=_probe_component)
    return {"file": spec, "format": "flatpak", "name": cand.name, "version": "", "summary": "", "target": target.label,
            "findings": [], "trust": manifest.trust_level if manifest else "unverified", "kind": "plan",
            "ref": cand.identity["flatpak_ref"], "download_bytes": cand.metadata.get("download_size") or 0,
            "placements": [asdict(p) for p in plan.placements],
            "components": [{"id": c.id, "name": c.name, "relation": c.relation, "privilege": c.privilege.value,
                            "already_present": c.already_present, "note": c.note} for c in plan.components],
            "runtime": plan.runtime or "", "usage": plan.usage_by_location(), "notes": plan.notes,
            "issues": [_issue(i) for i in plan.issues], "blocked": plan.blocked, "installable": not plan.blocked}


def install_flatpak_ref(spec: str, location_label: str | None, progress: Callable[[str], None],
                        registry_path: str | None = None) -> dict[str, Any]:
    """Install an app from a configured remote (e.g. flathub:org.example.App) into the installation
    the chosen location uses; its runtime goes into the same installation."""
    from cygnus.core.backends import flatpak as fb
    from cygnus.core.ops import flatpak_ops

    reg = _registry(registry_path)
    remote, app_id, branch = parse_flatpak_spec(spec)
    attention: list[str] = []
    where, kind, inst = _prepare_flatpak_installation(reg, location_label, progress, attention)
    env = fb.FlatpakEnv(target=inst)
    progress(f"Looking up {app_id} in {remote}…")
    cand, holder = flatpak_ops.remote_app(env, remote, app_id, branch)
    steps = flatpak_ops.plan_ref_install(inst, cand, holder, env)
    report = executor_for(reg).run("flatpak-install", steps, app_id=app_id,
                                   progress=progress_mod.steps(progress))
    if report.state != "succeeded":
        undone = " Everything done so far was undone." if report.state == "rolled_back" else ""
        return {"ok": False, "error": (report.error or "the installation failed") + undone}
    ref = cand.identity["flatpak_ref"]
    installed = flatpak_ops._installed(flatpak_ops.installation_for(kind), ref)
    manifest = catalog.find_for({**cand.identity, "name": cand.name or ""}, catalog.bundled_manifests())
    name = (installed.get_appdata_name() if installed else None) or cand.name
    try:
        iid = _record_flatpak(reg, app_id=manifest.manifest.application.id if manifest else app_id, name=name,
                              trust=manifest.trust_level if manifest else "unverified",
                              source={"ref": ref, "installation": kind, "remote": remote},
                              version=(installed.get_appdata_version() if installed else None),
                              location_id=_registered_id(reg, where), provider="flatpak-remote",
                              locator=f"{kind}:{ref}")
    except Exception as exc:  # noqa: BLE001 - the app IS installed: say that, rather than "failed"
        return {"ok": False, "error": _installed_but_unrecorded(name or app_id, exc)}
    progress("Installed.")
    return {"ok": True, "installation": kind, "installation_id": iid, "steps": [st.description for st in steps],
            "attention": " ".join(attention)}


def move_flatpak(installation_id: str, location_label: str, progress: Callable[[str], None],
                 registry_path: str | None = None) -> dict[str, Any]:
    """Move a Flatpak between the system installation and the user installation on another drive:
    install it in the target (from its own repository, or its bundle file), then uninstall the old copy.
    Its settings and data (~/.var/app) are shared by both installations and stay where they are."""
    from cygnus.core.backends import flatpak as fb
    from cygnus.core.executor import Step
    from cygnus.core.ops import flatpak_ops

    reg = _registry(registry_path)
    row = next((r for r in regdb.list_installations(reg) if r["id"] == installation_id), None)
    if row is None or row["format"] != "flatpak":
        raise CygnusError("not a Flatpak managed by Cygnus")
    src_kind, ref = row["source"].get("installation", "user"), row["source"]["ref"]
    where, kind = _flatpak_destination(reg, location_label)
    if kind == src_kind:
        raise CygnusError(f"{row['name']} already lives in that installation"
                          + (" (the personal Flatpak installation can be on one drive only)" if kind == "user" else ""))
    src_inst = flatpak_ops.installation_for(src_kind)
    installed = flatpak_ops._installed(src_inst, ref)
    if installed is None:
        raise CygnusError(f"{ref} is not installed in the {src_kind} installation")
    branch = ref.split("/")[3]
    attention: list[str] = []
    where, kind, inst = _prepare_flatpak_installation(reg, location_label, progress, attention)
    env = fb.FlatpakEnv(target=inst)
    if fb.FlatpakEnv.inst_id(src_inst) not in {fb.FlatpakEnv.inst_id(i) for i in env.installations}:
        env.installations.append(src_inst)  # its repositories are where the app came from
    origin = installed.get_origin()
    origin_remote = next((r for r in src_inst.list_remotes(None) if r.get_name() == origin), None)
    if origin_remote is not None and origin_remote.get_url() and not origin_remote.get_noenumerate():
        cand, holder = flatpak_ops.remote_app(env, origin, ref.split("/")[1], branch)
        steps = flatpak_ops.plan_ref_install(inst, cand, holder, env)
    elif row["source"].get("bundle") and Path(row["source"]["bundle"]).is_file():
        steps = flatpak_ops.plan_bundle_install(inst, Path(row["source"]["bundle"]), ref,
                                                installed_runtime(installed), env)
    else:
        raise CygnusError(f"{row['name']} came from a bundle file that is no longer available, and has no "
                          "repository to reinstall from; download the bundle again to move it")
    src_spec = flatpak_ops.installation_spec(src_inst)
    steps.append(Step(kind="registry.update", description="Record the new place", params={
        "installation_id": installation_id, "version": row["version"], "location_id": _registered_id(reg, where),
        "source": {**row["source"], "installation": kind},
        "artifacts": [{"kind": "flatpak_ref", "locator": f"{kind}:{ref}"}],
        "history": {"from": src_kind, "to": kind}, "history_kind": "moved"}))
    steps.append(Step(kind="flatpak.uninstall_ref", params={"installation": src_spec, "ref": ref},
                      description=f"Remove the copy from the {src_kind} installation"))
    runtimes = flatpak_ops.cygnus_installed_runtimes(reg, src_spec)
    if runtimes:
        steps.append(Step(kind="flatpak.prune_runtimes", params={"installation": src_spec, "candidates": runtimes},
                          description="Remove runtimes nothing uses any more"))
    report = executor_for(reg).run("move", steps, app_id=row["app_id"],
                                   progress=progress_mod.steps(progress))
    return {"ok": report.state == "succeeded", "state": report.state, "error": report.error or "",
            "steps": [st.description for st in steps], "attention": " ".join(attention)}


def installed_runtime(installed) -> str | None:
    md = installed.load_metadata(None)
    text = md.get_data().decode("utf-8", "replace") if md else ""
    from cygnus.core.detect.flatpak import parse_metadata

    return parse_metadata(text).get("Application", {}).get("runtime")


def adopt_appimage(path: str, progress: Callable[[str], None]) -> dict[str, Any]:
    from cygnus.core.desktop import integrate as integ
    from cygnus.core.inventory import entry_references
    from cygnus.core.ops import appimage_ops

    reg = _registry()
    cand = detect_file(path)
    if cand.format is not PackageFormat.APPIMAGE:
        raise CygnusError("not an AppImage")
    manifest = catalog.find_for({**cand.identity, "name": cand.name or ""}, catalog.bundled_manifests())
    if appimage_ops.require_pinned_signature(Path(cand.source), manifest):
        progress(f"Signature verified with {manifest.manifest.application.vendor.name}'s pinned key")
    key = appimage_ops.app_key_for(cand, manifest.manifest.application.id if manifest else None)
    loc, best = None, -1
    for l in reg.list_locations():
        rv = locations.resolve(l)
        if rv.online and rv.path and str(Path(cand.source)).startswith(rv.path.rstrip("/") + "/") \
                and len(rv.path) > best:  # the deepest location that holds the file, not merely the last one
            loc, best = l, len(rv.path)
    refs = entry_references([integ.xdg_config_home() / "autostart"]).get(cand.source, [])
    steps = appimage_ops.plan_adopt(cand, app_key=key, location=loc, manifest=manifest,
                                    autostart=[Path(r) for r in refs])
    report = appimage_ops.Ops(reg).executor().run(
        "adopt", steps, app_id=key, progress=progress_mod.steps(progress, numbered=True))
    return {"ok": report.state == "succeeded", "state": report.state, "error": report.error or ""}


def is_managed(path: str) -> bool:
    reg = _registry()
    return any(r["source"].get("path") == path for r in regdb.list_installations(reg))


# -- updates ------------------------------------------------------------------------------------------------
def _apply_flatpak_update(reg, row: dict[str, Any], progress: Callable[[str], None]) -> dict[str, Any]:
    from cygnus.core import updates
    from cygnus.core.ops import flatpak_ops

    progress("Checking for the newest version…")
    st = updates.check(row)
    updates.record(reg, st)
    if st.status != updates.AVAILABLE:
        return {"ok": False, "error": f"No update to install: {st.detail}"}
    report = executor_for(reg).run("update", flatpak_ops.plan_update(reg, row["id"]), app_id=row["app_id"],
                                   progress=progress_mod.steps(progress))
    if report.state != "succeeded":
        return {"ok": False, "error": report.error or "the update failed", "state": report.state}
    result = json.loads(reg.conn.execute("SELECT result FROM operation_step WHERE op_id=? ORDER BY seq DESC LIMIT 1",
                                         (report.op_id,)).fetchone()[0] or "{}")
    version = result.get("version") or row["version"]
    with reg.transaction() as c:
        c.execute("UPDATE installation SET version=? WHERE id=?", (version, row["id"]))
    regdb.add_history(reg, row["id"], "updated", {"from": row["version"], "to": version})
    updates.record(reg, updates.check({**row, "version": version}))
    progress("Updated.")
    return {"ok": True, "version": version or "", "eol": result.get("eol", [])}


def update_overview() -> list[dict[str, Any]]:
    """Last known update state of every managed application (no network)."""
    from cygnus.core import updates

    return [u.as_dict() for u in updates.last_known(_registry())]


def check_updates(progress: Callable[[str], None]) -> list[dict[str, Any]]:
    from cygnus.core import updates

    reg = _registry()
    updates.check_all(reg, progress)
    try:
        updates.check_system_updates(progress)
    except CygnusError as exc:
        progress(f"System updates could not be checked: {exc}")
    return [u.as_dict() for u in updates.last_known(reg)]


def system_update_overview() -> dict[str, Any] | None:
    """The last system-update check (no network)."""
    from cygnus.core import updates

    return updates.last_system_updates()


def apply_update(installation_id: str, progress: Callable[[str], None], *, download=None,
                 **check_kw) -> dict[str, Any]:
    """Update an AppImage in place (architecture §13): fresh check → download next to the current file →
    verify (published digest, same application, pinned signature) → journaled swap; the previous
    version is deleted only after everything else succeeded."""
    from cygnus.core import updates
    from cygnus.core.inventory import running_appimages
    from cygnus.core.ops import appimage_ops

    reg = _registry()
    row = next((r for r in regdb.list_installations(reg) if r["id"] == installation_id), None)
    if row is None:
        raise CygnusError("unknown installation")
    if row["format"] == "flatpak":
        return _apply_flatpak_update(reg, row, progress)
    if row["format"] != "appimage":
        raise CygnusError(f"{row['name']} is updated together with your system")
    path = Path(row["source"]["path"])
    progress("Checking for the newest version…")
    st = updates.check(row, **check_kw)
    updates.record(reg, st)
    if st.status != updates.AVAILABLE:
        return {"ok": False, "error": f"No update to install: {st.detail}"}
    if str(path) in running_appimages():
        return {"ok": False, "error": f"{row['name']} is running. Close it, then update."}
    url = st.facts.get("download_url") or ""
    progress(f"Downloading {url.rsplit('/', 1)[-1]}…")
    staged = appimage_ops.stage_update(path, url, expected_sha256=st.facts.get("expected_sha256"),
                                       expected_sha1=st.facts.get("expected_sha1"), download=download,
                                       progress=_download_progress(progress, url.rsplit("/", 1)[-1]))
    try:
        new = detect_file(str(staged))
        if new.format is not PackageFormat.APPIMAGE:
            raise CygnusError("the download is not an AppImage")
        manifest = catalog.bundled_manifests().get(row["app_id"])
        if appimage_ops.app_key_for(new, manifest.manifest.application.id if manifest else None) != row["app_id"]:
            raise CygnusError("the download is a different application; not installing it")
        if appimage_ops.pinned_fingerprint(manifest):
            progress("Verifying the vendor's signature…")
            appimage_ops.require_pinned_signature(staged, manifest)
        loc = next((l for l in reg.list_locations() if l.id == row["location_id"]), None)
        steps = appimage_ops.plan_update(reg, installation_id, staged, new, location=loc)
        report = appimage_ops.Ops(reg).executor().run(
            "update", steps, app_id=row["app_id"],
            progress=progress_mod.steps(progress, numbered=True))
    finally:
        appimage_ops.discard_staging(staged)
    if report.state != "succeeded":
        undone = " The previous version was put back." if report.state == "rolled_back" else ""
        return {"ok": False, "error": (report.error or "the update failed") + undone, "state": report.state}
    updates.record(reg, updates.check(next(r for r in regdb.list_installations(reg) if r["id"] == installation_id),
                                      **check_kw))
    progress("Updated.")
    return {"ok": True, "version": new.version or ""}


# -- interrupted operations (architecture §11) ----------------------------------------------------------------
_OPERATION_TITLES = {"adopt": "Adding {app} to Cygnus", "install": "Installing {app}", "update": "Updating {app}",
                     "move": "Moving {app}", "repair": "Repairing {app}",
                     "uninstall": "Removing {app}", "flatpak-install": "Installing {app}",
                     "flatpak-relocate": "Moving Flatpak's storage"}


def _download_progress(progress: Callable[[str], None], name: str) -> Callable[[int, int | None], None]:
    """The http download's (bytes so far, bytes expected) as a progress line; a fraction only when the size is known."""
    def report(done: int, size: int | None) -> None:
        mb = done / 1e6
        if size:
            progress(progress_mod.Progress(f"Downloading {name}… {mb:.0f} of {size / 1e6:.0f} MB", done / size, log=False))
        else:
            progress(progress_mod.Progress(f"Downloading {name}… {mb:.0f} MB", None, log=False))
    return report


def executor_for(reg):
    from cygnus.core.ops import executor_for as _executor_for

    return _executor_for(reg)


def interrupted_operations(registry_path: str | None = None) -> list[dict[str, Any]]:
    reg = _registry(registry_path)
    names = {r[0]: r[1] for r in reg.conn.execute("SELECT id, display_name FROM application")}
    out = []
    for op in executor_for(reg).incomplete():
        plan = json.loads(reg.conn.execute("SELECT plan FROM operation WHERE id=?", (op["id"],)).fetchone()[0])
        recorded = next((s["params"].get("name") for s in plan if s["kind"] == "registry.record"), None)
        app = names.get(op["app_id"] or "") or recorded or op["app_id"] or "an application"
        done = sum(1 for s in op["steps"] if s["state"] == "done")
        out.append({**op, "title": _OPERATION_TITLES.get(op["kind"], op["kind"]).format(app=app),
                    "progress": f"{done} of {len(op['steps'])} steps done",
                    "explanation": "Cygnus stopped before this finished (it was closed or the computer shut down)."
                    if op["state"] == "running" else "This did not finish and could not be undone automatically."})
    return out


def recover(op_id: str, action: str, progress: Callable[[str], None],
            registry_path: str | None = None) -> dict[str, Any]:
    reg = _registry(registry_path)
    ex = executor_for(reg)
    if action == "resume":
        report = ex.resume(op_id, progress=progress_mod.steps(progress, numbered=True))
    elif action == "rollback":
        progress("Undoing the completed steps…")
        report = ex.roll_back(op_id)
    else:
        raise CygnusError(f"unknown recovery action {action!r}")
    ok = report.state in ("succeeded", "rolled_back")
    return {"ok": ok, "state": report.state,
            "error": "" if ok else (report.error or "; ".join(report.compensation_errors) or "it did not complete")}


# -- move and repair (AppImages) ----------------------------------------------------------------------------
def _appimage_row(reg, installation_id: str) -> dict[str, Any]:
    row = next((r for r in regdb.list_installations(reg) if r["id"] == installation_id), None)
    if row is None:
        raise CygnusError("unknown installation")
    if row["format"] != "appimage":
        raise CygnusError(f"this is not available for {row['format']} applications yet")
    return row


def move_app(installation_id: str, location_label: str, progress: Callable[[str], None]) -> dict[str, Any]:
    from cygnus.core.inventory import running_appimages
    from cygnus.core.ops import appimage_ops

    reg = _registry()
    if reg.conn.execute("SELECT format FROM installation WHERE id=?", (installation_id,)).fetchone() == ("flatpak",):
        return move_flatpak(installation_id, location_label, progress)
    row = _appimage_row(reg, installation_id)
    target = next((l for l in reg.list_locations() if l.label.lower() == location_label.lower()), None)
    if target is None:
        raise CygnusError(f"no storage location named {location_label!r}")
    ok, reason = planner.location_supports(target, "appimage")
    if not ok:
        raise CygnusError(reason)
    rv = locations.resolve(target)
    if not rv.online:
        raise CygnusError(f"{target.label} is not available: {rv.reason}")
    if row["source"]["path"] in running_appimages():
        return {"ok": False, "error": f"{row['name']} is running. Close it, then move it."}
    cand = detect_file(row["source"]["path"])
    steps = appimage_ops.plan_move(reg, installation_id, cand, location=target,
                                   apps_dir=locations.apps_dir(target, rv))
    report = executor_for(reg).run("move", steps, app_id=row["app_id"],
                                   progress=progress_mod.steps(progress, numbered=True))
    return {"ok": report.state == "succeeded", "state": report.state, "error": report.error or ""}


def _flatpak_row(reg, installation_id: str) -> dict[str, Any] | None:
    row = next((r for r in regdb.list_installations(reg) if r["id"] == installation_id), None)
    return row if row and row["format"] == "flatpak" else None


def diagnose_app(installation_id: str) -> list[dict[str, str]]:
    from cygnus.core.ops import appimage_ops, flatpak_ops

    reg = _registry()
    if (row := _flatpak_row(reg, installation_id)) is not None:
        return flatpak_ops.diagnose(row["source"])
    _appimage_row(reg, installation_id)
    return appimage_ops.diagnose(reg, installation_id)


def repair_app(installation_id: str, progress: Callable[[str], None]) -> dict[str, Any]:
    from cygnus.core.ops import appimage_ops, flatpak_ops

    reg = _registry()
    if (frow := _flatpak_row(reg, installation_id)) is not None:
        problems = flatpak_ops.diagnose(frow["source"])
        steps = flatpak_ops.plan_repair(frow["source"])
        report = executor_for(reg).run("repair", steps, app_id=frow["app_id"],
                                       progress=progress_mod.steps(progress))
        return {"ok": report.state == "succeeded", "state": report.state, "error": report.error or "",
                "repaired": problems}
    row = _appimage_row(reg, installation_id)
    payload = next((p for p in appimage_ops.diagnose(reg, installation_id) if p["what"] == "payload"), None)
    if payload and payload["state"] != "changed":
        hint = " Is its drive connected?" if row["location_id"] else ""
        raise CygnusError(f"the application file is {payload['state']}: {payload['path']}.{hint}")
    cand = detect_file(row["source"]["path"])
    manifest = catalog.bundled_manifests().get(row["app_id"])
    loc = next((l for l in reg.list_locations() if l.id == row["location_id"]), None)
    steps = appimage_ops.plan_repair(reg, installation_id, cand, location=loc, manifest=manifest)
    if not steps:
        return {"ok": True, "repaired": []}
    problems = appimage_ops.diagnose(reg, installation_id)
    report = executor_for(reg).run("repair", steps, app_id=row["app_id"],
                                   progress=progress_mod.steps(progress, numbered=True))
    return {"ok": report.state == "succeeded", "state": report.state, "error": report.error or "",
            "repaired": problems}


# -- AUR (architecture §10.2) ---------------------------------------------------------------------------------
def aur_review(name: str) -> dict[str, Any]:
    """Fetch an AUR package's build files for review. Nothing from them is run."""
    from cygnus.core.backends import aur, pacman as pm
    from cygnus.core.ops import aur_ops

    found = aur.info([name])
    if name not in found:
        raise CygnusError(f"{name!r} is not in the AUR")
    meta = found[name]
    checkout = aur_ops.fetch(meta["PackageBase"])
    r = aur_ops.review(checkout)
    deps = sorted({*r.srcinfo.get("depends", []), *r.srcinfo.get("makedepends", []),
                   *r.srcinfo.get("checkdepends", [])})
    sat = pm.run_worker(pm.read_config(), {"op": "satisfy", "deps": deps})["satisfiers"] if deps else {}
    repo_needed = sorted({s["repo"]["name"] for s in sat.values() if not s.get("installed") and s.get("repo")})
    aur_needed = sorted(d for d, s in sat.items() if not s.get("installed") and not s.get("repo"))
    chain: list[dict[str, Any]] = []
    blockers: list[str] = []
    if aur_needed:
        # Other AUR packages are needed: every one of them is reviewed too, dependencies first.
        cfg = pm.read_config()
        plan = aur.resolve([name], satisfy=lambda deps: pm.run_worker(cfg, {"op": "satisfy", "deps": deps})["satisfiers"],
                           find_provider=aur.provider)
        blockers = [f"{i.title}: {i.explanation}" for i in plan.issues if i.severity.value == "blocker"]
        repo_needed = sorted({*repo_needed, *plan.repo_deps, *plan.repo_makedeps})
        for base in plan.build_order:
            if base == meta["PackageBase"]:
                continue
            dep = aur_ops.review(aur_ops.fetch(base))
            names = sorted(n for n, p in plan.packages.items() if (p.get("PackageBase") or n) == base)
            chain.append({**dep.as_dict(), "names": names,
                          "version": next((plan.packages[n].get("Version", "") for n in names), "")})
    complete = r.complete and all(d["complete"] for d in chain)
    return {**r.as_dict(), "name": name, "version": aur_ops.visible(str(meta.get("Version", ""))),
            "votes": meta.get("NumVotes", 0),
            "maintainer": aur_ops.visible(str(meta.get("Maintainer") or "(orphaned)")),
            "out_of_date": bool(meta.get("OutOfDate")),
            "description": aur_ops.visible(str(meta.get("Description") or "")), "repo_dependencies": repo_needed,
            "aur_dependencies": aur_needed, "dependencies": chain, "blockers": blockers,
            "buildable": complete and not blockers}


def aur_build(pkgbase: str, commit: str, progress: Callable[[str], None], name: str | None = None,
              names: list[str] | None = None) -> dict[str, Any]:
    """Record your approval of exactly `commit` and build it as you. The review is checked again
    here: an incomplete review, or files that changed since, are never built."""
    from cygnus.core.ops import aur_ops

    checkout = aur_ops.aur_dir() / pkgbase
    r = aur_ops.review(checkout)
    if r.commit != commit:
        raise CygnusError("the build files changed after you reviewed them; review them again")
    if not r.complete:
        raise CygnusError("Cygnus could not show you these build files in full (" + "; ".join(r.problems) + ")")
    aur_ops.approve(pkgbase, commit)
    built = aur_ops.build(checkout, commit, aur_ops.aur_dir() / ".packages" / pkgbase, progress)
    wanted = set(names or ([name] if name else []))
    if wanted:  # of a split package, only the parts that were asked for
        built = [p for p in built if detect_file(str(p)).name in wanted] or built
    return {"packages": [str(p) for p in built]}


def _record_packages(app_id: str, display_name: str, fmt: str, source: dict[str, Any], version: str,
                     packages: list[str], update_type: str, registry_path: str | None = None) -> str:
    """Register packages installed through the helper. Re-installing updates the existing entry
    (never a second, unremovable one); removal also goes through the helper."""
    reg = _registry(registry_path)
    regdb.add_application(reg, app_id=app_id, display_name=display_name, trust_level="unverified")
    row = reg.conn.execute("SELECT id FROM installation WHERE application_id=? AND format=?", (app_id, fmt)).fetchone()
    if row:
        iid = row[0]
        with reg.transaction() as c:
            c.execute("UPDATE installation SET version=?, source=? WHERE id=?", (version, json.dumps(source), iid))
    else:
        iid = regdb.add_installation(reg, app_id=app_id, fmt=fmt, source=source, version=version, location_id=None,
                                     origin="installed", update_provider={"type": update_type})
    for name in packages:
        regdb.add_artifact(reg, installation_id=iid, kind="package", locator=name, takeover=True)
    # A package that moved here from another entry leaves that entry with nothing to remove: drop it.
    for (ghost,) in reg.conn.execute(
            "SELECT i.id FROM installation i WHERE i.format IN ('pacman','aur') AND i.id != ? AND NOT EXISTS "
            "(SELECT 1 FROM artifact a WHERE a.installation_id = i.id)", (iid,)).fetchall():
        regdb.remove_installation(reg, ghost)
    return iid


def record_aur_install(pkgbase: str, name: str, commit: str, version: str, packages: list[str] | None = None,
                       dependency_of: str | None = None, registry_path: str | None = None) -> str:
    source = {"pkgbase": pkgbase, "package": name, "reviewed_commit": commit}
    if dependency_of:
        source["dependency_of"] = dependency_of
    return _record_packages(f"aur.{pkgbase}", name, "aur", source, version, packages or [name], "aur",
                            registry_path)



# -- DEB / RPM conversion (architecture §10.3) ----------------------------------------------------------------
def _foreign_verdict(cand, progress: Callable[[str], None] = lambda _: None):
    from cygnus.core.backends import filedb, foreign, pacman as pm, sources, translate

    from cygnus.core.ops.convert import pacman_name

    cfg = pm.read_config()
    # Look up the name the converted package would really get ("Git" becomes git): a case-only
    # difference must not hide that this would replace an installed Arch package.
    alts, _ = sources.find(pacman_name(cand.name or ""), app_name=cand.name, catalog=catalog.bundled_manifests(),
                           repo_info=lambda n: pm.run_worker(cfg, {"op": "info", "names": n})["packages"])
    # An installed package that Cygnus made from an earlier file of the same program is not "an Arch package that
    # pacman keeps up to date": nothing updates it. Installing the newer file replaces it, and that must stay possible.
    converted = _converted_packages()
    replaced = next(({"name": a.name, "version": (a.facts or {}).get("version", "")}
                     for a in alts if a.kind == "installed" and a.name in converted), None)
    kept = [a for a in alts if not (a.kind == "installed" and a.name in converted)]
    verdict = foreign.analyse(cand, satisfy=lambda d: pm.run_worker(cfg, {"op": "satisfy", "deps": d})["satisfiers"],
                              locate=lambda sonames: filedb.locate(sonames, progress=progress, cfg=cfg),
                              owner=translate.ignoring(_owner, pacman_name(cand.name or "")),
                              find_alternatives=lambda c: [a.as_dict() for a in kept])
    if replaced:
        verdict.replaces_converted = replaced
        verdict.issues.insert(0, Issue(
            code="REPLACES_CONVERTED", severity=IssueSeverity.NOTICE,
            title=f"This replaces the copy of {replaced['name']} you installed from an earlier file",
            explanation=f"Version {replaced['version']} was converted by Cygnus from a file like this one. Nothing "
                        "updates it automatically; installing this file puts the new version in its place."))
    return verdict


def _owner(path: str) -> str | None:
    """The installed package that owns `path` (None when nothing does): Cygnus never adds a file another package owns."""
    from cygnus.core.util import proc

    res = proc.run(["pacman", "-Qoq", "--", path], timeout=30)
    return res.stdout.strip().splitlines()[0] if res.returncode == 0 and res.stdout.strip() else None


def _converted_packages(registry_path: str | None = None) -> dict[str, str]:
    """Packages Cygnus converted from a .deb/.rpm and installed: name -> version."""
    reg = _registry(registry_path)
    out: dict[str, str] = {}
    for r in regdb.list_installations(reg):
        src = r.get("source") or {}
        if r["format"] == "pacman" and src.get("made_by") == "converted":
            out[str(src.get("package") or r["name"])] = str(r.get("version") or "")
    return out


def convert_foreign(path: str, progress: Callable[[str], None], expected_sha256: str | None = None,
                    optional: list[str] | tuple[str, ...] = (), accept_unread_scripts: bool = False) -> dict[str, Any]:
    """Convert a .deb/.rpm the analysis found safe into a pacman package (not installed yet).

    The file is copied once into a private folder while it is hashed, and everything after that (the check, the
    analysis, the conversion) works on that copy, so it is exactly the file that was hashed. With `expected_sha256`
    (from the analysis you confirmed), a file that has changed since is refused."""
    import shutil
    import tempfile

    from cygnus.core.backends import translate
    from cygnus.core.ops import convert

    incoming = paths.cache_dir() / "convert" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    original = Path(path)
    with tempfile.TemporaryDirectory(prefix="in-", dir=incoming) as tmp:
        copy = Path(tmp) / original.name
        digest = hashlib.sha256()
        from cygnus.core.backends import foreign

        with os.fdopen(open_regular(path), "rb") as src, open(copy, "xb") as dst:
            if os.fstat(src.fileno()).st_size > foreign.MAX_EXTRACT_BYTES:  # before any of it is copied
                raise CygnusError("the file is too large to convert")
            _copy_capped(src, dst, digest, foreign.MAX_EXTRACT_BYTES)
        if expected_sha256 and digest.hexdigest() != expected_sha256:
            raise CygnusError("the file changed after it was analysed, so what you were shown no longer applies; "
                              "please analyse it again")
        cand = detect_file(str(copy))
        if cand.format not in (PackageFormat.DEB, PackageFormat.RPM):
            raise CygnusError("not a .deb or .rpm file")
        progress("Checking the package again…")
        verdict = _foreign_verdict(cand, progress)
        pkg, notes = convert.convert(cand, verdict, paths.cache_dir() / "convert" / "packages", progress,
                                     install_optional=list(optional), accept_unread_scripts=accept_unread_scripts,
                                     owner=translate.ignoring(_owner, convert.pacman_name(cand.name or "")))
        shutil.rmtree(tmp, ignore_errors=True)
    return {"package": str(pkg), "name": convert.pacman_name(cand.name or ""), "notes": notes,
            "depends": [*verdict.repo_dependencies, *[o for o in optional if o not in verdict.repo_dependencies]],
            "source": path, "version": cand.version or "",
            # what the vendor calls it and what kind of file it was: how a newer version is found later (updates.py)
            "vendor_package": cand.name or "", "vendor_format": cand.format.value}


def _copy_capped(src, dst, digest, limit: int) -> None:
    """Copy and hash `src`; a file that is still growing is stopped at `limit` instead of filling the disk."""
    copied = 0
    while block := src.read(1 << 20):
        copied += len(block)
        if copied > limit:
            raise CygnusError("the file is too large to convert")
        digest.update(block)
        dst.write(block)


def record_package_install(name: str, version: str, *, origin: str, source: dict[str, Any],
                           registry_path: str | None = None) -> str:
    """Register a package Cygnus installed through the helper. `origin` says how it was made
    ("converted" from a .deb/.rpm, or "local" for a package file)."""
    return _record_packages(f"pkg.{name}", name, "pacman", {"package": name, "made_by": origin, **source},
                            version, [name], "manual", registry_path)


def package_names(installation_id: str) -> list[str]:
    reg = _registry()
    return [a["locator"] for a in regdb.artifacts_of(reg, installation_id) if a["kind"] == "package"]


def forget_installation(installation_id: str) -> None:
    reg = _registry()
    with reg.transaction() as c:
        c.execute("DELETE FROM artifact WHERE installation_id=?", (installation_id,))
    regdb.remove_installation(reg, installation_id)


def dismiss_component(app: str, component_id: str, dismiss: bool) -> None:
    """Stop (or resume) checking and suggesting an optional component you do not want."""
    loaded = next((m for m in catalog.bundled_manifests().values()
                   if app.lower() in (m.manifest.application.id.lower(), m.manifest.application.name.lower())), None)
    if loaded is None:
        raise CygnusError(f"Cygnus has no details about {app!r}")
    if loaded.manifest.component(component_id) is None:
        raise CygnusError(f"{loaded.manifest.application.name} has no component {component_id!r}")
    if dismiss and not dismissible(loaded.manifest, component_id):
        raise CygnusError("only optional features can be switched off; this one is needed by the application")
    preferences.set_dismissed(loaded.manifest.application.id, component_id, dismiss)


# -- background check (cygnus watch) ------------------------------------------------------------------------
def watch_gather(progress: Callable[[str], None] = lambda _: None,
                 registry_path: str | None = None) -> dict[str, Any]:
    """Everything the background check reports on: updates, managed applications' health, interrupted
    operations. Read-only, apart from the update caches."""
    from cygnus.core import updates

    reg = _registry(registry_path)
    app_updates = [u.as_dict() for u in updates.check_all(reg, progress) if u.status != updates.SYSTEM]
    try:
        system = updates.check_system_updates(progress)
    except CygnusError:
        system = None
    health, seen = [], set()
    manifests = catalog.bundled_manifests()
    for row in regdb.list_installations(reg):
        if row["app_id"] in seen or row["app_id"] not in manifests:
            continue
        seen.add(row["app_id"])
        try:
            health.append(check_app(row["app_id"], registry_path))
        except CygnusError:
            pass
    return {"app_updates": app_updates, "system": system, "health": health,
            "interrupted": interrupted_operations(registry_path)}


def background_checks() -> dict[str, Any]:
    from cygnus.core import watch

    try:
        return {"available": True, "enabled": watch.timer_enabled()}
    except Exception:  # noqa: BLE001 - no systemd user session (e.g. a container)
        return {"available": False, "enabled": False,
                "why": "Cygnus cannot reach systemd's user session here, so background checks cannot be turned on."}


def set_background_checks(enabled: bool) -> dict[str, Any]:
    from cygnus.core import watch

    watch.set_timer(enabled)
    return background_checks()


def stale_pacman_lock() -> dict[str, Any] | None:
    """pacman's lock left behind by a crashed package manager (none is running), or None."""
    import os
    import time as _time

    from cygnus.core.backends import pacman as pm
    from cygnus.helper.actions import package_managers_running

    try:
        lock = Path(pm.read_config().dbpath) / "db.lck"
        st = os.lstat(lock)
    except (OSError, CygnusError):
        return None
    if package_managers_running():
        return None
    return {"path": str(lock), "minutes": int((_time.time() - st.st_mtime) // 60)}
