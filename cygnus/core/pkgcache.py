"""Older versions of an installed package, from pacman's package cache (to go back to one, a "downgrade").

The cache is where pacman keeps the files it downloaded. Cygnus lists the cached versions of one package that are older than the
installed one, and installs a chosen one through the helper's ordinary local-package plan: the file is opened without following links, hashed, read by
libalpm, and checked like any package file (protected names, dependencies). The helper does not check a package file's signature
(it says UNSIGNED and shows the SHA-256); what vouches for a file in the cache is that the cache folder is root's and pacman put
the file there, after checking its signature.
A page or a window never names a path: it names a package and a version, and this module finds the file again at the moment of use.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from cygnus.core.backends import pacman as pm
from cygnus.core.errors import CygnusError
from cygnus.core.packages import PKG_NAME

MAX_LISTED = 12
MAX_VERSIONS = 5000  # versions of one package looked at (the most recently cached): far more than any cache holds
COMPARE_CHUNK = 4000  # comparisons with the installed version per question to the worker (it accepts 5000)
MAX_CACHE_ENTRIES = 200_000  # files looked at in one folder (a cache holds a few thousand)
_SUFFIX = re.compile(r"\.pkg\.tar(?:\.(?:zst|xz|gz|bz2|lz4|lzo|lrz|Z))?\Z")  # \Z, not $: "$" accepts a trailing newline
_VERSION = re.compile(r"(?:\d+:)?[A-Za-z0-9._+~]+-[A-Za-z0-9._+~]+\Z")  # [epoch:]pkgver-pkgrel


ROOT_UID = 0  # who must own a cache folder (a constant so that a test can stand in for root; it is root in every real run)


class DowngradeError(CygnusError):
    """Something about the request, in words (no such version in the cache, nothing older)."""


class CacheRefused(CygnusError):
    """A cache folder or a request Cygnus will not use, with the reason in words."""


def open_folder(path: str, viewer_uid: int | None = None) -> int:
    """A descriptor for a cache folder that may be used, or CacheRefused saying why not.

    The path is walked from "/" one component at a time, each opened relative to the one before with O_NOFOLLOW (so a link anywhere on
    the way is refused, and nothing can be swapped between a check and the open: the descriptor returned is the one that was walked).
    Every folder on the way must be root's own and not writable by anyone else (a sticky folder such as /tmp may be, since others cannot
    replace what is in it), and the cache folder itself must be root's own with nobody else able to write into it. (What is in a folder
    others can write is not what pacman put there, and is neither listed for a downgrade nor ever cleaned.)
    `viewer_uid`: when someone other than root asks, a folder they could not look into themselves is refused too (what is in it is
    not theirs to learn from a plan)."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise CacheRefused(f"{path!r} is not an absolute folder")
    clean = path.rstrip("/") or "/"
    if os.path.normpath(clean) != clean:
        raise CacheRefused(f"{path} has an odd part in its path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parts = [p for p in clean.split("/") if p]
    try:
        fd = os.open("/", flags)
    except OSError as exc:
        raise CacheRefused(f"{path} cannot be opened ({exc.strerror or exc})") from exc
    try:
        for index, part in enumerate(parts + [None]):
            st = os.fstat(fd)
            last = part is None
            if st.st_uid not in (ROOT_UID, 0) or (st.st_mode & 0o022 and (last or not st.st_mode & 0o1000)):
                raise CacheRefused(f"{path} is not root's own folder that only root can write to, so it is not used" if last else
                                   f"a folder on the way to {path} can be changed by someone other than root, so it is not used")
            if last:
                break
            try:
                following = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                raise CacheRefused(f"{path} cannot be opened ({exc.strerror or exc}); a link anywhere on its path is refused") from exc
            os.close(fd)
            fd = following
        if st.st_uid != ROOT_UID:
            raise CacheRefused(f"{path} is not root's own folder that only root can write to, so it is not used")
        if viewer_uid not in (None, 0) and (st.st_mode & 0o005) != 0o005:
            raise CacheRefused(f"{path} is a folder others cannot look into, so a plan for it is not shown to them")
    except BaseException:
        os.close(fd)
        raise
    return fd


@dataclass(frozen=True, slots=True)
class Cached:
    version: str
    arch: str
    path: str
    size: int
    modified: float

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "arch": self.arch, "size": self.size, "modified": self.modified}


