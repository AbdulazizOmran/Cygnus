"""Flatpak operations: relocating the user installation and running libflatpak transactions.

Relocation (architecture §7.2): only repo/, app/, runtime/ and .removed/ of the *user* installation
move to the chosen drive (they must share one filesystem: checkouts hardlink into the repository, and
uninstalling renames a deployment into .removed/);
db/ (portal permissions), overrides/ and exports/ stay on the system drive. No root process ever
touches the relocated directories.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cygnus.core import progress as progress_mod
from cygnus.core.backends.flatpak import user_dir
from cygnus.core.errors import CygnusError
from cygnus.core.executor import Step, StepOutcome
from cygnus.core.recovery.model import Issue, IssueSeverity, Resolution, SafetyClass, explain_only
from cygnus.core.util.fs import atomic_write

# .removed is where Flatpak renames a deployment when uninstalling or updating; a rename cannot cross
# filesystems, so it must live on the same drive as app/ and runtime/.
RELOCATED = ("repo", "app", "runtime", ".removed")




@dataclass(slots=True, kw_only=True)
class RelocationState:
    base: Path
    entries: dict[str, str] = field(default_factory=dict)  # name -> "absent" | "dir" | "symlink:<target>"

    @property
    def relocated_to(self) -> Path | None:
        targets = {v.split(":", 1)[1] for v in self.entries.values() if v.startswith("symlink:")}
        if len(targets) == 0:
            return None
        parents = {str(Path(t).parent) for t in targets}
        return Path(parents.pop()) if len(parents) == 1 else None


def relocation_state(base: Path | None = None) -> RelocationState:
    base = base or user_dir()
    st = RelocationState(base=base)
    for name in RELOCATED:
        p = base / name
        if p.is_symlink():
            st.entries[name] = "symlink:" + os.readlink(p)
        elif p.is_dir():
            st.entries[name] = "dir"
        else:
            st.entries[name] = "absent"
    return st


def ensure_relocation_links(base: Path | None = None, *, dry_run: bool = False) -> list[str]:
    """Put back links of a relocated user installation that something removed. `flatpak repair`, for
    one, erases .removed/ and Flatpak then recreates it as a folder on the system drive, where the next
    uninstall or update fails with "Invalid cross-device link". Returns the names that were fixed (with
    `dry_run`, the names that need fixing, and nothing is changed); does nothing while the drive is not
    connected."""
    base = base or user_dir()
    state = relocation_state(base)
    root = state.relocated_to
    if root is None or not root.is_dir():
        return []
    fixed = []
    for name in RELOCATED:
        link, target = base / name, root / name
        entry = state.entries[name]
        if entry == f"symlink:{target}":
            continue
        if entry.startswith("symlink:"):
            raise CygnusError(f"{link} points to {entry.split(':', 1)[1]}, not to {target}")
        if entry == "dir" and name != ".removed" and any(link.iterdir()):
            raise CygnusError(f"{link} is a folder on the system drive, but should link to {target}")
        if dry_run:
            fixed.append(name)
            continue
        if entry == "dir":
            if name == ".removed":
                shutil.rmtree(link)  # Flatpak's trash of already-uninstalled deployments
            else:
                link.rmdir()
        target.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)
        fixed.append(name)
    return fixed


def plan_relocation(target_root: Path, base: Path | None = None, *, fs_uuid: str | None = None) -> list[Step]:
    """One step that moves repo/, app/, runtime/, .removed/ under `target_root`
    (e.g. /mnt/data/.cygnus-flatpak-user). With `fs_uuid`, the step refuses to run unless the target
    is on that filesystem (an unmounted drive leaves an empty folder on the system drive behind)."""
    base = base or user_dir()
    state = relocation_state(base)
    if state.relocated_to and state.relocated_to != target_root:
        raise CygnusError(f"the Flatpak user installation is already stored at {state.relocated_to}")
    names = [n for n in RELOCATED if not state.entries[n].startswith("symlink:")]
    if not names:
        return []
    params = {"base": str(base), "target_root": str(target_root), "names": names}
    if fs_uuid:
        params["fs_uuid"] = fs_uuid
    return [Step(kind="flatpak.relocate", params=params,
                 description="Move Flatpak's application storage to the chosen drive")]


def filesystem_uuid(path: Path) -> str | None:
    """The UUID of the filesystem that actually holds `path` (from the mount table)."""
    from cygnus.core.storage import locations

    cand, _ = locations.candidate_for_path(os.path.realpath(path))
    return cand.fs_uuid


def _require_filesystem(root: Path, fs_uuid: str | None) -> None:
    if not root.parent.is_dir():
        raise CygnusError(f"{root.parent} is not available (is its drive connected?)")
    if root.is_symlink():  # e.g. a link to a folder on the system drive: the data would not reach the drive
        raise CygnusError(f"{root} is a link; Cygnus will not store Flatpak's data behind a link")
    if fs_uuid is None:
        return
    try:
        actual = filesystem_uuid(root if root.exists() else root.parent)
    except CygnusError as exc:
        raise CygnusError(f"{root.parent} is not on the chosen drive: {exc}") from exc
    if actual != fs_uuid:
        raise CygnusError(f"{root.parent} is not on the chosen drive (is it connected?); Flatpak's storage is "
                          "not moved")


RELOCATION_MARGIN = 512 * 1024**2


def _du_bytes(paths: list[Path]) -> int:
    """Disk usage counting every hardlinked file once (one `du` over all paths)."""
    from cygnus.core.util import proc

    if not paths:
        return 0
    res = proc.run(["du", "-s", "-c", "--block-size=1", "-x", *map(str, paths)], timeout=3600)
    if res.returncode != 0:
        raise CygnusError(f"cannot measure the Flatpak storage: {res.stderr.strip()[:200]}")
    return int(res.stdout.strip().splitlines()[-1].split()[0])


def _copy_together(sources: list[Path], dest_dir: Path) -> None:
    """Copy folders in ONE `cp -a`, so files hardlinked between them (repository objects and the
    deployments checked out from them) stay hardlinked instead of taking twice the space."""
    from cygnus.core.util import proc

    res = proc.run(["cp", "-a", "--reflink=auto", "-t", str(dest_dir), "--", *map(str, sources)], timeout=6 * 3600)
    if res.returncode != 0:
        raise CygnusError(f"copying failed: {res.stderr.strip()[:300]}")


PARTIAL = ".cygnus-partial"  # copies land here and move into place only when complete
COPIED = ".cygnus-copied"  # beside each complete copy: the fingerprint of the folder it was made from


def _backup(base: Path, name: str) -> Path:
    return base / f".{name}.cygnus-before-move"


def tree_fingerprint(path: Path) -> str:
    """What a folder tree looks like right now: every entry's path, type, size, link count and change
    times. Anything Flatpak writes (a new file, a removal, a new hardlink, a rewrite) changes it, so a
    copy is only trusted if the source still has the fingerprint it was copied with."""
    h = hashlib.sha256()
    stack = [path]
    while stack:
        folder = stack.pop()
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            st = e.stat(follow_symlinks=False)
            rel = os.path.relpath(e.path, path)
            h.update(f"{rel}\0{st.st_mode}\0{st.st_size}\0{st.st_nlink}\0{st.st_mtime_ns}\0{st.st_ctime_ns}\n"
                     .encode("utf-8", "surrogateescape"))
            if e.is_dir(follow_symlinks=False):
                stack.append(Path(e.path))
    return h.hexdigest()


def _mark(where: Path, name: str, fingerprint: str | None) -> None:
    marks = where / COPIED
    if fingerprint is None:
        (marks / name).unlink(missing_ok=True)
        return
    marks.mkdir(exist_ok=True)
    atomic_write(marks / name, fingerprint.encode())


def _marked(where: Path, name: str) -> str | None:
    try:
        return (where / COPIED / name).read_text().strip()
    except OSError:
        return None


def _mark_drive(where: Path, name: str, fingerprint: str | None) -> None:
    """The fingerprint of a drive copy at the moment it went live (before its link existed). If it
    differs later, something wrote into the drive's folder through the link."""
    _mark(where, f"{name}.drive", fingerprint)


