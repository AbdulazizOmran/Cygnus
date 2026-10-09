"""The optional tools Cygnus can use when they are installed: what they are for, whether they are there, and (through the
helper, with the usual confirmation) a way to install the missing ones. Cygnus's required parts are the package's own
dependencies, which pacman installs together with it; these are the ones the package only suggests."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from cygnus.core.util import proc


@dataclass(frozen=True)
class Part:
    id: str
    package: str  # the repository package that provides it
    what: str  # in plain words: what it is for
    tools: tuple[str, ...] = ()  # programs that must be found
    paths: tuple[str, ...] = ()  # files that must exist (a library, for example)


PARTS = (
    Part("fuse3", "fuse3", "Starts AppImages the normal way. Without it they run in a slower extract-and-run mode, or not at all.",
         tools=("fusermount3",)),
    Part("fuse2", "fuse2", "Lets AppImages built for the older FUSE 2 start.", paths=("/usr/lib/libfuse.so.2",)),
    Part("zsync", "zsync", "Downloads only what changed when an AppImage is updated.", tools=("zsync",)),
    Part("build", "base-devel", "Compilers and build tools, needed to build AUR packages from source.", tools=("gcc", "make")),
    Part("kservice", "kservice", "Refreshes the Plasma application menu at once after changes.", tools=("kbuildsycoca6",)),
)
PACKAGES = frozenset(p.package for p in PARTS)


def status(which: Callable[[str], str | None] = proc.which, exists: Callable[[str], bool] = os.path.exists) -> list[dict]:
    """Every optional part with whether it is installed."""
    return [{"id": p.id, "package": p.package, "what": p.what,
             "present": all(which(t) for t in p.tools) and all(exists(x) for x in p.paths)} for p in PARTS]
