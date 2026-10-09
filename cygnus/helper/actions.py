"""Plan computation and execution for the privileged helper. Pure logic, injectable runners.

Every request is validated here, independently of whatever the unprivileged client already
checked. Plans are computed by the helper itself and executed exactly as summarized.
"""

from __future__ import annotations

import grp
import hashlib
import os
import stat
import sys
import pwd
import re
import shutil
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cygnus.core.backends import pacman as pm
from cygnus.core.errors import CygnusError
from cygnus.helper import ACTION_PREFIX

PKG_NAME = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]{0,99}\Z")  # \Z, not $: "$" accepts a trailing newline
UNIT_NAME = re.compile(r"^[A-Za-z0-9:_.@][A-Za-z0-9:_.@-]{0,199}\.(service|socket|timer|path)\Z")
ALLOWED_GROUPS = frozenset({"input"})  # extended only by shipping a new helper, never by clients
PLAN_TTL = 600.0
MAX_TARGETS = 200
MAX_LOCAL_PACKAGE = 4 * 1024**3
MAX_STAGED_PER_REQUEST = 4 * 1024**3
MAX_STAGED_PER_USER = 8 * 1024**3
STAGING_FREE_MARGIN = 1024**3
PKG_VERSION = re.compile(r"^[A-Za-z0-9._+:~-]{1,100}\Z")
DBPATH = "/var/lib/pacman"
PACMAN_LOG = "/var/log/pacman.log"
LOCK_WAIT_SECONDS = 1800.0
STALE_LOCK_GRACE = 20.0
# Programs that run pacman transactions (or hold its lock) on Arch-based systems.
PACKAGE_MANAGERS = ("pacman", "paru", "yay", "pikaur", "trizen", "aura", "pamac", "pamac-daemon", "octopi",
                    "shelly", "packagekitd", "plasma-discover", "pkcon", "cachyos-pi", "topgrade", "makepkg")


class PlanRefused(CygnusError):
    pass


@dataclass(slots=True, kw_only=True)
class HelperPlan:
    id: str
    kind: str  # packages | unit | group
    caller_uid: int
    caller_sender: str
    action_id: str  # polkit action
    message: str  # shown in the polkit dialog
    summary: dict[str, Any]
    commands: list[list[str]]
    expires: float
    sync_snapshot: str | None = None  # temp dbpath whose sync DBs are installed before commit
    staged: list[str] = field(default_factory=list)
    ledger_ops: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    used: bool = False
    created: float = field(default_factory=time.time)
    local_fingerprint: str | None = None  # installed packages when planned (packages plans only)
    sync_fingerprint: str | None = None  # repository databases the plan was computed from


def _plan_id() -> str:
    return secrets.token_hex(16)


def _short(sha: str) -> str:
    return f"{sha[:8]}…{sha[-4:]}"


