"""AppImage operations: adopt, install (copy to a storage location), move, uninstall.

All steps are user-scope (no privileges). Each step is idempotent and has a compensation.
Payload deletion is always the *last* step and only happens after everything else succeeded.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cygnus.core.desktop import integrate
from cygnus.core.detect import appimage as ai_detect
from cygnus.core.errors import CygnusError
from cygnus.core.executor import Executor, Step, StepOutcome
from cygnus.core.models import Candidate
from cygnus.core.registry import db as regdb
from cygnus.core.registry.db import Registry, StorageLocation
from cygnus.core.util.fs import fsync_dir


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def app_key_for(cand: Candidate, manifest_app_id: str | None = None) -> str:
    key = manifest_app_id or cand.identity.get("appstream_id") or \
        "appimage." + (cand.identity.get("desktop_id") or cand.name or "app").removesuffix(".desktop")
    key = re.sub(r"[^A-Za-z0-9._-]", "-", key).strip(".-")[:100]
    if not key:
        raise CygnusError("cannot derive an application key")
    return key


# -- step handlers ----------------------------------------------------------------------------------------
def _copy_abort(params: dict) -> StepOutcome:
    """The process died while copying: remove the copy (and any partial file) it was making."""
    src, dest = Path(params["src"]), Path(params["dest"])
    for part in dest.parent.glob(f".{dest.name}.cygnus-*.part") if dest.parent.is_dir() else []:
        part.unlink(missing_ok=True)
    if params.get("existed") is False and dest.is_file() and dest != src \
            and sha256_file(dest) == params["expect_sha256"]:
        dest.unlink()
    return StepOutcome(result={"aborted": str(dest)})


def _copy(params: dict) -> StepOutcome:
    src, dest = Path(params["src"]), Path(params["dest"])
    if dest.exists():
        if sha256_file(dest) == params["expect_sha256"]:
            if params.get("existed") is False:
                # It was not there when this step was planned, so an earlier run of this very step made it
                # (and died before journaling it): undoing the operation must still remove it.
                return StepOutcome(result={"path": str(dest), "already_present": True},
                                   compensation=Step(kind="appimage.remove",
                                                     params={"path": str(dest),
                                                             "expect_sha256": params["expect_sha256"]}))
            return StepOutcome(result={"path": str(dest), "already_present": True})  # idempotent re-run
        raise CygnusError(f"{dest} already exists and is a different file")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.cygnus-{os.getpid()}.part")
    try:
        with open(src, "rb") as fin, open(tmp, "xb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
            fout.flush()
            os.fsync(fout.fileno())
        os.chmod(tmp, 0o755)
        if sha256_file(tmp) != params["expect_sha256"]:
            raise CygnusError("the copy does not match the original (storage error?)")
        os.rename(tmp, dest)
        fsync_dir(dest.parent)
    finally:
        if tmp.exists():
            tmp.unlink()
    return StepOutcome(result={"path": str(dest)},
                       compensation=Step(kind="appimage.remove", params={"path": str(dest),
                                                                         "expect_sha256": params["expect_sha256"]}))


def _remove(params: dict) -> StepOutcome:
    path = Path(params["path"])
    if not path.exists():
        return StepOutcome(result={"removed": False})
    if not path.is_file() or path.is_symlink():
        raise CygnusError(f"{path} is not a regular file; not removing it")
    if sha256_file(path) != params["expect_sha256"]:
        raise CygnusError(f"{path} changed since Cygnus checked it; not removing it")
    path.unlink()
    return StepOutcome(result={"removed": True})


@dataclass(slots=True)
class Ops:
    """Binds step handlers that need the registry."""

    registry: Registry

    def _record(self, params: dict) -> StepOutcome:
        regdb.add_application(self.registry, app_id=params["app_id"], display_name=params["name"],
                              vendor=params.get("vendor"), trust_level=params.get("trust"))
        existing = self.registry.conn.execute("SELECT 1 FROM installation WHERE id=?",
                                              (params["installation_id"],)).fetchone()
        if not existing:
            regdb.add_installation(self.registry, app_id=params["app_id"], fmt="appimage",
                                   source=params["source"], version=params.get("version"),
                                   location_id=params.get("location_id"), origin=params["origin"],
                                   update_provider=params.get("update_provider"),
                                   installation_id=params["installation_id"])
        for art in params.get("artifacts", []):
            regdb.add_artifact(self.registry, installation_id=params["installation_id"], **art)
        regdb.add_history(self.registry, params["installation_id"], params["origin"],
                          {"path": params["source"].get("path"), "version": params.get("version")})
        return StepOutcome(result={"installation_id": params["installation_id"]},
                           compensation=Step(kind="registry.forget",
                                             params={"installation_id": params["installation_id"]}))

    def _record_abort(self, params: dict) -> StepOutcome:
        """The process died while recording: whatever got recorded is forgotten again."""
        if self.registry.conn.execute("SELECT 1 FROM installation WHERE id=?", (params["installation_id"],)).fetchone():
            self._forget({"installation_id": params["installation_id"]})
        return StepOutcome(result={"aborted": params["installation_id"]})

    def _forget(self, params: dict) -> StepOutcome:
        iid = params["installation_id"]
        snapshot = self._snapshot(iid)
        with self.registry.transaction() as c:
            c.execute("DELETE FROM artifact WHERE installation_id=?", (iid,))
        regdb.remove_installation(self.registry, iid)
        return StepOutcome(result={"forgotten": iid},
                           compensation=Step(kind="registry.restore", params={"snapshot": snapshot}) if snapshot
                           else None)

    def _cascading_tables(self) -> list[str]:
        """Every table whose rows are deleted along with an installation (found from the schema, so a table
        added later is covered too)."""
        c = self.registry.conn
        out = []
        for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for fk in c.execute(f"PRAGMA foreign_key_list({table})").fetchall():
                if fk[2] == "installation" and fk[3] == "installation_id" and fk[6] == "CASCADE":
                    out.append(table)
        return sorted(set(out))

    def _snapshot(self, iid: str) -> dict[str, list[dict]] | None:
        c = self.registry.conn
        inst = c.execute("SELECT * FROM installation WHERE id=?", (iid,))
        cols = [d[0] for d in inst.description]
        row = inst.fetchone()
        if row is None:
            return None
        inst_row = dict(zip(cols, row))
        app = c.execute("SELECT * FROM application WHERE id=?", (inst_row["application_id"],))
        app_cols = [d[0] for d in app.description]
        arts = c.execute("SELECT * FROM artifact WHERE installation_id=?", (iid,))
        art_cols = [d[0] for d in arts.description]
        snapshot = {"application": [dict(zip(app_cols, r)) for r in app.fetchall()], "installation": [inst_row],
                    "artifact": [dict(zip(art_cols, r)) for r in arts.fetchall()]}
        for table in self._cascading_tables():  # history, update checks…: deleted with the installation
            rows = c.execute(f"SELECT * FROM {table} WHERE installation_id=?", (iid,))
            names = [d[0] for d in rows.description]
            snapshot[table] = [dict(zip(names, r)) for r in rows.fetchall()]
        return snapshot

    def _restore(self, params: dict) -> StepOutcome:
        snap = params["snapshot"]
        order = ["application", "installation", "artifact", *[k for k in snap if k not in
                                                              ("application", "installation", "artifact")]]
        known = {n for (n,) in self.registry.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with self.registry.transaction() as c:
            for table in order:
                if table not in known:
                    continue
                for row in snap.get(table, []):
                    cols = list(row)
                    c.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES "
                              f"({', '.join('?' for _ in cols)})", [row[k] for k in cols])
        return StepOutcome(result={"restored": True})

    def executor(self) -> Executor:
        ex = Executor(self.registry, dict(integrate.HANDLERS))
        ex.register("appimage.copy", _copy)
        ex.register("appimage.copy.abort", _copy_abort)
        ex.register("registry.record.abort", self._record_abort)
        ex.register("appimage.remove", _remove)
        ex.register("registry.record", self._record)
        ex.register("registry.forget", self._forget)
        ex.register("registry.restore", self._restore)
        ex.register("registry.update", lambda params: _update_record(self.registry, params))
        ex.register("appimage.swap", _swap)
        ex.register("appimage.swap.abort", _swap_abort)
        ex.register("appimage.unswap", _unswap)
        ex.register("desktop.refresh", lambda p: StepOutcome(result={"problems": integrate.refresh_caches()}))
        return ex


# -- plans --------------------------------------------------------------------------------------------------
def _icon(cand: Candidate) -> bytes | None:
    off = cand.metadata.get("payload_offset")
    if off is None or cand.metadata.get("payload_format") != "squashfs":
        return None
    try:
        return ai_detect._cat(Path(cand.source), off, ".DirIcon") if cand.metadata.get("icon_bytes") else None
    except CygnusError:
        return None


def integration_steps(cand: Candidate, *, app_key: str, payload_path: str, location: StorageLocation | None,
                      autostart: list[Path] = (), owned: frozenset[str] = frozenset()
                      ) -> tuple[list[Step], list[dict[str, Any]], list[str]]:
    """Integration files to write. `owned` are files this installation already has; an icon file that
    exists but is not one of them belongs to someone else and is used, never overwritten."""
    plan = integrate.plan_appimage_integration(
        app_key=app_key, display_name=cand.name or app_key, appimage_path=payload_path,
        fs_uuid=location.fs_uuid if location and location.location_class != "system" else None,
        location_label=location.label if location else "this computer",
        upstream_desktop=cand.metadata.get("desktop_entry"),
        upstream_actions=cand.metadata.get("desktop_action_groups"),
        upstream_desktop_id=cand.identity.get("desktop_id"), icon=_icon(cand),
        icon_kind=cand.metadata.get("icon_kind"), autostart_entries=list(autostart))
    steps, artifacts = [], []
    for f in plan.files:
        if f.kind == "icon" and str(f.path) not in owned and (f.path.exists() or f.path.is_symlink()):
            continue  # the menu entry names the icon; the existing file provides it
        steps.append(Step(kind="fs.write_owned", params={"path": str(f.path), "data_hex": f.data.hex(),
                                                         "mode": f.mode, "existed": f.path.exists()},
                          description=f"Write {f.kind}"))
        artifacts.append({"kind": f.kind, "locator": str(f.path), "sha256": f.sha256,
                          "ownership": "adopted" if f.kind == "autostart" else "created",
                          "on_uninstall": "ask" if f.kind == "autostart" else "remove"})
    return steps, artifacts, plan.notes


def plan_adopt(cand: Candidate, *, app_key: str, location: StorageLocation | None, manifest=None,
               autostart: list[Path] = ()) -> list[Step]:
    """Register an AppImage that already exists where it is, and integrate it with KDE."""
    iid = str(uuid.uuid4())
    path = cand.source
    digest = sha256_file(Path(path))
    if pinned_fingerprint(manifest):  # whoever calls this: the vendor signs it, so it must be signed
        require_pinned_signature(Path(path), manifest)
        if sha256_file(Path(path)) != digest:
            raise CygnusError(f"{Path(path).name} changed while it was being verified; try again")
    steps, artifacts, _ = integration_steps(cand, app_key=app_key, payload_path=path, location=location,
                                            autostart=autostart)
    app = manifest.manifest.application if manifest else None
    steps.append(Step(kind="desktop.refresh", params={"rerun_after_rollback": True}, description="Refresh the application menu"))
    steps.append(Step(kind="registry.record", description="Record the application", params={
        "installation_id": iid, "app_id": app_key, "name": (app.name if app else cand.name) or app_key,
        "vendor": app.vendor.name if app else cand.metadata.get("vendor"),
        "trust": manifest.trust_level if manifest else "unverified", "origin": "adopted",
        "source": {"path": path, "sha256": digest, "update_info": cand.metadata.get("update_info")},
        "version": cand.version, "location_id": location.id if location else None,
        "update_provider": {"type": (cand.metadata.get("update_info") or {}).get("type", "none")},
        "artifacts": artifacts}))
    return steps


def plan_install(cand: Candidate, *, app_key: str, location: StorageLocation, apps_dir: Path,
                 manifest=None) -> list[Step]:
    """Copy the AppImage into the location's applications folder, then integrate and record it."""
    src = Path(cand.source)
    folder = apps_dir / re.sub(r"[^A-Za-z0-9 ._-]", "_", cand.name or app_key)[:80]
    dest = folder / src.name
    digest = sha256_file(src)
    if pinned_fingerprint(manifest):  # every caller, CLI or GUI: the vendor signs it, so it must be signed
        require_pinned_signature(src, manifest)
        if sha256_file(src) != digest:
            raise CygnusError(f"{src.name} changed while it was being verified; try again")
    steps = [Step(kind="appimage.copy", params={"src": str(src), "dest": str(dest), "expect_sha256": digest,
                                                "existed": dest.exists()},
                  description=f"Copy to {location.label}")]
    integ, artifacts, _ = integration_steps(cand, app_key=app_key, payload_path=str(dest), location=location)
    steps += integ
    steps.append(Step(kind="desktop.refresh", params={"rerun_after_rollback": True}, description="Refresh the application menu"))
    app = manifest.manifest.application if manifest else None
    steps.append(Step(kind="registry.record", description="Record the application", params={
        "installation_id": str(uuid.uuid4()), "app_id": app_key, "name": (app.name if app else cand.name) or app_key,
        "vendor": app.vendor.name if app else cand.metadata.get("vendor"),
        "trust": manifest.trust_level if manifest else "unverified", "origin": "installed",
        "source": {"path": str(dest), "sha256": digest, "update_info": cand.metadata.get("update_info"),
                   "installed_at": datetime.now(UTC).isoformat()},
        "version": cand.version, "location_id": location.id,
        "update_provider": {"type": (cand.metadata.get("update_info") or {}).get("type", "none")},
        "artifacts": artifacts + [{"kind": "file", "locator": str(dest), "sha256": digest}]}))
    return steps