def parse_filename(filename: str) -> tuple[str, str, str] | None:
    """(name, version, arch) from `name-pkgver-pkgrel-arch.pkg.tar.zst`, or None when it is not a package file's name."""
    match = _SUFFIX.search(filename)
    if match is None or len(filename) > 255:
        return None
    parts = filename[:match.start()].rsplit("-", 3)
    if len(parts) != 4:
        return None
    name, pkgver, pkgrel, arch = parts
    version = f"{pkgver}-{pkgrel}"
    if not PKG_NAME.match(name) or not _VERSION.match(version) or not re.fullmatch(r"[a-z0-9_]+", arch):
        return None
    return name, version, arch


def cached_files(name: str, config: pm.PacmanConfig) -> list[Cached]:
    """Every file of package `name` in the cache folders that pass open_folder: regular files only (never a link), of an architecture
    this system accepts."""
    found: list[Cached] = []
    for folder in config.cachedirs:
        try:
            fd = open_folder(folder)
        except CacheRefused:
            continue
        clean = folder.rstrip("/") or "/"
        try:
            with os.scandir(fd) as entries:
                for count, entry in enumerate(entries):
                    if count > MAX_CACHE_ENTRIES:
                        break
                    parsed = parse_filename(entry.name)
                    if parsed is None or parsed[0] != name or parsed[2] not in (*config.arch, "any"):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    found.append(Cached(parsed[1], parsed[2], os.path.join(clean, entry.name), st.st_size, st.st_mtime))
        except OSError:
            continue
        finally:
            os.close(fd)
    return found


def _compare(pairs: list[list[str]], config: pm.PacmanConfig, worker: Callable | None) -> list[int]:
    if not pairs:
        return []
    try:
        answer = (worker or pm.run_worker)(config, {"op": "vercmp", "pairs": pairs})
    except pm.WorkerCrash as crash:
        raise DowngradeError(f"versions could not be compared ({crash})") from crash
    if not answer.get("ok"):
        raise DowngradeError("versions could not be compared")
    return list(answer["results"])


def _sorted(versions: list[str], config: pm.PacmanConfig, worker: Callable | None) -> list[str]:
    """`versions` newest first, by libalpm's rules (one question to the worker)."""
    if len(versions) < 2:
        return list(versions)
    try:
        answer = (worker or pm.run_worker)(config, {"op": "sort_versions", "groups": [versions]})
    except pm.WorkerCrash as crash:
        raise DowngradeError(f"versions could not be ordered ({crash})") from crash
    if not answer.get("ok") or sorted(answer["orders"][0]) != sorted(versions):
        raise DowngradeError("versions could not be ordered")
    return list(answer["orders"][0])


def older_versions(name: str, installed_version: str | None, *, config: pm.PacmanConfig | None = None, worker: Callable | None = None,
                   limit: int | None = MAX_LISTED) -> list[Cached]:
    """The cached versions of `name` older than `installed_version`, newest first (the newest `limit` of them, all when `limit` is
    None; one per version: the same version cached for two architectures keeps the one this system prefers). Nothing is offered for a
    package that is not installed. All the comparing is libalpm's: the versions against the installed one, in as few questions to the
    worker as it accepts, and then their order in one more."""
    if not PKG_NAME.match(name or "") or not installed_version:
        return []
    config = config or pm.read_config()
    files = cached_files(name, config)
    if not files:
        return []
    order = {a: i for i, a in enumerate(config.arch)}
    best: dict[str, Cached] = {}
    for f in files:
        if f.version not in best or order.get(f.arch, 99) < order.get(best[f.version].arch, 99):
            best[f.version] = f
    versions = sorted(best, key=lambda v: -best[v].modified)[:MAX_VERSIONS]
    against: dict[str, int] = {}
    for start in range(0, len(versions), COMPARE_CHUNK):
        chunk = versions[start:start + COMPARE_CHUNK]
        against.update(zip(chunk, _compare([[v, installed_version] for v in chunk], config, worker)))
    older = _sorted([v for v in versions if against[v] < 0], config, worker)
    return [best[v] for v in (older if limit is None else older[:limit])]


