"""Health engine: evaluate an application's features from manifest probes and install context.

Health states (architecture §12.1):
  ok                     ✓ fully functional
  optional_unavailable   ⚠/○ some optional functionality unavailable
  missing_component      ⚠ a feature's required component is missing
  likely_failing         ? evidence is indirect
  broken                 ✗ the core of the application does not work
  offline                ⏏ storage is not connected (never counted as broken)
  unknown                probes could not decide
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from cygnus.core.health.probes import ProbeOutcome, ProbeStatus, run_probe
from cygnus.core.manifest.schema import IMPLEMENTED_ACTIONS, PRIVILEGED_ACTIONS, Component, Manifest
from cygnus.core.recovery.model import (
    Action, Issue, IssueSeverity, Privilege, Resolution, SafetyClass,
)


class Health(StrEnum):
    OK = "ok"
    OPTIONAL_UNAVAILABLE = "optional_unavailable"
    MISSING_COMPONENT = "missing_component"
    LIKELY_FAILING = "likely_failing"
    BROKEN = "broken"
    OFFLINE = "offline"
    UNKNOWN = "unknown"
    DISMISSED = "dismissed"  # an optional feature you said you do not want


SEVERITY_ORDER = [Health.DISMISSED, Health.OK, Health.OPTIONAL_UNAVAILABLE, Health.UNKNOWN, Health.LIKELY_FAILING,
                  Health.MISSING_COMPONENT, Health.BROKEN, Health.OFFLINE]
SYMBOL = {Health.OK: "✓", Health.OPTIONAL_UNAVAILABLE: "○", Health.MISSING_COMPONENT: "⚠",
          Health.LIKELY_FAILING: "?", Health.BROKEN: "✗", Health.OFFLINE: "⏏", Health.UNKNOWN: "?",
          Health.DISMISSED: "–"}


def dismissible(manifest: Manifest, component_id: str) -> bool:
    """Only components that nothing essential needs can be switched off: every feature requiring
    it must be optional (never the core of the application)."""
    needing = [f for f in manifest.features if component_id in f.requires]
    return bool(needing) and all(f.optional and not f.core for f in needing)


@dataclass(slots=True, kw_only=True)
class InstallContext:
    """Which installation of the application is being checked."""

    source_id: str | None  # manifest source id (appimage, flatpak-bundle, ...)
    format: str  # appimage | flatpak | pacman | ...
    payload_path: str | None = None
    flatpak_ref: str | None = None
    package: str | None = None
    location_online: bool = True
    runtime_eol: str | None = None
    runtime_installed: bool | None = None


@dataclass(slots=True, kw_only=True)
class ComponentHealth:
    component: Component
    status: Health
    outcomes: list[ProbeOutcome]
    relogin_required: bool = False


@dataclass(slots=True, kw_only=True)
class FeatureHealth:
    id: str
    name: str
    status: Health
    explanation: str
    optional: bool = False
    components: list[ComponentHealth] = field(default_factory=list)


@dataclass(slots=True, kw_only=True)
class AppHealth:
    app_id: str
    name: str
    overall: Health
    features: list[FeatureHealth]
    issues: list[Issue]
    context: InstallContext


def privileges_note(comp: Component) -> str:
    """What the component can do, in its manifest's words: shown before anything is approved (spec 6.2)."""
    return f"What it can do: {', '.join(comp.privileges)}." if comp.privileges else ""


def _component_health(comp: Component, probe: Callable[[dict[str, Any]], ProbeOutcome]) -> ComponentHealth:
    outcomes = [probe(p.model_dump()) for p in comp.verify]
    statuses = [o.status for o in outcomes]
    relogin = any(o.facts.get("relogin_required") for o in outcomes)
    if not outcomes:
        status = Health.UNKNOWN
    elif comp.verify_mode == "any":
        if ProbeStatus.PASS in statuses:
            status = Health.OK
        elif ProbeStatus.FAIL in statuses:
            status = Health.MISSING_COMPONENT
        else:
            status = Health.UNKNOWN
    elif ProbeStatus.FAIL in statuses:
        status = Health.MISSING_COMPONENT
    elif ProbeStatus.UNKNOWN in statuses:
        status = Health.UNKNOWN
    else:
        status = Health.OK
    return ComponentHealth(component=comp, status=status, outcomes=outcomes, relogin_required=relogin)


def _core_health(ctx: InstallContext) -> tuple[Health, str]:
    if not ctx.location_online:
        return Health.OFFLINE, "the drive holding this application is not connected"
    if ctx.format == "appimage":
        if not ctx.payload_path or not os.path.isfile(ctx.payload_path):
            return Health.BROKEN, f"the AppImage is missing: {ctx.payload_path}"
        if not os.access(ctx.payload_path, os.X_OK):
            return Health.BROKEN, "the AppImage is not executable"
        return Health.OK, f"AppImage present at {ctx.payload_path}"
    if ctx.format == "flatpak":
        if ctx.runtime_installed is False:
            return Health.BROKEN, "its Flatpak runtime is not installed"
        if ctx.runtime_eol:
            return Health.OK, "installed; its runtime no longer receives security updates"
        return Health.OK, f"installed ({ctx.flatpak_ref})"
    if ctx.format == "pacman":
        return Health.OK, f"package {ctx.package} installed"
    return Health.UNKNOWN, "no core check for this format"


