"""Two-phase component fixes for the GUI: plan (show the helper's message) → commit (polkit asks).

Plans are kept in-process keyed by a token; a token can be committed once. Whatever has to be
recorded after a successful commit (an installation to forget, a package to register) is attached
to the plan and done here, not by the page, so it still happens if the user has left the page.
"""

from __future__ import annotations

import re
import secrets
import subprocess
import threading
import time
from typing import Any, Callable

from cygnus.core import paths
from cygnus.core import progress as progress_mod
from cygnus.core.errors import CygnusError
from cygnus.core.health.engine import Health, _component_health, component_issue, privileges_note
from cygnus.core.health.probes import run_probe
from cygnus.core.manifest import catalog
from cygnus.core.privilege import HelperClient, HelperTimeout, Plan
from cygnus.core.util import http, proc

import json


class _Action:
    """A registry update to make once a commit succeeded. `spec` describes it in plain data, so that one still waiting for the
    helper can be kept in a file and made after Cygnus has been closed and opened again."""

    def __init__(self, spec: dict[str, Any], fn: Callable[[], None]):
        self.spec, self._fn = spec, fn

    def __call__(self) -> None:
        self._fn()


_PENDING: dict[str, tuple[HelperClient, list[Plan]]] = {}
_ON_SUCCESS: dict[str, Callable[[], None]] = {}  # token -> registry update once the commit succeeded
_LOCK = threading.Lock()
# AUR installs that are not finished: the package being installed -> the dependencies installed for it so far. Kept
# here (not in the page) so that leaving the install page half-way does not lose track of what was left, and in a file
# (aur-chains.json in the state folder) so that closing Cygnus does not either.
_CHAINS: dict[str, list[str]] = {}
_CHAINS_LOADED = False
# commits that timed out while the helper might still have been working: op id -> (client, registry update, since), where
# `since` is a wall-clock time. Those whose update can be described (see _Action) are also kept in late-commits.json.
_LATE: dict[str, tuple[HelperClient, Callable[[], None], float]] = {}
_LATE_LOADED = False
LATE_LIMIT_S = 12 * 3600  # an operation still unknown after this long is given up on (the next Refresh shows the truth)
SETTLE_TIMEOUT_MS = 5000  # settling runs on a worker the lists wait for: a wedged helper must not hold it for minutes


def _state_file(name: str):
    return paths.state_dir() / name