def _drive_unchanged(where: Path, name: str) -> bool:
    mark = _marked(where, f"{name}.drive")
    return mark is not None and tree_fingerprint(where / name) == mark


_CHANGED = ("Flatpak's storage changed while Cygnus was copying it (was something being installed or updated?). "
            "Nothing was moved; try again when no Flatpak operation is running.")


def _remove_if_empty(*folders: Path) -> None:
    """rmdir each folder in order; one that still holds anything (or is gone) is left alone."""
    for folder in folders:
        try:
            folder.rmdir()
        except OSError:
            pass


def _trash(base: Path, name: str) -> Path:
    return base / f".{name}.cygnus-trash"  # a backup being deleted: recognisable, never mistaken for a backup


def _switch(base: Path, root: Path, n: str) -> None:
    """Make base/n a link to its copy on the drive. The slow part comes first (the drive copy's fingerprint, the
    baseline for telling later whether it was written to); then the folder is checked, renamed and linked back to
    back, so that nothing can change it or recreate base/n in between for longer than an instant."""
    link, backup = base / n, _backup(base, n)
    if link.is_symlink():
        return  # live already (an earlier run): nothing to do here, and no baseline is ever recorded for a live link
    mark = _marked(root, n)
    if link.is_dir():
        _mark_drive(root, n, tree_fingerprint(root / n))  # what the drive copy looks like as it goes live (nothing
        # can write to it yet: it is not linked)
        if mark is not None and tree_fingerprint(link) != mark:
            raise CygnusError(_CHANGED)  # written to while it was copied: the copy is not complete
        os.rename(link, backup)
    elif backup.exists() and not link.exists():  # an interrupted earlier run stopped between rename and link
        if mark is not None and tree_fingerprint(backup) != mark:
            raise CygnusError(_CHANGED)
        if _marked(root, f"{n}.drive") is None:  # not live yet: nothing can have been written to the copy
            _mark_drive(root, n, tree_fingerprint(root / n))
    else:  # there was no folder at all (e.g. an empty trash folder): just the link
        (root / n).mkdir(exist_ok=True)
    os.symlink(root / n, link)


def _relocate(params: dict) -> StepOutcome:
    """Move the folders onto the drive. Resumable: every intermediate state (partial copy, copy done
    but not switched, switched but backup not removed) is recognised and carried on from. A copy is
    used only if the folder still looks exactly as it did when it was copied.

    Once a link is live, the drive's folder is the only place where new data lands, so nothing is ever deleted
    on the strength of an older copy: a backup goes only after it has been verified complete."""
    base, root = Path(params["base"]), Path(params["target_root"])
    names = list(params["names"])
    for n in names:
        if (base / n).is_symlink() and os.readlink(base / n) != str(root / n):
            raise CygnusError(f"{base / n} already points elsewhere")
        if _backup(base, n).exists() and (base / n).is_dir() and not (base / n).is_symlink():
            raise CygnusError(f"{base / n} was created again while moving it was interrupted; the original is in "
                              f"{_backup(base, n)}. Please check which one to keep.")
    _require_filesystem(root, params.get("fs_uuid"))
    base.mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.rmtree(_trash(base, n), ignore_errors=True)  # what a crash left of a backup that was being deleted
    if "repo" in names and not (base / "repo").exists() and not _backup(base, "repo").exists():
        installation_for("user", base).list_remotes(None)  # Flatpak creates its repository, then it is moved
    root.mkdir(exist_ok=True)
    shutil.rmtree(root / PARTIAL, ignore_errors=True)  # an interrupted earlier copy is never trusted
    # Still to copy: real folders without a copy on the drive made from exactly what they hold now.
    to_copy, prints = [], {}
    for n in names:
        src = base / n
        if not src.is_dir() or src.is_symlink():
            if (not src.is_symlink() and (root / n).is_dir() and _marked(root, n) is None
                    and any((root / n).iterdir())):
                raise CygnusError(f"{root / n} already exists; Cygnus did not create it, so it is left alone")
            continue
        prints[n] = tree_fingerprint(src)
        if (root / n).exists():
            mark = _marked(root, n)
            if mark == prints[n]:
                continue  # an earlier run's complete copy of the very same content
            if mark is None:
                raise CygnusError(f"{root / n} already exists; Cygnus did not create it, so it is left alone")
            shutil.rmtree(root / n)  # Cygnus's own copy of an older state
        to_copy.append(n)
    if to_copy:
        needed = _du_bytes([base / n for n in to_copy])
        st = os.statvfs(root)
        free = st.f_bavail * st.f_frsize
        if needed + RELOCATION_MARGIN > free:
            raise CygnusError(f"the drive needs {needed // 2**20} MiB free for Flatpak's storage but has "
                              f"{free // 2**20} MiB")
        (root / PARTIAL).mkdir()
        try:
            _copy_together([base / n for n in to_copy], root / PARTIAL)
            for n in to_copy:
                _mark(root, n, prints[n])  # first the fingerprint, then the copy appears under its name
                os.rename(root / PARTIAL / n, root / n)
        finally:
            shutil.rmtree(root / PARTIAL, ignore_errors=True)
    try:
        for n in names:  # one folder at a time: verified, then renamed and linked back to back
            _switch(base, root, n)
    except Exception as exc:
        try:
            _relocate_abort(params)
        except CygnusError as abort_exc:
            raise CygnusError(f"{exc} {abort_exc}") from exc
        raise
    # Every link is live. The old folders are only a safety net now, and a backup goes only if it is still exactly
    # what was copied (a write through a handle opened before the move would have landed in it). All are checked
    # before any is deleted (deleting one changes the hardlinks it shares with another), and each is renamed first,
    # so a crash in the middle of deleting leaves something recognisable, not a half backup that looks complete.
    kept, doomed = [], []
    for n in names:
        backup = _backup(base, n)
        if backup.exists():
            (doomed if tree_fingerprint(backup) == _marked(root, n) else kept).append((n, backup))
    trash = []
    for n, backup in doomed:
        os.rename(backup, _trash(base, n))
        trash.append(_trash(base, n))
    for folder in trash:
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            kept.append((folder.name, folder))
    if not kept:
        shutil.rmtree(root / COPIED, ignore_errors=True)  # the marks are needed only while a backup exists
    return StepOutcome(result={"moved": names, "kept": bool(kept),
                               **({"path": str(kept[0][1]), "left_over": [str(k[1]) for k in kept],
                                   "reason": "changes made while Flatpak's storage was being moved are in it"}
                                  if kept else {})},
                       compensation=Step(kind="flatpak.unrelocate",
                                         params={"base": str(base), "target_root": str(root), "names": names}))