# -- packages ----------------------------------------------------------------------------------------
def stage_local_package(src_fd: int, expected_sha256: str, staging: Path,
                        expected_size: int | None = None) -> tuple[Path, str]:
    """Copy a package from a passed file descriptor into a root-owned staging dir, hashing as we go.

    The helper never opens a client-supplied *path*: only the descriptor, so the file cannot be
    swapped between verification and installation. Only regular files are accepted (a pipe or a
    device could stall the helper or never end), and nothing is left behind on failure.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise PlanRefused("a SHA-256 checksum is required for local packages")
    st = os.fstat(src_fd)
    if not stat.S_ISREG(st.st_mode):
        raise PlanRefused("a local package must be a regular file")
    if st.st_size > MAX_LOCAL_PACKAGE:
        raise PlanRefused("package is implausibly large")
    if expected_size is not None and st.st_size != expected_size:
        # The size charged to the staging budget must be the size that is copied.
        raise PlanRefused("the package file changed while it was being read")
    # The folder can be entered but not listed or changed by anyone else, and the file is readable but not writable by
    # anyone else: the unprivileged package reader must be able to open it, nobody may be able to alter what is
    # installed later. The name is random, so another user cannot find it.
    staging.mkdir(parents=True, exist_ok=True, mode=0o711)
    os.chmod(staging, 0o711)
    dest = staging / f"{secrets.token_hex(8)}.pkg.tar"
    h = hashlib.sha256()
    total = 0
    try:
        with open(dest, "xb") as out:
            os.fchmod(out.fileno(), 0o644)
            # Exactly the size that was checked against the staging budget is copied: a file that grows
            # (or shrinks) meanwhile is refused instead of filling the system drive.
            while total < st.st_size:
                block = os.pread(src_fd, min(1 << 20, st.st_size - total), total)
                if not block:
                    raise PlanRefused("the package file changed while it was being read")
                total += len(block)
                h.update(block)
                out.write(block)
            if os.pread(src_fd, 1, total):
                raise PlanRefused("the package file changed while it was being read")
        digest = h.hexdigest()
        if digest != expected_sha256:
            raise PlanRefused(f"the package does not match the expected checksum ({_short(digest)})")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest, digest


INSPECT_WORKER = Path(__file__).with_name("inspect_worker.py")
READER_ACCOUNT = "cygnus-reader"  # an otherwise unused, locked account (created when Cygnus is installed)
MAX_FACT_LIST = 2000
MAX_FACT_TEXT = 500


def reader_account() -> tuple[int, int] | None:
    """(uid, gid) of the unprivileged account package files are read as; None when we are not root (a development
    checkout, the tests), where there is nothing to give up. Missing account: the file is not read."""
    if os.geteuid() != 0:
        return None
    try:
        entry = pwd.getpwnam(READER_ACCOUNT)
    except KeyError:
        raise PlanRefused(f"the account '{READER_ACCOUNT}', which reads package files without administrator rights, "
                          "does not exist; reinstall Cygnus (installing it creates the account)") from None
    if entry.pw_uid == 0 or entry.pw_gid == 0:
        raise PlanRefused(f"the account '{READER_ACCOUNT}' has administrator rights; the package was not read")
    return entry.pw_uid, entry.pw_gid


def inspect_package(path: str, *, timeout: float = 240.0) -> dict[str, Any]:
    """What an Arch package file says about itself, read by a process that has given up administrator rights.

    The file comes from a local user and nobody has authorized anything yet, so it is never opened by this (root)
    process: the reader starts, becomes the unprivileged reader account with no network, no way to start processes
    and a syscall filter, proves it, and only then opens the file. Whatever it answers is untrusted too and is
    checked here; and if it did not report exactly the account and the restrictions it was given (when we are
    root), the plan is refused."""
    import json

    from cygnus.core.util import proc
    from cygnus.helper import seccomp

    ids = reader_account()
    try:
        res = proc.run([sys.executable, "-I", str(INSPECT_WORKER), path, *map(str, ids or ())], timeout=timeout,
                       max_output=1 << 20, cwd="/")
    except Exception as exc:  # noqa: BLE001 - a timeout or a missing interpreter: nothing was read
        raise PlanRefused(f"the package could not be read: {exc}") from None
    try:
        data = json.loads(res.stdout)
    except ValueError:
        raise PlanRefused("the package could not be read safely (the reader crashed or was killed)") from None
    if not isinstance(data, dict):
        raise PlanRefused("the package could not be read safely (its reader answered nonsense)")
    if data.get("sandbox_error"):
        raise PlanRefused("the package was not read, because its reader could not lock itself down "
                          f"({str(data['sandbox_error'])[:200]})")
    identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
    if ids is not None and not (
            identity.get("sandboxed") is True and (identity.get("uid"), identity.get("gid")) == ids
            and identity.get("capabilities") == 0 and identity.get("groups") == []
            and identity.get("no_new_privs") is True and identity.get("no_processes") is True
            and (identity.get("seccomp") is True or not seccomp.available())):
        raise PlanRefused("the package was not read, because its reader did not give up administrator rights")
    if not data.get("ok"):
        raise PlanRefused(str(data.get("refused") or "pacman cannot read this package: " + _complaint(data.get("error"), path)))
    return {**_checked_facts(data.get("facts")), "reader_uid": identity.get("uid")}


def _complaint(error: Any, path: str) -> str:
    """What the reader said went wrong, without the exception's class name and without the staged file's random name
    (which means nothing to the person who chose the file)."""
    text = re.sub(r"^\w+(?:\.\w+)*: ", "", str(error)[:300])
    name = os.path.basename(path)
    return text.replace(f"{name}: ", "").replace(path, "the file").replace(name, "the file")


def _checked_facts(raw: Any) -> dict[str, Any]:
    """The reader's answer, validated as if it came from a stranger: exact shapes, bounded sizes."""
    if not isinstance(raw, dict):
        raise PlanRefused("the package could not be read safely (its reader answered nonsense)")

    def text(key: str, limit: int = MAX_FACT_TEXT) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or len(value) > limit:
            raise PlanRefused(f"the package could not be read safely (bad {key})")
        return value

    def items(key: str) -> list[str]:
        value = raw.get(key)
        if not isinstance(value, list) or len(value) > MAX_FACT_LIST or \
                not all(isinstance(v, str) and len(v) <= MAX_FACT_TEXT for v in value):
            raise PlanRefused(f"the package could not be read safely (bad {key})")
        return value

    if not isinstance(raw.get("has_scriptlet"), bool):
        raise PlanRefused("the package could not be read safely (bad has_scriptlet)")
    return {"name": text("name", 200), "version": text("version", 200), "arch": text("arch", 50),
            "desc": text("desc", 4000), "depends": items("depends"), "conflicts": items("conflicts"),
            "provides": items("provides"), "replaces": items("replaces"), "has_scriptlet": raw["has_scriptlet"]}


def load_with_alpm(paths: list[str], config=None) -> list[dict[str, Any]]:
    """Package facts as libalpm reads them (in the crash-isolated worker)."""
    try:
        return pm.run_worker(config or pm.read_config(), {"op": "load", "paths": paths})["packages"]
    except CygnusError as exc:
        raise PlanRefused(f"pacman cannot read this package: {exc}") from None


