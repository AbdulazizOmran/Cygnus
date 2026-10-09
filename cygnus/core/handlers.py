"""Making Cygnus the program that opens what it installs: Flathub's Install button (.flatpakref files and appstream: links),
Flatpak bundles, .deb, .rpm and AppImage files.

This changes one thing, your own list of default applications (`~/.config/mimeapps.list`, through `xdg-mime`), and nothing
system-wide. The previous handler of each type is remembered, so turning it off puts back what you had (Shelly, Discover, ...).
`.flatpakrepo` files are left out on purpose: Cygnus only explains those, so it must not become the program that opens them."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from cygnus.core import preferences
from cygnus.core.errors import CygnusError
from cygnus.core.util import proc
from cygnus.core.util.fs import atomic_write

DESKTOP_ID = "io.github.omranabdulaziz.Cygnus.desktop"
MIME_TYPES = ("x-scheme-handler/appstream", "x-scheme-handler/flatpak+https", "application/vnd.flatpak.ref", "application/vnd.flatpak",
              "application/vnd.debian.binary-package", "application/x-rpm", "application/vnd.appimage",
              "application/x-iso9660-appimage")


def _env() -> dict[str, str]:
    """The variables xdg-mime needs to look in the right place. The process helper passes very little of the
    environment on purpose, and without these a custom config folder (or a test's) would be ignored."""
    return {k: os.environ[k] for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_CURRENT_DESKTOP",
                                        "XDG_CONFIG_DIRS") if k in os.environ}


def _desktop_file_installed() -> bool:
    dirs = [os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share"),
            *(os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]
    return any((Path(d) / "applications" / DESKTOP_ID).is_file() for d in dirs if d)


def _query(mime: str, run) -> str:
    """The program that opens `mime` for this user, as GLib sees it (what Firefox and most programs ask). `xdg-mime query`
    is not used for this: on KDE it answers from the menu cache and does not look at mimeapps.list."""
    first = run(["gio", "mime", mime], timeout=20, env=_env()).stdout.splitlines()[:1]
    m = re.match(r"Default application for .*?:\s*(\S+\.desktop)\s*$", first[0]) if first else None
    return m.group(1) if m else ""


def status(run=proc.run) -> dict:
    """{"available", "why", "enabled", "current": {mime: desktop id}}: whether Cygnus is the default for every type."""
    missing = [tool for tool in ("xdg-mime", "gio") if shutil.which(tool) is None]
    if missing:
        return {"available": False, "why": f"{' and '.join(missing)} (packages xdg-utils and glib2) not installed",
                "enabled": False, "current": {}}
    if not _desktop_file_installed():
        return {"available": False, "why": "Cygnus's menu entry is not installed, so it cannot be chosen",
                "enabled": False, "current": {}}
    current = {m: _query(m, run) for m in MIME_TYPES}
    return {"available": True, "why": "", "enabled": all(v == DESKTOP_ID for v in current.values()), "current": current}


def enable(run=proc.run) -> dict:
    """Make Cygnus the default for every type, remembering what each one was (only the first time)."""
    state = status(run)
    if not state["available"]:
        raise CygnusError(state["why"])
    prefs = preferences.load()
    backup = dict(prefs.get("handler_backup") or {})
    for mime in MIME_TYPES:
        current = state["current"][mime]
        if current != DESKTOP_ID and mime not in backup:
            backup[mime] = current  # "" when nothing was set
    prefs["handler_backup"] = backup
    preferences.save(prefs)  # remembered before anything changes
    for mime in MIME_TYPES:
        run(["xdg-mime", "default", DESKTOP_ID, mime], timeout=20, env=_env())
    after = status(run)
    stuck = [m for m, v in after["current"].items() if v != DESKTOP_ID]
    if stuck:
        raise CygnusError("these could not be set: " + ", ".join(stuck) + ". Either another program keeps resetting them, "
                          "or the installed Cygnus menu entry does not claim these file types (an older version of Cygnus: "
                          "update it).")
    return after


def _forget_default(mime: str) -> None:
    """Remove Cygnus from the default list of `mime` in mimeapps.list (xdg-mime can set a default but not clear one)."""
    home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = (home / "mimeapps.list").resolve()  # a file kept as a link into someone's dotfiles stays a link
    try:
        # surrogateescape: a stray non-UTF-8 byte in a comment is written back unchanged instead of stopping the switch
        lines = path.read_bytes().decode(errors="surrogateescape").splitlines()
    except OSError:
        return
    out, section, changed = [], "", False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
        key, _, value = stripped.partition("=")
        if section == "[Default Applications]" and key.strip() == mime:
            kept = [v.strip() for v in value.split(";") if v.strip() and v.strip() != DESKTOP_ID]
            if kept:
                out.append(f"{key.strip()}={';'.join(kept)};")
            changed = True
            continue
        out.append(line)
    if changed:
        atomic_write(path, ("\n".join(out) + "\n").encode(errors="surrogateescape"))


def disable(run=proc.run) -> dict:
    """Put back what each type was opened by before. A type someone else has changed since is left as they set it."""
    missing = [tool for tool in ("xdg-mime", "gio") if shutil.which(tool) is None]
    if missing:  # without them the current state cannot be read, and nothing could be put back
        raise CygnusError(f"{' and '.join(missing)} (packages xdg-utils and glib2) not installed")
    prefs = preferences.load()
    backup = prefs.get("handler_backup") or {}
    for mime in MIME_TYPES:
        if _query(mime, run) != DESKTOP_ID:
            continue
        previous = backup.get(mime, "")
        if previous:
            run(["xdg-mime", "default", previous, mime], timeout=20, env=_env())
        else:
            _forget_default(mime)
    prefs.pop("handler_backup", None)
    preferences.save(prefs)
    return status(run)