def _relocate_abort(params: dict) -> StepOutcome:
    """Put things back after a relocation that was interrupted part-way, losing nothing.

    A folder goes back only when the old copy is certainly all there is to keep: it was never linked (nothing
    could have been written to the drive copy), or it is linked but the old copy is complete and the drive copy has
    not changed since it went live. In every other case (the drive is not connected, a folder was created again,
    the old copy is incomplete or the drive copy was written to) both copies are kept, and the user is told where."""
    base, root = Path(params["base"]), Path(params["target_root"])
    drive_ok = root.is_dir()
    if drive_ok:
        shutil.rmtree(root / PARTIAL, ignore_errors=True)
    kept = []
    for n in params["names"]:
        backup, link, copy = _backup(base, n), base / n, root / n
        live = link.is_symlink() and os.readlink(link) == str(copy)
        ours = drive_ok and _marked(root, n) is not None
        if backup.exists():
            if link.exists() and not link.is_symlink():
                kept.append(f"{backup} (the original) and {link} (created again meanwhile)")
                continue
            if link.is_symlink() and not live:
                kept.append(f"{backup} (the original) and {link}, which points elsewhere")
                continue
            if live:
                if not drive_ok:
                    kept.append(f"{backup} (the earlier state) and {link}, whose drive is not connected")
                    continue
                complete = tree_fingerprint(backup) == _marked(root, n)
                if not (complete and ours and _drive_unchanged(root, n)):
                    kept.append(f"{copy} (in use) and {backup} (the earlier state)")
                    continue
                os.unlink(link)  # the old copy is complete and the drive copy has nothing more: safe to go back
            os.rename(backup, link)  # (a folder that never went live: its old copy is simply put back)
            if ours:
                shutil.rmtree(copy, ignore_errors=True)
        elif link.is_dir() and not link.is_symlink():
            if ours:
                shutil.rmtree(copy, ignore_errors=True)  # copied but not switched: the copy goes
        elif live and drive_ok and (not copy.exists() or not any(copy.iterdir())):
            os.unlink(link)  # there was no folder before: the link to an empty drive folder goes too
            copy.rmdir() if copy.exists() else None
        elif live:
            # It was moved completely (its old folder is gone) and holds data now: this is not something an undo of an
            # interrupted move can reverse, and saying "rolled back" would be false. "Move back" is the way.
            kept.append(f"{copy}, which was already moved completely (use \"Move back\" to return it)")
        if drive_ok and not backup.exists() and not (live and link.is_symlink()):
            _mark(root, n, None)
            _mark_drive(root, n, None)
    if kept:
        raise CygnusError("Cygnus kept both copies of Flatpak's data and deleted nothing, because it cannot be sure "
                          "which one has everything: " + "; ".join(kept) + ". Please check which one to keep.")
    _remove_if_empty(root / COPIED, root)  # what this step created on the drive goes, when nothing is left in it
    return StepOutcome(result={"aborted": True})


