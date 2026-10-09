"""Update checks for managed applications (architecture §13).

Each installation is checked through the provider it was installed with: AppImage update
information (zsync or GitHub releases), or the Flatpak remote it came from. A failed check is
reported as "unknown" with the reason; it is never mistaken for "up to date". Results are stored
so the UI can show the last check without going online. System packages are not checked here:
they are only ever updated together with the whole system.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cygnus.core.errors import CygnusError
from cygnus.core.registry import db as regdb
from cygnus.core.registry.db import Registry

UP_TO_DATE, AVAILABLE, UNKNOWN, MANUAL, SYSTEM, OFFLINE = (
    "up-to-date", "update-available", "unknown", "manual", "system", "offline")


@dataclass(slots=True, kw_only=True)
class UpdateStatus:
    installation_id: str
    name: str
    format: str
    provider: str
    status: str
    detail: str
    current: str | None = None
    available: str | None = None
    checked_at: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)  # download URL, expected digests, …

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(row: dict[str, Any], status: str, detail: str, *, provider: str | None = None, **kw) -> UpdateStatus:
    return UpdateStatus(installation_id=row["id"], name=row["name"], format=row["format"],
                        provider=provider or row["update_provider"].get("type", "none"), status=status, detail=detail,
                        current=row.get("version"), **kw)


def _check_appimage(row: dict[str, Any], **kw) -> UpdateStatus:
    from cygnus.core.backends import appimage as ab

    path = Path(row["source"].get("path", ""))
    info = row["source"].get("update_info")
    if not info:
        return _status(row, MANUAL, "This AppImage has no update information; download newer versions from the vendor.")
    try:
        if not path.is_file():
            return _status(row, OFFLINE, f"{path} is not available (is its drive connected?)")
    except OSError as exc:
        return _status(row, OFFLINE, f"{path} cannot be read: {exc.strerror}")
    res = ab.check_update(path, info, row.get("version"), **kw)
    status = {"up-to-date": UP_TO_DATE, "update-available": AVAILABLE}.get(res.status, UNKNOWN)
    return _status(row, status, res.detail, available=res.available_version,
                   facts={**res.facts, "download_url": res.download_url, "expected_sha256": res.expected_sha256,
                          "expected_sha1": res.expected_sha1})


def _check_flatpak(row: dict[str, Any], **_kw) -> UpdateStatus:
    from cygnus.core.ops import flatpak_ops

    if row["update_provider"].get("type") == "manual":
        return _status(row, MANUAL, "This bundle names no update source; install a newer bundle from the vendor.")
    src = row["source"]
    ref = src.get("ref") or ""
    _, _, GLib = flatpak_ops._gi()
    try:
        inst = flatpak_ops.installation_for(src.get("installation", "user"))
        installed = flatpak_ops._installed(inst, ref)
        if installed is None:
            return _status(row, UNKNOWN, f"{ref} is no longer installed")
        pending = {r.format_ref(): r for r in inst.list_installed_refs_for_update(None)}
    except GLib.Error as exc:
        return _status(row, UNKNOWN, f"Flatpak could not check for updates: {exc.message}")
    facts = {"origin": installed.get_origin(), "commit": installed.get_commit()}
    if installed.get_eol():
        facts["eol"] = installed.get_eol()
    if ref in pending:
        return _status(row, AVAILABLE, f"A newer build is available from {installed.get_origin()}.", facts=facts,
                       available=installed.get_appdata_version() or None)
    return _status(row, UP_TO_DATE, f"Matches the latest build on {installed.get_origin()}.", facts=facts)


def is_converted(row: dict[str, Any]) -> bool:
    """A program Cygnus converted from a .deb/.rpm: pacman has it, but nothing updates it."""
    return row["format"] == "pacman" and row["source"].get("made_by") == "converted"


def _check_converted(row: dict[str, Any], *, fetch=None, **_kw) -> UpdateStatus:
    """The newest version in the vendor's own package list (see vendor_feeds), when Cygnus knows where that is."""
    from cygnus.core.backends import vendor_feeds as vf

    source = row["source"]
    name = source.get("vendor_package") or source.get("package") or row["name"]
    fmt = source.get("vendor_format") or "deb"
    feed = vf.feed_for(name)
    if feed is None:
        return _status(row, MANUAL, "Cygnus does not know where newer versions of this program are published. When you "
                       "download a newer file, open it with Cygnus: it replaces this copy.", provider="manual")
    if fmt == "rpm" and not feed.rpm:
        return _status(row, MANUAL, "Cygnus does not know where this vendor publishes the rpm of this program. When you "
                       "download a newer file, open it with Cygnus: it replaces this copy.", provider="manual")
    if not row.get("version"):
        return _status(row, UNKNOWN, "The installed version is not recorded.", provider="vendor-feed")
    release = vf.newest(feed, fmt, **({"fetch": fetch} if fetch else {}))
    facts = {"download_url": release.url, "expected_sha256": release.sha256, "kind": "converted", "format": fmt}
    if vf.vercmp(release.version, row["version"]) > 0:
        return _status(row, AVAILABLE, f"{release.version} is published by the vendor.", provider="vendor-feed",
                       available=release.version, facts=facts)
    return _status(row, UP_TO_DATE, "Matches the newest version the vendor publishes.", provider="vendor-feed", facts=facts)


CHECKERS: dict[str, Callable[..., UpdateStatus]] = {"appimage": _check_appimage, "flatpak": _check_flatpak}


def _checker(row: dict[str, Any]) -> Callable[..., UpdateStatus] | None:
    return _check_converted if is_converted(row) else CHECKERS.get(row["format"])