def find(name: str, version: str, installed_version: str | None, *, config: pm.PacmanConfig | None = None,
         worker: Callable | None = None) -> Cached:
    """The cached file of exactly `name` `version`, which must be older than the installed one (the file is found again now: what was
    listed earlier is not trusted)."""
    if not isinstance(version, str) or not _VERSION.match(version):
        raise DowngradeError("that is not a version Cygnus can work with")
    for f in older_versions(name, installed_version, config=config, worker=worker, limit=None):  # any older one, not only the ones listed
        if f.version == version:
            return f
    raise DowngradeError(f"{name} {version} is not in your package cache as an older version than the installed one")


def _sha256(path: str) -> str:
    import hashlib

    from cygnus.core.util.fs import open_regular

    digest = hashlib.sha256()
    with os.fdopen(open_regular(path, follow_symlinks=False), "rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def check_downgrade(name: str, version: str, *, config: pm.PacmanConfig | None = None, worker: Callable | None = None) -> tuple[Cached, str]:
    """(the cached file, the installed version) for going back to `version`, or an error in words: the package is not installed, is a
    protected one (the helper never replaces those from a file), or the version is not in the cache as an older one. Nothing is
    asked of the helper here."""
    from cygnus.core import packages

    config = config or pm.read_config()
    info = packages.details(name, config=config, worker=worker)
    if not info["installed"]:
        raise DowngradeError(f"{name} is not installed, so there is nothing to go back from")
    if info["protected"]:
        raise DowngradeError(f"{name} is a protected system package: Cygnus never replaces it from a file")
    if pm.package_signature_required(config):
        raise DowngradeError("pacman here requires a signature for package files (LocalFileSigLevel / SigLevel in /etc/pacman.conf), and the "
                             "helper does not pass a signature file along with a package, so pacman would refuse it. You can install the "
                             f"older version yourself from the cache with `sudo pacman -U` (its .sig file next to it is then checked).")
    return find(name, version, info["installed_version"], config=config, worker=worker), info["installed_version"]


def plan_downgrade(client: Any, name: str, version: str, *, checked: tuple[Cached, str] | None = None, config: pm.PacmanConfig | None = None,
                   worker: Callable | None = None):
    """The helper's plan for going back to an older cached version of an installed package: (plan, installed_version).

    The file is the one found now in the cache (see check_downgrade), hashed without following links; the helper checks it again and
    reads its real name and version through libalpm, and what it read must be what was asked for (a file whose contents are not its
    name is dropped, and the helper's plan for it simply expires). Nothing is recorded in Cygnus's list of applications: a repository
    package stays a system package."""
    cached, installed = checked or check_downgrade(name, version, config=config, worker=worker)
    plan = client.plan_packages(local_files=[(cached.path, _sha256(cached.path))], claim_ownership=False)
    local = (plan.summary or {}).get("local") or []
    if len(local) != 1 or local[0].get("name") != name or local[0].get("version") != version:
        raise DowngradeError(f"the file in the cache is not {name} {version} (it says "
                             + (f"{local[0].get('name')} {local[0].get('version')}" if len(local) == 1 else "something else") + "), so nothing was planned")
    return plan, installed
