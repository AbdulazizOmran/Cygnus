"""Pacman (repository and local package) analysis — read-only, unprivileged (architecture §10.1).

Commits are NOT done here: the privileged helper owns plans and runs the pacman CLI.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cygnus.core.errors import CygnusError
from cygnus.core.models import Candidate
from cygnus.core.recovery.model import (
    Action, Issue, IssueSeverity, Privilege, Resolution, SafetyClass, explain_only,
)
from cygnus.core.util import proc

WORKER = Path(__file__).with_name("alpm_worker.py")
# Packages Cygnus will never remove or replace (architecture §10.1), plus every kernel, firmware
# package and whatever pacman.conf lists as HoldPkg (see is_protected).
PROTECTED = frozenset({
    "base", "glibc", "gcc-libs", "systemd", "systemd-libs", "systemd-sysvcompat", "pacman", "pacman-mirrorlist",
    "archlinux-keyring", "cachyos-keyring", "mkinitcpio", "dracut", "booster", "limine", "grub", "refind",
    "efibootmgr", "systemd-boot", "sudo", "polkit", "filesystem", "bash", "coreutils", "util-linux", "shadow",
    "pam", "dbus", "dbus-broker", "openssl", "zlib", "xz", "zstd", "btrfs-progs", "snapper",
})
_KERNEL = re.compile(r"^linux(-[a-z0-9.-]+)?$")
_NOT_KERNEL = ("-headers", "-docs", "-api-headers", "-tools")


_FIRMWARE = re.compile(r"(^|-)firmware(-|$)|-ucode$")  # linux-firmware*, sof-firmware, intel-ucode, …


def is_protected(name: str, config: "PacmanConfig | None" = None) -> bool:
    if name in PROTECTED or _FIRMWARE.search(name):
        return True
    if _KERNEL.match(name) and not name.endswith(_NOT_KERNEL):
        return True
    return bool(config and name in config.hold)


class WorkerCrash(CygnusError):
    pass


@dataclass(frozen=True, slots=True)
class PacmanConfig:
    arch: tuple[str, ...]
    dbpath: str
    cachedirs: tuple[str, ...]
    gpgdir: str
    repos: tuple[str, ...]
    servers: dict[str, tuple[str, ...]]
    hold: tuple[str, ...]

    @property
    def worker_base(self) -> dict[str, Any]:
        return {"dbpath": self.dbpath, "arch": list(self.arch), "repos": list(self.repos)}


def parse_pacman_conf(text: str) -> PacmanConfig:
    section, opts, servers, repos = None, {}, {}, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            if section != "options":
                repos.append(section)
                servers.setdefault(section, [])
            continue
        key, _, value = (part.strip() for part in line.partition("="))
        if section == "options":
            opts.setdefault(key, []).append(value)
        elif key == "Server" and section:
            servers[section].append(value)
    return PacmanConfig(
        arch=tuple(opts.get("Architecture", ["x86_64"])),
        dbpath=(opts.get("DBPath", ["/var/lib/pacman/"])[0]).rstrip("/"),
        cachedirs=tuple(opts.get("CacheDir", ["/var/cache/pacman/pkg/"])),
        gpgdir=opts.get("GPGDir", ["/etc/pacman.d/gnupg/"])[0],
        repos=tuple(repos),
        servers={k: tuple(v) for k, v in servers.items()},
        hold=tuple(opts.get("HoldPkg", [])),
    )


def read_config() -> PacmanConfig:
    res = proc.run([proc.require("pacman-conf", "pacman")], timeout=30)
    if not res.ok:
        raise CygnusError(f"pacman-conf failed: {res.stderr.strip()}")
    return parse_pacman_conf(res.stdout)


def run_worker(config: PacmanConfig, request: dict[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
    payload = json.dumps({**config.worker_base, **request}).encode()
    try:
        proc_ = subprocess.run([sys.executable, "-I", str(WORKER)], input=payload, capture_output=True,
                               timeout=timeout, env=proc.clean_env())
    except subprocess.TimeoutExpired as exc:
        raise WorkerCrash(f"libalpm worker did not finish within {timeout:.0f}s") from exc
    if proc_.returncode < 0 or not proc_.stdout.strip():
        raise WorkerCrash(f"libalpm worker died (exit {proc_.returncode}): "
                          f"{proc_.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    try:
        return json.loads(proc_.stdout)
    except json.JSONDecodeError as exc:
        raise WorkerCrash(f"libalpm worker returned invalid output: {exc}") from exc


@dataclass(slots=True, kw_only=True)
class PacmanAnalysis:
    targets: list[str]
    to_add: list[dict[str, Any]] = field(default_factory=list)
    to_remove: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    method: str = "pyalpm"
    sysupgrade: bool = False

    @property
    def blocked(self) -> bool:
        return any(i.severity is IssueSeverity.BLOCKER for i in self.issues)

    @property
    def upgrades(self) -> list[dict[str, Any]]:
        return [p for p in self.to_add if p.get("installed_version") and p["installed_version"] != p["version"]]

    @property
    def download_bytes(self) -> int:
        return sum(p.get("download_size") or 0 for p in self.to_add)

    @property
    def installed_bytes_delta(self) -> int:
        return sum(p.get("installed_size") or 0 for p in self.to_add if not p.get("installed_version"))


def _cli_print_plan(targets: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    """Dependency resolution through `pacman -Sp` (no conflict checks; see analyse_install)."""
    res = proc.run(["pacman", "-Sp", "--noconfirm", "--print-format", "%n|%v|%r|%s", "--", *targets], timeout=120)
    if not res.ok:
        return [], (res.stderr or res.stdout).strip()
    plan = []
    for line in res.stdout.splitlines():
        parts = line.split("|")
        if len(parts) == 4:
            plan.append({"name": parts[0], "version": parts[1], "repo": parts[2],
                         "download_size": int(parts[3]) if parts[3].isdigit() else None})
    return plan, None


def analyse_install(targets: list[str], *, config: PacmanConfig | None = None, sysupgrade: bool = False,
                    worker=run_worker, syncdir: str | None = None) -> PacmanAnalysis:
    """Dry-run installing repository `targets` and diagnose problems (nothing is changed).

    `syncdir` analyses against a snapshot of freshly downloaded sync databases instead of the
    system's (used by the helper so a full upgrade runs exactly what was planned).
    """
    config = config or read_config()
    if syncdir is not None:
        base_worker = worker
        worker = lambda cfg, req: base_worker(cfg, {**req, "syncdir": syncdir})  # noqa: E731
    result = PacmanAnalysis(targets=list(targets), sysupgrade=sysupgrade)
    try:
        resp = worker(config, {"op": "resolve", "targets": list(targets), "sysupgrade": sysupgrade})
    except WorkerCrash as crash:
        resp = None
        result.method = "pacman-cli+metadata"
        if sysupgrade or syncdir is not None:
            # The CLI fallback cannot reproduce a pinned full-upgrade plan: refuse rather than show
            # something that is not what would run.
            result.issues.append(Issue(code="UNKNOWN_FAILURE", severity=IssueSeverity.BLOCKER,
                                       title="The upgrade could not be planned",
                                       explanation=f"Package analysis failed ({crash}).",
                                       resolutions=[explain_only("retry", "Try again later", str(crash))]))
            return result
        plan, err = _cli_print_plan(list(targets))
        if err:
            result.issues.append(_classify_cli_error(err, targets))
            return result
        info = worker(config, {"op": "info", "names": [p["name"] for p in plan]})["packages"]
        result.to_add = [
            {**(info[p["name"]]["sync"] or p),
             "installed_version": (info[p["name"]]["local"] or {}).get("version")} for p in plan
        ]

    if resp is not None and not resp.get("ok"):
        result.issues.append(_classify_worker_error(resp, targets))
        return result
    if resp is not None:
        result.to_add, result.to_remove = resp["to_add"], resp["to_remove"]

    # Conflicts are computed from metadata ourselves: libalpm's conflict path crashes pyalpm,
    # and `pacman -Sp` does not check conflicts at all.
    names = [p["name"] for p in result.to_add]
    conflicts = worker(config, {"op": "conflicts", "names": names}).get("conflicts", []) if names else []
    for c in conflicts:
        result.issues.append(_conflict_issue(c, config))

    if not sysupgrade:
        outdated, unchecked = [], None
        try:
            outdated = worker(config, {"op": "outdated"}).get("outdated", [])
        except WorkerCrash as crash:
            unchecked = str(crash)  # not knowing is treated like "out of date", never like "up to date"
        if result.upgrades or outdated or unchecked:
            # Either the plan upgrades installed packages, or the sync databases are already newer than
            # the installed system: installing anything now would be a partial upgrade.
            result.issues.append(_partial_upgrade_issue(result.upgrades, outdated, unchecked=unchecked))
    for p in result.to_add:
        if p.get("arch") not in (*config.arch, "any", None):
            result.issues.append(Issue(
                code="ARCH_INCOMPATIBLE", severity=IssueSeverity.BLOCKER,
                title=f"{p['name']} is built for {p['arch']}",
                explanation=f"This system accepts {', '.join(config.arch)} packages.",
                facts={"package": p["name"], "arch": p.get("arch")},
                resolutions=[explain_only("none", "No compatible build", "Look for a build for your CPU.")]))
    return result


def _classify_worker_error(resp: dict[str, Any], targets: list[str]) -> Issue:
    err = resp.get("error") or {}
    data = err.get("data")
    if resp.get("kind") == "target_not_found":
        missing = data or targets
        return Issue(
            code="PKG_NOT_FOUND", severity=IssueSeverity.BLOCKER,
            title="Not found in your package repositories: " + ", ".join(missing),
            explanation="The package is not in any configured repository. It may be in the AUR, "
                        "on Flathub, or offered by the vendor in another format.",
            facts={"missing": missing},
            resolutions=[Resolution(id="search-other-sources", title="Search the AUR, Flathub and vendor sources",
                                    explanation="Cygnus will look for the application in other sources.",
                                    safety=SafetyClass.AUTO, recommended=True,
                                    actions=[Action(kind="search.sources", params={"names": missing})])])
    message = str(err.get("message", ""))
    if isinstance(data, list) and data and isinstance(data[0], list):
        deps = [{"target": d[0], "dependency": d[1]} for d in data if len(d) >= 2]
        return Issue(
            code="PKG_DEP_UNRESOLVABLE", severity=IssueSeverity.BLOCKER,
            title="Some required dependencies are not available",
            explanation="; ".join(f"{d['target']} needs {d['dependency']}" for d in deps)
                        + ". No configured repository provides them.",
            facts={"unsatisfied": deps},
            resolutions=[
                Resolution(id="search-aur-deps", title="Look for the missing dependencies in the AUR",
                           explanation="AUR packages are community-maintained and must be reviewed before building.",
                           safety=SafetyClass.APPROVAL, rank=50,
                           actions=[Action(kind="search.aur", params={"names": [d["dependency"] for d in deps]})]),
                explain_only("explain", "Cannot be resolved automatically",
                             "Without a provider for these dependencies the package cannot be installed safely."),
            ])
    return Issue(code="UNKNOWN_FAILURE", severity=IssueSeverity.BLOCKER, title="Package analysis failed",
                 explanation=message or "libalpm reported an unexpected error.",
                 facts={"error": err}, resolutions=[explain_only("raw", "Details", message)])


def _classify_cli_error(text: str, targets: list[str]) -> Issue:
    missing = [line.split("target not found:", 1)[1].strip() for line in text.splitlines()
               if "target not found:" in line]
    if missing:
        return _classify_worker_error({"kind": "target_not_found", "error": {"data": missing}}, targets)
    return Issue(code="UNKNOWN_FAILURE", severity=IssueSeverity.BLOCKER, title="Package analysis failed",
                 explanation=text[-500:], resolutions=[explain_only("raw", "Details", text[-2000:])])


def _conflict_issue(c: dict[str, Any], config: "PacmanConfig | None" = None) -> Issue:
    installed, new = c.get("installed"), c["new"]
    if installed is None:
        return Issue(code="PKG_CONFLICT", severity=IssueSeverity.BLOCKER,
                     title=f"{new} conflicts with {c['other_new']}",
                     explanation="Two packages in this plan cannot be installed together.",
                     facts=c, resolutions=[explain_only("explain", "Choose one", "Install only one of them.")])
    protected = is_protected(installed, config)  # HoldPkg entries count, so no "Replace" is offered for them
    resolutions = [explain_only("cancel", "Keep your current package",
                                f"Cancel, keeping {installed}. {new} cannot be installed alongside it.")]
    if not protected:
        resolutions.insert(0, Resolution(
            id="replace", title=f"Replace {installed} with {new}",
            explanation=f"{installed} will be removed and {new} installed in the same transaction. "
                        "Anything that relies specifically on it may stop working.",
            safety=SafetyClass.APPROVAL, privilege=Privilege.ROOT, rank=60,
            actions=[Action(kind="pacman.replace", params={"remove": installed, "install": new})]))
    return Issue(
        code="PKG_CONFLICT", severity=IssueSeverity.BLOCKER,
        title=f"{new} conflicts with your installed {installed}",
        explanation=(f"{c['declared_by']} declares a conflict ({c['rule']})."
                     + (f" {installed} is a protected system package and will not be replaced." if protected else "")),
        facts=c, resolutions=resolutions)


def _partial_upgrade_issue(upgrades: list[dict[str, Any]], outdated: list[dict[str, Any]] = (), *,
                           unchecked: str | None = None) -> Issue:
    if unchecked and not upgrades and not outdated:
        why = (f"Cygnus could not check whether your system is up to date ({unchecked}), so installing now "
               "could be a partial upgrade.")
    elif upgrades:
        listing = ", ".join(f"{p['name']} {p['installed_version']} → {p['version']}" for p in upgrades[:8])
        why = (f"The plan upgrades already-installed packages ({listing}{' …' if len(upgrades) > 8 else ''}).")
    else:
        why = (f"Your package lists are newer than your installed system ({len(outdated)} updates pending), so "
               "a new package could be built against newer libraries than you have.")
    return Issue(
        code="PKG_PARTIAL_UPGRADE_RISK", severity=IssueSeverity.BLOCKER,
        title="Your system needs to be upgraded first",
        explanation=why + " On Arch-based systems upgrading only some packages can break others, so the whole "
                          "system must be upgraded together.",
        facts={"upgrades": upgrades, "outdated": list(outdated)[:50]},
        resolutions=[
            Resolution(id="full-upgrade", title="Upgrade the whole system, then install",
                       explanation="Runs a full system upgrade and installs the application in the same "
                                   "transaction. Snapper takes a snapshot first.",
                       safety=SafetyClass.APPROVAL, privilege=Privilege.ROOT, recommended=True, rank=10,
                       actions=[Action(kind="pacman.sysupgrade_with_targets")]),
            explain_only("cancel", "Install later", "Cancel and install after your next system upgrade."),
        ])


def analyse_local_package(cand: Candidate, *, config: PacmanConfig | None = None, worker=run_worker) -> list[Issue]:
    """Diagnose a local .pkg.tar.* file against this system (arch, dependencies, conflicts, version)."""
    config = config or read_config()
    issues: list[Issue] = []
    if cand.arch not in (*config.arch, "any"):
        issues.append(Issue(code="ARCH_INCOMPATIBLE", severity=IssueSeverity.BLOCKER,
                            title=f"This package is built for {cand.arch}",
                            explanation=f"This system accepts {', '.join(config.arch)} packages.",
                            facts={"arch": cand.arch},
                            resolutions=[explain_only("none", "Find a matching build",
                                                      "Download the build for your architecture from the vendor.")]))
    if cand.depends:
        sat = worker(config, {"op": "satisfy", "deps": cand.depends})["satisfiers"]
        missing = [d for d, s in sat.items() if not s["installed"] and not s["repo"]]
        to_install = [s["repo"] for d, s in sat.items() if not s["installed"] and s["repo"]]
        if missing:
            issues.append(Issue(code="PKG_DEP_UNRESOLVABLE", severity=IssueSeverity.BLOCKER,
                                title="Missing dependencies: " + ", ".join(missing),
                                explanation="No repository provides these dependencies.",
                                facts={"missing": missing},
                                resolutions=[explain_only("explain", "Cannot install safely",
                                                          "Install the dependencies first or ask the vendor.")]))
        if to_install:
            issues.append(Issue(code="PKG_DEP_MISSING", severity=IssueSeverity.NOTICE,
                                title="Will also install: " + ", ".join(p["name"] for p in to_install),
                                explanation="These dependencies come from your configured repositories.",
                                facts={"from_repos": to_install},
                                resolutions=[Resolution(id="install-deps", title="Install them as dependencies",
                                                        explanation="Installed and marked as dependencies.",
                                                        safety=SafetyClass.APPROVAL, privilege=Privilege.ROOT,
                                                        recommended=True,
                                                        actions=[Action(kind="pacman.install_repo",
                                                                        params={"names": [p["name"] for p in to_install],
                                                                                "asdeps": True})])]))
    lc = worker(config, {"op": "local_conflicts", "name": cand.name, "version": cand.version,
                         "conflicts": cand.conflicts, "provides": cand.provides})
    if lc.get("installed_version"):
        cand.metadata["installed_version"] = lc["installed_version"]
        if (lc.get("version_cmp") or 0) < 0:
            issues.append(Issue(code="PKG_DOWNGRADE", severity=IssueSeverity.DEGRADED,
                                title=f"This is older than the installed {cand.name} {lc['installed_version']}",
                                explanation="Installing it would downgrade the package.",
                                resolutions=[explain_only("keep", "Keep the installed version",
                                                          "Cancel unless you need this older version.")]))
    for c in lc.get("conflicts", []):
        issues.append(_conflict_issue({"new": cand.name, **c}, config))
    if not cand.metadata.get("detached_signature"):
        issues.append(Issue(code="PKG_UNSIGNED_LOCAL", severity=IssueSeverity.NOTICE,
                            title="This package is not signed",
                            explanation="pacman accepts unsigned local packages on this system "
                                        "(LocalFileSigLevel = Optional). Its origin must be verified another way, "
                                        "for example with a checksum published by the vendor.",
                            resolutions=[Resolution(id="verify-hash", title="Verify against the vendor's checksum",
                                                    explanation="Cygnus compares the file's SHA-256 with the one "
                                                                "published by the vendor (from a manifest).",
                                                    safety=SafetyClass.APPROVAL, recommended=True,
                                                    actions=[Action(kind="verify.sha256")])]))
    return issues