def plan_uninstall(registry: Registry, installation_id: str, *, remove_payload: bool,
                   remove_adopted: list[str] = ()) -> list[Step]:
    """Remove what Cygnus created (and, only if asked, adopted items). Payload goes last."""
    row = registry.conn.execute("SELECT origin, source FROM installation WHERE id=?", (installation_id,)).fetchone()
    if row is None:
        raise CygnusError("unknown installation")
    origin, source = row[0], __import__("json").loads(row[1])
    steps: list[Step] = []
    payload: Step | None = None
    arts = regdb.artifacts_of(registry, installation_id)
    # Entries you edited are kept (never deleted); the launcher they point at is kept with them.
    edited = {}
    for a in arts:
        if a["kind"] in ("desktop_entry", "autostart") and a["sha256"]:
            try:
                data = Path(a["locator"]).read_bytes()
            except OSError:
                continue
            if hashlib.sha256(data).hexdigest() != a["sha256"]:
                edited[a["locator"]] = data.decode("utf-8", "replace")
    for art in arts:
        if art["kind"] == "launcher_shim" and any(art["locator"] in text for text in edited.values()):
            continue
        if art["kind"] == "file":
            if remove_payload:
                payload = Step(kind="appimage.remove", params={"path": art["locator"],
                                                               "expect_sha256": art["sha256"]},
                               description="Delete the application file")
            continue
        if art["ownership"] == "adopted" and art["locator"] not in remove_adopted:
            if art["kind"] == "autostart":  # kept: it must not go on running the launcher that is removed
                step = _release_autostart(Path(art["locator"]), art["sha256"], source.get("path"))
                if step:
                    steps.append(step)
            continue
        steps.append(Step(kind="fs.remove_owned", params={"path": art["locator"], "expect_sha256": art["sha256"],
                                                          "keep_if_changed": True},
                          description=f"Remove {art['kind']}"))
    if remove_payload and payload is None and origin == "adopted" and source.get("path"):
        payload = Step(kind="appimage.remove", params={"path": source["path"], "expect_sha256": source["sha256"]},
                       description="Delete the application file")
    steps.append(Step(kind="desktop.refresh", params={"rerun_after_rollback": True}, description="Refresh the application menu"))
    steps.append(Step(kind="registry.forget", params={"installation_id": installation_id},
                      description="Forget the application"))
    if payload is not None:
        steps.append(payload)  # irreversible: only after everything else succeeded
    return steps