def _applies(comp: Component, ctx: InstallContext) -> bool:
    return not comp.applies_to_sources or (ctx.source_id in comp.applies_to_sources)


def evaluate(manifest: Manifest, ctx: InstallContext, *,
             probe: Callable[[dict[str, Any]], ProbeOutcome] = run_probe, trusted: bool = True,
             dismissed: frozenset[str] | set[str] = frozenset()) -> AppHealth:
    comps = {c.id: c for c in manifest.components if _applies(c, ctx)}
    cache: dict[str, ComponentHealth] = {}
    for cid in dismissed:
        if cid in comps and dismissible(manifest, cid):  # not probed, not suggested
            cache[cid] = ComponentHealth(component=comps[cid], status=Health.DISMISSED, outcomes=[])
    core_status, core_expl = _core_health(ctx)
    features: list[FeatureHealth] = []

    for f in manifest.features:
        if core_status is Health.OFFLINE:
            features.append(FeatureHealth(id=f.id, name=f.name, status=Health.OFFLINE, explanation=core_expl,
                                          optional=f.optional))
            continue
        chs = []
        for cid in f.requires:
            if cid not in comps:
                continue
            if cid not in cache:
                cache[cid] = _component_health(comps[cid], probe)
            chs.append(cache[cid])
        failing = [c for c in chs if c.status not in (Health.OK, Health.DISMISSED)]
        if chs and not failing and any(c.status is Health.DISMISSED for c in chs) and not f.core:
            features.append(FeatureHealth(id=f.id, name=f.name, status=Health.DISMISSED,
                                          explanation="You chose not to use this", optional=f.optional,
                                          components=chs))
            continue
        if f.core:
            status, expl = core_status, core_expl
            if status is Health.OK and failing:
                # Spec 6.4(2): only a hard component that definitely failed makes the app broken, and missing
                # evidence never does. Components that are not hard (recommended, optional) never change the
                # app's own state, whatever their outcome; they still show up as issues. Evidence we could not
                # get about a hard component keeps the app's state unknown.
                hard = [c for c in failing if c.component.relation == "hard"]
                definite = [c for c in hard if c.status is Health.MISSING_COMPONENT]
                unsure = [c for c in hard if c.status is not Health.MISSING_COMPONENT]
                if definite:
                    status = Health.BROKEN
                    shown = failing
                elif unsure:
                    status = Health.UNKNOWN
                    shown = unsure
                if status is not Health.OK:
                    expl = "; ".join(o.evidence for c in shown for o in c.outcomes if o.status is not ProbeStatus.PASS)
        elif not failing:
            status = Health.OK
            expl = "; ".join(o.evidence for c in chs for o in c.outcomes[:1]) or "nothing to check"
        else:
            missing = [c for c in failing if c.status is Health.MISSING_COMPONENT]
            unsure_hard = [c for c in failing if c.component.relation == "hard" and c.status is not Health.MISSING_COMPONENT]
            if f.optional:
                status = Health.OPTIONAL_UNAVAILABLE
            elif missing and any(c.component.relation == "hard" for c in missing):
                status = Health.MISSING_COMPONENT
            elif unsure_hard:  # a requirement that could not be checked must not be hidden behind a missing extra
                status = Health.UNKNOWN
            elif missing:
                status = Health.OPTIONAL_UNAVAILABLE
            else:
                status = Health.UNKNOWN
            names = ", ".join(c.component.name for c in failing)
            reasons = "; ".join(o.evidence for c in failing for o in c.outcomes if o.status is not ProbeStatus.PASS)
            lead = "Optional" if f.optional else ("Could not check" if status is Health.UNKNOWN else "Missing")
            expl = f"{lead}: {names} — {reasons}"
        features.append(FeatureHealth(id=f.id, name=f.name, status=status, explanation=expl, optional=f.optional,
                                      components=chs))

    considered = [f.status for f in features
                  if f.status is not Health.DISMISSED and not (f.optional and f.status is Health.OPTIONAL_UNAVAILABLE)]
    overall = max(considered or [Health.OK], key=SEVERITY_ORDER.index)
    if overall is Health.OK and any(f.status is Health.OPTIONAL_UNAVAILABLE for f in features):
        overall = Health.OPTIONAL_UNAVAILABLE
    issues = [i for ch in cache.values() if ch.status not in (Health.OK, Health.DISMISSED)
              for i in [component_issue(ch, manifest, trusted=trusted)] if i is not None]
    return AppHealth(app_id=manifest.application.id, name=manifest.application.name, overall=overall,
                     features=features, issues=issues, context=ctx)