def _unrelocate(params: dict) -> StepOutcome:
    """Move the folders back from the drive. Resumable like _relocate: copies land in a .partial
    folder and become the restore folder only when complete; a link is removed only when its copy is
    complete, and the copy is used only if the drive's folder did not change after it was made (once
    the link is gone nothing can write there any more). Nothing happens while the drive is absent."""
    base, root = Path(params["base"]), Path(params["target_root"])
    names = list(params["names"])
    restore = base / ".cygnus-restore"
    if all((base / n).is_dir() and not (base / n).is_symlink() for n in names) and not restore.exists():
        return StepOutcome(result={"already": True})  # finished by an earlier run
    if not root.is_dir():
        raise CygnusError(f"{root} is not available (is its drive connected?); Flatpak's storage stays linked "
                          "to it until it can be copied back")
    restore, partial = base / ".cygnus-restore", base / ".cygnus-restore.partial"
    shutil.rmtree(partial, ignore_errors=True)  # an interrupted copy is never trusted
    for n in names:
        link, here = base / n, (base / n).is_symlink() and os.readlink(base / n) == str(root / n)
        if link.is_symlink() and not here:
            raise CygnusError(f"{link} points to {os.readlink(link)}, not to {root / n}")
        if not (root / n).is_dir() and n != ".removed" and (here or (not link.exists() and not (restore / n).is_dir())):
            raise CygnusError(f"{root / n} is missing, so {link} cannot be brought back")
        if link.is_dir() and not link.is_symlink() and (restore / n).is_dir():
            # Flatpak made a new folder after the link was removed by an interrupted run
            if any(link.iterdir()):
                raise CygnusError(f"{link} was created again while moving it back was interrupted; the complete "
                                  f"copy is in {restore / n}. Please check which one to keep.")
            link.rmdir()
    # Copies still needed: for each folder on the drive whose link is (or was) ours.
    need, prints = [], {}
    for n in names:
        if (base / n).is_dir() and not (base / n).is_symlink():
            continue  # already back
        if not (root / n).is_dir():
            continue  # nothing to bring back (.removed is created empty)
        prints[n] = tree_fingerprint(root / n)
        if (restore / n).is_dir() and _marked(restore, n) == prints[n]:
            continue
        shutil.rmtree(restore / n, ignore_errors=True)  # Cygnus's own copy of an older state
        need.append(n)
    if need:
        needed = _du_bytes([root / n for n in need])
        st = os.statvfs(base)
        if needed + RELOCATION_MARGIN > st.f_bavail * st.f_frsize:
            raise CygnusError(f"the system drive needs {needed // 2**20} MiB free to take Flatpak's storage back")
        partial.mkdir()
        try:
            _copy_together([root / n for n in need], partial)
            restore.mkdir(exist_ok=True)
            for n in need:
                _mark(restore, n, prints[n])
                os.rename(partial / n, restore / n)
        finally:
            shutil.rmtree(partial, ignore_errors=True)
    restored = []
    for n in names:
        link = base / n
        if link.is_symlink():
            os.unlink(link)  # from here on nothing can write into the drive's folder
            if (root / n).is_dir() and tree_fingerprint(root / n) != _marked(restore, n):
                os.symlink(root / n, link)  # it changed while it was copied: keep using it as it was
                raise CygnusError(_CHANGED)
        if link.is_symlink():  # something linked it to the drive again in the meantime
            raise CygnusError(f"{link} was linked to the drive again while it was being moved back; nothing "
                              "was deleted. Please try again when no Flatpak operation is running.")
        if link.exists() and (restore / n).is_dir():  # Flatpak made a new folder in the window
            raise CygnusError(f"{link} was created again while it was being moved back; the complete copy is in "
                              f"{restore / n} and in {root / n}. Nothing was deleted. Please check which to keep.")
        if not link.exists():
            if (restore / n).is_dir():
                os.rename(restore / n, link)
            elif n == ".removed":
                link.mkdir()
            else:
                raise CygnusError(f"no complete copy of {root / n} to bring back")
            restored.append(n)
    for n in names:  # the drive's copy goes only once the folder is back in place, and only if Cygnus copied it
        if ((base / n).is_dir() and not (base / n).is_symlink() and _marked(restore, n) is not None
                and not (restore / n).is_dir()):
            shutil.rmtree(root / n, ignore_errors=True)
    shutil.rmtree(restore, ignore_errors=True)
    _remove_if_empty(root)  # the drive's folder for Flatpak, once nothing is left in it
    return StepOutcome(result={"restored": restored} if restored else {"already": True})


def _relocate_dir_abort(params: dict) -> StepOutcome:
    """A per-folder move from an older release was interrupted. If the folder was renamed away and not yet linked,
    it is put back (the drive's copy, made by this step, goes). A link that is already live, or a folder created
    again, cannot be judged (no marks were kept by that release): both copies stay and the user is told."""
    base, name, target = Path(params["base"]), params["name"], Path(params["target"])
    link, backup = base / name, base / f".{name}.cygnus-before-move"
    if backup.exists():
        if link.is_symlink() or link.exists():
            raise CygnusError(f"Cygnus kept the old {backup} and the new {link} because it cannot be sure which has "
                              "everything. Please check which one to keep.")
        os.rename(backup, link)
        shutil.rmtree(target, ignore_errors=True)
    return StepOutcome(result={"aborted": True})


def _relocate_dir(params: dict) -> StepOutcome:
    base, name, target = Path(params["base"]), params["name"], Path(params["target"])
    link = base / name
    if link.is_symlink():
        if os.readlink(link) == str(target):
            return StepOutcome(result={"already": True})
        raise CygnusError(f"{link} already points elsewhere")
    if target.exists() and any(target.iterdir()):
        raise CygnusError(f"{target} already exists and is not empty")
    base.mkdir(parents=True, exist_ok=True)
    if name == "repo" and not link.exists():
        installation_for("user", base).list_remotes(None)  # Flatpak creates its repository, then it is moved
    target.mkdir(parents=True, exist_ok=True)
    backup = base / f".{name}.cygnus-before-move"
    if link.is_dir():
        # Copy preserving symlinks and hardlink-free; repo objects are immutable files.
        shutil.copytree(link, target, symlinks=True, dirs_exist_ok=True, copy_function=shutil.copy2)
        os.rename(link, backup)
    os.symlink(target, link)
    if backup.exists():
        shutil.rmtree(backup)
    return StepOutcome(result={"linked": str(link), "target": str(target)},
                       compensation=Step(kind="flatpak.unrelocate_dir", params=params))


def _unrelocate_dir(params: dict) -> StepOutcome:
    base, name, target = Path(params["base"]), params["name"], Path(params["target"])
    link = base / name
    if not link.is_symlink():
        return StepOutcome(result={"already": True})
    os.unlink(link)
    if target.is_dir():
        shutil.copytree(target, link, symlinks=True, copy_function=shutil.copy2)
        shutil.rmtree(target)
    return StepOutcome(result={"restored": str(link)})


# relocate_dir/unrelocate_dir: the earlier per-folder steps, kept so older journals can be finished or undone.
HANDLERS = {"flatpak.relocate": _relocate, "flatpak.relocate_dir.abort": _relocate_dir_abort, "flatpak.relocate.abort": _relocate_abort, "flatpak.unrelocate": _unrelocate,
            "flatpak.relocate_dir": _relocate_dir, "flatpak.unrelocate_dir": _unrelocate_dir}


# -- transactions ---------------------------------------------------------------------------------------
def _gi():
    import gi

    gi.require_version("Flatpak", "1.0")
    from gi.repository import Flatpak, Gio, GLib

    return Flatpak, Gio, GLib


@dataclass(slots=True, kw_only=True)
class TransactionResult:
    ok: bool
    operations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    issues: list[Issue] = field(default_factory=list)
    eol: list[dict[str, Any]] = field(default_factory=list)


def installation_for(kind: str, path: Path | None = None):
    Flatpak, Gio, _ = _gi()
    if kind == "user":
        return Flatpak.Installation.new_for_path(Gio.File.new_for_path(str(path or user_dir())), True, None)
    if kind == "system":
        return Flatpak.Installation.new_system(None)
    return Flatpak.Installation.new_system_with_id(kind, None)