def _release_autostart(path: Path, sha256: str | None, program: str | None) -> Step | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not program or (sha256 and integrate.sha256_bytes(data) != sha256):
        return None  # changed by you since: left exactly as it is
    new = integrate.release_autostart(data.decode("utf-8", "replace"), Path(program))
    if new is None:
        return None
    return Step(kind="fs.write_owned", params={"path": str(path), "data_hex": new.hex(), "mode": 0o644},
                description="Point the kept autostart entry at the application file")


# -- updates ------------------------------------------------------------------------------------------------
STAGING_PREFIX = ".cygnus-update-"


def _sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def stage_update(current: Path, url: str, *, expected_sha256: str | None = None, expected_sha1: str | None = None,
                 download=None, progress=None) -> Path:
    """Download a new build next to `current` (same filesystem, so the swap is an atomic rename) and
    verify the digest the vendor published. Returns the staged file; nothing else is touched."""
    import tempfile

    from cygnus.core.util import http

    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=current.parent))
    try:
        path = (download or http.download)(url, staging, expected_sha256=expected_sha256, progress=progress)
        path = Path(path)
        if expected_sha1 and _sha1_file(path) != expected_sha1:
            raise CygnusError("the download does not match the vendor's published checksum")
        return path
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def discard_staging(staged: Path) -> None:
    folder = staged.parent
    if folder.name.startswith(STAGING_PREFIX):
        shutil.rmtree(folder, ignore_errors=True)


