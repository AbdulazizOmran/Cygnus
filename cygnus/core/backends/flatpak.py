"""Flatpak analysis via libflatpak (GObject introspection) — read-only (architecture §7.2, §9)."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cygnus.core.errors import CygnusError
from cygnus.core.models import Candidate, PackageFormat
from cygnus.core.recovery.model import (
    Action, Issue, IssueSeverity, Privilege, Resolution, SafetyClass, explain_only,
)


def _gi():
    try:
        import gi

        gi.require_version("Flatpak", "1.0")
        from gi.repository import Flatpak, GLib
    except (ImportError, ValueError) as exc:
        raise CygnusError(f"libflatpak introspection is unavailable: {exc}") from exc
    return Flatpak, GLib


@dataclass(frozen=True, slots=True)
class RefParts:
    kind: str  # app | runtime
    name: str
    arch: str
    branch: str

    @classmethod
    def parse(cls, ref: str, default_kind: str = "runtime") -> RefParts:
        parts = ref.split("/")
        if parts[0] in ("app", "runtime"):
            kind, parts = parts[0], parts[1:]
        else:
            kind = default_kind
        if len(parts) != 3:
            raise ValueError(f"not a full Flatpak ref: {ref!r}")
        return cls(kind, *parts)

    def __str__(self) -> str:
        return f"{self.kind}/{self.name}/{self.arch}/{self.branch}"


@dataclass(slots=True, kw_only=True)
class RuntimeStatus:
    ref: RefParts
    installed_in: list[str] = field(default_factory=list)  # installation ids ("user", "default", ...)
    installed_eol: str | None = None
    remotes: dict[str, dict[str, Any]] = field(default_factory=dict)  # remote -> {available, eol, eol_rebase, size}
    target: str | None = None  # the installation the app goes into; runtimes must live there too (§2.4)

    @property
    def installed(self) -> bool:
        return self.target in self.installed_in if self.target else bool(self.installed_in)

    @property
    def install_source(self) -> tuple[str, str] | None:
        """(installation, remote) to install the runtime from, preferring the target's own remotes."""
        available = [(info.get("installation"), r) for r, info in self.remotes.items() if info.get("available")]
        available.sort(key=lambda x: x[0] != self.target)
        return available[0] if available else None

    @property
    def available_remotes(self) -> list[str]:
        return [r for r, info in self.remotes.items() if info.get("available")]

    @property
    def eol(self) -> str | None:
        if self.installed_eol:
            return self.installed_eol
        for info in self.remotes.values():
            if info.get("eol"):
                return info["eol"]
        return None


def user_dir() -> Path:
    """The user installation's directory, resolved like libflatpak does. libflatpak computes this
    once per process; Cygnus resolves it on every call and opens the installation by path, so the
    directory it relocates and the one it installs into can never disagree."""
    env = os.environ.get("FLATPAK_USER_DIR", "")
    if env and os.path.isabs(env):
        return Path(env)
    data = os.environ.get("XDG_DATA_HOME", "")
    return (Path(data) if data and os.path.isabs(data) else Path.home() / ".local/share") / "flatpak"


def user_installation():
    Flatpak, _ = _gi()
    from gi.repository import Gio

    return Flatpak.Installation.new_for_path(Gio.File.new_for_path(str(user_dir())), True, None)