_ISSUE_CODES = {
    "permission": "PERM_GROUP_MISSING", "system-service": "SVC_MISSING", "user-service": "SVC_MISSING",
    "browser-extension": "BROWSER_EXT_MISSING", "native-messaging-host": "NMH_MISSING",
    "codec": "CODEC_MISSING", "driver": "DRIVER_MISSING", "kernel-feature": "KERNEL_FEATURE_MISSING",
}


def component_issue(ch: ComponentHealth, manifest: Manifest, distro: str = "arch", *,
                    trusted: bool = True) -> Issue | None:
    """Turn a failing component into an Issue whose resolutions only redo what actually failed.

    Components from untrusted manifests are reported, but never come with actions.
    """
    comp = ch.component
    # Each probe with its own outcome (they are in the order of `comp.verify`): one probe passing must never
    # hide another probe of the same kind that failed.
    pairs = list(zip(comp.verify, ch.outcomes))
    failed = [o for _spec, o in pairs if o.status is not ProbeStatus.PASS]
    package_probes = [o for spec, o in pairs if spec.probe == "pacman_installed"]
    package_ok = bool(package_probes) and all(o.status is ProbeStatus.PASS for o in package_probes)
    code = _ISSUE_CODES.get(comp.type, "COMPANION_MISSING")
    actions: list[Action] = []
    install = (comp.platform_actions.get(distro) or comp.action) if trusted else None
    if install is not None and not package_ok:
        actions.append(Action(kind=install.kind, params=install.model_dump(exclude={"kind"})))
    for post in (comp.post if trusted else []):
        if post.kind.startswith("systemd"):
            # skip enabling a unit only when the probe for THAT unit passed
            own = [o for spec, o in pairs if spec.probe == "systemd_unit" and getattr(spec, "unit", None)
                   == getattr(post, "unit", None)]
            if own and all(o.status is ProbeStatus.PASS for o in own):
                continue
        actions.append(Action(kind=post.kind, params=post.model_dump(exclude={"kind"})))
    if comp.type == "system-service" and package_ok and any(
            o.probe == "systemd_unit" and o.facts.get("installed") for o in failed):
        code = "SVC_DISABLED"
    unavailable = sorted({a.kind for a in actions if a.kind not in IMPLEMENTED_ACTIONS})
    if unavailable:  # spec §7: a fix Cygnus cannot carry out is unavailable, never approximated
        actions = []
    needs_root = any(a.kind in PRIVILEGED_ACTIONS for a in actions)
    resolutions: list[Resolution] = []
    if ch.relogin_required:
        resolutions.append(Resolution(id="relogin", title="Log out and back in",
                                      explanation="The change is already made; your session needs to restart to "
                                                  "pick it up.", safety=SafetyClass.APPROVAL, recommended=True))
    elif actions:
        what = {"group.add_user": "Add you to the group", "pacman.install_local": "Install the package",
                "pacman.install_repo": "Install the package", "systemd.enable_now": "Enable and start the service",
                "browser.open_store": "Open the browser's extension store page"}
        steps = [what.get(a.kind, a.kind) for a in actions]
        note = f" {comp.security_note}" if comp.security_note else ""
        resolutions.append(Resolution(
            id=f"install-{comp.id}", title=f"Install missing component: {comp.name}",
            explanation=" → ".join(steps) + "." + note
                        + (" You will need to log out and back in afterwards." if comp.requires_relogin else ""),
            safety=SafetyClass.APPROVAL,
            privilege=Privilege.ROOT if needs_root else Privilege.USER,
            recommended=comp.relation != "optional", actions=actions))
    severity = IssueSeverity.NOTICE if comp.relation == "optional" else IssueSeverity.DEGRADED
    return Issue(code=code, severity=severity, title=f"{comp.name} is not working",
                 explanation="; ".join(o.evidence for o in failed),
                 facts={"component": comp.id, "relation": comp.relation,
                        "discouraged_vendor_instructions": comp.discouraged_vendor_instructions,
                        **({"unavailable_actions": unavailable} if unavailable else {})},
                 evidence=[o.evidence for o in ch.outcomes], resolutions=resolutions)


def render_text(h: AppHealth) -> str:
    lines = [f"{h.name}  —  {SYMBOL[h.overall]} {h.overall.value.replace('_', ' ')}"]
    width = max(len(f.name) for f in h.features) + 2
    for f in h.features:
        sym = "○" if f.optional and f.status is Health.OPTIONAL_UNAVAILABLE else SYMBOL[f.status]
        lines.append(f"  {f.name:<{width}} {sym}  {f.explanation}")
    for i in h.issues:
        lines.append(f"  [{i.code}] {i.title}")
        for r in i.resolutions:
            lines.append(f"      → {r.title} ({r.safety.value}, {r.privilege.value}): {r.explanation}")
    return "\n".join(lines)