def _keep_previous(path: Path, backup: Path) -> None:
    """Make `backup` a complete copy of `path` without ever removing `path`: a hard link, or (on a
    filesystem without them) a copy that appears under its final name only when complete."""
    try:
        os.link(path, backup)
    except OSError:
        partial = backup.with_name(backup.name + ".partial")
        partial.unlink(missing_ok=True)
        shutil.copy2(path, partial)
        os.rename(partial, backup)


def _swap(params: dict) -> StepOutcome:
    """Put the staged build in place of the current file; the current file is kept as a backup.

    The path is never empty: the backup is made first (a link or a complete copy) and the new build then
    replaces the path in one atomic step, so a crash at any point leaves a usable application."""
    path, staged, backup = Path(params["path"]), Path(params["staged"]), Path(params["backup"])
    if backup.exists() and path.exists() and sha256_file(path) == params["new_sha256"]:
        return StepOutcome(result={"already": True}, compensation=Step(kind="appimage.unswap", params=params))
    if not path.exists() and backup.exists() and sha256_file(backup) == params["old_sha256"]:
        os.rename(backup, path)  # an older release stopped between its two renames: the file is in the backup
    if sha256_file(path) != params["old_sha256"]:
        raise CygnusError(f"{path} changed since the update was checked; not replacing it")
    if sha256_file(staged) != params["new_sha256"]:
        raise CygnusError("the downloaded build changed on disk; not installing it")
    if backup.exists():
        if sha256_file(backup) != params["old_sha256"]:
            raise CygnusError(f"{backup} already exists")
        backup.unlink()  # left by an earlier, interrupted try: the very same content
    os.chmod(staged, 0o755)
    _keep_previous(path, backup)
    try:
        os.replace(staged, path)
    except OSError:
        backup.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)
    return StepOutcome(result={"path": str(path), "backup": str(backup)},
                       compensation=Step(kind="appimage.unswap", params=params))


