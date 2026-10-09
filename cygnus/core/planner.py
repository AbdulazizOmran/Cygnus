"""Installation planner: decide what goes where, with what privileges, and what remains unresolved.

The planner never changes anything. Its output (InstallPlan) is what the UI shows before the
user confirms, and what the executor (Phase 3) runs as typed actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from cygnus.core.health.engine import privileges_note
from cygnus.core.manifest.catalog import LoadedManifest
from cygnus.core.manifest.schema import PRIVILEGED_ACTIONS
from cygnus.core.models import Candidate, PackageFormat
from cygnus.core.recovery.model import Action, Issue, IssueSeverity, Privilege, Resolution, SafetyClass
from cygnus.core.registry.db import StorageLocation

INTEGRATION_BYTES = 256 * 1024  # desktop entry, icons, shim, metadata


@dataclass(slots=True, kw_only=True)
class Placement:
    role: str  # payload | integration | runtime | dependency | component | configuration
    name: str
    location: str  # label shown to the user ("HDD", "SSD", ...)
    bytes: int | None
    reason: str = ""


@dataclass(slots=True, kw_only=True)
class PlannedComponent:
    id: str
    name: str
    relation: str  # hard | recommended | optional
    privilege: Privilege
    actions: list[Action]
    note: str = ""
    already_present: bool = False


@dataclass(slots=True, kw_only=True)
class InstallPlan:
    app_name: str
    source_label: str
    format: str
    target: str
    placements: list[Placement] = field(default_factory=list)
    components: list[PlannedComponent] = field(default_factory=list)
    runtime: str | None = None
    actions: list[Action] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    trust: str = "unverified"
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(i.severity is IssueSeverity.BLOCKER and not _auto_resolvable(i) for i in self.issues)

    def usage_by_location(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.placements:
            out[p.location] = out.get(p.location, 0) + (p.bytes or 0)
        return out

    @property
    def privileges(self) -> set[Privilege]:
        privs = {c.privilege for c in self.components if not c.already_present}
        for i in self.issues:
            pref = i.preferred()
            if pref:
                privs.add(pref.privilege)
        return privs


def _auto_resolvable(issue: Issue) -> bool:
    pref = issue.preferred()
    return pref is not None and pref.safety is SafetyClass.AUTO


def _size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.0f} {unit}" if n >= 10 * div else f"{n / div:.1f} {unit}"
    return f"{n} B"


def location_supports(loc: StorageLocation, what: str) -> tuple[bool, str]:
    """Can `loc` hold this kind of payload? Uses the stored probe results (architecture §6.3)."""
    caps = loc.capabilities or {}
    if loc.location_class == "system":
        return True, ""
    if what == "system-package":
        return False, (f"{loc.label} cannot hold system packages: pacman installs them to fixed system "
                       "folders, and only the system drive can hold files owned by the system.")
    if not caps:
        return False, f"{loc.label} has not been tested yet (run a storage probe)"
    if not caps.get("exec_allowed"):
        return False, f"{loc.label} does not allow running programs"
    if what == "appimage":
        return bool(caps.get("chmod_persists")), "" if caps.get("chmod_persists") else \
            f"{loc.label} does not keep file permissions"
    if what == "flatpak":
        needed = ("symlinks", "hardlinks", "atomic_rename", "chmod_persists")
        missing = [k for k in needed if not caps.get(k)]
        if caps.get("ostree_bare_user_only") is False:
            missing.append("OSTree repositories")
        return (not missing), ("" if not missing else f"{loc.label} lacks: {', '.join(missing)}")
    return False, f"unknown payload kind {what}"


def source_for(manifest: LoadedManifest | None, fmt: str):
    """The manifest source describing this format (first match), or None."""
    if manifest is None:
        return None
    aliases = {"flatpak": ("flatpak-remote",), "flatpak-bundle": ("flatpak-bundle",), "appimage": ("appimage",),
               "localpkg": ("pacman-local",), "pacman": ("pacman-repo", "aur")}
    wanted = aliases.get(fmt, (fmt,))
    return next((s for s in manifest.manifest.sources if s.format in wanted), None)


def _components_from_manifest(manifest: LoadedManifest | None, source_id: str | None,
                              health_probe: Callable[[Any], Any] | None) -> list[PlannedComponent]:
    if manifest is None:
        return []
    trusted = manifest.can_drive_actions
    out = []
    for comp in manifest.manifest.components:
        if comp.applies_to_sources and source_id not in comp.applies_to_sources:
            continue
        install = comp.platform_actions.get("arch") or comp.action
        actions = ([Action(kind=install.kind, params=install.model_dump(exclude={"kind"}))] if install else []) + \
                  [Action(kind=p.kind, params=p.model_dump(exclude={"kind"})) for p in comp.post]
        if not trusted:
            # Unverified manifests may describe components, but never propose actions for them.
            actions = []
        present = bool(health_probe(comp)) if health_probe else False
        out.append(PlannedComponent(
            id=comp.id, name=comp.name, relation=comp.relation,
            privilege=Privilege.ROOT if any(a.kind in PRIVILEGED_ACTIONS for a in actions) else Privilege.USER,
            actions=actions, already_present=present,
            note=" ".join(x for x in (comp.security_note or "", privileges_note(comp),
                                      "Requires logging out and back in." if comp.requires_relogin else "",
                                      "" if trusted else "Unverified source: Cygnus will not install this "
                                                         "automatically.") if x)))
    return out


def plan_appimage(cand: Candidate, target: StorageLocation, system: StorageLocation, *,
                  manifest: LoadedManifest | None = None, signature_status: str | None = None,
                  extra_issues: list[Issue] | None = None, health_probe=None) -> InstallPlan:
    name = cand.name or "the application"
    plan = InstallPlan(app_name=name, source_label="AppImage", format=PackageFormat.APPIMAGE.value,
                       target=target.label, trust=manifest.trust_level if manifest else "unverified")
    ok, reason = location_supports(target, "appimage")
    where = target if ok else system
    plan.placements.append(Placement(role="payload", name="Main application", location=where.label,
                                     bytes=cand.metadata.get("file_size"),
                                     reason="" if ok else f"{reason}; using {system.label} instead"))
    plan.placements.append(Placement(role="integration", name="Desktop integration", location=system.label,
                                     bytes=INTEGRATION_BYTES, reason="menu entry, icon and launcher"))
    plan.placements.append(Placement(role="configuration", name="Settings and data", location=system.label,
                                     bytes=None, reason="kept in your home folder"))
    if not ok:
        plan.issues.append(Issue(code="STORAGE_INCAPABLE", severity=IssueSeverity.NOTICE,
                                 title=f"{name} will be stored on {system.label}", explanation=reason))
    plan.actions.append(Action(kind="appimage.place", params={"source": cand.source, "location": where.id}))
    plan.actions.append(Action(kind="desktop.integrate", params={"desktop_id": cand.identity.get("desktop_id")}))
    src = source_for(manifest, "appimage")
    plan.components = _components_from_manifest(manifest, src.id if src else None, health_probe)
    required = bool(src and manifest and manifest.authentic  # an expired manifest's pin still applies
                    and src.verification.type in ("appimage-openpgp", "openpgp-detached"))
    if signature_status == "verified":
        plan.notes.append("Signature verified with the vendor's key.")
    elif required or signature_status in ("bad-signature", "wrong-key"):
        # The vendor signs this application: anything short of a verified signature is a stop.
        what = {"unsigned": "carries no signature", "error": "has a signature that could not be checked",
                "bad-signature": "does not match its signature", "wrong-key": "was signed with a different key",
                None: "was not checked"}.get(signature_status, f"failed verification ({signature_status})")
        plan.issues.append(Issue(code="APPIMAGE_SIGNATURE_INVALID", severity=IssueSeverity.BLOCKER,
                                 title=f"This {name} file {what}",
                                 explanation=f"{name} is signed by its vendor, so an unverified copy may have been "
                                             "tampered with. Download it again from the official source.",
                                 resolutions=[]))
    else:
        plan.notes.append("Unsigned download: its integrity relies on the download source (HTTPS).")
    plan.issues += extra_issues or []
    return plan


def flatpak_installation(target: StorageLocation, system: StorageLocation, user_install_location: str | None
                         ) -> tuple[StorageLocation, dict[str, Any], bool, str]:
    """Where a Flatpak chosen for `target` really goes: (location, installation, target_ok, reason).

    Per the approved design the system drive uses the system installation; any other drive hosts
    the (partially relocated) user installation, which can live on one drive only. Custom system
    installations are out of scope."""
    ok, reason = location_supports(target, "flatpak")
    if target.location_class != "system" and ok and user_install_location not in (None, target.id):
        ok, reason = False, "your Flatpak user installation already lives on another drive"
    where = target if ok else system
    installation = {"kind": "system"} if where.location_class == "system" else {"kind": "user",
                                                                                  "location_id": where.id}
    return where, installation, ok, reason


def plan_flatpak(cand: Candidate, target: StorageLocation, system: StorageLocation, *,
                 runtime_issues: list[Issue], runtime_bytes: int | None = None,
                 manifest: LoadedManifest | None = None, user_install_location: str | None = None,
                 health_probe=None) -> InstallPlan:
    name = cand.name or cand.identity.get("flatpak_id", "the application")
    plan = InstallPlan(app_name=name, source_label="Flatpak" + (" bundle" if cand.format is
                                                                PackageFormat.FLATPAK_BUNDLE else ""),
                       format=cand.format.value, target=target.label,
                       trust=manifest.trust_level if manifest else "unverified")
    where, installation, ok, reason = flatpak_installation(target, system, user_install_location)
    where_text = "system" if installation["kind"] == "system" else f"user (stored on {where.label})"
    plan.placements.append(Placement(role="payload", name="Main application", location=where.label,
                                     bytes=cand.installed_size,
                                     reason=f"Flatpak {where_text} installation" + ("" if ok else f"; {reason}")))
    runtime = cand.metadata.get("runtime")
    plan.runtime = runtime
    if runtime and any(i.code in ("FP_RUNTIME_MISSING", "FP_RUNTIME_EOL") and i.severity is not IssueSeverity.DEGRADED
                       for i in runtime_issues):
        rt_name, _, rt_branch = runtime.split("/")
        plan.placements.append(Placement(role="runtime", name="Runtime", location=where.label, bytes=runtime_bytes,
                                         reason=f"{rt_name} {rt_branch}, installed next to the application"))
    plan.placements.append(Placement(role="integration", name="Desktop integration", location=system.label,
                                     bytes=INTEGRATION_BYTES, reason="exported menu entry and icons"))
    plan.placements.append(Placement(role="configuration", name="Settings and data", location=system.label,
                                     bytes=None, reason="~/.var/app"))
    plan.issues += runtime_issues
    plan.actions.append(Action(kind="flatpak.install_bundle" if cand.format is PackageFormat.FLATPAK_BUNDLE
                               else "flatpak.install", params={"source": cand.source, "installation": installation}))
    src = source_for(manifest, cand.format.value)
    plan.components = _components_from_manifest(manifest, src.id if src else None, health_probe)
    if installation["kind"] == "system":
        plan.issues.append(Issue(code="FP_SYSTEM_INSTALL", severity=IssueSeverity.NOTICE,
                                 title="Installs for all users of this computer",
                                 explanation="Flatpak will ask for authorization.",
                                 resolutions=[Resolution(id="authorize", title="Authorize when asked",
                                                         explanation="Handled by Flatpak's own permission check.",
                                                         safety=SafetyClass.APPROVAL,
                                                         privilege=Privilege.FLATPAK_SYSTEM)]))
    return plan


def plan_system_package(name: str, analysis, target: StorageLocation, system: StorageLocation, *,
                        manifest: LoadedManifest | None = None, health_probe=None,
                        source_label: str = "System package", alternatives: list | None = None) -> InstallPlan:
    """pacman / AUR / local package: always system storage (pacman cannot follow relocation, §2.2)."""
    plan = InstallPlan(app_name=name, source_label=source_label, format="pacman", target=target.label,
                       trust=manifest.trust_level if manifest else "package-metadata")
    reason = ""
    if target.location_class != "system":
        _, reason = location_supports(target, "system-package")
    main = next((p for p in analysis.to_add if p["name"] == name), None)
    deps = [p for p in analysis.to_add if p["name"] != name]
    plan.placements.append(Placement(role="payload", name="Main application", location=system.label,
                                     bytes=(main or {}).get("installed_size"), reason=reason))
    if deps:
        plan.placements.append(Placement(role="dependency", name=f"{len(deps)} dependencies",
                                         location=system.label,
                                         bytes=sum(p.get("installed_size") or 0 for p in deps),
                                         reason=", ".join(p["name"] for p in deps[:6]) + (" …" if len(deps) > 6 else "")))
    if reason:
        # Format advisor: offer a vendor-supported format that *can* live on the requested drive.
        movable = [a for a in (alternatives or []) if a.relocatable and a.vendor_supported]
        if movable:
            plan.issues.append(Issue(
                code="FORMAT_ADVISOR", severity=IssueSeverity.NOTICE,
                title=f"{name} can be stored on {target.label} in another format",
                explanation=reason,
                resolutions=[Resolution(id=f"switch-{a.kind}-{a.name}", title=f"Use {a.label} on {target.label}",
                                        explanation=a.note, safety=SafetyClass.APPROVAL, recommended=(i == 0),
                                        rank=10 + i, actions=[Action(kind="source.switch", params=a.as_dict())])
                             for i, a in enumerate(movable)]
                + [Resolution(id="keep-system", title=f"Install the system package on {system.label}",
                              explanation="Updated together with your system.", safety=SafetyClass.APPROVAL,
                              rank=50)]))
    plan.issues += analysis.issues
    plan.actions.append(Action(kind="pacman.install_repo", params={"names": [name]}))
    src = source_for(manifest, "pacman")
    plan.components = _components_from_manifest(manifest, src.id if src else None, health_probe)
    return plan


def render_text(plan: InstallPlan) -> str:
    lines = [f"Install {plan.app_name}  ({plan.source_label})"]
    width = max((len(p.name) for p in plan.placements), default=10) + 2
    for p in plan.placements:
        size = f"  {_size(p.bytes)}" if p.bytes else ""
        reason = f"  — {p.reason}" if p.reason else ""
        lines.append(f"  {p.name + ':':<{width}} {p.location}{size}{reason}")
    if plan.runtime:
        lines.append(f"  {'Runtime:':<{width}} {plan.runtime}")
    req = [c for c in plan.components if c.relation != "optional"]
    opt = [c for c in plan.components if c.relation == "optional"]
    if req:
        lines.append(f"  {'Required components:':<{width}} " + " · ".join(
            c.name + (" (already set up)" if c.already_present else "") for c in req))
    if opt:
        lines.append(f"  {'Optional components:':<{width}} " + " · ".join(c.name for c in opt))
    for loc, n in plan.usage_by_location().items():
        lines.append(f"  Estimated {loc} usage: {_size(n)}")
    status = "Cannot be installed as planned" if plan.blocked else (
        "Needs your decision" if any(i.severity is IssueSeverity.BLOCKER for i in plan.issues)
        else "All required components available")
    lines.append(f"  Status: {status}")
    for note in plan.notes:
        lines.append(f"  Note: {note}")
    for c in plan.components:
        if c.note and not c.already_present:
            lines.append(f"  Note ({c.name}): {c.note}")
    for i in plan.issues:
        pref = i.preferred()
        lines.append(f"  [{i.code}] {i.title}" + (f" → {pref.title}" if pref else ""))
    return "\n".join(lines)