def run_transaction(inst, *, installs: list[tuple[str, str]] = (), bundles: list[Path] = (),
                    uninstalls: list[str] = (), updates: list[str] = (), dry_run: bool = False,
                    resolve_dependencies: bool = True, as_dependency: bool = False) -> TransactionResult:
    """Run (or dry-run) a libflatpak transaction and classify any failure.

    `resolve_dependencies=False` is for bundles whose runtime Cygnus has already installed itself:
    libflatpak would otherwise download the bundle's `RuntimeRepo` URL (an address chosen by
    whoever built the file) on every install, and fail when it is unreachable.
    `as_dependency=True` installs runtimes the way Flatpak installs an app's dependencies (not
    pinned), so they become removable once no application uses them.
    """
    Flatpak, Gio, GLib = _gi()
    t = Flatpak.Transaction.new_for_installation(inst, None)
    t.add_default_dependency_sources()  # the CLI does this; libflatpak callers must do it themselves
    t.set_no_interaction(True)
    t.set_disable_dependencies(not resolve_dependencies)
    t.set_disable_auto_pin(as_dependency)
    for remote, ref in installs:
        t.add_install(remote, ref, None)
    for b in bundles:
        t.add_install_bundle(Gio.File.new_for_path(str(b)), None)
    for ref in uninstalls:
        t.add_uninstall(ref)
    for ref in updates:
        t.add_update(ref, None, None)
    result = TransactionResult(ok=False)
    counted = {"total": 0, "started": 0}

    def on_new_operation(tr, op, progress):
        # libflatpak says how far each operation is (0-100); with the operations finished so far that is how far the whole
        # transaction is, and it is shown inside the step that runs it
        ref = op.get_ref().split("/")[1] if op.get_ref().count("/") >= 2 else op.get_ref()
        verb = {"install": "Installing", "update": "Updating", "uninstall": "Removing",
                "install-bundle": "Installing"}.get(op.get_operation_type().value_nick, "Working on")
        progress.set_update_frequency(250)
        finished = counted["started"]  # operations run one after the other: those started before this one are done
        counted["started"] += 1

        def changed(p):
            total = max(1, counted["total"])
            progress_mod.within_step((finished + p.get_progress() / 100) / total,
                                     f"{verb} {ref}… {p.get_progress()}%")
        progress.connect("changed", changed)

    def on_ready(tr):
        counted["total"] = len(tr.get_operations())
        for op in tr.get_operations():
            result.operations.append({
                "type": op.get_operation_type().value_nick, "ref": op.get_ref(), "remote": op.get_remote(),
                "download_size": op.get_download_size(), "installed_size": op.get_installed_size()})
        return not dry_run

    def on_eol(tr, ref, reason, rebase):
        result.eol.append({"ref": ref, "reason": reason, "rebase": rebase})
        return False  # never rebase automatically

    t.connect("ready", on_ready)
    t.connect("new-operation", on_new_operation)
    t.connect("end-of-lifed-with-rebase", lambda tr, remote, ref, reason, rebase, prev: on_eol(tr, ref, reason, rebase))
    try:
        t.run(None)
        result.ok = True
    except GLib.Error as exc:
        if dry_run and exc.matches(Flatpak.error_quark(), Flatpak.Error.ABORTED):
            result.ok = True  # aborted on purpose after planning
        else:
            result.error = exc.message
            if exc.domain == "flatpak-error-quark":
                try:
                    result.error_code = Flatpak.Error(exc.code).value_nick
                except ValueError:
                    result.error_code = str(exc.code)
            result.issues.append(classify_error(result.error_code, exc.message))
    return result


def classify_error(code: str | None, message: str) -> Issue:
    """Map libflatpak error codes to diagnoses (architecture §8.1)."""
    table = {
        "runtime-not-found": ("FP_RUNTIME_UNAVAILABLE", "The required runtime is not available",
                              "None of your Flatpak repositories offers the runtime this app needs."),
        "remote-not-found": ("FP_REMOTE_MISSING", "The repository is not configured",
                             "The app comes from a repository that has not been added."),
        "need-new-flatpak": ("FP_NEEDS_NEWER_FLATPAK", "Requires a newer Flatpak",
                             "This app needs a newer Flatpak version than the one installed."),
        "out-of-space": ("STORAGE_FULL", "Not enough free space", "The target drive is full."),
        "not-authorized": ("NOT_AUTHORIZED", "Not authorized", "The administrator authorization was refused."),
        "untrusted": ("FP_UNTRUSTED", "The repository signature could not be verified",
                      "Cygnus will not install unverified content."),
        "already-installed": ("ALREADY_INSTALLED", "Already installed", "It is already installed."),
        "ref-not-found": ("VERSION_UNAVAILABLE", "The requested app or version is not available",
                          "The repository does not offer it (any more)."),
        "permission-denied": ("PERMISSION_DENIED", "Permission denied", "Flatpak was not allowed to do this."),
    }
    code_, title, expl = table.get(code or "", ("UNKNOWN_FAILURE", "Flatpak reported an error", ""))
    return Issue(code=code_, severity=IssueSeverity.BLOCKER, title=title,
                 explanation=(expl + " " if expl else "") + f"({message})",
                 resolutions=[explain_only("details", "Details", message)]
                 if code_ in ("UNKNOWN_FAILURE", "FP_UNTRUSTED", "NOT_AUTHORIZED")
                 else [Resolution(id="diagnose", title="Diagnose", explanation="Run the full analysis again.",
                                  safety=SafetyClass.AUTO)])


# -- journaled install steps ------------------------------------------------------------------------
# Installations are passed as {"path": <dir>, "user": bool} so a journaled step can be replayed later
# against exactly the same installation (no reliance on environment variables at resume time).
KEYRING_MAX = 1 << 20


def installation_spec(inst) -> dict[str, Any]:
    user = bool(inst.get_is_user())
    return {"path": inst.get_path().get_path(), "user": user, "id": None if user else (inst.get_id() or None)}


def _open(spec: dict[str, Any]):
    """Reopen a journaled installation. Configured system installations are opened by id: writes to
    them go through Flatpak's own system helper, which identifies the installation that way."""
    Flatpak, Gio, _ = _gi()
    if spec["user"]:
        ensure_relocation_links(Path(spec["path"]))
    if not spec["user"] and spec.get("id"):
        return (Flatpak.Installation.new_system(None) if spec["id"] == "default"
                else Flatpak.Installation.new_system_with_id(spec["id"], None))
    return Flatpak.Installation.new_for_path(Gio.File.new_for_path(spec["path"]), spec["user"], None)


