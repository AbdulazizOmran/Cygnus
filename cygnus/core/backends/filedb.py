"""Which repository package ships a library file: pacman's file lists, kept in Cygnus's own copy of the databases.

Many Arch packages declare no "provides" for their libraries (Qt 5 is the common one), so a library that is
really available from the repositories cannot be found by name. The file lists answer exactly that question
("which package installs usr/lib/libQt5Core.so.5?"). They are downloaded without root into the private copy of the
databases (about 80 MB, refreshed weekly) and only when something could not be resolved the cheap way."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from cygnus.core.errors import CygnusError
from cygnus.core.util import proc

MAX_AGE = 7 * 24 * 3600
RETRY_AFTER = 24 * 3600  # after a download that reported errors
# What a real shared-library name looks like. Names come from files nobody has vetted, so anything else (above all a
# leading "-", which pacman would read as an option) is never passed on.
_SONAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_+.-]*\.so(?:\.[0-9][0-9A-Za-z_.-]*)?$")


def is_soname(name: str) -> bool:
    return bool(_SONAME.match(name)) and len(name) <= 200


# What pacman says when the network (not a repository without file lists) is the problem.
_NETWORK_TROUBLE = re.compile(r"could not resolve|resolve host|timed out|timeout|connection (?:refused|reset)|unreachable|"
                              r"failed to connect|ssl|certificate|operation too slow|no route", re.I)

STAMP = "files-refreshed"  # when Cygnus last fetched the lists: the files' own times are the server's, not ours


def _fresh(db: Path, now: float) -> bool:
    """Fetched less than a week ago (by our own stamp) and at least one list is there."""
    try:
        recent = now - (db / STAMP).stat().st_mtime <= MAX_AGE
    except OSError:
        return False
    return recent and any((db / "sync").glob("*.files"))


def refresh(db: Path, progress: Callable[[str], None], run=proc.run, now: Callable[[], float] = time.time) -> None:
    from cygnus.core import updates

    fakeroot = proc.which("fakeroot")
    if fakeroot is None:
        raise CygnusError("fakeroot is not installed (package fakeroot), so the package file lists cannot be fetched")
    updates.clear_stale_lock(db)
    progress("Downloading the package file lists to find which package provides the missing libraries "
             "(about 80 MB, once a week)…")
    res = run([fakeroot, "--", "pacman", "-Fy", "--disable-sandbox-filesystem", "--dbpath", str(db),
               "--logfile", "/dev/null"], timeout=1800)
    # A repository that publishes no file list makes pacman report an error although it fetched the others: what
    # matters is whether there is something to search.
    if res.returncode != 0 and not any((db / "sync").glob("*.files")):
        raise CygnusError("could not download the package file lists: " + (res.stderr.strip().splitlines() or ["?"])[-1])
    if res.returncode != 0 and _NETWORK_TROUBLE.search(res.stderr):
        return  # the old lists are good enough to search, but they were not renewed: the next lookup tries again
    # A clean run is good for a week. A run that reported errors may mean a repository that publishes no file list (it
    # will fail every time) or a mirror that is a little behind: the lists are used, but looked at again after a day.
    fresh_until = now() if res.returncode == 0 else now() - MAX_AGE + RETRY_AFTER
    stamp = db / STAMP
    stamp.write_text(str(int(now())))
    os.utime(stamp, (fresh_until, fresh_until))


def locate(sonames: list[str], *, progress: Callable[[str], None] = lambda _: None, run=proc.run, cfg=None,
           now: Callable[[], float] = time.time) -> dict[str, str]:
    """soname -> the first repository package that installs /usr/lib/<soname>. Names that are not found are left out;
    raises CygnusError when the file lists cannot be had at all."""
    from cygnus.core import updates
    from cygnus.core.backends import pacman as pm

    names = sorted({s for s in sonames if is_soname(s)})
    if not names:
        return {}
    cfg = cfg or pm.read_config()
    db = updates.private_db(cfg)
    if not _fresh(db, now()):
        refresh(db, progress, run, now)
    res = run(["pacman", "--dbpath", str(db), "-F", "--machinereadable", "--", *names], timeout=300, max_output=64 << 20)
    if res.returncode not in (0, 1):
        raise CygnusError("could not search the package file lists: " + (res.stderr.strip().splitlines() or ["?"])[-1])
    found: dict[str, str] = {}
    for line in res.stdout.splitlines():
        parts = line.split("\0")
        if len(parts) != 4:
            continue
        _repo, package, _version, path = parts
        name = path.rsplit("/", 1)[-1]
        if path == f"usr/lib/{name}" and name in names and name not in found:  # repositories are listed in order
            found[name] = package
    return found