def _swap_abort(params: dict) -> StepOutcome:
    """Undo a swap the process died in: whatever state it left, the application file ends up as it was."""
    path, backup = Path(params["path"]), Path(params["backup"])
    if backup.exists():
        if sha256_file(backup) != params["old_sha256"]:
            raise CygnusError(f"{backup} changed; not restoring it")
        if not path.exists() or sha256_file(path) == params["new_sha256"]:
            os.replace(backup, path)  # the new build (or nothing) is in place: the previous one goes back
            fsync_dir(path.parent)
        elif sha256_file(path) == params["old_sha256"]:
            backup.unlink()  # nothing was replaced yet: the backup is just a second copy
        else:
            raise CygnusError(f"{path} changed; not touching it")
    backup.with_name(backup.name + ".partial").unlink(missing_ok=True)
    return StepOutcome(result={"aborted": True})


def _unswap(params: dict) -> StepOutcome:
    path, backup = Path(params["path"]), Path(params["backup"])
    if not backup.exists():
        return StepOutcome(result={"already": True})
    if sha256_file(backup) != params["old_sha256"]:
        raise CygnusError(f"{backup} changed; not restoring it")
    if path.exists():
        if sha256_file(path) != params["new_sha256"]:
            raise CygnusError(f"{path} changed; not replacing it with the previous version")
        path.unlink()
    os.rename(backup, path)
    fsync_dir(path.parent)
    return StepOutcome(result={"restored": str(path)})


