"""Unprivileged filesystem capability probe (architecture §6.2).

All tests run inside a freshly created, randomly named directory
``<location>/.cygnus-probe-<random>`` which is removed afterwards. Nothing is
ever executed from the probed filesystem; "exec allowed" is derived from mount
flags plus access(X_OK).
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import mmap
import os
import secrets
import shutil
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cygnus.core import paths
from cygnus.core.errors import StorageError
from cygnus.core.storage import mountinfo
from cygnus.core.util import proc

PROBE_VERSION = 1
PROBE_PREFIX = ".cygnus-probe-"


@dataclass(slots=True)
class ProbeResult:
    path: str
    probe_version: int = PROBE_VERSION
    boot_id: str = ""
    fs_type: str = ""
    mount_options: list[str] = field(default_factory=list)
    writable: bool = False
    created: bool = False
    caps: dict[str, bool | None] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    cleaned_up: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def supports_appimages(self) -> bool:
        return bool(self.writable and self.caps.get("exec_allowed") and self.caps.get("chmod_persists"))

    @property
    def supports_flatpak(self) -> bool:
        needed = ("exec_allowed", "symlinks", "hardlinks", "chmod_persists", "atomic_rename")
        if not all(self.caps.get(k) for k in needed):
            return False
        # Only trust the OSTree verdict when that sub-probe actually ran.
        return self.caps.get("ostree_bare_user_only") is not False


def _check(result: ProbeResult, name: str, fn) -> None:
    try:
        result.caps[name] = bool(fn())
    except OSError as exc:
        result.caps[name] = False
        result.errors[name] = f"{errno.errorcode.get(exc.errno, exc.errno)}: {exc.strerror}"
    except Exception as exc:  # noqa: BLE001 - a probe must never crash the caller
        result.caps[name] = None
        result.errors[name] = repr(exc)


def _renameat2_noreplace(src: Path, dst: Path) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, "renameat2", None)
    if fn is None:
        return False
    AT_FDCWD, RENAME_NOREPLACE = -100, 1
    rc = fn(AT_FDCWD, os.fsencode(src), AT_FDCWD, os.fsencode(dst), RENAME_NOREPLACE)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return True


def _ostree_probe(base: Path) -> bool | None:
    """Mimic what a Flatpak user installation does: bare-user-only repo, commit, hardlink checkout."""
    ostree = proc.which("ostree")
    if ostree is None:
        return None
    repo, tree, co = base / "repo", base / "tree", base / "checkout"
    (tree / "bin").mkdir(parents=True)
    exe = tree / "bin" / "tool"
    exe.write_bytes(b"#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    (tree / "data.txt").write_text("cygnus ostree probe\n")
    (tree / "link").symlink_to("data.txt")
    steps = [
        [ostree, "init", "--mode=bare-user-only", f"--repo={repo}"],
        [ostree, "commit", f"--repo={repo}", "--branch=probe", f"--tree=dir={tree}", "--no-xattrs",
         "--subject=probe", "--canonical-permissions"],
        [ostree, "checkout", f"--repo={repo}", "-U", "--require-hardlinks", "probe", str(co)],
        [ostree, "fsck", f"--repo={repo}"],
    ]
    for argv in steps:
        res = proc.run(argv, timeout=120)
        if not res.ok:
            raise OSError(errno.EIO, f"{argv[1]} failed: {res.stderr.strip()[:300]}")
    co_exe = co / "bin" / "tool"
    if not (co_exe.stat().st_mode & 0o111):
        raise OSError(errno.EPERM, "exec bit lost on checkout")
    if os.readlink(co / "link") != "data.txt":
        raise OSError(errno.EINVAL, "symlink not preserved on checkout")
    if co_exe.stat().st_nlink < 2:
        raise OSError(errno.EMLINK, "checkout did not hardlink into the repo")
    return True


def probe(location: str | Path, *, ostree: bool = True) -> ProbeResult:
    """Probe `location` (an existing, mounted directory). Never raises for unsupported features."""
    location = Path(location)
    if not location.is_absolute():
        raise StorageError("probe location must be an absolute path")
    # The mount is looked up for the folder that is actually probed, wherever links lead.
    location = Path(os.path.realpath(location))
    mounts = mountinfo.read()
    mount = mountinfo.mount_for_path(mounts, str(location))
    result = ProbeResult(path=str(location), boot_id=paths.boot_id())
    if mount is not None:
        result.fs_type = mount.fstype
        result.mount_options = sorted(mount.options)
    if mount is not None and mount.fstype == "autofs":
        raise StorageError(f"{location} is an automount point that is not mounted; refusing to trigger it")

    base = location / f"{PROBE_PREFIX}{secrets.token_hex(6)}"
    try:
        os.mkdir(base, 0o700)
    except OSError as exc:
        result.errors["mkdir"] = exc.strerror or str(exc)
        result.cleaned_up = True  # nothing was created
        return result
    result.writable = result.created = True
    try:
        _run_checks(result, base, mount, ostree=ostree)
    finally:
        result.cleaned_up = _cleanup(base)
    return result


def _run_checks(result: ProbeResult, base: Path, mount: mountinfo.Mount | None, *, ostree: bool) -> None:
    f = base / "file"

    def write_fsync() -> bool:
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, b"x" * 4096)
            os.fsync(fd)
        finally:
            os.close(fd)
        return f.read_bytes() == b"x" * 4096

    _check(result, "write_read_fsync", write_fsync)

    def chmod_persists() -> bool:
        f.chmod(0o640)
        a = stat.S_IMODE(f.stat().st_mode)
        f.chmod(0o755)
        b = stat.S_IMODE(f.stat().st_mode)
        return a == 0o640 and b == 0o755

    _check(result, "chmod_persists", chmod_persists)

    def exec_allowed() -> bool:
        if mount is not None and "noexec" in mount.options:
            return False
        f.chmod(0o755)
        return os.access(f, os.X_OK)

    _check(result, "exec_allowed", exec_allowed)
    result.caps["suid_honoured"] = None if mount is None else "nosuid" not in mount.options

    def symlinks() -> bool:
        (base / "sym").symlink_to("file")
        return os.readlink(base / "sym") == "file" and (base / "sym").read_bytes()[:1] == b"x"

    _check(result, "symlinks", symlinks)

    def hardlinks() -> bool:
        os.link(f, base / "hard")
        return f.stat().st_nlink == 2

    _check(result, "hardlinks", hardlinks)

    def atomic_rename() -> bool:
        (base / "new").write_bytes(b"new")
        os.replace(base / "new", f)
        return f.read_bytes() == b"new"

    _check(result, "atomic_rename", atomic_rename)

    def rename_noreplace() -> bool:
        (base / "nr").write_bytes(b"1")
        return _renameat2_noreplace(base / "nr", base / "nr2")

    _check(result, "rename_noreplace", rename_noreplace)

    def case_sensitive() -> bool:
        (base / "CaseTest").write_bytes(b"")
        return not (base / "casetest").exists()

    _check(result, "case_sensitive", case_sensitive)

    def user_xattr() -> bool:
        os.setxattr(f, "user.cygnus.probe", b"1")
        return os.getxattr(f, "user.cygnus.probe") == b"1"

    _check(result, "user_xattr", user_xattr)

    def o_tmpfile() -> bool:
        fd = os.open(base, os.O_TMPFILE | os.O_WRONLY, 0o600)
        os.close(fd)
        return True

    _check(result, "o_tmpfile", o_tmpfile)

    def flock() -> bool:
        with open(f, "rb") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return True

    _check(result, "flock", flock)

    def shared_mmap() -> bool:
        p = base / "mm"
        p.write_bytes(b"\0" * 4096)
        with open(p, "r+b") as fh, mmap.mmap(fh.fileno(), 4096, mmap.MAP_SHARED) as mm:
            mm[:4] = b"abcd"
            mm.flush()
        return p.read_bytes()[:4] == b"abcd"

    _check(result, "shared_mmap", shared_mmap)

    def long_names() -> bool:
        (base / ("n" * 240)).write_bytes(b"")
        return True

    _check(result, "long_names", long_names)

    def special_chars() -> bool:
        (base / "a:b?c").write_bytes(b"")
        return True

    _check(result, "special_chars", special_chars)

    if ostree:
        sub = base / "ostree"
        sub.mkdir()
        try:
            res = _ostree_probe(sub)
            result.caps["ostree_bare_user_only"] = res
        except OSError as exc:
            result.caps["ostree_bare_user_only"] = False
            result.errors["ostree_bare_user_only"] = exc.strerror or str(exc)


def _cleanup(base: Path) -> bool:
    """Remove only the probe directory we created; never touch anything outside it."""
    try:
        st = os.lstat(base)
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(st.st_mode) or not base.name.startswith(PROBE_PREFIX):
        return False
    # OSTree marks objects read-only: make everything *inside* base writable first, fd-relative and
    # without following symlinks, so no permission change can ever escape the probe directory.
    for dirpath, dirnames, _filenames, dirfd in os.fwalk(base, follow_symlinks=False):
        for name in dirnames:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dirfd)
            except OSError:
                continue
            try:
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
    try:
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.fchmod(fd, 0o700)
        os.close(fd)
    except OSError:
        return False
    shutil.rmtree(base, ignore_errors=True)  # fd-based: does not follow symlinks
    return not os.path.lexists(base)
