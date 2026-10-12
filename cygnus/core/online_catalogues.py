"""The online catalogues Discover can add to its lists: directories of programs that live somewhere on the internet (AppImageHub, Debian's and
Fedora's lists of applications), kept as small files in Cygnus's cache folder.

Reading the lists (Discover, searching) never touches the network: it only reads what is in the cache folder. A catalogue is downloaded when the
person's window asks for it (the first time Discover is opened, and again when the copy is older than a week), only while the catalogue is
switched on, and only from the one address written for it in its module. What was downloaded is cut down to a few plain fields per program and
stored; on the way back in it is checked again, because a file in a cache folder is still only a file.

A catalogue LISTS programs. Installing one goes through the same look-before-you-confirm page as any file the person chose."""

from __future__ import annotations

import json
import math
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from cygnus.core import paths, preferences
from cygnus.core.backends import appimagehub, debian_index, fedora_index
from cygnus.core.errors import CygnusError
from cygnus.core.util.fs import atomic_write

SCHEMA = 1
STALE_SECONDS = 7 * 24 * 3600
MAX_CACHE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Catalogue:
    id: str
    label: str
    what: str  # one plain sentence
    size: str  # what a download costs, in words
    module: ModuleType  # fetch(progress) -> records, clean(records) -> records


CATALOGUES: tuple[Catalogue, ...] = (
    Catalogue(appimagehub.ID, "AppImageHub", "Programs that people have listed as AppImages on GitHub's release pages (a community directory: nobody has checked them).",
              "about 2 MB", appimagehub),
    Catalogue(debian_index.ID, "Debian", "Programs from Debian's own repository, as .deb files that Cygnus tries to convert. Debian builds them for Debian: they may not "
              "work here. A program that the Arch repositories, the AUR or Flathub already have is not listed from here.", "about 15 MB", debian_index),
    Catalogue(fedora_index.ID, "Fedora", "Programs from Fedora's own repository, as .rpm files that Cygnus tries to convert. Fedora builds them for Fedora: they may not "
              "work here. A program that the Arch repositories, the AUR, Flathub or the lists above already have is not listed from here.", "about 30 MB", fedora_index),
)
BY_ID = {c.id: c for c in CATALOGUES}


def _folder():
    return paths.cache_dir() / "catalogues"


def _file(cat_id: str):
    return _folder() / f"{cat_id}.json"


def decided() -> bool:
    """Has the person been asked whether Discover may download THESE lists? Nothing is downloaded by itself until they have answered: the lists add up
    to tens of megabytes and come from other people's servers. The answer is recorded with the catalogues it covered, so a catalogue added in a later
    version is asked about again instead of being downloaded because an earlier answer was yes (or switched on because it was a no)."""
    asked = preferences.load().get("catalogues_decided")
    return isinstance(asked, list) and set(BY_ID) <= {a for a in asked if isinstance(a, str)}


def decide(accept: bool) -> None:
    """Record the answer to that question, for the catalogues it has not covered before. A no switches those off (they can be switched on one by one
    later); a yes leaves every switch as it is (a catalogue is on until it is switched off, and one the person switched off by hand, before or after, stays off)."""
    data = preferences.load()
    asked = data.get("catalogues_decided")
    covered = {a for a in asked if isinstance(a, str)} if isinstance(asked, list) else set()
    new = {cat_id for cat_id in BY_ID if cat_id not in covered}
    raw = data.get("catalogues_off")
    off = {c for c in raw if isinstance(c, str) and c in BY_ID} if isinstance(raw, list) else set()
    off = off if accept else off | new
    data["catalogues_decided"] = sorted(BY_ID)
    if off:
        data["catalogues_off"] = sorted(off)
    else:
        data.pop("catalogues_off", None)
    preferences.save(data)


def is_on(cat_id: str) -> bool:
    """On unless the person switched it off (the catalogues are what Discover is for)."""
    off = preferences.load().get("catalogues_off")
    return not (isinstance(off, list) and cat_id in off)