def _update_record(registry: Registry, params: dict) -> StepOutcome:
    """Set version/source/artifacts of an installation; the compensation sets the previous values."""
    import json

    iid = params["installation_id"]
    row = registry.conn.execute("SELECT version, source, location_id FROM installation WHERE id=?", (iid,)).fetchone()
    if row is None:
        raise CygnusError("unknown installation")
    previous = {"installation_id": iid, "version": row[0], "source": json.loads(row[1]), "location_id": row[2],
                "artifacts": regdb.artifacts_of(registry, iid), "history": None}
    location_id = params.get("location_id", row[2])
    with registry.transaction() as c:  # all or nothing: a refused artifact must not leave a half-updated record
        c.execute("UPDATE installation SET version=?, source=?, location_id=? WHERE id=?",
                  (params["version"], json.dumps(params["source"]), location_id, iid))
        c.execute("DELETE FROM artifact WHERE installation_id=?", (iid,))
        for art in params["artifacts"]:
            regdb.add_artifact(registry, installation_id=iid, within=c,
                               **{k: art[k] for k in ("kind", "locator", "scope", "sha256", "ownership", "on_uninstall")
                                  if art.get(k) is not None})
        if params.get("history"):
            regdb.add_history(registry, iid, params.get("history_kind", "updated"), params["history"], within=c)
    return StepOutcome(result={"version": params["version"]},
                       compensation=Step(kind="registry.update", params=previous))


def plan_update(registry: Registry, installation_id: str, staged: Path, new: Candidate, *,
                location: StorageLocation | None) -> list[Step]:
    import json

    row = registry.conn.execute("SELECT application_id, version, source FROM installation WHERE id=?",
                                (installation_id,)).fetchone()
    if row is None:
        raise CygnusError("unknown installation")
    app_key, old_version, source = row[0], row[1], json.loads(row[2])
    path = Path(source["path"])
    new_sha = sha256_file(staged)
    steps = [Step(kind="appimage.swap", description="Replace the application file", params={
        "path": str(path), "staged": str(staged), "backup": str(path.with_name(f".{path.name}.cygnus-previous")),
        "old_sha256": source["sha256"], "new_sha256": new_sha})]
    old = regdb.artifacts_of(registry, installation_id)
    integ, artifacts, _ = integration_steps(new, app_key=app_key, payload_path=str(path), location=location,
                                            owned=frozenset(a["locator"] for a in old))
    steps += integ
    new_locators = {a["locator"] for a in artifacts}
    for art in old:
        if art["kind"] in ("file", "autostart"):
            continue  # the payload is replaced in place; adopted autostart entries already point at the launcher
        if art["locator"] not in new_locators:
            steps.append(Step(kind="fs.remove_owned", params={"path": art["locator"], "expect_sha256": art["sha256"]},
                              description=f"Remove the old {art['kind']}"))
    kept = [a for a in old if a["kind"] == "autostart"]
    kept += [{**a, "sha256": new_sha} for a in old if a["kind"] == "file"]
    steps.append(Step(kind="desktop.refresh", params={"rerun_after_rollback": True},
                      description="Refresh the application menu"))
    steps.append(Step(kind="registry.update", description="Record the new version", params={
        "installation_id": installation_id, "version": new.version,
        "source": {**source, "sha256": new_sha, "update_info": new.metadata.get("update_info")},
        "artifacts": artifacts + kept, "history": {"from": old_version, "to": new.version}}))
    steps.append(Step(kind="appimage.remove", description="Delete the previous version",
                      params={"path": steps[0].params["backup"], "expect_sha256": source["sha256"]}))
    return steps