def discard_staged(paths: list[str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _default_satisfy(config):
    def satisfy(deps: list[str]) -> dict[str, dict[str, Any]]:
        return pm.run_worker(config or pm.read_config(), {"op": "satisfy", "deps": deps})["satisfiers"]
    return satisfy


def discard_plan(plan: HelperPlan) -> None:
    """Free what a plan holds on disk (staged package files, its database snapshot)."""
    discard_staged(plan.staged)
    if plan.sync_snapshot:
        shutil.rmtree(plan.sync_snapshot, ignore_errors=True)


def plan_packages(req: dict[str, Any], *, sync_fresh: Callable[[], str] | None = None, **kw) -> HelperPlan:
    """Compute a packages plan; a database snapshot downloaded for it is removed if planning fails."""
    snapshots: list[str] = []

    def tracked() -> str:
        snapshots.append(sync_fresh())
        return snapshots[-1]

    try:
        return _plan_packages(req, sync_fresh=tracked if sync_fresh is not None else None, **kw)
    except BaseException:
        for snap in snapshots:
            shutil.rmtree(snap, ignore_errors=True)
        raise


def _default_installed(config):
    def installed(names: list[str]) -> dict[str, dict[str, Any] | None]:
        info = pm.run_worker(config or pm.read_config(), {"op": "info", "names": names})["packages"]
        return {n: (info.get(n) or {}).get("local") for n in names}
    return installed


def _plan_packages(req: dict[str, Any], *, caller_uid: int, caller_sender: str, ledger,
                   analyse=pm.analyse_install, analyse_local=None, config=None,
                   sync_fresh: Callable[[], str] | None = None, local: list[dict[str, Any]] = (),
                   satisfy: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
                   installed: Callable[[list[str]], dict[str, dict[str, Any] | None]] | None = None) -> HelperPlan:
    install = list(req.get("install_repo") or [])
    remove = list(req.get("remove") or [])
    sysupgrade = bool(req.get("sysupgrade"))
    asdeps = bool(req.get("asdeps"))
    if len(install) + len(remove) + len(local) > MAX_TARGETS:
        raise PlanRefused("too many packages in one request")
    for n in install + remove:
        if not PKG_NAME.match(n):
            raise PlanRefused(f"invalid package name {n!r}")
    if not (install or remove or local or sysupgrade):
        raise PlanRefused("nothing to do")
    # What the plan is computed from, captured before any analysis (checked again before running).
    local_fp, sync_fp = local_db_fingerprint(), sync_db_fingerprint()
    if config is None:
        try:
            config = pm.read_config()  # HoldPkg is part of the protected set
        except (CygnusError, OSError) as exc:
            # Without it, HoldPkg would be silently ignored: refuse rather than guess.
            raise PlanRefused(f"pacman's configuration could not be read ({exc})") from None
        if os.path.normpath(config.dbpath) != os.path.normpath(DBPATH):
            # Locks, snapshots and fingerprints are all taken from the default folder: with another one the
            # helper would watch the wrong lock and analyse a different database than pacman will use.
            raise PlanRefused(f"pacman is set up to keep its databases in {config.dbpath}, but the Cygnus helper "
                              f"supports only {DBPATH}")
    protected = sorted(n for n in remove if pm.is_protected(n, config))
    if protected:
        raise PlanRefused(f"protected system packages cannot be removed with Cygnus: {', '.join(protected)}")
    local_names = set()
    for item in local:
        name, version = item.get("name") or "", item.get("version") or ""
        if not PKG_NAME.match(name) or not PKG_VERSION.match(version):
            raise PlanRefused("the package file has an invalid name or version")
        if pm.is_protected(name, config):
            raise PlanRefused(f"{name} is a protected system package; Cygnus does not replace it from a file")
        hit = sorted(c for c in [*item.get("conflicts", []), *item.get("replaces", [])]
                     if pm.is_protected(_dep_name(c), config))
        if hit:
            raise PlanRefused(f"{name} conflicts with or replaces protected system packages: {', '.join(hit)}")
        local_names.add(name)
        local_names.update(_dep_name(p) for p in item.get("provides", []))
    # Dependencies of package files come from your repositories; they are part of this plan (shown
    # and checked like any repository install), never installed silently by `pacman -U`.
    local_deps: list[str] = []
    wanted = sorted({d for item in local for d in item.get("depends", []) if _dep_name(d) not in local_names})
    if wanted:
        sat = (satisfy or _default_satisfy(config))(wanted)
        missing = [d for d in wanted if not (sat.get(d) or {}).get("installed") and not (sat.get(d) or {}).get("repo")]
        if missing:
            raise PlanRefused("it needs packages none of your repositories provide: " + ", ".join(missing))
        local_deps = sorted({sat[d]["repo"]["name"] for d in wanted if not sat[d].get("installed")})

    summary: dict[str, Any] = {"install": [], "upgrade": [], "remove": [], "local": [], "notes": []}
    commands: list[list[str]] = []
    snapshot = None
    if install or sysupgrade:
        if sysupgrade:
            if sync_fresh is None:
                raise PlanRefused("full upgrades need a fresh database snapshot")
            snapshot = sync_fresh()
        analysis = analyse(install, config=config, sysupgrade=sysupgrade,
                           **({"syncdir": os.path.join(snapshot, "sync")} if snapshot else {}))
        blockers = [i for i in analysis.issues if i.severity.value == "blocker"]
        if blockers:
            raise PlanRefused("; ".join(f"{i.code}: {i.title}" for i in blockers))
        for p in analysis.to_add:
            entry = {"name": p["name"], "version": p["version"], "repo": p.get("repo"),
                     "from": p.get("installed_version")}
            if p.get("pulled_in_by"):  # a dependency of a package that replaces an installed one
                entry["reason"] = f"needed by {p['pulled_in_by']}"
            (summary["upgrade"] if p.get("installed_version") else summary["install"]).append(entry)
        # Repository replacements during a full upgrade are the distribution's (signed) decision and
        # are shown as such; any other removal of a protected package is refused outright.
        forced = sorted(p["name"] for p in analysis.to_remove
                        if not (sysupgrade and p.get("replaced_by")) and pm.is_protected(p["name"], config))
        if forced:
            raise PlanRefused(f"this would remove protected system packages: {', '.join(forced)}")
        summary["remove"] += [{"name": p["name"], "version": p["version"],
                               "reason": f"replaced by {p['replaced_by']} (repository decision)"
                               if p.get("replaced_by") else "replaced"}
                              for p in analysis.to_remove]
        summary["download_bytes"] = analysis.download_bytes
        if sysupgrade:
            commands.append(["pacman", "-Su", "--noconfirm", "--needed", *(["--"] + install if install else [])])
        else:
            commands.append(["pacman", "-S", "--noconfirm", "--needed", *(["--asdeps"] if asdeps else []),
                             "--", *install])
    if local_deps:
        deps_analysis = analyse(local_deps, config=config, sysupgrade=False)
        blockers = [i for i in deps_analysis.issues if i.severity.value == "blocker"]
        if blockers:
            raise PlanRefused("; ".join(f"{i.code}: {i.title}" for i in blockers))
        forced = sorted(p["name"] for p in deps_analysis.to_remove if pm.is_protected(p["name"], config))
        if forced:  # the same rule as for an install: nothing protected is ever removed on the way
            raise PlanRefused(f"this would remove protected system packages: {', '.join(forced)}")
        for p in deps_analysis.to_add:
            entry = {"name": p["name"], "version": p["version"], "repo": p.get("repo"),
                     "from": p.get("installed_version"), "reason": "dependency"}
            (summary["upgrade"] if p.get("installed_version") else summary["install"]).append(entry)
        summary["remove"] += [{"name": p["name"], "version": p["version"], "reason": "replaced"}
                              for p in deps_analysis.to_remove]
        commands.append(["pacman", "-S", "--noconfirm", "--needed", "--asdeps", "--", *local_deps])
    if local:
        current = (installed or _default_installed(config))([item["name"] for item in local])
        for item in local:
            entry = {k: item[k] for k in ("name", "version", "sha256", "signed", "has_install_script")}
            if item.get("reader_uid") is not None:
                entry["read_by_uid"] = item["reader_uid"]  # who read the file: the reader account when the helper is root
            if current.get(item["name"]):  # the file replaces an installed package: say so, with versions
                entry["replaces_installed"] = current[item["name"]].get("version")
            summary["local"].append(entry)
        # No --needed: it makes pacman skip a package whose version is already installed, quietly, while the plan and the
        # ledger say it was done. A file the person chose is installed (a reinstall of the same version is a real one).
        commands.append(["pacman", "-U", "--noconfirm", *(["--asdeps"] if asdeps else []), "--",
                         *[item["path"] for item in local]])
    if remove:
        not_ours = [n for n in remove if not ledger.owns_package(n)]
        if not_ours and not req.get("confirm_not_installed_by_cygnus"):
            raise PlanRefused(f"these packages were not installed by Cygnus: {', '.join(not_ours)}; "
                              "the request must confirm their removal explicitly")
        summary["remove"] += [{"name": n, "reason": "requested", "installed_by_cygnus": n not in not_ours}
                              for n in remove]
        commands.append(["pacman", "-R", "--noconfirm", "--", *remove])

    # Polkit tier (architecture §16.2): the strictest action that applies.
    if remove or summary["remove"]:
        action = "packages.remove"
    elif local:
        action = "packages.install-local"
    elif sysupgrade:
        action = "system.upgrade"
    else:
        action = "packages.install-repo"
    message = _package_message(summary, sysupgrade)
    ledger_ops = [("record_package", {"name": p["name"], "version": p["version"],
                                      "reason": "asdeps" if asdeps or p.get("reason") == "dependency" else "explicit"})
                  for p in summary["install"]]
    ledger_ops += [("record_package", {"name": p["name"], "version": p["version"], "reason": "local"})
                   for p in summary["local"]]
    ledger_ops += [("forget_package", {"name": p["name"]}) for p in summary["remove"]]
    return HelperPlan(id=_plan_id(), kind="packages", caller_uid=caller_uid, caller_sender=caller_sender,
                      action_id=f"{ACTION_PREFIX}.{action}", message=message, summary=summary,
                      commands=commands, expires=time.monotonic() + PLAN_TTL, sync_snapshot=snapshot,
                      staged=[item["path"] for item in local], ledger_ops=ledger_ops,
                      local_fingerprint=local_fp, sync_fingerprint=sync_fp)


def _package_message(s: dict[str, Any], sysupgrade: bool) -> str:
    parts = []
    if sysupgrade:
        kernels = [p["name"] for p in s["upgrade"] if pm._KERNEL.match(p["name"]) and pm.is_protected(p["name"])]
        parts.append(f"Upgrade the whole system ({len(s['upgrade'])} packages)"
                     + (f", including a new kernel ({', '.join(kernels)}): restart afterwards" if kernels else ""))
    if s["install"]:
        names = ", ".join(f"{p['name']} {p['version']}" for p in s["install"][:5])
        parts.append(f"Install {names}" + (f" and {len(s['install']) - 5} more" if len(s["install"]) > 5 else ""))
    for p in s["local"]:
        trust = "signed" if p["signed"] else "UNSIGNED"
        script = "; it runs an install script as root" if p["has_install_script"] else ""
        if p.get("replaces_installed") and p["replaces_installed"] == p["version"]:
            replaces = f", REINSTALLING the same version ({p['version']}) that is installed"
        elif p.get("replaces_installed"):
            replaces = f", REPLACING the installed {p['name']} {p['replaces_installed']}"
        else:
            replaces = ""
        parts.append(f"Install {p['name']} {p['version']} from a local file ({trust}, SHA-256 "
                     f"{_short(p['sha256'])}){replaces}{script}")
    removed = s["remove"]
    if removed:  # the password dialog must never hide part of what is removed
        parts.append("Remove " + ", ".join(p["name"] for p in removed[:8])
                     + (f" and {len(removed) - 8} more" if len(removed) > 8 else ""))
    return ". ".join(parts) + "."


# -- stale pacman lock ------------------------------------------------------------------------------------
def _boot_time() -> float:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def plan_clear_stale_lock(*, caller_uid: int, caller_sender: str, dbpath: str | None = None,
                          running: Callable[[], list[str]] | None = None) -> HelperPlan:
    """Remove pacman's lock file only when no package manager is running; the very same file must
    still be there when you commit (checked again then)."""
    lock = Path(dbpath or DBPATH) / "db.lck"
    try:
        st = os.lstat(lock)
    except FileNotFoundError:
        raise PlanRefused("pacman is not locked; there is nothing to remove") from None
    if not stat.S_ISREG(st.st_mode):
        raise PlanRefused(f"{lock} is not a regular file; not touching it")
    holders = (running or (lambda: _holders(lock)))()
    if holders:
        raise PlanRefused(f"{', '.join(holders)} is running: the lock is in use, not stale")
    age = time.time() - st.st_mtime
    since_boot = st.st_mtime < _boot_time()
    when = "before this computer was last started" if since_boot else f"{int(age // 60)} minutes ago"
    message = (f"Remove pacman's lock file {lock}, left behind {when} by a package manager that stopped "
               "unexpectedly. No package manager is running now.")
    return HelperPlan(id=_plan_id(), kind="lock", caller_uid=caller_uid, caller_sender=caller_sender,
                      action_id=f"{ACTION_PREFIX}.recovery.manage", message=message,
                      summary={"lock": str(lock), "inode": st.st_ino, "mtime_ns": st.st_mtime_ns,
                               "created_before_boot": since_boot},
                      commands=[], expires=time.monotonic() + PLAN_TTL)


def clear_stale_lock(plan: HelperPlan, running: Callable[[], list[str]] | None = None) -> str:
    lock = Path(plan.summary["lock"])
    holders = (running or (lambda: _holders(lock)))()
    if holders:
        raise PlanRefused(f"{', '.join(holders)} started meanwhile; the lock is in use now")
    try:
        st = os.lstat(lock)
    except FileNotFoundError:
        return "the lock was already gone"
    if (st.st_ino, st.st_mtime_ns) != (plan.summary["inode"], plan.summary["mtime_ns"]):
        raise PlanRefused("the lock file changed since you approved removing it; another program is using pacman")
    os.unlink(lock)
    return f"removed {lock}"


# -- units --------------------------------------------------------------------------------------------
def plan_unit(unit: str, action: str, *, caller_uid: int, caller_sender: str,
              owner_of: Callable[[str], str | None]) -> HelperPlan:
    if not UNIT_NAME.match(unit):
        raise PlanRefused(f"invalid unit name {unit!r}")
    verbs = {"enable_now": ["enable", "--now"], "disable_now": ["disable", "--now"], "restart": ["restart"]}
    if action not in verbs:
        raise PlanRefused(f"unsupported unit action {action!r}")
    package = owner_of(unit)
    if package is None:
        raise PlanRefused(f"{unit} is not shipped by an installed package; Cygnus only manages packaged units")
    words = {"enable_now": "Enable and start", "disable_now": "Stop and disable", "restart": "Restart"}[action]
    return HelperPlan(id=_plan_id(), kind="unit", caller_uid=caller_uid, caller_sender=caller_sender,
                      action_id=f"{ACTION_PREFIX}.services.manage",
                      message=f"{words} the system service {unit} (from the package {package}).",
                      summary={"unit": unit, "action": action, "package": package},
                      commands=[["systemctl", *verbs[action], "--", unit]], expires=time.monotonic() + PLAN_TTL,
                      ledger_ops=[("record_unit", {"unit": unit, "action": action})])


def unit_owner(unit: str) -> str | None:
    from cygnus.core.util import proc

    for base in ("/usr/lib/systemd/system", "/etc/systemd/system"):
        path = os.path.join(base, unit)
        if os.path.exists(path):
            res = proc.run(["pacman", "-Qqo", "--", path], timeout=30)
            if res.ok and res.stdout.strip():
                return res.stdout.split()[0]
    return None


# -- groups -------------------------------------------------------------------------------------------
def plan_group(group: str, op: str, *, caller_uid: int, caller_sender: str, ledger) -> HelperPlan:
    if group not in ALLOWED_GROUPS:
        raise PlanRefused(f"Cygnus does not manage membership of the group {group!r}")
    if op not in ("add", "remove"):
        raise PlanRefused("invalid group operation")
    try:
        entry = pwd.getpwuid(caller_uid)
        user = entry.pw_name
        gr = grp.getgrnam(group)
    except KeyError as exc:
        raise PlanRefused(f"unknown user or group: {exc}") from exc
    # Only a membership Cygnus really creates is recorded: if an administrator already added the user,
    # a later "remove" must not be allowed to undo something Cygnus never did.
    already_member = user in gr.gr_mem or entry.pw_gid == gr.gr_gid
    if op == "remove" and not ledger.added_group(user, group):
        raise PlanRefused(f"Cygnus did not add {user} to {group}; remove it in System Settings instead")
    note = {"input": "Programs you run will be able to read every keystroke and mouse event."}.get(group, "")
    verb = "Add" if op == "add" else "Remove"
    return HelperPlan(id=_plan_id(), kind="group", caller_uid=caller_uid, caller_sender=caller_sender,
                      action_id=f"{ACTION_PREFIX}.permissions.manage",
                      message=f"{verb} {user} {'to' if op == 'add' else 'from'} the '{group}' group. {note} "
                              "Takes effect after logging out and back in.".strip(),
                      summary={"user": user, "group": group, "op": op},
                      commands=[["gpasswd", "-a" if op == "add" else "-d", user, group]],
                      expires=time.monotonic() + PLAN_TTL,
                      ledger_ops=[] if op == "add" and already_member
                      else [("record_group", {"username": user, "group": group, "action": op})])


# -- execution ------------------------------------------------------------------------------------------
ALLOWED_PROGRAMS = {"pacman", "systemctl", "gpasswd"}
PACMAN_FLAGS = {"-S", "-Su", "-U", "-R", "--noconfirm", "--needed", "--asdeps", "--"}


def check_command(argv: list[str], staged: list[str]) -> None:
    """Last line of defence: only allow-listed programs and flags ever run."""
    if not argv or argv[0] not in ALLOWED_PROGRAMS:
        raise PlanRefused(f"command not allowed: {argv[:1]}")
    if argv[0] == "pacman":
        seen_dd = False
        for a in argv[1:]:
            if a == "--":
                seen_dd = True
                continue
            if not seen_dd and a not in PACMAN_FLAGS:
                raise PlanRefused(f"pacman flag not allowed: {a}")
            if seen_dd and a.startswith("/") and a not in staged:
                raise PlanRefused("pacman may only install files Cygnus staged itself")
            if seen_dd and not a.startswith("/") and not PKG_NAME.match(a):
                raise PlanRefused(f"invalid package name {a!r}")


def install_sync_snapshot(snapshot: str, dbpath: str | None = None) -> None:
    """Atomically put the planned sync DBs in place, so `pacman -Su` uses exactly what was shown."""
    src = Path(snapshot) / "sync"
    dst = Path(dbpath or DBPATH) / "sync"
    for f in sorted(src.iterdir()):
        if f.is_symlink() or not f.is_file() or not re.fullmatch(r"[A-Za-z0-9._-]+\.(db|db\.sig|files|files\.sig)",
                                                                   f.name):
            continue
        tmp = dst / f".{f.name}.cygnus"
        shutil.copy2(f, tmp)
        os.replace(tmp, dst / f.name)


# -- coexistence with other package managers --------------------------------------------------------------
def local_db_fingerprint(dbpath: str | None = None) -> str | None:
    """Identity of the installed package set (pacman's local DB has one directory per name-version)."""
    try:
        entries = sorted(os.listdir(Path(dbpath or DBPATH) / "local"))
    except OSError:
        return None
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def sync_db_fingerprint(dbpath: str | None = None) -> str | None:
    """Identity of the repository databases (a `pacman -Sy` by any tool replaces these files)."""
    try:
        sync = Path(dbpath or DBPATH) / "sync"
        entries = []
        for f in sorted(sync.iterdir()):
            if f.name.endswith(".db"):
                st = f.stat()
                entries.append(f"{f.name}:{st.st_size}:{st.st_mtime_ns}:{st.st_ino}")
    except OSError:
        return None
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def _dep_name(dep: str) -> str:
    return re.split(r"[<>=]", dep.strip(), maxsplit=1)[0].strip()


def package_managers_running(proc_root: str = "/proc") -> list[str]:
    """Running package managers, e.g. ["pacman (pid 1234)"]. Only processes running as root from the
    real program count: a process name is chosen by whoever starts it, so any user could otherwise make
    the helper wait or refuse. (AUR helpers run as you and change packages through a root pacman.)"""
    found = []
    for pid in filter(str.isdigit, os.listdir(proc_root)):
        try:
            comm = Path(proc_root, pid, "comm").read_text().strip()
            if comm not in PACKAGE_MANAGERS or int(pid) == os.getpid():
                continue
            if os.stat(Path(proc_root, pid)).st_uid != 0:
                continue
            exe = os.readlink(Path(proc_root, pid, "exe")).removesuffix(" (deleted)")
        except OSError:
            continue
        if os.path.basename(exe) in PACKAGE_MANAGERS and exe.startswith(("/usr/bin/", "/usr/lib/")):
            found.append(f"{comm} (pid {pid})")
    return found


def lock_holders(lock: Path, proc_root: str = "/proc") -> list[str]:
    """Processes that have pacman's lock file open (libalpm keeps it open for the whole transaction).
    This finds any libalpm program, whatever it is called; the helper runs as root and sees them all."""
    try:
        target = os.stat(lock)
    except OSError:
        return []
    found = []
    for pid in filter(str.isdigit, os.listdir(proc_root)):
        fd_dir = Path(proc_root, pid, "fd")
        try:
            for fd in os.listdir(fd_dir):
                try:
                    st = os.stat(fd_dir / fd)
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
                    comm = Path(proc_root, pid, "comm").read_text().strip()
                    found.append(f"{comm} (pid {pid})")
                    break
        except OSError:
            continue
    return found


def _holders(lock: Path) -> list[str]:
    return list(dict.fromkeys(lock_holders(lock) + package_managers_running()))


def wait_for_pacman_lock(progress: Callable[[str], None], *, dbpath: str | None = None,
                         timeout: float = LOCK_WAIT_SECONDS, sleep: Callable[[float], None] = time.sleep,
                         clock: Callable[[], float] = time.monotonic,
                         running: Callable[[], list[str]] | None = None) -> None:
    """Wait while another program holds pacman's lock. A lock nobody holds is reported, never removed."""
    lock = Path(dbpath or DBPATH) / "db.lck"
    running = running or (lambda: _holders(lock))
    start, last_said, idle_since = clock(), "", None
    while lock.exists():
        holders = running()
        if holders:
            idle_since = None
            said = f"Waiting for {', '.join(holders)} to finish…"
        else:
            idle_since = idle_since if idle_since is not None else clock()
            said = "Waiting for another package manager to finish…"
            if clock() - idle_since > STALE_LOCK_GRACE:
                raise PlanRefused(f"pacman's lock file {lock} exists, but no package manager is running (left "
                                  "behind by a crash?). Cygnus does not remove it automatically: use \"Remove it\" "
                                  "on the Applications page, or `cygnus recover --pacman-lock`.")
        if said != last_said:
            progress(said)
            last_said = said
        if clock() - start > timeout:
            raise PlanRefused(f"another package manager has been running for over {int(timeout // 60)} minutes; "
                              "try again when it has finished")
        sleep(2.0)


class _HeldLock:
    """Hold pacman's own lock while Cygnus writes into its database directory."""

    def __init__(self, dbpath: str | None = None):
        self.path = Path(dbpath or DBPATH) / "db.lck"

    def __enter__(self):
        try:
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o000))
        except FileExistsError:
            raise PlanRefused("another package manager started at the same moment; try again") from None
        return self

    def __exit__(self, *exc) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def changes_since(when: float, log_path: str | None = None, limit: int = 1 << 20) -> str:
    """What pacman's log says happened after `when`, in one line (who changed the system meanwhile)."""
    from datetime import datetime

    try:
        with open(log_path or PACMAN_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - limit))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    commands, counts = [], {"installed": 0, "upgraded": 0, "removed": 0, "downgraded": 0}
    running = None  # the command whose transaction the following lines belong to
    for line in lines:
        m = re.match(r"\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)([+-]\d{2})(\d{2})\] \[(PACMAN|ALPM)\] (.*)", line)
        if not m:
            continue
        try:
            at = datetime.fromisoformat(f"{m[1]}{m[2]}:{m[3]}").timestamp()
        except ValueError:
            continue
        if m[4] == "PACMAN" and m[5].startswith("Running '"):
            running = m[5][len("Running '"):].rstrip("'")  # may have started before the plan was made
            if at >= when - 1:
                commands.append(running)
            continue
        if at < when - 1 or m[4] != "ALPM":
            continue
        verb = m[5].split(" ", 1)[0]
        if verb in counts:
            counts[verb] += 1
            if running:
                commands.append(running)
    done = ", ".join(f"{verb} {n}" for verb, n in counts.items() if n)
    who = "; ".join(dict.fromkeys(commands)) or "another package manager"
    return f"{who}" + (f" ({done})" if done else "")


