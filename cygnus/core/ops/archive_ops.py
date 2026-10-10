"""Portable archive operations: unpack a program onto a drive, integrate it with the menu, record it, remove it. User scope only.

The unpacking is the careful part. The archive is read again here (not trusted to be what it was when it was looked at: its
checksum must still be the one the person approved), every entry is checked a second time with the same rules the reader used, each
file is created fresh (never through a link, never over anything), and everything is unpacked into a hidden folder next to the
final one and renamed into place only when it is complete and has been checked against what the archive said it holds.

Removing a program removes its folder, which the program itself may have written into since (Electron applications update
themselves in place). So the rule here is not "only files that are byte for byte what Cygnus wrote" (that would never remove an
updated program) but: the folder must be a real folder, must carry the marker file Cygnus put in it for this very installation,
and is removed only after the person asked for it, after being told how many files changed or were added since."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cygnus.core.desktop import integrate
from cygnus.core.detect import archive as ad
from cygnus.core.errors import CygnusError, UnsafeInputError
from cygnus.core.executor import Step, StepOutcome
from cygnus.core.models import Candidate
from cygnus.core.ops import appimage_ops, scripts
from cygnus.core.registry import db as regdb
from cygnus.core.registry.db import Registry, StorageLocation
from cygnus.core.util.fs import fsync_dir, rename_noreplace
from cygnus.core.util.limits import CappedReader, decompressor

MARKER = ad.RESERVED
MAX_MANIFEST_FILES = 50_000  # past this the marker keeps the counts only
MAX_MARKER_BYTES = 32 * 1024 * 1024
_SLACK_BYTES = 64 * 1024 * 1024


def app_key_for(cand: Candidate, manifest_app_id: str | None = None) -> str:
    base = manifest_app_id or (cand.identity.get("desktop_id") or cand.name or "app").removesuffix(".desktop")
    key = re.sub(r"[^A-Za-z0-9._-]", "-", base).strip(".-")
    if not manifest_app_id:
        key = "archive." + key
    key = key[:100].strip(".-")
    if not key:
        raise CygnusError("cannot derive an application key")
    return key


def folder_name(name: str) -> str:
    """The name of the program's own folder inside the applications folder: what the program is called, made safe to be a name."""
    safe = re.sub(r"[^A-Za-z0-9 ._()+-]", "_", name).strip(" .")[:80]
    return safe or "Application"


# -- the marker -----------------------------------------------------------------------------------------------------------------------
def read_marker(folder: Path) -> dict[str, Any] | None:
    path = folder / MARKER
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MARKER_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def tree_changes(folder: Path, marker: dict[str, Any] | None) -> dict[str, Any]:
    """What is in the program's folder now, against what was unpacked: {"files", "bytes", "changed", "added", "removed"} (the last
    three are None when the marker holds no per-file record, for a very big program)."""
    known = (marker or {}).get("manifest")
    seen: set[str] = set()
    files = total = changed = added = 0
    pending = [folder]
    while pending:
        here = pending.pop()
        try:
            with os.scandir(here) as it:
                for entry in it:
                    rel = os.path.relpath(entry.path, folder)
                    if entry.is_symlink():
                        files += 1
                        seen.add(rel)
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                    if rel == MARKER:
                        continue
                    st = entry.stat(follow_symlinks=False)
                    files += 1
                    total += st.st_size
                    seen.add(rel)
                    if isinstance(known, dict):
                        was = known.get(rel)
                        if was is None:
                            added += 1
                        elif was[0] != st.st_size or abs(was[1] - st.st_mtime_ns) > 2_000_000_000:  # two seconds: some drives round times
                            changed += 1
        except OSError:
            continue
    removed = len([k for k in known if k not in seen]) if isinstance(known, dict) else None
    return {"files": files, "bytes": total, "changed": changed if known is not None else None,
            "added": added if known is not None else None, "removed": removed}