# -- move and repair ----------------------------------------------------------------------------------------
def _installation(registry: Registry, installation_id: str) -> tuple[str, str | None, dict, str | None]:
    import json

    row = registry.conn.execute("SELECT application_id, version, source, location_id FROM installation WHERE id=?",
                                (installation_id,)).fetchone()
    if row is None:
        raise CygnusError("unknown installation")
    return row[0], row[1], json.loads(row[2]), row[3]


def _replace_integration(registry: Registry, installation_id: str, cand: Candidate, *, app_key: str,
                         payload_path: str, location: StorageLocation | None) -> tuple[list[Step], list[dict]]:
    """Integration files for the new state, plus removal of old ones the new state no longer has."""
    current = regdb.artifacts_of(registry, installation_id)
    integ, artifacts, _ = integration_steps(cand, app_key=app_key, payload_path=payload_path, location=location,
                                            owned=frozenset(a["locator"] for a in current))
    new_locators = {a["locator"] for a in artifacts}
    for art in regdb.artifacts_of(registry, installation_id):
        if art["kind"] not in ("file", "autostart") and art["locator"] not in new_locators:
            integ.append(Step(kind="fs.remove_owned", params={"path": art["locator"], "expect_sha256": art["sha256"]},
                              description=f"Remove the old {art['kind']}"))
    kept = [a for a in regdb.artifacts_of(registry, installation_id) if a["kind"] == "autostart"]
    return integ, artifacts + kept


def plan_move(registry: Registry, installation_id: str, cand: Candidate, *, location: StorageLocation,
              apps_dir: Path) -> list[Step]:
    """Copy to another location, point the launcher and menu entry there, then delete the old copy.
    Autostart entries already run the launcher, so they follow automatically."""
    app_key, version, source, _ = _installation(registry, installation_id)
    old = Path(source["path"])
    folder = apps_dir / re.sub(r"[^A-Za-z0-9 ._-]", "_", cand.name or app_key)[:80]
    dest = folder / old.name
    if dest == old:
        raise CygnusError("the application is already there")
    steps = [Step(kind="appimage.copy", params={"src": str(old), "dest": str(dest), "expect_sha256": source["sha256"],
                                                "existed": dest.exists()},
                  description=f"Copy to {location.label}")]
    integ, artifacts = _replace_integration(registry, installation_id, cand, app_key=app_key,
                                            payload_path=str(dest), location=location)
    steps += integ
    steps.append(Step(kind="desktop.refresh", params={"rerun_after_rollback": True},
                      description="Refresh the application menu"))
    steps.append(Step(kind="registry.update", description="Record the new place", params={
        "installation_id": installation_id, "version": version, "location_id": location.id,
        "source": {**source, "path": str(dest)},
        "artifacts": artifacts + [{"kind": "file", "locator": str(dest), "sha256": source["sha256"]}],
        "history": {"from": str(old), "to": str(dest)}, "history_kind": "moved"}))
    steps.append(Step(kind="appimage.remove", params={"path": str(old), "expect_sha256": source["sha256"]},
                      description="Delete the old copy"))
    return steps