class FlatpakEnv:
    """Thin facade over the installations visible to this user (system ones plus the user one)."""

    def __init__(self, installations=None, *, target=None):
        """`target`: the installation an app will go into. Its runtime then counts as installed only
        when it is installed *there* (architecture §2.4: no cross-installation runtime sharing)."""
        Flatpak, self.GLib = _gi()
        self.Flatpak = Flatpak
        if installations is None:
            installations = list(Flatpak.get_system_installations(None))
            try:
                installations.append(user_installation())
            except self.GLib.Error:
                pass
        if target is not None:
            installations = [target] + [i for i in installations if self.inst_id(i) != self.inst_id(target)]
        self.installations = installations
        self.target = target

    @staticmethod
    def inst_id(inst) -> str:
        # "user" is the user's own installation; anything opened by another path is identified by it.
        path = inst.get_path().get_path()
        if inst.get_is_user():
            return "user" if path == str(user_dir()) else path
        return inst.get_id() or path

    def remotes(self) -> list[tuple[Any, Any]]:
        out = []
        for inst in self.installations:
            try:
                for r in inst.list_remotes(None):
                    out.append((inst, r))
            except self.GLib.Error:
                continue
        return out

    def installed_refs(self) -> list[tuple[Any, Any]]:
        out = []
        for inst in self.installations:
            try:
                out.extend((inst, ref) for ref in inst.list_installed_refs(None))
            except self.GLib.Error:
                continue
        return out

    def runtime_status(self, runtime: str, *, query_remotes: bool = True) -> RuntimeStatus:
        ref = RefParts.parse(runtime, "runtime")
        status = RuntimeStatus(ref=ref, target=self.inst_id(self.target) if self.target is not None else None)
        kind = self.Flatpak.RefKind.RUNTIME if ref.kind == "runtime" else self.Flatpak.RefKind.APP
        for inst in self.installations:
            try:
                installed = inst.get_installed_ref(kind, ref.name, ref.arch, ref.branch, None)
            except self.GLib.Error:
                continue
            status.installed_in.append(self.inst_id(inst))
            if status.target in (None, self.inst_id(inst)):
                status.installed_eol = status.installed_eol or installed.get_eol() or None
        if not query_remotes:
            return status
        seen = set()
        for inst, remote in self.remotes():
            name = remote.get_name()
            if remote.get_disabled() or not remote.get_url() or name in seen:
                continue
            seen.add(name)
            try:
                rr = inst.fetch_remote_ref_sync(name, kind, ref.name, ref.arch, ref.branch, None)
            except self.GLib.Error as exc:
                not_found = exc.domain == "flatpak-error-quark" and exc.code in (
                    int(self.Flatpak.Error.REF_NOT_FOUND), int(self.Flatpak.Error.RUNTIME_NOT_FOUND))
                status.remotes[name] = {"available": False, "error": exc.message,
                                        "unreachable": not not_found and "not found" not in exc.message.lower()}
                continue
            status.remotes[name] = {
                "available": True, "eol": rr.get_eol() or None, "eol_rebase": rr.get_eol_rebase() or None,
                "download_size": rr.get_download_size(), "installed_size": rr.get_installed_size(),
                "installation": self.inst_id(inst),
            }
        return status

    def gl_driver_extension(self) -> str | None:
        """Expected NVIDIA GL extension name for the host driver, or None without NVIDIA."""
        try:
            version = Path("/sys/module/nvidia/version").read_text().strip()
        except OSError:
            return None
        return "org.freedesktop.Platform.GL.nvidia-" + version.replace(".", "-")


