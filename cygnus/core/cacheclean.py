"""Cleaning pacman's package cache: what would go (a preview anyone may look at) and the removal itself (done by the root helper).

One implementation serves both, so that what is previewed is what the helper chooses. The rules are deliberately narrow:
- only the cache folders `pacman-conf` names, and only if each is a real folder of root's that nobody else can write into (no link
  anywhere on its path, every folder on the way and the folder itself owned by root and not group- or world-writable): a folder that does
  not pass is left alone and said so;
- only regular files named like a package (`name-pkgver-pkgrel-arch.pkg.tar[.ext]`) and the signature next to such a package
  (`….sig`): never a `.part` file, a folder (`download-*`), a link, or anything else;
- per package and architecture the INSTALLED version always stays, and so do the `keep` newest others (by libalpm's own version rules,
  never by file date); with `uninstalled`, packages that are not installed at all lose every cached version;
- the plan holds the exact files (the person sees their count, their size, the folder and some examples); the helper chooses again under
  pacman's lock and removes only what is in the plan AND is still chosen (a new package installed in between can only make the list
  shorter, never longer);
- a folder that also holds a repository database (`*.db`, `*.files`: a local repository that is used as a cache folder) is left alone:
  the database still lists the packages in it, and deleting them would break installing from that repository.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from typing import Any, Callable

from cygnus.core.backends import pacman as pm
from cygnus.core.pkgcache import CacheRefused, open_folder, parse_filename

MAX_KEEP = 10
DEFAULT_KEEP = 2  # older versions kept besides the installed one
MAX_ENTRIES = 40_000  # a folder with more entries than this is left alone (and it is under what the worker will order in one question)
MAX_EXAMPLES = 12
_REPOSITORY = re.compile(r".+\.(?:db|files)(?:\.tar(?:\.[A-Za-z0-9]+)?)?(?:\.sig)?\Z")  # a repository database or its file list (or a signature of one)


@dataclass(frozen=True, slots=True)
class Entry:
    folder: str
    filename: str
    name: str
    version: str
    arch: str
    size: int
    signature: bool  # this is the `.sig` of a package, not the package
    dev: int
    ino: int

    @property
    def key(self) -> tuple[str, str, int, int]:
        return (self.folder, self.filename, self.dev, self.ino)


def check_request(keep: Any, uninstalled: Any) -> tuple[int, bool]:
    """The two choices a person makes, checked: how many older versions to keep (0..MAX_KEEP) and whether packages that are not installed
    lose all their cached versions. Anything else is refused: the helper takes nothing but these two from a client."""
    if isinstance(keep, bool) or not isinstance(keep, int) or not 0 <= keep <= MAX_KEEP:
        raise CacheRefused(f"how many older versions to keep must be a number from 0 to {MAX_KEEP}")
    if not isinstance(uninstalled, bool):
        raise CacheRefused("whether to remove packages that are not installed must be yes or no")
    return keep, uninstalled


def list_entries(config: pm.PacmanConfig, viewer_uid: int | None = None) -> tuple[list[Entry], list[str]]:
    """(the package files and signatures in every cache folder that may be cleaned, what was said about each folder that may not).
    A folder is judged whole or not at all: one that holds a repository database or more than MAX_ENTRIES entries is left alone."""
    entries: list[Entry] = []
    problems: list[str] = []
    seen: set[str] = set()
    for folder in config.cachedirs:
        clean = folder.rstrip("/") or "/"
        if clean in seen:
            continue
        seen.add(clean)
        try:
            fd = open_folder(folder, viewer_uid)
        except CacheRefused as exc:
            problems.append(str(exc))
            continue
        found: list[Entry] = []
        leave: str | None = None
        try:
            with os.scandir(fd) as it:
                for count, entry in enumerate(it):
                    if count >= MAX_ENTRIES:
                        leave = f"{clean} holds more than {MAX_ENTRIES} entries, which is too many to judge safely: it is left alone"
                        break
                    name = entry.name
                    if _REPOSITORY.match(name):
                        leave = f"{clean} also holds a repository database ({name[:60]}), so it is a local repository, not only a cache: it is left alone"
                        break
                    is_sig = name.endswith(".sig")
                    parsed = parse_filename(name[:-4] if is_sig else name)
                    if parsed is None:
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    found.append(Entry(clean, name, parsed[0], parsed[1], parsed[2], st.st_size, is_sig, st.st_dev, st.st_ino))
        except OSError as exc:
            leave = f"{clean} could not be read ({exc.strerror or exc})"
        finally:
            os.close(fd)
        if leave:
            problems.append(leave)
        else:
            entries.extend(found)
    return entries, problems


def _ask(config: pm.PacmanConfig, request: dict[str, Any], worker: Callable | None) -> dict[str, Any]:
    try:
        answer = (worker or pm.run_worker)(config, request)
    except pm.WorkerCrash as crash:
        raise CacheRefused(f"the package databases could not be read ({crash})") from crash
    if not answer.get("ok"):
        raise CacheRefused(str((answer.get("error") or {}).get("message") or "the package databases could not be read"))
    return answer


def choose(entries: list[Entry], installed: dict[str, str], order: dict[tuple[str, str], list[str]], keep: int, uninstalled: bool) -> list[Entry]:
    """The entries to delete. `order` has, for each (name, arch) of a cached package, its cached versions newest first."""
    by_group: dict[tuple[str, str], dict[str, list[Entry]]] = {}
    for e in entries:
        by_group.setdefault((e.name, e.arch), {}).setdefault(e.version, []).append(e)
    doomed: list[Entry] = []
    for group, versions in by_group.items():
        packages = {v for v, es in versions.items() if any(not x.signature for x in es)}
        if not packages:
            continue  # only signatures: not ours to judge
        newest_first = [v for v in order.get(group, []) if v in packages]
        if set(newest_first) != packages:  # the order is not about exactly these versions: do not guess
            continue
        name = group[0]
        if name in installed:
            kept = {installed[name]} | set([v for v in newest_first if v != installed[name]][:keep])
        elif uninstalled:
            kept = set()
        else:
            kept = set(newest_first[:keep])
        for v in packages - kept:
            doomed.extend(versions[v])
    return sorted(doomed, key=lambda e: (e.folder, e.filename))


def selection(config: pm.PacmanConfig, keep: int, uninstalled: bool, *, worker: Callable | None = None,
              viewer_uid: int | None = None) -> tuple[list[Entry], list[str], int]:
    """(the entries to delete, what was said about folders left alone, how many entries were looked at)."""
    keep, uninstalled = check_request(keep, uninstalled)
    entries, problems = list_entries(config, viewer_uid)
    if not entries:
        return [], problems, 0
    groups: dict[tuple[str, str], set[str]] = {}
    for e in entries:
        if not e.signature:
            groups.setdefault((e.name, e.arch), set()).add(e.version)
    keys = sorted(groups)
    orders = _ask(config, {"op": "sort_versions", "groups": [sorted(groups[k]) for k in keys]}, worker)["orders"] if keys else []
    installed = _ask(config, {"op": "installed_versions"}, worker)["versions"]
    if not isinstance(orders, list) or len(orders) != len(keys) or not isinstance(installed, dict):
        raise CacheRefused("the package databases gave an answer Cygnus did not expect")
    return choose(entries, installed, dict(zip(keys, orders)), keep, uninstalled), problems, len(entries)


def preview(config: pm.PacmanConfig, keep: int = DEFAULT_KEEP, uninstalled: bool = False, *, worker: Callable | None = None) -> dict[str, Any]:
    """What a cleaning with these choices would remove: {"files", "bytes", "packages", "examples", "folders", "problems"} (removes nothing)."""
    doomed, problems, looked = selection(config, keep, uninstalled, worker=worker)
    packages = {(e.name, e.arch, e.version) for e in doomed if not e.signature}
    return {"files": len(doomed), "bytes": sum(e.size for e in doomed), "packages": len(packages), "looked_at": looked,
            "examples": sorted({f"{n} {v}" for n, _a, v in packages})[:MAX_EXAMPLES], "folders": sorted({e.folder for e in doomed}),
            "problems": problems, "keep": keep, "uninstalled": uninstalled}


def delete(entries: list[Entry], allowed: set[tuple[str, str, int, int]]) -> dict[str, Any]:
    """Remove `entries`, but only those whose identity (folder, name, device, inode) is in `allowed` (what the person was shown) and that
    are still the same regular file. Each folder is opened afresh (it must still pass the checks) and each file is removed relative to
    that descriptor, so a link put in its place cannot redirect the removal. Returns {"removed", "bytes", "skipped": [(name, why)]}."""
    removed = freed = 0
    skipped: list[tuple[str, str]] = []
    folders: dict[str, int] = {}
    try:
        for e in entries:
            if e.key not in allowed:
                skipped.append((e.filename, "it was not in the list that was shown"))
                continue
            fd = folders.get(e.folder)
            if fd is None:
                try:
                    fd = folders[e.folder] = open_folder(e.folder)
                except CacheRefused as exc:
                    skipped.append((e.filename, str(exc)))
                    continue
            if parse_filename(e.filename[:-4] if e.signature else e.filename) is None:
                skipped.append((e.filename, "its name is not a package's"))
                continue
            try:
                st = os.stat(e.filename, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (e.dev, e.ino):
                    skipped.append((e.filename, "it changed after it was listed"))
                    continue
                os.unlink(e.filename, dir_fd=fd)
            except FileNotFoundError:
                continue  # already gone: nothing to do
            except OSError as exc:
                skipped.append((e.filename, exc.strerror or str(exc)))
                continue
            removed += 1
            freed += st.st_size
    finally:
        for fd in folders.values():
            os.close(fd)
    return {"removed": removed, "bytes": freed, "skipped": skipped}