# -- unpacking ------------------------------------------------------------------------------------------------------------------------
def _refuse(message: str) -> CygnusError:
    return CygnusError(f"the archive is not unpacked: {message}")


class _Hashing:
    """A file whose every byte read is hashed: what is unpacked is then exactly what was hashed, whatever happens to the file
    meanwhile (the approval named a checksum; this is what makes the unpacked bytes be those)."""

    def __init__(self, raw) -> None:
        self.raw, self.digest = raw, hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.raw.read(size)
        self.digest.update(data)
        return data

    def readable(self) -> bool:
        return True

    def rest(self) -> None:
        while self.read(1 << 20):
            pass


def _inside(parent: Path, base: str, known: set[str]) -> bool:
    """Is the folder `parent` really inside the unpacked folder (links followed)? Remembered, so a folder is looked at once."""
    key = str(parent)
    if key in known:
        return True
    real = os.path.realpath(parent)
    if real == base or real.startswith(base + os.sep):
        known.add(key)
        return True
    return False


def unpack(src, compression: str, staging: Path, *, root: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Unpack the archive (a path, or a file already open) into the empty folder `staging`, with its one top folder `root`, if it has
    one, left out of the path. Every entry is checked again; anything not a plain file, folder or inside link is refused, nothing is
    written outside, and with `expected_sha256` the bytes read must be exactly that file."""
    manifest: dict[str, list[int]] = {}
    files = links = total = 0
    base = os.path.realpath(staging)
    tree = ad.PathTree()
    known: set[str] = set()
    opened = None
    try:
        raw = open(src, "rb") if isinstance(src, (str, os.PathLike)) else src
        opened = raw if raw is not src else None
        hashed = _Hashing(raw)
        capped = CappedReader(decompressor(hashed, compression), ad.MAX_BYTES + ad._STREAM_SLACK)
        with ad.open_stream(capped) as tar:
            for member in tar:
                name = ad.normal_name(member.name)
                if name is None:
                    raise _refuse(f"an entry has a path that leaves the folder ({ad.shown(member.name)})")
                if name == "" or name == root:
                    continue
                if root:
                    if not name.startswith(root + "/"):
                        raise _refuse(f"an entry is outside the archive's folder ({ad.shown(name)})")
                    rel = name[len(root) + 1:]
                else:
                    rel = name
                too_long = ad.name_problem(rel)
                if too_long:
                    raise _refuse(f"{too_long} ({ad.shown(rel[:40])}…)")
                if member.isdir():
                    kind = "d"
                elif member.isreg():
                    kind = "f"
                elif member.issym():
                    kind = "l"
                else:
                    raise _refuse(f"{ad.shown(rel)} is a link, device or pipe of a kind Cygnus does not unpack")
                refused = tree.add(rel, kind, member.linkname if kind == "l" else "")
                if refused or rel == MARKER:
                    raise _refuse(f"the path {ad.shown(rel)} is not unique, or is inside a file or written through a link")
                target = staging.joinpath(*rel.split("/"))
                if links and not _inside(target.parent, base, known):  # once there is a link, a folder is only used if it is really inside
                    raise _refuse(f"{ad.shown(rel)} would be written outside the program's folder")
                if kind == "d":
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(target.parent, exist_ok=True)
                if links and not _inside(target.parent, base, known):
                    raise _refuse(f"{ad.shown(rel)} would be written outside the program's folder")
                if kind == "l":
                    if ad._link_escapes(rel, member.linkname):
                        raise _refuse(f"the link {ad.shown(rel)} leads outside its folder")
                    try:
                        os.symlink(member.linkname, target)
                    except OSError as exc:
                        raise CygnusError("this drive cannot hold the links the archive contains "
                                          f"({exc.strerror or exc}): choose another place") from exc
                    links += 1
                    continue
                total += member.size
                if total > ad.MAX_BYTES:
                    raise _refuse("it holds more than the size Cygnus allows")
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as out:
                    handle = tar.extractfile(member)
                    if handle is not None:
                        shutil.copyfileobj(handle, out, 1 << 20)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)  # never setuid, setgid, sticky or writable by others
                try:
                    os.utime(target, (max(0, member.mtime), max(0, member.mtime)))
                except (OSError, OverflowError, ValueError):
                    pass
                files += 1
                if files <= MAX_MANIFEST_FILES:
                    manifest[rel] = [member.size, os.stat(target).st_mtime_ns]
        hashed.rest()  # the end of the file too: the hash is of all of it
        if expected_sha256 is not None and hashed.digest.hexdigest() != expected_sha256:
            raise _refuse("the file changed while it was being unpacked, so it is not what you approved: open it again")
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise CygnusError("the drive is full: nothing was installed") from exc
        raise CygnusError(f"the archive could not be unpacked: {exc.strerror or exc}") from exc
    except ad.HeaderTooBig as exc:
        raise _refuse("an entry's header is far bigger than a real one") from exc
    except UnsafeInputError as exc:
        raise _refuse("it unpacks to more than the size Cygnus allows") from exc
    except CygnusError:
        raise
    except Exception as exc:  # noqa: BLE001 - tarfile and the decompressors raise many types on damaged input
        raise CygnusError(f"the archive is damaged and cannot be unpacked ({type(exc).__name__})") from exc
    finally:
        if opened is not None:
            opened.close()
    _normalise_and_check(staging, base)
    return {"files": files, "links": links, "bytes": total, "manifest": manifest if files <= MAX_MANIFEST_FILES else None}


def _normalise_and_check(staging: Path, base: str) -> None:
    """Every folder enterable, and every link checked on what it really leads to now that the whole tree is there."""
    pending = [staging]
    while pending:
        here = pending.pop()
        os.chmod(here, 0o755)
        with os.scandir(here) as it:
            for entry in it:
                if entry.is_symlink():
                    real = os.path.realpath(entry.path)
                    if real != base and not real.startswith(base + os.sep):
                        raise _refuse(f"the link {os.path.relpath(entry.path, staging)} leads outside the program's folder")
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


def _mounts_inside(folder: Path) -> list[str]:
    """Mount points at or below `folder` (a drive, or a bind mount of someone's own folder, put inside it): deleting the folder would
    delete what is mounted there, not just the mount point."""
    from cygnus.core.storage import mountinfo

    base = os.path.realpath(folder).rstrip("/")
    try:
        mounts = mountinfo.read()
    except (OSError, ValueError):
        return []
    return sorted({m.mountpoint for m in mounts if m.mountpoint == base or m.mountpoint.startswith(base + "/")})


def _other_devices_inside(folder: Path) -> list[str]:
    root_dev = os.lstat(folder).st_dev
    found: list[str] = []
    pending = [folder]
    while pending and len(found) < 3:
        here = pending.pop()
        try:
            with os.scandir(here) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.stat(follow_symlinks=False).st_dev != root_dev:
                            found.append(entry.path)
                        else:
                            pending.append(Path(entry.path))
        except OSError:
            continue
    return found


def _delete_tree(root: Path) -> None:
    """Delete everything in `root` and then `root`. Never into another drive or a link. Files and folders the program made read-only
    are made deletable first (they are the person's own). The marker goes LAST, so that a removal that stops half way leaves a folder
    Cygnus still recognises as its own and can finish later."""
    marker = root / MARKER
    shown_path = lambda p: os.path.relpath(p, root)  # noqa: E731
    try:
        dirs: list[Path] = []
        pending = [root]
        while pending:
            here = pending.pop()
            dirs.append(here)
            if not os.access(here, os.R_OK | os.W_OK | os.X_OK):
                os.chmod(here, 0o700)
            with os.scandir(here) as it:
                entries = list(it)
            for entry in entries:
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif path != marker:
                    os.unlink(path)
        for here in reversed(dirs[1:]):  # children before their parents
            os.rmdir(here)
        os.unlink(marker)
        os.rmdir(root)
    except OSError as exc:
        where = shown_path(exc.filename) if isinstance(exc.filename, str) and exc.filename.startswith(str(root)) else "its files"
        raise CygnusError(f"the program's folder could not be removed completely ({where}: {exc.strerror or exc}). It is left partly "
                          "removed, and Cygnus still recognises it: fix that and remove the program again.") from exc


def free_space_problem(parent: Path, needed: int) -> str | None:
    """Words for "it will not fit" when the drive has too little room (the folder may not exist yet: its parent is asked)."""
    probe = parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return None
    if free < needed + _SLACK_BYTES:
        return f"not enough room: it needs about {needed // 2**20 + 64} MB and only {free // 2**20} MB are free"
    return None


# -- step handlers --------------------------------------------------------------------------------------------------------------------
def _staging_for(dest: Path, iid: str) -> Path:
    return dest.parent / f".{dest.name}.cygnus-{iid[:8]}.part"


def _removal(dest: Path, iid: str, created: bool = True) -> Step:
    return Step(kind="archive.remove_tree", params={"path": str(dest), "installation_id": iid, "created": created})


def _extract(params: dict) -> StepOutcome:
    src, dest, iid = Path(params["src"]), Path(params["dest"]), params["installation_id"]
    if dest.is_symlink() or dest.exists():
        marker = read_marker(dest)
        if marker and marker.get("installation") == iid:  # an earlier run of this very step finished it
            return StepOutcome(result={"path": str(dest), "already_present": True}, compensation=_removal(dest, iid))
        if marker and marker.get("installation"):
            raise CygnusError(f"{dest} is left over from an earlier installation of this program (its files were kept when it was "
                              "removed): delete or rename it, then install again")
        raise CygnusError(f"{dest} already exists, and Cygnus did not make it")
    if appimage_ops.sha256_file(src) != params["expect_sha256"]:
        raise CygnusError("the archive changed after Cygnus looked at it, so what it found no longer applies: open it again")
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    problem = free_space_problem(parent, int(params.get("expect_bytes") or 0))
    if problem:
        raise CygnusError(f"the program cannot be unpacked there: {problem}")
    staging = _staging_for(dest, iid)
    if staging.is_symlink() or staging.is_file():
        staging.unlink(missing_ok=True)  # a leftover of ours with the wrong shape: only the name goes, never what a link points at
    elif staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)  # a leftover of a run that died: it is named for this very installation
    os.mkdir(staging, 0o755)
    try:
        info = unpack(src, params["compression"], staging, root=params.get("root") or "", expected_sha256=params["expect_sha256"])
        if params.get("expect_files") is not None and info["files"] + info["links"] != params["expect_files"]:
            raise _refuse("what is in it is not what Cygnus showed (the number of files differs)")
        marker = {"installation": iid, "archive_sha256": params["expect_sha256"], "program": params.get("main"),
                  "files": info["files"], "links": info["links"], "bytes": info["bytes"], "manifest": info["manifest"],
                  "unpacked_at": datetime.now(UTC).isoformat()}
        (staging / MARKER).write_text(json.dumps(marker), encoding="utf-8")
        main = params.get("main")
        if main and not (staging / main).is_file():
            raise _refuse(f"the program to start ({main}) is not in it")
        try:
            rename_noreplace(staging, dest)  # never onto something that appeared meanwhile, not even an empty folder
        except FileExistsError:
            raise CygnusError(f"{dest} appeared while the program was being unpacked; it was not touched") from None
        fsync_dir(parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return StepOutcome(result={"path": str(dest), "files": info["files"], "bytes": info["bytes"]}, compensation=_removal(dest, iid))


def _extract_abort(params: dict) -> StepOutcome:
    """The process died while unpacking: the half-made folder, and the finished one if it got that far, are removed."""
    dest, iid = Path(params["dest"]), params["installation_id"]
    staging = _staging_for(dest, iid)
    if staging.is_dir() and not staging.is_symlink():
        shutil.rmtree(staging, ignore_errors=True)
    marker = read_marker(dest) if dest.is_dir() and not dest.is_symlink() else None
    if marker and marker.get("installation") == iid and not _mounts_inside(dest) and not _other_devices_inside(dest):
        shutil.rmtree(dest, ignore_errors=True)
    return StepOutcome(result={"aborted": str(dest)})


def _remove_tree(params: dict) -> StepOutcome:
    path, iid = Path(params["path"]), params["installation_id"]
    if not (path.exists() or path.is_symlink()):
        return StepOutcome(result={"removed": False})
    if path.is_symlink() or not path.is_dir():
        raise CygnusError(f"{path} is not a plain folder; not removing it")
    marker = read_marker(path)
    if marker is None or marker.get("installation") != iid:
        raise CygnusError(f"{path} was not made by this installation (its marker is missing or belongs to another); not removing it")
    if path == Path(path.anchor) or path == Path.home() or len(path.parts) < 3:
        raise CygnusError(f"{path} is not a place Cygnus will remove")
    inside = _mounts_inside(path) or _other_devices_inside(path)
    if inside:
        raise CygnusError(f"something else is mounted inside the program's folder ({inside[0]}): removing the folder would delete "
                          "what is mounted there too. Unmount it first; nothing was deleted.")
    _delete_tree(path)
    return StepOutcome(result={"removed": True})


def _run_script(params: dict) -> StepOutcome:
    """A script the person chose to run, as themselves. The program's folder must be the one Cygnus made for this installation, and
    the script the one whose checksum was shown. It never fails the operation: the answer says what happened, because a script that
    goes wrong must not undo an installation that worked (and a step that raised would roll it back)."""
    dest, iid = Path(params["dest"]), params["installation_id"]
    result: dict[str, Any] = {"script": params["path"], "sha256": params["sha256"], "as": "user", "ran": False, "ok": False, "exit": None,
                              "timed_out": False, "output": "", "error": ""}
    if dest.is_symlink() or not dest.is_dir():
        result["error"] = f"the program's folder ({dest}) is not there"
        return StepOutcome(result=result)
    marker = read_marker(dest)
    if not marker or marker.get("installation") != iid:
        result["error"] = "that folder is not the one Cygnus made for this program, so nothing in it was run"
        return StepOutcome(result=result)
    return StepOutcome(result=scripts.claim_and_run(root=dest, rel=params["path"], sha=params["sha256"], run_id=params["run_id"],
                                                    timeout=float(params.get("timeout") or scripts.scriptrun.DEFAULT_TIMEOUT)))


HANDLERS = {"archive.extract": _extract, "archive.extract.abort": _extract_abort, "archive.remove_tree": _remove_tree,
            "archive.run_script": _run_script}


def script_steps(dest: Path, installation_id: str, chosen: list[dict[str, str]]) -> list[Step]:
    """One step for each script the person chose ({"name", "sha256"}), in the order given."""
    return [Step(kind="archive.run_script", description=f"Run the script {c['name'].rsplit('/', 1)[-1]}",
                 params={"dest": str(dest), "installation_id": installation_id, "path": c["name"], "sha256": c["sha256"],
                         "run_id": scripts.new_run_id(), "timeout": scripts.scriptrun.DEFAULT_TIMEOUT}) for c in chosen]


def chosen_scripts(known: dict[str, str], chosen: list[dict[str, Any]] | None, phase: str) -> list[dict[str, str]]:
    """The scripts asked for, each checked against what the archive was shown to hold ({name: sha256}): a script that is not
    there, or is not the one shown, or belongs to the other phase, is refused (nothing is run by a mistake of a name)."""
    out: list[dict[str, str]] = []
    for c in chosen or []:
        name, sha = (c.get("name"), c.get("sha256")) if isinstance(c, dict) else (None, None)
        if not isinstance(name, str) or not isinstance(sha, str) or known.get(name) != sha:
            raise CygnusError(f"{str(name)[:60]!r} is not a script of this program, or it is not the one that was shown")
        if scripts.phase_of(name) != phase:
            raise CygnusError(f"{name} is a script for {'removing' if phase == 'install' else 'installing'} the program, not for this step")
        if all(o["name"] != name for o in out):
            out.append({"name": name, "sha256": sha})
    return out


def executor(registry: Registry):
    ex = appimage_ops.Ops(registry).executor()
    for kind, handler in HANDLERS.items():
        ex.register(kind, handler)
    return ex


# -- plans ----------------------------------------------------------------------------------------------------------------------------
def integration(cand: Candidate, *, app_key: str, program: Path, location: StorageLocation | None) -> tuple[list[Step], list[dict[str, Any]], list[str]]:
    """The menu entry, launcher and icon, as steps and as the artifacts to record. The entry that came with the archive is the
    model (its Exec names a program by a system path such as /usr/bin/discord: it is pointed at the program in the folder)."""
    meta = cand.metadata
    icon = meta.get("icon") or {}
    icon_bytes = base64.b64decode(icon["base64"]) if icon.get("base64") else None
    upstream = dict(meta.get("desktop_entry") or {"Name": cand.name or app_key, "Categories": "Utility;",
                                                  **({"Comment": cand.summary} if cand.summary else {})})
    wm_class = upstream.get("StartupWMClass")
    if wm_class and integrate.desktop_id_taken(f"{wm_class}.desktop"):
        del upstream["StartupWMClass"]  # that would group this program's windows under another application's menu entry
    if not icon_bytes:
        upstream.pop("Icon", None)  # no icon came with it: a bare name could show another application's icon
    plan = integrate.plan_appimage_integration(
        app_key=app_key, display_name=cand.name or app_key, appimage_path=str(program),
        fs_uuid=location.fs_uuid if location and location.location_class != "system" else None,
        location_label=location.label if location else "this computer", upstream_desktop=upstream,
        upstream_actions=meta.get("desktop_action_groups"), upstream_desktop_id=cand.identity.get("desktop_id"),
        icon=icon_bytes, icon_kind=icon.get("kind"))
    steps, artifacts = [], []
    for f in plan.files:
        if f.kind == "icon" and (f.path.exists() or f.path.is_symlink()):
            continue  # the menu entry names the icon; the file that is there provides it
        steps.append(Step(kind="fs.write_owned", params={"path": str(f.path), "data_hex": f.data.hex(), "mode": f.mode,
                                                         "existed": f.path.exists()}, description=f"Write {f.kind}"))
        artifacts.append({"kind": f.kind, "locator": str(f.path), "sha256": f.sha256, "ownership": "created", "on_uninstall": "remove"})
    return steps, artifacts, plan.notes


def plan_install(cand: Candidate, *, app_key: str, location: StorageLocation, apps_dir: Path, main: str,
                 sha256: str | None = None, run_scripts: list[dict[str, Any]] | None = None) -> list[Step]:
    """Unpack the archive into <applications folder>/<program name>, integrate it and record it. `run_scripts` ({"name", "sha256"}
    for each install script the person ticked, to be run as themselves) come last, after the program is recorded: whatever a
    script does or fails to do leaves the installation as it is."""
    meta = cand.metadata
    known_scripts = dict(meta.get("script_sha256") or {})
    chosen = chosen_scripts(known_scripts, run_scripts, "install")
    program = (meta.get("executables") or {}).get(main)
    if program is None or program.get("kind") not in ("elf", "script") or ad.INSTALL_SCRIPT.match(main.rsplit("/", 1)[-1]):
        raise CygnusError(f"{main!r} is not a program in this archive that can be started")
    src = Path(cand.source)
    digest = appimage_ops.sha256_file(src)
    if sha256 and digest != sha256:
        raise CygnusError("the archive changed after Cygnus showed it to you: open it again")
    dest = apps_dir / folder_name(cand.name or app_key)
    iid = str(uuid.uuid4())
    steps = [Step(kind="archive.extract", description=f"Unpack to {location.label}", params={
        "src": str(src), "dest": str(dest), "expect_sha256": digest, "installation_id": iid, "root": meta.get("root") or "",
        "compression": "" if meta.get("compression") in (None, "none") else meta["compression"], "main": main,
        "expect_files": (meta.get("file_count") or 0) + (meta.get("link_count") or 0),
        "expect_bytes": meta.get("total_bytes") or 0})]
    integ, artifacts, _notes = integration(cand, app_key=app_key, program=dest / main, location=location)
    steps += integ
    steps.append(Step(kind="desktop.refresh", params={"rerun_after_rollback": True}, description="Refresh the application menu"))
    steps.append(Step(kind="registry.record", description="Record the application", params={
        "installation_id": iid, "format": "archive", "app_id": app_key, "name": cand.name or app_key, "vendor": None,
        "trust": "unverified", "origin": "installed",
        "source": {"path": str(dest), "main": main, "archive_name": src.name, "archive_sha256": digest,
                   "installed_at": datetime.now(UTC).isoformat(), "update_info": None,
                   "scripts": [{"name": n, "sha256": h, "phase": scripts.phase_of(n)} for n, h in sorted(known_scripts.items())]},
        "version": cand.version, "location_id": location.id, "update_provider": {"type": "none"},
        "artifacts": artifacts + [{"kind": "tree", "locator": str(dest), "sha256": None}]}))
    return steps + script_steps(dest, iid, chosen)


def recorded_scripts(source: dict[str, Any]) -> dict[str, str]:
    """{name: sha256} of the scripts the program's archive held, as recorded when it was installed."""
    return {e["name"]: e["sha256"] for e in source.get("scripts") or [] if isinstance(e, dict) and isinstance(e.get("name"), str)
            and isinstance(e.get("sha256"), str)}


def plan_uninstall(registry: Registry, installation_id: str, *, remove_payload: bool,
                   run_scripts: list[dict[str, Any]] | None = None) -> list[Step]:
    """Remove the menu entry, launcher and icon and forget the program; the folder goes last, and only when asked. `run_scripts` are
    the removal scripts (prerm, postrm, uninstall) the person ticked: they run first, while the folder they live in is still there."""
    row = registry.conn.execute("SELECT source FROM installation WHERE id=?", (installation_id,)).fetchone()
    source = json.loads(row[0]) if row else {}
    chosen = chosen_scripts(recorded_scripts(source), run_scripts, "remove")
    steps = (script_steps(Path(source.get("path") or ""), installation_id, chosen) if chosen else [])
    steps += appimage_ops.plan_uninstall(registry, installation_id, remove_payload=False)
    if remove_payload and source.get("path"):
        steps.append(Step(kind="archive.remove_tree", description="Delete the program's folder",
                          params={"path": source["path"], "installation_id": installation_id, "created": False}))
    return steps


def info(registry: Registry, installation_id: str) -> dict[str, Any]:
    """What removing the program's folder would remove, for the question put to the person."""
    row = registry.conn.execute("SELECT source FROM installation WHERE id=?", (installation_id,)).fetchone()
    source = json.loads(row[0]) if row else {}
    folder = Path(source.get("path") or "")
    if not source.get("path") or folder.is_symlink() or not folder.is_dir():
        return {"path": source.get("path", ""), "present": False}
    marker = read_marker(folder)
    out = {"path": str(folder), "present": True, "ours": bool(marker and marker.get("installation") == installation_id)}
    out.update(tree_changes(folder, marker))
    return out
