"""CAM v1 schema (pydantic models; the JSON Schema is generated from these).

Design rules enforced here:
  * the action vocabulary is closed — there is no way to express a shell command;
  * probes must name a known probe type with valid parameters;
  * cross references (features ↔ components, conflicts ↔ sources) must resolve;
  * user-data paths are confined to the user's home directory.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

_REVERSE_DNS = r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+$"
_COMPONENT_ID = r"^[a-z0-9][a-z0-9-]{0,63}$"
_SHA256 = r"^[0-9a-f]{64}$"
_PGP_FPR = r"^[0-9A-F]{40}$"
_PKG_NAME = r"^[a-z0-9@_+][a-z0-9@._+-]{0,99}$"  # as the helper reads it: no leading "-" or "."
_UNIT = r"^[A-Za-z0-9_@][A-Za-z0-9:_.@-]*\.(service|socket|timer|path)$"  # no leading '-'
_FLATPAK_REF = r"^(app|runtime)/[A-Za-z][A-Za-z0-9_.-]*/[A-Za-z0-9_]+/[A-Za-z0-9_.-]+$"
_REMOTE_NAME = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$"
_CHROMIUM_EXT = r"^[a-p]{32}$"
_FIREFOX_EXT = r"^([A-Za-z0-9._+-]+@[A-Za-z0-9.-]+|\{[0-9a-fA-F-]{36}\})$"
_SUBSTRING = Annotated[str, Field(min_length=1, max_length=120)]
_NAME = Annotated[str, Field(min_length=1, max_length=120)]  # a display name
_TEXT = Annotated[str, Field(max_length=2000)]  # explanatory text shown to the user
_LINE = Annotated[str, Field(max_length=500)]  # one entry of a list of notes
_IDS = Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=50)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- probes ---------------------------------------------------------------------------------------
class GroupMembershipProbe(_Strict):
    probe: Literal["group_membership"]
    group: Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_-]{0,31}$")]
    effective: Literal["configured", "session"] = "session"


class ReadableProbe(_Strict):
    probe: Literal["readable"]
    path: Annotated[str, Field(pattern=r"^/(dev|sys|proc)/[A-Za-z0-9_.*?\[\]/-]+$", max_length=200)]

    @field_validator("path")
    @classmethod
    def _confined(cls, v: str) -> str:
        import posixpath

        if ".." in v.split("/") or not posixpath.normpath(v).startswith(("/dev/", "/sys/", "/proc/")):
            raise ValueError("readable probes may only look at /dev, /sys or /proc")
        return v


class SystemdUnitProbe(_Strict):
    probe: Literal["systemd_unit"]
    unit: Annotated[str, Field(pattern=_UNIT)]
    scope: Literal["system", "user"] = "system"
    expect: list[Literal["enabled", "active"]] = ["enabled", "active"]


class JournalMatchProbe(_Strict):
    """Passes if any recent log line of the unit contains one of `contains` (plain substrings:
    manifests never supply regular expressions, which could hang the health check)."""

    probe: Literal["journal_recent_match"]
    unit: Annotated[str, Field(pattern=_UNIT)]
    contains: Annotated[list[_SUBSTRING], Field(min_length=1, max_length=10)]
    within_minutes: Annotated[int, Field(ge=1, le=10080)] = 30


class TcpListenerProbe(_Strict):
    probe: Literal["tcp_listener"]
    addr: Literal["127.0.0.1", "::1", "0.0.0.0", "::"] = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)]
    owner_exe_contains: _SUBSTRING | None = None


class TcpPeerProbe(_Strict):
    probe: Literal["tcp_peer"]
    addr: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)]


class PackageInstalledProbe(_Strict):
    probe: Literal["pacman_installed"]
    name: Annotated[str, Field(pattern=_PKG_NAME)]


class BrowserExtensionProbe(_Strict):
    probe: Literal["browser_extension_present"]
    family: Literal["chromium", "firefox"]
    id: Annotated[str, Field(max_length=120)]

    @model_validator(mode="after")
    def _id_shape(self) -> BrowserExtensionProbe:
        if not re.fullmatch(_CHROMIUM_EXT if self.family == "chromium" else _FIREFOX_EXT, self.id):
            raise ValueError(f"invalid {self.family} extension id")
        return self


class SysctlProbe(_Strict):
    probe: Literal["sysctl"]
    key: Annotated[str, Field(pattern=r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")]
    expect: Annotated[str, Field(max_length=64)] | None = None
    minimum: int | None = None  # numeric lower bound, e.g. user.max_user_namespaces >= 1
    absent_ok: bool = False  # the setting exists only on some kernels; where it is missing, the probe passes

    @model_validator(mode="after")
    def _one(self) -> SysctlProbe:
        if (self.expect is None) == (self.minimum is None):
            raise ValueError("sysctl probes need exactly one of 'expect' or 'minimum'")
        return self


class ProcessRunningProbe(_Strict):
    probe: Literal["process_running"]
    exe_contains: _SUBSTRING


Probe = Annotated[
    GroupMembershipProbe | ReadableProbe | SystemdUnitProbe | JournalMatchProbe | TcpListenerProbe
    | TcpPeerProbe | PackageInstalledProbe | BrowserExtensionProbe | SysctlProbe | ProcessRunningProbe,
    Field(discriminator="probe"),
]


# -- actions (closed vocabulary, architecture §13.4) ------------------------------------------------
class PacmanInstallRepo(_Strict):
    kind: Literal["pacman.install_repo"]
    names: list[Annotated[str, Field(pattern=_PKG_NAME)]]


class PacmanInstallLocal(_Strict):
    kind: Literal["pacman.install_local"]
    url: Annotated[str, Field(pattern=r"^https://")]
    sha256: Annotated[str, Field(pattern=_SHA256)]


class AurBuild(_Strict):
    kind: Literal["aur.build"]
    pkgbase: Annotated[str, Field(pattern=_PKG_NAME)]


class FlatpakInstall(_Strict):
    kind: Literal["flatpak.install"]
    ref: Annotated[str, Field(pattern=_FLATPAK_REF)]
    remote: Annotated[str, Field(pattern=_REMOTE_NAME)]


class FlatpakInstallBundle(_Strict):
    kind: Literal["flatpak.install_bundle"]
    url: Annotated[str, Field(pattern=r"^https://")]
    sha256: Annotated[str, Field(pattern=_SHA256)] | None = None


# Overrides a manifest may request: only narrow, non-escaping permissions. Anything that grants
# host access, bus access to Flatpak/systemd, autostart persistence or all devices is refused.
_OVERRIDE_ALLOWED = re.compile(
    r"^--(share=(network|ipc)"
    r"|socket=(x11|wayland|fallback-x11|pulseaudio|pcsc|cups)"
    r"|device=(dri|input|usb|kvm)"
    r"|filesystem=xdg-(download|music|videos|pictures|documents|desktop)(/[A-Za-z0-9_. -]+)?(:(ro|rw|create))?"
    r"|talk-name=[A-Za-z_][A-Za-z0-9_.-]*"
    r"|env=[A-Z_][A-Z0-9_]*=[A-Za-z0-9_.:/-]*)$")
_OVERRIDE_DENIED_NAMES = ("org.freedesktop.Flatpak", "org.freedesktop.portal.Flatpak", "org.freedesktop.systemd1",
                          "org.freedesktop.login1", "org.freedesktop.PolicyKit1", "org.freedesktop.PackageKit",
                          "org.freedesktop.impl.portal.", "org.kde.KWin", "org.kde.kwalletd", "org.kde.KWallet",
                          "org.kde.klauncher", "org.freedesktop.secrets", "org.gnome.keyring", "org.gnome.SessionManager")
# Variables that change what code a program loads or where it looks for it.
_OVERRIDE_DENIED_ENV = re.compile(r"(?:LD_.*|GIO_.*|GTK.*|QT_.*|QML.*|GST_.*|PYTHON.*|PERL.*|RUBY.*|NODE_.*|XDG_.*|"
                                  r"DBUS_.*|PATH|IFS|ENV|BASH_ENV)")


class FlatpakOverride(_Strict):
    kind: Literal["flatpak.override"]
    app: Annotated[str, Field(pattern=_REVERSE_DNS)]
    permissions: Annotated[list[Annotated[str, Field(max_length=200)]], Field(min_length=1, max_length=10)]

    @field_validator("permissions")
    @classmethod
    def _allowed(cls, perms: list[str]) -> list[str]:
        for p in perms:
            # fullmatch: `$` would also accept a trailing newline.
            if not _OVERRIDE_ALLOWED.fullmatch(p):
                raise ValueError(f"Flatpak permission not allowed in manifests: {p}")
            if p.startswith("--filesystem=") and "/" in p:
                # "." and ".." are plain characters to the regex but would leave the xdg folder.
                if p.split("/", 1)[1].split(":", 1)[0] in (".", ".."):
                    raise ValueError(f"Flatpak permission not allowed in manifests: {p}")
            if p.startswith("--talk-name=") and p.split("=", 1)[1].startswith(_OVERRIDE_DENIED_NAMES):
                raise ValueError(f"Flatpak permission not allowed in manifests: {p}")
            if p.startswith("--env=") and _OVERRIDE_DENIED_ENV.fullmatch(p.split("=", 2)[1]):
                raise ValueError(f"environment variable not allowed: {p}")
        return perms


class AppImageInstall(_Strict):
    kind: Literal["appimage.install"]
    url: Annotated[str, Field(pattern=r"^https://")]


class SystemdEnableNow(_Strict):
    kind: Literal["systemd.enable_now", "systemd.user.enable_now"]
    unit: Annotated[str, Field(pattern=_UNIT)]


class GroupAddUser(_Strict):
    kind: Literal["group.add_user"]
    group: Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_-]{0,31}$")]


class BrowserOpenStore(_Strict):
    kind: Literal["browser.open_store"]


class InfoShow(_Strict):
    kind: Literal["info.show"]
    text: Annotated[str, Field(max_length=2000)]


ActionSpec = Annotated[
    PacmanInstallRepo | PacmanInstallLocal | AurBuild | FlatpakInstall | FlatpakInstallBundle | FlatpakOverride
    | AppImageInstall | SystemdEnableNow | GroupAddUser | BrowserOpenStore | InfoShow,
    Field(discriminator="kind"),
]
# The actions this consumer can carry out. Spec §7: for any other kind the fix is unavailable, never approximated.
IMPLEMENTED_ACTIONS = frozenset({"pacman.install_repo", "pacman.install_local", "systemd.enable_now", "group.add_user",
                                 "browser.open_store"})

# Actions that need administrator approval through Cygnus's root helper. Flatpak actions are not
# listed: they run as the user (user installation, user-level overrides), and a system installation
# authorizes through Flatpak's own polkit rules.
PRIVILEGED_ACTIONS = frozenset({"pacman.install_repo", "pacman.install_local", "aur.build", "systemd.enable_now",
                                "group.add_user"})


# -- application, sources ---------------------------------------------------------------------------
class Vendor(_Strict):
    name: _NAME
    domain: Annotated[str, Field(pattern=r"^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$", max_length=253)] | None = None


class Platform(_Strict):
    os: Literal["linux"] = "linux"
    arch: list[Literal["x86_64", "aarch64", "i686", "any"]]
    sessions: list[Literal["x11", "wayland"]] | None = None


class ApplicationInfo(_Strict):
    id: Annotated[str, Field(pattern=_REVERSE_DNS)]
    appstream_ids: list[Annotated[str, Field(pattern=_REVERSE_DNS, max_length=200)]] = []
    flatpak_ids: list[Annotated[str, Field(pattern=_REVERSE_DNS, max_length=200)]] = []
    package_names: list[Annotated[str, Field(pattern=_PKG_NAME)]] = []
    desktop_ids: _IDS = []
    name: _NAME
    vendor: Vendor
    homepage: Annotated[str, Field(pattern=r"^https://")] | None = None
    license: Annotated[str, Field(max_length=200)] | None = None
    supported_versions: Annotated[str, Field(max_length=200)] | None = None
    platforms: list[Platform]


class Verification(_Strict):
    type: Literal["tls-only", "sha256", "openpgp-detached", "appimage-openpgp", "pkgbuild-openpgp",
                  "pacman-signature"]
    sha256: Annotated[str, Field(pattern=_SHA256)] | None = None
    openpgp_fingerprint: Annotated[str, Field(pattern=_PGP_FPR)] | None = None
    signature_url: Annotated[str, Field(pattern=r"^https://")] | None = None

    @model_validator(mode="after")
    def _complete(self) -> Verification:
        if self.type == "sha256" and not self.sha256:
            raise ValueError("verification type sha256 needs 'sha256'")
        if self.type in ("openpgp-detached", "appimage-openpgp", "pkgbuild-openpgp") and not self.openpgp_fingerprint:
            raise ValueError(f"verification type {self.type} needs a pinned 'openpgp_fingerprint'")
        if self.type == "openpgp-detached" and not self.signature_url:
            raise ValueError("verification type openpgp-detached needs a 'signature_url'")
        return self


class UpdateSpec(_Strict):
    type: Literal["zsync", "gh-releases", "flatpak-remote", "pacman", "aur", "manual", "none"]
    self_updating: bool = False
    url: Annotated[str, Field(pattern=r"^https://")] | None = None


class Source(_Strict):
    id: Annotated[str, Field(pattern=_COMPONENT_ID)]
    format: Literal["appimage", "flatpak-bundle", "flatpak-remote", "pacman-repo", "aur", "pacman-local", "deb",
                    "rpm", "tarball"]
    label: _NAME | None = None
    channel: Literal["stable", "beta"] = "stable"
    url: Annotated[str, Field(pattern=r"^https://")] | None = None
    ref: str | None = None
    package: Annotated[str, Field(pattern=_PKG_NAME)] | None = None
    runtime: str | None = None
    update: UpdateSpec
    verification: Verification
    vendor_supported: bool = True
    recommended_when: list[Literal["relocatable", "system-package", "wayland-window-tracking", "default"]] = []
    known_issues: Annotated[list[_LINE], Field(max_length=50)] = []
    notes: _TEXT | None = None

    @model_validator(mode="after")
    def _verification_fits_format(self) -> Source:
        if self.verification.type == "pkgbuild-openpgp" and self.format != "aur":
            raise ValueError("verification type pkgbuild-openpgp is only for AUR sources")
        return self


# -- features and components -------------------------------------------------------------------------
class BrowserTarget(_Strict):
    id: str
    store_url: Annotated[str, Field(pattern=r"^https://")] | None = None


class Component(_Strict):
    id: Annotated[str, Field(pattern=_COMPONENT_ID)]
    name: _NAME
    description: _TEXT | None = None
    type: Literal["permission", "system-service", "user-service", "browser-extension", "native-messaging-host",
                  "companion-app", "runtime", "codec", "driver", "plugin", "cli-helper", "kernel-feature"]
    relation: Literal["hard", "recommended", "optional"]
    action: ActionSpec | None = None
    platform_actions: dict[Literal["arch"], ActionSpec] = {}
    post: list[ActionSpec] = []
    verify: list[Probe] = []
    verify_mode: Literal["all", "any"] = "all"  # "any": e.g. an extension installed in any one browser
    privileges: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=20)] = []
    requires_relogin: bool = False
    applies_to_sources: list[str] = []  # empty = all sources
    browsers: dict[Literal["chromium", "firefox"], BrowserTarget] = {}
    security_note: _TEXT | None = None
    notes: _TEXT | None = None
    discouraged_vendor_instructions: Annotated[list[_LINE], Field(max_length=20)] = []  # quoted text, never executed


class Feature(_Strict):
    id: Annotated[str, Field(pattern=_COMPONENT_ID)]
    name: _NAME
    core: bool = False
    optional: bool = False
    requires: list[str] = []
    notes: _TEXT | None = None


class Conflict(_Strict):
    between: list[str]
    reason: _TEXT


class UserData(_Strict):
    paths: list[str] = []

    @field_validator("paths")
    @classmethod
    def _inside_home(cls, paths: list[str]) -> list[str]:
        prefixes = ("~/", "$XDG_DATA_HOME/", "$XDG_CONFIG_HOME/", "$XDG_CACHE_HOME/", "$XDG_STATE_HOME/")
        for p in paths:
            prefix = next((x for x in prefixes if p.startswith(x)), None)
            rest = p[len(prefix):] if prefix else ""
            parts = [x for x in rest.split("/") if x]
            if (prefix is None or not parts or any(x in (".", "..") for x in rest.split("/") if x)
                    or "//" in rest or any(c in p for c in "*?[\\") or len(p) > 200
                    or (prefix == "~/" and len(parts) == 1 and parts[0] in (".config", ".local", ".cache"))):
                raise ValueError(f"user-data path must name a specific folder inside the home directory: {p!r}")
        return paths


class Manifest(_Strict):
    manifest_version: Literal[1]
    serial: Annotated[int, Field(ge=1)]
    expires: AwareDatetime
    application: ApplicationInfo
    sources: list[Source]
    features: list[Feature]
    components: list[Component] = []
    conflicts: list[Conflict] = []
    user_data: UserData = UserData()

    @model_validator(mode="after")
    def _references(self) -> Manifest:
        source_ids = [s.id for s in self.sources]
        comp_ids = [c.id for c in self.components]
        feat_ids = [f.id for f in self.features]
        for kind, ids in (("source", source_ids), ("component", comp_ids), ("feature", feat_ids)):
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {kind} ids")
        if not any(f.core for f in self.features):
            raise ValueError("exactly one feature must be marked core")
        if sum(f.core for f in self.features) > 1:
            raise ValueError("only one feature may be marked core")
        for f in self.features:
            missing = set(f.requires) - set(comp_ids)
            if missing:
                raise ValueError(f"feature {f.id!r} requires unknown components {sorted(missing)}")
        for c in self.components:
            missing = set(c.applies_to_sources) - set(source_ids)
            if missing:
                raise ValueError(f"component {c.id!r} refers to unknown sources {sorted(missing)}")
            if c.type == "browser-extension" and not c.browsers:
                raise ValueError(f"browser-extension component {c.id!r} must list browsers")
        for cf in self.conflicts:
            if set(cf.between) - set(source_ids):
                raise ValueError("conflict refers to unknown sources")
        own_flatpaks = set(self.application.flatpak_ids)
        for c in self.components:
            for a in [c.action, *c.post, *c.platform_actions.values()]:
                if a is not None and a.kind == "flatpak.override" and a.app not in own_flatpaks:
                    raise ValueError("flatpak.override may only target this application's own Flatpak ids")
        return self

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def component(self, comp_id: str) -> Component | None:
        return next((c for c in self.components if c.id == comp_id), None)


def json_schema() -> dict[str, Any]:
    schema = Manifest.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://github.com/AbdulazizOmran/Cygnus/schemas/cam-1.schema.json"
    schema["title"] = "Cygnus Application Manifest v1"
    return schema