def execute(plan: HelperPlan, *, run: Callable[[list[str], Callable[[str], None]], int], ledger, op_id: str,
            progress: Callable[[str], None] = lambda line: None) -> tuple[bool, str]:
    log: list[str] = []

    def sink(line: str) -> None:
        log.append(line)
        progress(line)

    ok = True
    # The state the next command expects: what the plan was computed from, then (after each of our
    # own commands) what that command left. Anything else means another program changed the system.
    expect_local = plan.local_fingerprint
    expect_sync = None if plan.sync_snapshot else plan.sync_fingerprint

    def still_current() -> None:
        changed = []
        if expect_local and local_db_fingerprint() not in (None, expect_local):
            changed.append("your installed packages")
        if expect_sync and sync_db_fingerprint() not in (None, expect_sync):
            changed.append("your package databases")
        if changed:
            raise PlanRefused(f"{' and '.join(changed)} changed after this plan was made, by "
                              f"{changes_since(plan.created)}. Cygnus will not apply an outdated plan; "
                              "please review it again.")

    try:
        ledger.begin(op_id, plan.kind, plan.caller_uid, plan.summary, plan.commands)  # inside: cleanup must always run
        if plan.kind == "lock":  # no commands: the helper removes the file itself, after checking again
            sink(clear_stale_lock(plan))
        if plan.kind == "packages":
            wait_for_pacman_lock(sink)
            still_current()
        if plan.sync_snapshot:
            with _HeldLock():
                install_sync_snapshot(plan.sync_snapshot)
            expect_sync = sync_db_fingerprint()
        for argv in plan.commands:
            check_command(argv, plan.staged)
            if plan.kind == "packages":
                still_current()  # before EVERY command: another program may act between two of ours
            for attempt in range(3):
                sink("$ " + " ".join(argv))
                lines_before = len(log)
                rc = run(argv, sink)
                raced = rc != 0 and argv[0] == "pacman" and any(
                    "unable to lock database" in line for line in log[lines_before:])
                if not raced:
                    break
                sink("Another package manager started at the same moment; waiting for it…")
                wait_for_pacman_lock(sink)
                still_current()
            if rc != 0:
                sink(f"[exit status {rc}]")
                ok = False
                break
            if plan.kind == "packages":
                expect_local = local_db_fingerprint()  # our own change
        if ok:
            for method, kwargs in plan.ledger_ops:
                getattr(ledger, method)(**kwargs, op_id=op_id)
    except Exception as exc:  # noqa: BLE001 - the ledger must record every outcome
        ok = False
        sink(f"[helper error] {exc}")
    finally:
        for path in plan.staged:
            try:
                os.unlink(path)
            except OSError:
                pass
        if plan.sync_snapshot:
            shutil.rmtree(plan.sync_snapshot, ignore_errors=True)
        ledger.finish(op_id, "succeeded" if ok else "failed", "\n".join(log))
    return ok, "\n".join(log[-50:])


def fresh_sync_snapshot(dbpath: str = "/var/lib/pacman") -> str:
    """Download current sync DBs into a private dbpath (root). Never touches /var/lib/pacman/sync."""
    from cygnus.core.util import proc

    tmp = tempfile.mkdtemp(prefix="cygnus-sync-")
    # pacman 7 downloads as its unprivileged DownloadUser (alpm), which must be able to reach the
    # folder; mkdtemp's 0700 would stop it. The helper's /tmp is private to its service (PrivateTmp).
    os.chmod(tmp, 0o755)
    os.symlink(os.path.join(dbpath, "local"), os.path.join(tmp, "local"))
    res = proc.run(["pacman", "-Sy", "--dbpath", tmp, "--logfile", "/dev/null"], timeout=600)
    if res.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise PlanRefused(f"could not download the package databases: {res.stderr.strip()[-300:]}")
    return tmp
