"""Issue / Resolution model shared by every backend, the planner and the UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class SafetyClass(StrEnum):
    # Unprivileged, reversible, user-scope only, trusted source, no security downgrade.
    AUTO = "auto"
    # Anything else that is safe: every privileged helper verb, EOL/unsigned components,
    # permission changes, removals.
    APPROVAL = "approval"
    # Not safely recoverable: explain and list the legitimate options.
    NONE = "none"


class Privilege(StrEnum):
    USER = "user"
    ROOT = "root"  # via cygnus-helper + polkit
    FLATPAK_SYSTEM = "flatpak-system"  # via flatpak-system-helper's own polkit actions


class IssueSeverity(StrEnum):
    BLOCKER = "blocker"  # the operation cannot proceed until resolved
    DEGRADED = "degraded"  # proceeds, but something will not work fully
    NOTICE = "notice"  # information the user should see


@dataclass(slots=True, kw_only=True)
class Action:
    """One typed action from the closed vocabulary (architecture §13.4). Never a shell string."""

    kind: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, kw_only=True)
class Resolution:
    id: str
    title: str
    explanation: str
    safety: SafetyClass
    privilege: Privilege = Privilege.USER
    actions: list[Action] = field(default_factory=list)
    recommended: bool = False
    # Ordering key for the deterministic selection policy (lower is preferred).
    rank: int = 100


@dataclass(slots=True, kw_only=True)
class Issue:
    code: str
    severity: IssueSeverity
    title: str
    explanation: str
    facts: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)

    @property
    def recoverable(self) -> bool:
        return any(r.safety is not SafetyClass.NONE for r in self.resolutions)

    def preferred(self) -> Resolution | None:
        viable = [r for r in self.resolutions if r.safety is not SafetyClass.NONE]
        return min(viable, key=lambda r: (not r.recommended, r.rank), default=None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def explain_only(rid: str, title: str, explanation: str) -> Resolution:
    """A NONE-class resolution: tells the user what their legitimate options are."""
    return Resolution(id=rid, title=title, explanation=explanation, safety=SafetyClass.NONE, rank=1000)