def _read_json(name: str) -> dict:
    try:
        data = json.loads(_state_file(name).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(name: str, data: dict) -> None:
    from cygnus.core.util.fs import atomic_write

    try:
        if data:
            atomic_write(_state_file(name), json.dumps(data, sort_keys=True).encode(), 0o600)
        else:
            _state_file(name).unlink(missing_ok=True)
    except OSError:
        pass  # what is kept in memory still works; only surviving a restart is lost


def _reset_loaded() -> None:
    """Forget what was read from the files, so they are read again (for tests, and after the state folder changes)."""
    global _CHAINS_LOADED, _LATE_LOADED
    _CHAINS_LOADED = _LATE_LOADED = False


def _manifest(app: str):
    q = app.lower()
    for k, m in catalog.bundled_manifests().items():
        if q in (k.lower(), m.manifest.application.name.lower()):
            return m
    raise CygnusError(f"no manifest for {app!r}")


def identity_caution(loaded, registry=None) -> str:
    """When the app is installed as an unsigned AppImage, Cygnus knows it only by what the file says
    about itself, and a privileged fix must say so (a lookalike could claim to be the app)."""
    from cygnus.core.ops.appimage_ops import pinned_fingerprint
    from cygnus.core.registry import db as regdb
    from cygnus.core.registry import open_registry

    app = loaded.manifest.application
    if pinned_fingerprint(loaded):
        return ""  # its AppImages are only ever managed with a verified signature
    reg = registry or open_registry()
    if not any(r["app_id"] == app.id and r["format"] == "appimage" for r in regdb.list_installations(reg)):
        return ""
    where = app.homepage or (f"https://{app.vendor.domain}" if app.vendor.domain else f"{app.vendor.name}'s website")
    return (f"Cygnus recognised {app.name} from what the AppImage file says about itself. The file is not signed, "
            f"so Cygnus cannot confirm it really comes from {app.vendor.name}: continue only if you downloaded "
            f"it from {where}.")


def plan(app: str, component_id: str) -> dict[str, Any]:
    loaded = _manifest(app)
    if not loaded.can_drive_actions:
        raise CygnusError("this manifest is not trusted enough to install components")
    comp = loaded.manifest.component(component_id)
    if comp is None:
        raise CygnusError(f"unknown component {component_id!r}")
    health = _component_health(comp, run_probe)
    if health.status is Health.OK:
        return {"kind": "info", "title": comp.name, "message": f"{comp.name} is working."}
    issue = component_issue(health, loaded.manifest)
    res = issue.preferred() if issue else None
    if res is None or not res.actions:
        if res is not None:  # e.g. "log out and back in"
            return {"kind": "info", "title": res.title, "message": res.explanation}
        why = issue.explanation if issue and issue.explanation else "its checks did not pass"
        return {"kind": "info", "title": comp.name,
                "message": f"{comp.name} is not working ({why}). Cygnus has no automatic fix for this."}
    if all(a.kind == "browser.open_store" for a in res.actions):
        urls = [t.store_url for t in comp.browsers.values() if t.store_url]
        return {"kind": "browser", "title": comp.name, "urls": urls,
                "message": "Install the extension from your browser's store, then approve the pairing inside "
                           f"{loaded.manifest.application.name}."}
    client = HelperClient()
    plans: list[Plan] = []
    for a in res.actions:
        k, p = a.kind, a.params
        if k == "group.add_user":
            plans.append(client.plan_group(p["group"], "add"))
        elif k == "systemd.enable_now":
            plans.append(client.plan_unit(p["unit"], "enable_now"))
        elif k == "pacman.install_repo":
            plans.append(client.plan_packages(install_repo=p["names"]))
        elif k == "pacman.install_local":
            path = http.download(p["url"], paths.cache_dir() / "downloads", expected_sha256=p["sha256"])
            plans.append(client.plan_packages(local_files=[(str(path), p["sha256"])]))
        else:
            raise CygnusError(f"action {k!r} is not supported yet")
    token = secrets.token_hex(12)
    with _LOCK:
        _PENDING[token] = (client, plans)
    note = "\n\n".join(n for n in (comp.security_note or "", privileges_note(comp), identity_caution(loaded)) if n)
    return {"kind": "helper", "token": token, "title": comp.name,
            "message": " ".join(pl.message for pl in plans),
            "security_note": note, "relogin": comp.requires_relogin,
            "not_doing": comp.discouraged_vendor_instructions}


def plan_local_package(path: str) -> dict[str, Any]:
    """Ask the helper to plan installing a local Arch package; returns its exact description."""
    from cygnus.gui.service import file_sha256

    client = HelperClient()
    plan_ = client.plan_packages(local_files=[(path, file_sha256(path))])
    out = _remember(client, [plan_], "Install package")
    local = plan_.summary.get("local") or []
    if len(local) == 1:  # what libalpm read from the file, so the entry matches what pacman installs
        _after_commit(out["token"], _recording({"kind": "local", "name": local[0]["name"],
                                                "version": local[0]["version"], "source": path}))
    return out


def _after_commit(token: str, action: Callable[[], None]) -> None:
    with _LOCK:
        _ON_SUCCESS[token] = action


_RECORD_FIELDS = {"aur": ("pkgbase", "name", "commit", "version"), "converted": ("name", "version", "source"),
                  "local": ("name", "version", "source")}


def _recording(record: dict[str, Any]) -> Callable[[], None]:
    """The registry entry for a package Cygnus built or converted, made once pacman has installed it.

    The record is checked now, so a malformed one is refused before anything is planned or installed."""
    from cygnus.gui import service

    kind = record.get("kind") if isinstance(record, dict) else None
    if not isinstance(kind, str) or kind not in _RECORD_FIELDS:
        raise CygnusError("unknown kind of record for what is being installed")
    if not all(isinstance(record.get(k), str) and record[k] for k in _RECORD_FIELDS[kind]):
        raise CygnusError("incomplete record of what is being installed")
    if kind in ("converted", "local"):
        source = {"file": record["source"]}
        vendor, fmt = record.get("vendor_package"), record.get("vendor_format")
        if kind == "converted" and isinstance(vendor, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,100}", vendor) \
                and fmt in ("deb", "rpm"):
            source.update(vendor_package=vendor, vendor_format=fmt)  # optional: only what looks right is kept
        return _Action({"record": record}, lambda: service.record_package_install(record["name"], record["version"],
                                                                                 origin=kind, source=source))
    extra = {}
    if record.get("dependency_of"):
        names = record.get("names")
        if not isinstance(record["dependency_of"], str) or not isinstance(names, list) \
                or not all(isinstance(n, str) for n in names):
            raise CygnusError("incomplete record of what is being installed")
        extra = {"packages": list(names), "dependency_of": record["dependency_of"]}

    def record_aur() -> None:
        service.record_aur_install(record["pkgbase"], record["name"], record["commit"], record["version"], **extra)
        _chain_progress(record["name"], record.get("dependency_of"))

    return _Action({"record": record}, record_aur)


def _chains() -> dict[str, list[str]]:
    """The unfinished AUR installs, read from their file the first time they are asked for."""
    global _CHAINS_LOADED
    if not _CHAINS_LOADED:
        _CHAINS_LOADED = True
        for target, deps in _read_json("aur-chains.json").items():
            if isinstance(target, str) and isinstance(deps, list) and all(isinstance(d, str) for d in deps):
                _CHAINS.setdefault(target, list(deps))
    return _CHAINS


def _save_chains() -> None:
    _write_json("aur-chains.json", {target: list(deps) for target, deps in _CHAINS.items()})


def _chain_progress(name: str, dependency_of: str | None) -> None:
    """A dependency was installed for `dependency_of`: that install is now unfinished. The package itself was installed:
    it is finished."""
    with _LOCK:
        chains = _chains()
        if dependency_of:
            done = chains.setdefault(dependency_of, [])
            if name not in done:
                done.append(name)
        else:
            chains.pop(name, None)
        _save_chains()


def interrupted_aur_chains() -> list[dict[str, Any]]:
    with _LOCK:
        return [{"target": target, "dependencies": list(deps)} for target, deps in _chains().items()]


def dismiss_aur_chain(target: str) -> None:
    with _LOCK:
        _chains().pop(target, None)
        _save_chains()


def _remember(client: HelperClient, plans: list[Plan], title: str) -> dict[str, Any]:
    token = secrets.token_hex(12)
    with _LOCK:
        _PENDING[token] = (client, plans)
    return {"kind": "helper", "token": token, "title": title, "message": " ".join(p.message for p in plans),
            "security_note": "", "relogin": False, "not_doing": []}


def plan_repo_dependencies(names: list[str]) -> dict[str, Any]:
    """Build/run dependencies from your repositories, installed as dependencies (removable later)."""
    client = HelperClient()
    return _remember(client, [client.plan_packages(install_repo=names, asdeps=True)], "Install build dependencies")


def plan_optional_parts(packages: list[str]) -> dict[str, Any]:
    """Install some of Cygnus's own optional tools from your repositories (as ordinary, explicitly installed packages, so
    nothing removes them as unused). Only the packages Cygnus lists as its optional parts are accepted."""
    from cygnus.core import optional_parts

    if not isinstance(packages, list) or not packages or not all(isinstance(n, str) and n in optional_parts.PACKAGES for n in packages):
        raise CygnusError("these are not among Cygnus's optional parts")
    client = HelperClient()
    return _remember(client, [client.plan_packages(install_repo=sorted(set(packages)))], "Install optional parts")


_BUILT_NOTES = {
    "aur": "Built from community build files that you reviewed. pacman installs it with administrator rights, "
           "including any install script it has.",
    "converted": "Converted by Cygnus from the vendor's package: its install scripts were left out, and pacman "
                 "will own every file it installs.",
}


def plan_built_packages(paths: list[str], asdeps: bool = False, origin: str = "aur",
                        record: dict[str, Any] | None = None) -> dict[str, Any]:
    """`record` says what to register once the packages are installed (see `_recording`)."""
    from cygnus.gui.service import file_sha256

    recorder = _recording(record) if record else None
    client = HelperClient()
    plan_ = client.plan_packages(local_files=[(p, file_sha256(p)) for p in paths], asdeps=asdeps)
    out = _remember(client, [plan_], "Install the package you built" if origin == "aur" else "Install the converted package")
    out["security_note"] = _BUILT_NOTES[origin]
    if recorder:
        _after_commit(out["token"], recorder)
    return out


def plan_system_upgrade() -> dict[str, Any]:
    """The helper's full-upgrade plan (fresh databases, never a partial upgrade), with the details to show."""
    client = HelperClient()
    plan_ = client.plan_packages(sysupgrade=True)
    out = _remember(client, [plan_], "Update the system")
    s = plan_.summary
    out["upgrades"] = [{"name": p["name"], "from": p.get("from") or "", "to": p["version"]} for p in s.get("upgrade", [])]
    out["installs"] = [{"name": p["name"], "to": p["version"], "reason": p.get("reason", "")} for p in s.get("install", [])]
    out["removals"] = [{"name": p["name"], "reason": p.get("reason", "")} for p in s.get("remove", [])]
    out["download_bytes"] = s.get("download_bytes") or 0
    return out


def plan_clear_stale_lock() -> dict[str, Any]:
    client = HelperClient()
    return _remember(client, [client.plan_clear_stale_lock()], "Remove pacman's stale lock")


def plan_remove_packages(names: list[str], forget: str | None = None) -> dict[str, Any]:
    """Plan removing packages; `forget` is the installation whose record goes once they are removed."""
    from cygnus.gui import service

    client = HelperClient()
    out = _remember(client, [client.plan_packages(remove=names)], "Remove packages")
    if forget:
        _after_commit(out["token"], _Action({"forget": forget}, lambda: service.forget_installation(forget)))
    return out


def commit(token: str, progress: Callable[[str], None]) -> dict[str, Any]:
    with _LOCK:
        entry = _PENDING.pop(token, None)
        after = _ON_SUCCESS.pop(token, None)
    if entry is None:
        raise CygnusError("this confirmation has expired; please try again")
    client, plans = entry
    for index, pl in enumerate(plans):
        try:
            ok, detail = client.commit(pl, on_progress=lambda line: progress(progress_mod.pacman_line(line)))
        except HelperTimeout as timeout:
            if index < len(plans) - 1:  # later steps were never started: this is not a finished change
                return {"ok": False, "detail": "Cygnus lost contact with the administrator helper part-way through; "
                                               "the remaining steps were not run. Check the result and try again."}
            return _after_timeout(client, timeout.op_id, after)
        if not ok:
            return {"ok": False, "detail": detail}
    if after is not None:
        return _record(after)
    return {"ok": True}


def _record(after: Callable[[], None]) -> dict[str, Any]:
    try:
        after()
    except Exception as exc:  # noqa: BLE001 - the change itself is done; say what is left to do
        return {"ok": False, "detail": f"The change was made, but Cygnus could not update its records: {exc}. "
                                       "Run `cygnus doctor` to see what is out of step."}
    return {"ok": True}


def _installed_version(name: str) -> str | None:
    res = proc.run(["pacman", "-Q", "--", name], timeout=30)
    parts = res.stdout.split() if res.returncode == 0 else []
    return parts[1] if len(parts) == 2 else None


def _outcome_is_in_place(after: Any) -> bool:
    """Did the change really get made? Asked when the helper stopped half-way (a reboot during an install: pacman may have
    finished anyway) and before a removal is forgotten long afterwards (the program may have been installed again since).
    Judged from what pacman says is installed. An update that cannot be described is taken as not made."""
    from cygnus.core.ops import convert
    from cygnus.gui import service

    spec = getattr(after, "spec", None)
    if not isinstance(spec, dict):
        return False
    if isinstance(spec.get("forget"), str):
        try:
            return all(_installed_version(n) is None for n in service.package_names(spec["forget"]))
        except Exception:  # noqa: BLE001 - not knowing is not a yes
            return False
    record = spec.get("record")
    if not isinstance(record, dict) or not isinstance(record.get("name"), str) or not isinstance(record.get("version"), str):
        return False
    installed = _installed_version(record["name"])
    if installed is None:
        return False
    if record.get("kind") == "converted":  # the package carries Cygnus's own release number; the version is the vendor's
        epoch, pkgver = convert.pacman_version(record["version"])
        have_epoch, _, rest = installed.rpartition(":")
        return rest.rsplit("-", 1)[0] == pkgver and (epoch is None or have_epoch == epoch)
    return installed == record["version"]


def _after_timeout(client: HelperClient, op_id: str, after: Callable[[], None] | None) -> dict[str, Any]:
    """The helper did not answer in time. Its ledger says what became of the operation, so the registry is updated
    if (and only if) it succeeded; if it is still running, the update waits for `settle_late_commits`."""
    try:
        state = client.operation_state(op_id, timeout_ms=SETTLE_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - not knowing is not an answer
        state = None
    if state == "succeeded":
        return _record(after) if after is not None else {"ok": True}
    if state == "failed":
        return {"ok": False, "detail": "The administrator helper reported that the change failed."}
    if state == "interrupted":
        if after is not None and _outcome_is_in_place(after):  # pacman finished before the helper went away
            return _record(after)
        return {"ok": False, "detail": "The administrator helper stopped before it finished, so the change may be half "
                                       "done. Check the result, and do it again if it is not there (that is safe): "
                                       "Cygnus then records it."}
    if after is not None:
        with _LOCK:
            _LATE[op_id] = (client, after, time.time())
            _save_late()
    return {"ok": False, "detail": "Cygnus lost contact with the administrator helper while it was working. The change "
                                   "may still finish; while this window stays open, Cygnus records the result if it does."}


def _save_late() -> None:
    """Keep the waiting updates that can be described in data (call with the lock held)."""
    _write_json("late-commits.json", {op: {"spec": after.spec, "since": since} for op, (_c, after, since) in _LATE.items()
                                      if isinstance(getattr(after, "spec", None), dict)})


def _action_from(spec: Any) -> _Action | None:
    from cygnus.gui import service

    if not isinstance(spec, dict):
        return None
    try:
        if isinstance(spec.get("record"), dict):
            return _recording(spec["record"])
        if isinstance(spec.get("forget"), str):
            return _Action(spec, lambda: service.forget_installation(spec["forget"]))
    except CygnusError:
        return None
    return None


def _load_late() -> None:
    """Pick up the updates a closed Cygnus was still waiting to make (once). They are matched with the helper's ledger like
    any other; ones that cannot be rebuilt, or that are too old, are dropped."""
    global _LATE_LOADED
    with _LOCK:
        if _LATE_LOADED:
            return
        _LATE_LOADED = True
        saved = _read_json("late-commits.json")
    if not saved:
        return
    try:
        client = HelperClient()
    except Exception:  # noqa: BLE001 - no bus now: the file stays for the next time
        with _LOCK:
            _LATE_LOADED = False
        return
    with _LOCK:
        for op_id, item in saved.items():
            try:
                after = _action_from(item.get("spec")) if isinstance(item, dict) else None
            except Exception:  # noqa: BLE001 - one damaged entry must not keep the others (or the list) from working
                after = None
            since = item.get("since") if isinstance(item, dict) else None
            if after is not None and isinstance(since, (int, float)) and op_id not in _LATE:
                _LATE[op_id] = (client, after, float(since))
        _save_late()


def settle_late_commits() -> None:
    """Apply (or drop) the registry updates of commits that timed out, once the ledger knows how they ended."""
    _load_late()
    with _LOCK:
        waiting = dict(_LATE)
    for op_id, (client, after, since) in waiting.items():
        try:
            state = client.operation_state(op_id, timeout_ms=SETTLE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - try again at the next refresh
            state = None
        over = time.time() - since > LATE_LIMIT_S
        if state in ("succeeded", "failed", "interrupted") or over:
            with _LOCK:
                mine = _LATE.pop(op_id, None) is not None  # two refreshes may look at once: only one acts
                if mine:
                    _save_late()
            if mine and state == "succeeded":
                spec = getattr(after, "spec", None)
                if isinstance(spec, dict) and "forget" in spec and not _outcome_is_in_place(after):
                    return  # the program was installed again since: its new record is not to be removed
                _record(after)
            elif mine and state == "interrupted" and _outcome_is_in_place(after):
                _record(after)


def open_urls(urls: list[str]) -> None:
    for u in urls:
        if u.startswith("https://"):
            subprocess.Popen(["xdg-open", u], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