def check(row: dict[str, Any], **kw) -> UpdateStatus:
    checker = _checker(row)
    if checker is None:
        return _status(row, SYSTEM, "Updated together with your whole system.")
    try:
        result = checker(row, **kw)
    except (CygnusError, ValueError) as exc:  # network errors, malformed vendor responses
        result = _status(row, UNKNOWN, f"Could not check: {exc}", provider="vendor-feed" if checker is _check_converted else None)
    result.checked_at = _now()
    return result


def record(reg: Registry, st: UpdateStatus) -> None:
    with reg.transaction() as c:
        c.execute("INSERT INTO update_check(installation_id, provider, current, available, checked_at, notes) "
                  "VALUES (?,?,?,?,?,?) ON CONFLICT(installation_id) DO UPDATE SET provider=excluded.provider, "
                  "current=excluded.current, available=excluded.available, checked_at=excluded.checked_at, "
                  "notes=excluded.notes",
                  (st.installation_id, st.provider, st.current, st.available, st.checked_at,
                   json.dumps({"status": st.status, "detail": st.detail, "facts": st.facts})))


def check_all(reg: Registry, progress: Callable[[str], None] = lambda _: None, **kw) -> list[UpdateStatus]:
    from cygnus.core.progress import Progress

    out = []
    rows = regdb.list_installations(reg)
    checked = [r for r in rows if _checker(r)]
    for row in rows:
        if _checker(row):
            progress(Progress(f"Checking {row['name']}…", checked.index(row) / len(checked)))
        st = check(row, **kw)
        if st.status != SYSTEM:
            record(reg, st)
        out.append(st)
    return out


def last_known(reg: Registry) -> list[UpdateStatus]:
    """The stored result of the last check for every installation (no network)."""
    stored = {r[0]: r for r in reg.conn.execute(
        "SELECT installation_id, provider, current, available, checked_at, notes FROM update_check")}
    out = []
    for row in regdb.list_installations(reg):
        s = stored.get(row["id"])
        if _checker(row) is None:
            out.append(_status(row, SYSTEM, "Updated together with your whole system."))
        elif s is None or (s[2] or None) != (row.get("version") or None):
            out.append(_status(row, UNKNOWN, "Not checked yet."))
        else:
            notes = json.loads(s[5] or "{}")
            out.append(_status(row, notes.get("status", UNKNOWN), notes.get("detail", ""), available=s[3],
                               checked_at=s[4], facts=notes.get("facts", {})))
    return out


# -- system packages ----------------------------------------------------------------------------------------
def system_updates_cache() -> Path:
    from cygnus.core import paths

    return paths.cache_dir() / "system-updates.json"


def private_db(cfg) -> Path:
    """Cygnus's own copy of the package databases, which it may refresh without root. The installed-packages
    database is read through a link, never copied or written."""
    from cygnus.core import paths

    db = paths.cache_dir() / "pacman-db"
    db.mkdir(parents=True, exist_ok=True)
    local = db / "local"
    if not local.is_symlink():
        if local.is_dir():
            shutil.rmtree(local)
        elif local.exists():
            local.unlink()
        os.symlink(os.path.join(cfg.dbpath, "local"), local)
    return db


def clear_stale_lock(db: Path) -> None:
    """Remove the lock a killed pacman left in Cygnus's private database copy. Only Cygnus uses that folder, so
    when no pacman is running on it the lock is stale; left alone it would stop every later check and file search."""
    lock = db / "db.lck"
    if not lock.exists():
        return
    needle = str(db).encode()
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmdline and b"pacman" in cmdline:
            return  # a run that is still going holds it
    try:
        lock.unlink()
    except OSError:
        pass


def check_system_updates(progress: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    """Which installed packages have newer versions in your repositories, without root and without
    touching /var/lib/pacman: fresh databases are downloaded into Cygnus's own copy (as
    `checkupdates` does). Nothing is installed; the result is cached for the Updates page."""
    from cygnus.core import paths
    from cygnus.core.backends import pacman as pm
    from cygnus.core.util import proc

    result: dict[str, Any] = {"checked_at": _now(), "packages": [], "kernel": False, "error": None}
    fakeroot = proc.which("fakeroot")
    if fakeroot is None:
        result["error"] = "fakeroot is not installed (package fakeroot), so Cygnus cannot check without root"
    else:
        cfg = pm.read_config()
        db = private_db(cfg)
        clear_stale_lock(db)
        progress("Checking your repositories for system updates…")
        # pacman's download sandbox protects root's pacman; this copy runs as you, and the sandbox cannot
        # be applied under fakeroot (checkupdates disables it the same way).
        res = proc.run([fakeroot, "--", "pacman", "-Sy", "--disable-sandbox-filesystem", "--dbpath", str(db),
                        "--logfile", "/dev/null"], timeout=600)
        if res.returncode != 0:
            result["error"] = "could not download the package databases: " + (res.stderr.strip().splitlines() or ["?"])[-1]
        else:
            try:
                outdated = pm.run_worker(cfg, {"op": "outdated", "syncdir": str(db / "sync")})["outdated"]
            except CygnusError as exc:  # recorded like any other failed check, never as "up to date"
                result["error"] = f"could not compare the package databases: {exc}"
                outdated = []
            result["packages"] = sorted(outdated, key=lambda p: p["name"])
            result["kernel"] = any(pm.is_protected(p["name"]) and pm._KERNEL.match(p["name"]) for p in outdated)
    path = system_updates_cache()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result))
    return result


def last_system_updates() -> dict[str, Any] | None:
    try:
        return json.loads(system_updates_cache().read_text())
    except (OSError, ValueError):
        return None