def analyse_runtime_requirement(runtime: str, env: FlatpakEnv, *, app_name: str,
                                runtime_repo: str | None = None, newer_build: dict[str, Any] | None = None,
                                alternatives: list[dict[str, Any]] | None = None) -> list[Issue]:
    """The runtime-resolution algorithm of architecture §9, steps 2–9.

    `newer_build` describes a newer vendor build on a supported runtime (from a manifest), and
    `alternatives` lists other vendor formats; both are optional inputs, never guessed.
    """
    status = env.runtime_status(runtime)
    facts = {"runtime": str(status.ref), "installed_in": status.installed_in, "remotes": status.remotes}
    issues: list[Issue] = []
    eol = status.eol
    newer = [Resolution(id="newer-build", title=f"Install the newer {app_name} build",
                        explanation=f"Version {newer_build.get('version')} uses the supported runtime "
                                    f"{newer_build.get('runtime')}.",
                        safety=SafetyClass.APPROVAL, recommended=True, rank=5,
                        actions=[Action(kind="flatpak.install_bundle", params=newer_build)])] if newer_build else []
    alts = [Resolution(id=f"alt-{a['id']}", title=f"Use the official {a['format_label']} instead",
                       explanation=a.get("explanation", "The vendor also provides this format, which does not "
                                                        "need this runtime."),
                       safety=SafetyClass.APPROVAL, recommended=not newer_build, rank=8,
                       actions=[Action(kind="source.switch", params={"source": a["id"]})])
            for a in (alternatives or [])]

    if status.installed and not eol:
        return issues
    if status.installed and eol:
        issues.append(Issue(
            code="FP_RUNTIME_EOL", severity=IssueSeverity.DEGRADED,
            title=f"{app_name} uses a runtime that is no longer supported",
            explanation=f"{status.ref.name} {status.ref.branch} is installed but no longer receives fixes or "
                        "security updates.",
            evidence=[eol],
            facts=facts,
            resolutions=newer + alts + [explain_only(
                "wait-vendor", "Ask the vendor for an updated build",
                f"Flatpak cannot switch {app_name} to a newer runtime by itself; only a rebuild by the vendor can.")]))
        return issues

    source = status.install_source
    if source:
        source_inst, remote = source
        info = status.remotes[remote]
        privilege = Privilege.USER if status.target == "user" else Privilege.FLATPAK_SYSTEM
        # A user installation gets the runtime from the same, already trusted repository: when only
        # another installation has that repository configured, it is copied over (address + signing key).
        mirror = status.target is not None and source_inst != status.target
        facts = {**facts, "install_from": {"remote": remote, "installation": source_inst, "mirror": mirror}}
        mirror_text = (f" {remote} is configured for the whole system, so Cygnus will also add it to your personal "
                       "Flatpak installation with the same address and signing key." if mirror else "")
        if eol:
            issues.append(Issue(
                code="FP_RUNTIME_EOL", severity=IssueSeverity.BLOCKER,
                title=f"{app_name} needs an obsolete runtime",
                explanation=(f"It requires {status.ref.name} {status.ref.branch}, which is available from "
                             f"{remote} but no longer receives fixes or security updates. "
                             f"Cygnus will not substitute a different runtime version."),
                facts=facts,
                resolutions=newer + alts + [
                    Resolution(id="install-eol-runtime", title="Install with the obsolete runtime (not recommended)",
                               explanation=f"Downloads about {info.get('download_size', 0) // 2**20} MiB. The app "
                                           "will work, but the runtime gets no security fixes.",
                               safety=SafetyClass.APPROVAL, privilege=privilege, rank=50,
                               actions=[Action(kind="flatpak.install",
                                               params={"ref": str(status.ref), "remote": remote,
                                                       "mirror_from": source_inst if mirror else None})]),
                    explain_only("cancel", "Cancel", "Do not install."),
                ]))
        else:
            issues.append(Issue(
                code="FP_RUNTIME_MISSING", severity=IssueSeverity.NOTICE,
                title=f"Will also install the {status.ref.name} {status.ref.branch} runtime",
                explanation=f"The runtime is available from {remote} and is supported." + mirror_text,
                facts=facts,
                resolutions=[Resolution(id="install-runtime", title="Install the runtime",
                                        explanation=f"About {info.get('download_size', 0) // 2**20} MiB to download.",
                                        safety=SafetyClass.APPROVAL, privilege=privilege,
                                        recommended=True, rank=1,
                                        actions=[Action(kind="flatpak.install",
                                                        params={"ref": str(status.ref), "remote": remote,
                                                                "mirror_from": source_inst if mirror else None})])]))
        return issues

    if any(info.get("unreachable") for info in status.remotes.values()):
        issues.append(Issue(
            code="FP_REMOTE_UNREACHABLE", severity=IssueSeverity.BLOCKER,
            title="Cannot check the Flatpak repositories right now",
            explanation="The repository could not be reached (offline?): "
                        + "; ".join(i["error"] for i in status.remotes.values() if i.get("unreachable")),
            facts=facts,
            resolutions=[Resolution(id="retry", title="Try again", explanation="Check your connection and retry.",
                                    safety=SafetyClass.AUTO)]))
        return issues

    if runtime_repo and not _repo_configured(runtime_repo, env):
        issues.append(Issue(
            code="FP_REMOTE_MISSING", severity=IssueSeverity.BLOCKER,
            title=f"The runtime for {app_name} comes from a repository you have not added",
            explanation=f"{app_name} declares {runtime_repo} as the source of its runtime.",
            facts={**facts, "runtime_repo": runtime_repo},
            resolutions=newer + alts + [explain_only(
                "add-remote", "Add the declared repository yourself, if you trust it",
                f"Cygnus never adds repositories that a downloaded file names. If you trust {runtime_repo}, add it "
                "in Discover or with `flatpak remote-add`, check its signing key, then try again.")]))
        return issues

    issues.append(Issue(
        code="FP_RUNTIME_UNAVAILABLE", severity=IssueSeverity.BLOCKER,
        title=f"{app_name} cannot be installed: its runtime is unavailable",
        explanation=(f"It requires {status.ref.name} {status.ref.branch}, which none of your Flatpak repositories "
                     "offers. Flatpak cannot run the app with a different runtime version."),
        facts=facts,
        resolutions=newer + alts + [explain_only(
            "contact-vendor", "No safe fix available",
            "Ask the vendor for a build that uses a current runtime, or use another format they provide.")]))
    return issues


def _normalise_url(u: str | None) -> str:
    return (u or "").strip().rstrip("/").lower()


def _repo_configured(flatpakrepo_url: str, env: FlatpakEnv, fetch=None) -> bool:
    """Is the repository described by a .flatpakrepo file already configured (compared by its Url=)?"""
    configured = {_normalise_url(r.get_url()) for _, r in env.remotes()}
    from cygnus.core.util import http

    try:
        text = (fetch or http.get)(flatpakrepo_url, limit=64 * 1024).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - unknown: assume not configured, the user will be asked
        return False
    for line in text.splitlines():
        if line.strip().startswith("Url="):
            return _normalise_url(line.split("=", 1)[1]) in configured
    return False


# Permissions that amount to leaving the sandbox (or persisting outside it).
_ESCAPE_BUS_NAMES = ("org.freedesktop.Flatpak", "org.freedesktop.systemd1", "org.freedesktop.portal.Flatpak",
                     "org.freedesktop.PolicyKit1")
_ESCAPE_FILESYSTEMS = ("host", "host-os", "host-etc", "xdg-config/autostart", "home")


