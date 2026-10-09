"""Core data types shared across the engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class PackageFormat(StrEnum):
    PACMAN_REPO = "pacman"
    AUR = "aur"
    LOCAL_PKG = "localpkg"
    FLATPAK_REMOTE = "flatpak"
    FLATPAK_BUNDLE = "flatpak-bundle"
    FLATPAK_REF_FILE = "flatpakref"
    FLATPAK_REPO_FILE = "flatpakrepo"
    APPIMAGE = "appimage"
    DEB = "deb"
    RPM = "rpm"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True, kw_only=True)
class Finding:
    """A fact or concern noticed while inspecting an input.

    Codes are stable identifiers (e.g. ``DEB_MAINTAINER_SCRIPTS``) that the
    recovery engine and UI key on; messages are human-readable.
    """

    code: str
    severity: Severity
    message: str
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, kw_only=True)
class Candidate:
    """What detection learned about an installable input."""

    format: PackageFormat
    source: str
    name: str | None = None
    version: str | None = None
    arch: str | None = None
    summary: str | None = None
    # Format-independent identity hints: appstream_id, flatpak_ref, desktop_id, ...
    identity: dict[str, str] = field(default_factory=dict)
    depends: list[str] = field(default_factory=list)
    optional_depends: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    installed_size: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, severity: Severity, message: str, **facts: Any) -> None:
        self.findings.append(Finding(code=code, severity=severity, message=message, facts=facts))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