def diagnose(registry: Registry, installation_id: str) -> list[dict[str, str]]:
    """What is wrong with what Cygnus set up (no changes): missing/changed integration files, payload state."""
    _, _, source, _ = _installation(registry, installation_id)
    problems = []
    path = Path(source["path"])
    try:
        if not path.is_file():
            problems.append({"what": "payload", "state": "missing", "path": str(path)})
        elif sha256_file(path) != source["sha256"]:
            problems.append({"what": "payload", "state": "changed", "path": str(path)})
    except OSError as exc:
        problems.append({"what": "payload", "state": "unreadable", "path": str(path), "detail": exc.strerror or ""})
    for art in regdb.artifacts_of(registry, installation_id):
        if art["kind"] in ("file", "autostart"):
            continue
        p = Path(art["locator"])
        if not p.exists():
            problems.append({"what": art["kind"], "state": "missing", "path": str(p)})
        elif art["sha256"] and integrate.sha256_bytes(p.read_bytes()) != art["sha256"]:
            problems.append({"what": art["kind"], "state": "changed", "path": str(p)})
    return problems


def plan_repair(registry: Registry, installation_id: str, cand: Candidate, *, location: StorageLocation | None,
                manifest=None) -> list[Step]:
    """Rewrite missing or modified integration files. A payload that changed but is still the same
    application (e.g. it updated itself) is accepted and recorded; anything else is refused. "The same
    application" is judged from the file's own identity, and a vendor-signed application must still
    carry the vendor's signature."""
    app_key, version, source, _ = _installation(registry, installation_id)
    problems = diagnose(registry, installation_id)
    payload = next((p for p in problems if p["what"] == "payload"), None)
    if payload and payload["state"] != "changed":
        raise CygnusError(f"the application file is {payload['state']}: {payload['path']}")
    new_source, new_version = source, version
    if payload:
        from cygnus.core.manifest import catalog

        own = catalog.find_for({**cand.identity, "name": cand.name or ""},
                               {manifest.manifest.application.id: manifest} if manifest else {})
        if app_key_for(cand, own.manifest.application.id if own else None) != app_key:
            raise CygnusError(f"{source['path']} is now a different application; not repairing it")
        digest = sha256_file(Path(source["path"]))
        if pinned_fingerprint(manifest):
            require_pinned_signature(Path(source["path"]), manifest)
            if sha256_file(Path(source["path"])) != digest:
                raise CygnusError(f"{source['path']} changed while it was being verified; try again")
        new_source = {**source, "sha256": digest,
                      "update_info": cand.metadata.get("update_info")}
        new_version = cand.version
    if not problems:
        return []
    integ, artifacts = _replace_integration(registry, installation_id, cand, app_key=app_key,
                                            payload_path=source["path"], location=location)
    return integ + [
        Step(kind="desktop.refresh", params={"rerun_after_rollback": True}, description="Refresh the application menu"),
        Step(kind="registry.update", description="Record the repair", params={
            "installation_id": installation_id, "version": new_version, "source": new_source,
            "artifacts": artifacts + [{**a, "sha256": new_source["sha256"]} for a in
                                      regdb.artifacts_of(registry, installation_id) if a["kind"] == "file"],
            "history": {"problems": problems}, "history_kind": "repaired"})]


def pinned_fingerprint(manifest) -> str | None:
    """The vendor key an authentic manifest pins. Expiry stops a manifest proposing actions, but a pin only
    ever makes Cygnus stricter, so it stays in force when the manifest is out of date."""
    if manifest is None or not manifest.authentic:
        return None
    return next((s.verification.openpgp_fingerprint for s in manifest.manifest.sources
                 if s.format == "appimage" and s.verification.openpgp_fingerprint), None)


def require_pinned_signature(path: Path, manifest) -> str | None:
    """If a trusted manifest pins the vendor's signing key, the file must carry a good signature by
    that key; otherwise Cygnus refuses to manage it under that application's name."""
    fpr = pinned_fingerprint(manifest)
    if fpr is None:
        return None
    from cygnus.core.backends import appimage as ab

    result = ab.verify_signature(Path(path), fpr)
    if result.status != "verified":
        raise CygnusError(f"{Path(path).name} is not signed by {manifest.manifest.application.vendor.name}'s key "
                          f"({result.status}); Cygnus will not manage it as {manifest.manifest.application.name}")
    return result.status