def _ref_parts(ref: str) -> tuple[Any, str, str, str]:
    Flatpak, _, _ = _gi()
    kind, name, arch, branch = ref.split("/")
    return (Flatpak.RefKind.APP if kind == "app" else Flatpak.RefKind.RUNTIME), name, arch, branch


def _installed(inst, ref: str):
    _, _, GLib = _gi()
    try:
        return inst.get_installed_ref(*_ref_parts(ref), None)
    except GLib.Error:
        return None


def _raise_for(result: TransactionResult, what: str) -> None:
    if not result.ok:
        issue = result.issues[0] if result.issues else None
        raise CygnusError(f"{what}: " + (f"{issue.title}. {issue.explanation}" if issue else (result.error or "failed")))


def _mirror_remote(params: dict) -> StepOutcome:
    """Copy a remote the user already trusts (address + its own signing keyring) into `target`."""
    Flatpak, _, GLib = _gi()
    name = params["remote"]
    target, source = _open(params["target"]), _open(params["source"])
    if any(r.get_name() == name for r in target.list_remotes(None)):
        return StepOutcome(result={"already": True})
    src = source.get_remote_by_name(name, None)
    if src.get_disabled() or not src.get_url():
        raise CygnusError(f"the {name} repository is disabled")
    if not src.get_gpg_verify():
        raise CygnusError(f"the {name} repository is not signed; Cygnus does not copy unsigned repositories")
    keyring = Path(params["source"]["path"]) / "repo" / f"{name}.trustedkeys.gpg"
    try:
        with open(keyring, "rb", opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as fh:
            key = fh.read(KEYRING_MAX + 1)
    except OSError as exc:
        raise CygnusError(f"cannot read the signing key of {name}: {exc.strerror}") from None
    if not key or len(key) > KEYRING_MAX:
        raise CygnusError(f"the signing key of {name} is missing or too large")
    remote = Flatpak.Remote.new(name)
    remote.set_url(src.get_url())
    remote.set_title(src.get_title() or name)
    if src.get_collection_id():
        remote.set_collection_id(src.get_collection_id())
    remote.set_gpg_verify(True)
    remote.set_gpg_key(GLib.Bytes.new(key))
    target.add_remote(remote, False, None)
    return StepOutcome(result={"added": name, "url": src.get_url()},
                       compensation=Step(kind="flatpak.remove_remote",
                                         params={"target": params["target"], "remote": name}))


def _remove_remote(params: dict) -> StepOutcome:
    _, _, GLib = _gi()
    inst, name = _open(params["target"]), params["remote"]
    if not any(r.get_name() == name for r in inst.list_remotes(None)):
        return StepOutcome(result={"already": True})
    in_use = [r.format_ref() for r in inst.list_installed_refs(None) if r.get_origin() == name]
    if in_use:
        raise CygnusError(f"{name} is still used by {', '.join(in_use)}")
    inst.remove_remote(name, None)
    return StepOutcome(result={"removed": name})


def _install_ref(params: dict) -> StepOutcome:
    inst, ref = _open(params["installation"]), params["ref"]
    if _installed(inst, ref) is not None:
        return StepOutcome(result={"already": True})
    as_dependency = params.get("as_dependency", ref.startswith("runtime/"))
    result = run_transaction(inst, installs=[(params["remote"], ref)], as_dependency=as_dependency)
    _raise_for(result, f"installing {ref.split('/')[1]}")
    return StepOutcome(result={"installed": ref, "also_installed": _new_refs(result, ref)},
                       compensation=Step(kind="flatpak.uninstall_ref",
                                         params={"installation": params["installation"], "ref": ref}))


def _install_bundle(params: dict) -> StepOutcome:
    inst, ref = _open(params["installation"]), params["ref"]
    if _installed(inst, ref) is not None:
        raise CygnusError(f"{ref.split('/')[1]} is already installed in this Flatpak installation")
    # The runtime was resolved and installed by an earlier step (or already present).
    result = run_transaction(inst, bundles=[Path(params["bundle"])], resolve_dependencies=False)
    _raise_for(result, "installing the bundle")
    return StepOutcome(result={"installed": ref, "also_installed": _new_refs(result, ref)},
                       compensation=Step(kind="flatpak.uninstall_ref",
                                         params={"installation": params["installation"], "ref": ref}))


def _new_refs(result: TransactionResult, main: str) -> list[str]:
    """Refs a transaction installed besides `main` (related extensions, translations, GL drivers)."""
    return sorted({op["ref"] for op in result.operations if op["type"] == "install" and op["ref"] != main})


def _uninstall_ref(params: dict) -> StepOutcome:
    inst, ref = _open(params["installation"]), params["ref"]
    if _installed(inst, ref) is None:
        return StepOutcome(result={"already": True})
    _raise_for(run_transaction(inst, uninstalls=[ref]), f"removing {ref}")
    return StepOutcome(result={"removed": ref})


HANDLERS.update({
    "flatpak.mirror_remote": _mirror_remote, "flatpak.remove_remote": _remove_remote,
    "flatpak.install_ref": _install_ref, "flatpak.install_bundle": _install_bundle,
    "flatpak.uninstall_ref": _uninstall_ref,
})


def _remote_source(env, spec: dict[str, Any], target_id: str, remote: str, source_inst: str | None,
                   mirrored: set[str]) -> list[Step]:
    """A step copying `remote` into the target installation when only another installation has it."""
    from cygnus.core.backends.flatpak import FlatpakEnv

    if source_inst in (None, target_id) or remote in mirrored:
        return []
    if not spec["user"]:
        raise CygnusError(f"{remote} is not configured for this installation")
    src = next(i for i in env.installations if FlatpakEnv.inst_id(i) == source_inst)
    mirrored.add(remote)
    return [Step(kind="flatpak.mirror_remote",
                 params={"target": spec, "source": installation_spec(src), "remote": remote},
                 description=f"Add {remote} to your personal Flatpak installation")]


def _runtime_steps(env, spec: dict[str, Any], runtime: str, *, allow_obsolete_runtime: bool,
                   mirrored: set[str]) -> list[Step]:
    status = env.runtime_status(runtime)
    if status.eol and not allow_obsolete_runtime:
        raise CygnusError(f"{status.ref.name} {status.ref.branch} no longer receives security updates; "
                          "Cygnus will not install it without your explicit approval")
    if status.installed:
        return []
    source = status.install_source
    if source is None:
        raise CygnusError(f"none of your Flatpak repositories offers {status.ref.name} {status.ref.branch}")
    source_inst, remote = source
    return _remote_source(env, spec, status.target, remote, source_inst, mirrored) + [
        Step(kind="flatpak.install_ref", params={"installation": spec, "remote": remote, "ref": str(status.ref)},
             description=f"Install the {status.ref.name} {status.ref.branch} runtime")]


def plan_bundle_install(inst, bundle: Path, app_ref: str, runtime: str | None, env=None, *,
                        allow_obsolete_runtime: bool = False) -> list[Step]:
    """Steps for installing a bundle into `inst` (architecture §9): the runtime is resolved for this
    installation, from a repository the user already trusts; the bundle never adds repositories."""
    from cygnus.core.backends.flatpak import FlatpakEnv

    spec = installation_spec(inst)
    env = env or FlatpakEnv(target=inst)
    steps = _runtime_steps(env, spec, runtime, allow_obsolete_runtime=allow_obsolete_runtime,
                           mirrored=set()) if runtime else []
    steps.append(Step(kind="flatpak.install_bundle",
                      params={"installation": spec, "bundle": str(bundle), "ref": app_ref},
                      description=f"Install {app_ref.split('/')[1]}"))
    return steps


# -- apps from remotes -----------------------------------------------------------------------------------
def remote_app(env, remote: str, app_id: str, branch: str | None = None):
    """Look an app up in a configured remote (any installation, the target's first) and return
    (Candidate, installation id holding the remote). Branches are looked up, never guessed."""
    from cygnus.core.backends.flatpak import FlatpakEnv
    from cygnus.core.detect.flatpak import candidate_from_metadata

    Flatpak, _, GLib = _gi()
    arch = Flatpak.get_default_arch()
    holders = [i for i in env.installations
               if any(r.get_name() == remote and not r.get_disabled() for r in i.list_remotes(None))]
    if not holders:
        raise CygnusError(f"no Flatpak repository named {remote!r} is configured")
    inst = holders[0]
    if branch is None:
        try:
            refs = [r for r in inst.list_remote_refs_sync(remote, None)
                    if r.get_kind() == Flatpak.RefKind.APP and r.get_name() == app_id and r.get_arch() == arch]
        except GLib.Error as exc:
            raise CygnusError(f"cannot read {remote}: {exc.message}") from None
        branches = sorted({r.get_branch() for r in refs})
        if not branches:
            raise CygnusError(f"{remote} does not offer {app_id} for {arch}")
        if len(branches) > 1 and "stable" not in branches:
            raise CygnusError(f"{app_id} has several branches ({', '.join(branches)}); choose one with //BRANCH")
        branch = "stable" if "stable" in branches else branches[0]
    try:
        rr = inst.fetch_remote_ref_sync(remote, Flatpak.RefKind.APP, app_id, arch, branch, None)
    except GLib.Error as exc:
        raise CygnusError(f"{remote} does not offer {app_id}//{branch}: {exc.message}") from None
    md = rr.get_metadata()
    text = md.get_data().decode("utf-8", "replace") if md else ""
    cand = candidate_from_metadata(ref=f"app/{app_id}/{arch}/{branch}", remote=remote, metadata_text=text,
                                   download_size=rr.get_download_size(), installed_size=rr.get_installed_size(),
                                   eol=rr.get_eol() or None)
    return cand, FlatpakEnv.inst_id(inst)


def plan_ref_install(inst, cand, holder: str, env=None, *, allow_obsolete_runtime: bool = False) -> list[Step]:
    """Steps for installing an app from a remote into `inst`: the remote (copied with its signing key
    when only another installation has it), the runtime into the same installation, then the app."""
    from cygnus.core.backends.flatpak import FlatpakEnv

    spec = installation_spec(inst)
    env = env or FlatpakEnv(target=inst)
    target_id = FlatpakEnv.inst_id(inst)
    if cand.metadata.get("eol") and not allow_obsolete_runtime:
        raise CygnusError(f"{cand.name} is end-of-life: {cand.metadata['eol']}")
    remote, ref = cand.metadata["remote"], cand.identity["flatpak_ref"]
    if _installed(inst, ref) is not None:
        raise CygnusError(f"{cand.name} is already installed in this Flatpak installation")
    mirrored: set[str] = set()
    steps = _remote_source(env, spec, target_id, remote, holder, mirrored)
    if cand.metadata.get("runtime"):
        steps += _runtime_steps(env, spec, cand.metadata["runtime"], allow_obsolete_runtime=allow_obsolete_runtime,
                                mirrored=mirrored)
    steps.append(Step(kind="flatpak.install_ref",
                      params={"installation": spec, "remote": remote, "ref": ref, "as_dependency": False},
                      description=f"Install {cand.name}"))
    return steps


def cygnus_installed_runtimes(registry, spec: dict[str, Any]) -> list[str]:
    """Runtimes and extensions Cygnus itself installed into this installation (from the operation
    journal), including those that came along with a runtime or app."""
    import json

    refs = set()
    for action, result in registry.conn.execute(
            "SELECT action, result FROM operation_step WHERE state='done' "
            "AND (action LIKE '%flatpak.install_ref%' OR action LIKE '%flatpak.install_bundle%')"):
        a, r = json.loads(action), json.loads(result or "{}")
        if a["kind"] not in ("flatpak.install_ref", "flatpak.install_bundle") \
                or a["params"]["installation"]["path"] != spec["path"]:
            continue
        refs.update(x for x in [r.get("installed", ""), *r.get("also_installed", [])] if x.startswith("runtime/"))
    return sorted(refs)


def _prune_runtimes(params: dict) -> StepOutcome:
    """Remove runtimes Cygnus installed that no installed application uses any more. Best effort:
    this always runs after an irreversible step (an uninstall), so a failure here must not undo the
    operation; whatever could not be removed is reported and stays installed."""
    _, _, GLib = _gi()
    removed: list[str] = []
    for _ in range(4):  # an extension only becomes unused once the runtime it extends is gone
        inst = _open(params["installation"])
        try:
            unused = {r.format_ref() for r in inst.list_unused_refs(None, None)}
        except GLib.Error as exc:
            return StepOutcome(result={"removed": removed, "skipped": exc.message})
        now = [ref for ref in params["candidates"]
               if ref in unused and ref not in removed and _installed(inst, ref) is not None]
        if not now:
            break
        try:
            _raise_for(run_transaction(inst, uninstalls=now), "removing unused runtimes")
        except CygnusError as exc:
            return StepOutcome(result={"removed": removed, "skipped": str(exc), "left": now})
        removed += now
    return StepOutcome(result={"removed": removed})


HANDLERS["flatpak.prune_runtimes"] = _prune_runtimes


def plan_uninstall(registry, installation_id: str) -> list[Step]:
    import json

    row = registry.conn.execute("SELECT source FROM installation WHERE id=?", (installation_id,)).fetchone()
    if row is None:
        raise CygnusError("unknown installation")
    source = json.loads(row[0])
    spec = installation_spec(installation_for(source.get("installation", "user")))
    steps = [Step(kind="registry.forget", params={"installation_id": installation_id},
                  description="Forget the application"),
             # Irreversible (a bundle may no longer exist to reinstall from): only after the above.
             Step(kind="flatpak.uninstall_ref", params={"installation": spec, "ref": source["ref"]},
                  description=f"Uninstall {source['ref'].split('/')[1]}")]
    runtimes = cygnus_installed_runtimes(registry, spec)
    if runtimes:
        steps.append(Step(kind="flatpak.prune_runtimes", params={"installation": spec, "candidates": runtimes},
                          description="Remove runtimes nothing uses any more"))
    return steps


def _update_ref(params: dict) -> StepOutcome:
    """Update an installed ref (and the runtimes/extensions it needs). Not reversible: Flatpak does not
    keep a previous deployment to return to; this runs only as the last step of an operation."""
    inst, ref = _open(params["installation"]), params["ref"]
    if _installed(inst, ref) is None:
        raise CygnusError(f"{ref} is not installed")
    result = run_transaction(inst, updates=[ref])
    _raise_for(result, f"updating {ref.split('/')[1]}")
    installed = _installed(_open(params["installation"]), ref)
    return StepOutcome(result={"operations": result.operations, "eol": result.eol,
                               "version": installed.get_appdata_version() if installed else None})


HANDLERS["flatpak.update_ref"] = _update_ref


def plan_update(registry, installation_id: str) -> list[Step]:
    import json

    row = registry.conn.execute("SELECT source FROM installation WHERE id=?", (installation_id,)).fetchone()
    if row is None:
        raise CygnusError("unknown installation")
    source = json.loads(row[0])
    spec = installation_spec(installation_for(source.get("installation", "user")))
    return [Step(kind="flatpak.update_ref", params={"installation": spec, "ref": source["ref"]},
                 description=f"Update {source['ref'].split('/')[1]}")]


# -- repair -----------------------------------------------------------------------------------------------
def diagnose(source: dict[str, Any], env=None) -> list[dict[str, str]]:
    """What is wrong with a Flatpak Cygnus installed (no changes): relocation links, the app, its runtime."""
    from cygnus.core.backends.flatpak import FlatpakEnv

    problems = []
    kind, ref = source.get("installation", "user"), source["ref"]
    if kind == "user":
        state = relocation_state()
        root = state.relocated_to
        if root is not None and root.is_dir():
            for name in RELOCATED:
                if state.entries[name] != f"symlink:{root / name}":
                    problems.append({"what": "storage link", "state": "missing", "path": str(state.base / name)})
        elif root is not None:
            problems.append({"what": "storage", "state": "offline", "path": str(root)})
            return problems
    inst = installation_for(kind)
    installed = _installed(inst, ref)
    if installed is None:
        problems.append({"what": "application", "state": "missing", "path": ref})
        return problems
    md = installed.load_metadata(None)
    from cygnus.core.detect.flatpak import parse_metadata

    runtime = parse_metadata(md.get_data().decode("utf-8", "replace") if md else "").get("Application", {}).get("runtime")
    if runtime and not (env or FlatpakEnv(target=inst)).runtime_status(runtime, query_remotes=False).installed:
        problems.append({"what": "runtime", "state": "missing", "path": f"runtime/{runtime}"})
    return problems


def _verify_installation(params: dict) -> StepOutcome:
    """Flatpak's own integrity check of the user installation (re-downloads damaged files)."""
    from cygnus.core.util import proc

    res = proc.run(["flatpak", "repair", "--user"], timeout=6 * 3600,
                   env=proc.clean_env({"FLATPAK_USER_DIR": params["path"]}))
    if res.returncode != 0:
        raise CygnusError("Flatpak could not repair the installation: " + (res.stderr.strip().splitlines() or ["?"])[-1])
    fixed = [l.strip() for l in res.stdout.splitlines() if l.strip().startswith(("Deleting", "Removing", "Reinstalling",
                                                                                "Erasing", "Pruning"))]
    relinked = ensure_relocation_links(Path(params["path"]))  # `flatpak repair` erases the .removed link
    return StepOutcome(result={"actions": fixed, "relinked": relinked})


HANDLERS["flatpak.verify_installation"] = _verify_installation


def plan_repair(source: dict[str, Any], env=None, *, bundle_ok: bool = True) -> list[Step]:
    """Steps that put back what `diagnose` finds missing. The relocation links are restored as soon
    as the installation is opened; the app comes back from its repository (or its bundle file)."""
    from cygnus.core.backends.flatpak import FlatpakEnv

    kind, ref = source.get("installation", "user"), source["ref"]
    problems = {p["what"]: p for p in diagnose(source, env)}
    if "storage" in problems:
        raise CygnusError(f"{problems['storage']['path']} is not available; connect its drive first")
    inst = installation_for(kind)  # opening it restores missing relocation links
    env = env or FlatpakEnv(target=inst)
    spec = installation_spec(inst)
    steps: list[Step] = []
    if "application" in problems:
        remote = source.get("remote")
        app_id, branch = ref.split("/")[1], ref.split("/")[3]
        if remote:
            cand, holder = remote_app(env, remote, app_id, branch)
            steps += plan_ref_install(inst, cand, holder, env)
        elif bundle_ok and source.get("bundle") and Path(source["bundle"]).is_file():
            from cygnus.core.detect import detect_file

            cand = detect_file(source["bundle"])
            steps += plan_bundle_install(inst, Path(source["bundle"]), ref, cand.metadata.get("runtime"), env)
        else:
            raise CygnusError(f"{app_id} is gone and came from a bundle file that is no longer available; "
                              "download the bundle again to reinstall it")
    elif "runtime" in problems:
        steps += _runtime_steps(env, spec, problems["runtime"]["path"].removeprefix("runtime/"),
                                allow_obsolete_runtime=False, mirrored=set())
    if spec["user"]:
        steps.append(Step(kind="flatpak.verify_installation", params={"path": spec["path"]},
                          description="Check every file of your Flatpak installation"))
    return steps