def permission_audit(meta: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (sandbox escapes, broad permissions) in plain language."""
    perms = meta.get("permissions") or {}
    escapes, broad = [], []
    for bus in ("session_bus", "system_bus"):
        for name, policy in (meta.get(bus) or {}).items():
            if name.startswith(_ESCAPE_BUS_NAMES) and policy in ("talk", "own"):
                escapes.append(f"can talk to {name} ({'system' if bus == 'system_bus' else 'session'} bus)")
    sockets = [x for x in (perms.get("sockets") or "").split(";") if x]
    for s_ in ("system-bus", "session-bus"):
        if s_ in sockets:
            escapes.append(f"full access to the {s_.replace('-', ' ')}")
    for item in [x for x in (perms.get("filesystems") or "").split(";") if x]:
        name, _, mode = item.partition(":")
        if name in ("host", "host-os", "host-etc", "xdg-config/autostart") or name.startswith("xdg-config/autostart"):
            escapes.append(f"filesystem access to {name}" + (" (read-only)" if mode == "ro" else ""))
        elif name in ("home", "~"):
            broad.append("your home folder" + (" (read-only)" if mode == "ro" else ""))
    if "all" in [x for x in (perms.get("devices") or "").split(";") if x]:
        broad.append("all devices")
    if "devel" in (perms.get("features") or ""):
        broad.append("debugging features (features=devel)")
    return escapes, broad


def analyse_bundle(cand: Candidate, env: FlatpakEnv, *, newer_build=None, alternatives=None) -> list[Issue]:
    if cand.format not in (PackageFormat.FLATPAK_BUNDLE, PackageFormat.FLATPAK_REMOTE):
        raise ValueError("not a Flatpak bundle or remote app")
    issues: list[Issue] = []
    runtime = cand.metadata.get("runtime")
    name = cand.name or cand.identity.get("flatpak_id", "the application")
    if runtime:
        issues += analyse_runtime_requirement(runtime, env, app_name=name,
                                              runtime_repo=cand.metadata.get("runtime_repo"),
                                              newer_build=newer_build, alternatives=alternatives)
    if not cand.metadata.get("origin_url"):
        issues.append(Issue(
            code="FP_BUNDLE_NO_UPDATE_SOURCE", severity=IssueSeverity.NOTICE,
            title=f"{name} will not update automatically",
            explanation="This bundle does not say where updates come from. To update, a newer bundle has to "
                        "be downloaded from the vendor.",
            resolutions=[explain_only("manual", "Manual updates", "Cygnus will tell you when it knows of a newer "
                                                                   "build (from a vendor manifest).")]))
    escapes, broad = permission_audit(cand.metadata)
    if escapes:
        issues.append(Issue(
            code="FP_SANDBOX_ESCAPE", severity=IssueSeverity.DEGRADED,
            title=f"{name} can leave its sandbox",
            explanation="It requests: " + "; ".join(escapes) + ". It effectively has the same access as a "
                        "normal program.", facts={"escapes": escapes}))
    if broad:
        issues.append(Issue(
            code="FP_BROAD_PERMISSIONS", severity=IssueSeverity.NOTICE,
            title=f"{name} asks for broad access",
            explanation="Requested access to: " + ", ".join(broad), facts={"broad": broad}, resolutions=[]))
    return issues


def _perm_mode(perms: dict[str, str], spec: str) -> str | None:
    """Return the access mode ('rw', 'ro', 'create') if the permission is granted, else None."""
    key, _, value = spec.partition("=")
    for item in (perms.get(key) or "").split(";"):
        name, _, mode = item.partition(":")
        if name == value:
            return mode or "rw"
    return None


def installed_apps_with_runtime(env: FlatpakEnv) -> list[dict[str, Any]]:
    """Installed apps with their runtime and EOL state (for health checks and the adopt flow)."""
    out = []
    by_ref = {}
    for inst, ref in env.installed_refs():
        by_ref[(ref.format_ref())] = (inst, ref)
    for (fmt, (inst, ref)) in by_ref.items():
        if not fmt.startswith("app/"):
            continue
        meta_text = ref.load_metadata(None).get_data().decode("utf-8", "replace")
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        cp.read_string(meta_text)
        runtime = cp.get("Application", "runtime", fallback=None)
        rt = by_ref.get(f"runtime/{runtime}") if runtime else None
        out.append({
            "ref": fmt, "installation": env.inst_id(inst), "origin": ref.get_origin(),
            "version": ref.get_appdata_version(), "name": ref.get_appdata_name() or ref.get_name(),
            "installed_size": ref.get_installed_size(), "runtime": runtime,
            "runtime_installed": rt is not None, "runtime_eol": rt[1].get_eol() if rt else None,
            "eol": ref.get_eol() or None,
        })
    return out