def set_on(cat_id: str, on: bool) -> None:
    if cat_id not in BY_ID:
        raise CygnusError("that is not one of the online catalogues")
    data = preferences.load()
    raw = data.get("catalogues_off")
    off = [c for c in raw if isinstance(c, str) and c in BY_ID and c != cat_id] if isinstance(raw, list) else []
    if not on:
        off.append(cat_id)
    if off:
        data["catalogues_off"] = sorted(set(off))
    else:
        data.pop("catalogues_off", None)
    preferences.save(data)


def stored(cat_id: str) -> dict[str, Any] | None:
    """What was downloaded for the catalogue and when, {"records", "fetched"}, or None when there is nothing usable (not there, not a plain file,
    too big, not JSON, another schema)."""
    cat = BY_ID.get(cat_id)
    if cat is None:
        return None
    path = _file(cat_id)
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = json.loads(f.read(MAX_CACHE_BYTES + 1))
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        os.close(fd)
    fetched = data.get("fetched") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or isinstance(fetched, bool) or not isinstance(fetched, (int, float)):
        return None
    try:
        when = float(fetched)
    except (OverflowError, ValueError):  # a number with hundreds of digits
        return None
    if not math.isfinite(when) or not 0 <= when <= time.time() + 86400:  # NaN, infinity or a date in the future would keep a copy "recent" for ever
        return None
    return {"records": cat.module.clean(data.get("records")), "fetched": when}


def records(cat_id: str) -> list[dict[str, Any]]:
    """The catalogue's programs, when it is on and has been downloaded; otherwise nothing."""
    if cat_id not in BY_ID or not is_on(cat_id):
        return []
    got = stored(cat_id)
    return got["records"] if got else []


def stamps() -> list[list[Any]]:
    """What the lists in use are made of, for telling when the merged lists must be built again: each catalogue that is on and downloaded."""
    out = []
    for cat in CATALOGUES:
        if not is_on(cat.id):
            continue
        try:
            st = os.lstat(_file(cat.id))
        except OSError:
            continue
        out.append([cat.id, st.st_size, st.st_mtime_ns, st.st_ino])
    return out


def status(now: float | None = None) -> list[dict[str, Any]]:
    """For the window: each catalogue, whether it is on, how many programs it has, when it was downloaded, and whether that is too long ago."""
    now = time.time() if now is None else now
    rows = []
    for cat in CATALOGUES:
        got = stored(cat.id)
        fetched = got["fetched"] if got else 0.0
        rows.append({"id": cat.id, "label": cat.label, "what": cat.what, "size": cat.size, "on": is_on(cat.id), "count": len(got["records"]) if got else 0,
                     "fetched": fetched, "stale": got is None or now - fetched > STALE_SECONDS})
    return rows


def needs_download(now: float | None = None) -> list[str]:
    """The catalogues that are on and have never been downloaded or are older than a week; none before the person has said that Discover may download."""
    if not decided():
        return []
    return [r["id"] for r in status(now) if r["on"] and r["stale"]]


def refresh(cat_id: str, progress: Callable[[str], None] = lambda _: None, *, fetch: Callable[..., list[dict[str, Any]]] | None = None,
            now: float | None = None) -> int:
    """Download the catalogue (even when it is off: the person asked for it) and keep it. Returns how many programs it lists. A failed download
    leaves the copy that was there."""
    cat = BY_ID.get(cat_id)
    if cat is None:
        raise CygnusError("that is not one of the online catalogues")
    recs = cat.module.clean((fetch or cat.module.fetch)(progress))
    if not recs:
        raise CygnusError(f"{cat.label} listed no programs, so Cygnus kept the copy it had")
    payload = json.dumps({"schema": SCHEMA, "fetched": time.time() if now is None else now, "records": recs}, separators=(",", ":")).encode()
    if len(payload) > MAX_CACHE_BYTES:
        raise CygnusError(f"{cat.label}'s list is larger than Cygnus will keep")
    folder = _folder()
    for part in (folder.parent, folder):
        if part.is_symlink() or (part.exists() and not part.is_dir()):
            raise CygnusError(f"{part} is not a plain folder, so Cygnus will not put files there")
    atomic_write(_file(cat_id), payload)
    return len(recs)
