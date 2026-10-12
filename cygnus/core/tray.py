"""A small icon in the system tray that says how many updates are waiting (optional, off until you switch it on in Settings).

The icon is a separate little program (`cygnus-tray`) that starts when you log in and does one thing: look at what the last update checks found
(the stored results only: it never goes to the network, never installs anything and never starts the root helper), and show the icon only while
there is something to update. Clicking it opens Cygnus. The checks themselves are still made by the background timer (`cygnus watch`) or by
the Updates page.

This module is the part that has no window: what is pending, how it is described, and switching it on and off. Switching on writes a menu-style
autostart entry in your own config folder (`~/.config/autostart`) and remembers the choice in the preferences; switching off removes both, and
the running icon sees that and goes away."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cygnus import APP_ID
from cygnus.core import paths, preferences
from cygnus.core.desktop import entry as desktop_entry
from cygnus.core.desktop import integrate
from cygnus.core.errors import CygnusError
from cygnus.core.util.fs import atomic_write

AUTOSTART_NAME = f"{APP_ID}.Tray.desktop"
PREFERENCE = "tray"
ICON = "system-software-update"
MAX_CACHE_BYTES = 8 * 1024 * 1024  # of the stored system-update result
MAX_NAMES = 5  # program names listed in the tooltip
PACMAN_LOCAL = Path("/var/lib/pacman/local")  # changes whenever a package is installed, upgraded or removed


def autostart_path() -> Path:
    return integrate.xdg_config_home() / "autostart" / AUTOSTART_NAME


def _system_count() -> tuple[int, bool]:
    """(how many system updates the last check found, whether one is a kernel). A check that failed, or none yet, is no updates."""
    from cygnus.core import updates

    path = updates.system_updates_cache()
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return 0, False
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0, False
    if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("packages"), list):
        return 0, False
    if _changed_since(data.get("checked_at")):  # packages were installed or upgraded after the check: its list is out of date, and saying so would be wrong
        return 0, False
    return len(data["packages"]), bool(data.get("kernel"))


def _changed_since(checked_at: Any) -> bool:
    """Did pacman's installed-packages database change after the update check at `checked_at` ("2026-10-12T14:00:00Z")? An unreadable time or
    folder says no (the count is then shown, as the last check found it)."""
    try:
        checked = datetime.strptime(str(checked_at), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        return PACMAN_LOCAL.stat().st_mtime > checked
    except (ValueError, OSError):
        return False


def pending(registry_path: str | None = None) -> dict[str, Any]:
    """What the icon shows: {"programs", "system", "kernel", "total", "names"} from the results already stored (no network). A program
    whose check has not run, or failed, is not counted: the icon only says what is known."""
    from cygnus.core import updates
    from cygnus.core.registry import open_registry

    try:  # read-only: a poll every minute must never migrate, back up or write the registry
        names = [s.name for s in updates.last_known(open_registry(registry_path, migrate=False)) if s.status == updates.AVAILABLE]
    except (CygnusError, OSError, ValueError, sqlite3.Error):
        names = []
    system, kernel = _system_count()
    return {"programs": len(names), "system": system, "kernel": kernel, "total": len(names) + system, "names": names[:MAX_NAMES]}


def describe(state: dict[str, Any]) -> dict[str, Any]:
    """{"visible", "tooltip", "icon"} for a `pending()` result: the icon is shown only while something waits."""
    total = state["total"]
    if not total:
        return {"visible": False, "tooltip": "Cygnus: everything is up to date", "icon": ICON}
    parts = []
    if state["programs"]:
        listed = ", ".join(state["names"]) + (" and more" if state["programs"] > len(state["names"]) else "")
        parts.append(f"{state['programs']} {'program' if state['programs'] == 1 else 'programs'} ({listed})")
    if state["system"]:
        parts.append(f"{state['system']} system {'package' if state['system'] == 1 else 'packages'}" + (", including a new kernel" if state["kernel"] else ""))
    return {"visible": True, "tooltip": f"{total} {'update' if total == 1 else 'updates'} waiting: " + "; ".join(parts) + ". Click to open Cygnus.", "icon": ICON}


# -- switching it on and off -------------------------------------------------------------------------------------------------------------
def command() -> list[str]:
    """How to start the icon: the installed `cygnus-tray`, or this Python running the module (a checkout)."""
    found = shutil.which("cygnus-tray")
    if found:
        return [found]
    root = Path(__file__).resolve().parent.parent.parent  # a checkout: the folder that holds the cygnus package; a login session does not start there
    return ["env", f"PYTHONPATH={root}", sys.executable, "-m", "cygnus.gui.tray"]


def autostart_text(argv: list[str] | None = None) -> str:
    argv = argv or command()
    de = desktop_entry.DesktopEntry()
    de.set_raw("Type", "Application")
    de.set("Name", "Cygnus update icon")
    de.set("Comment", "Shows an icon in the system tray while updates are waiting")
    de.set("Exec", " ".join(desktop_entry.exec_program(a) for a in argv))
    de.set("Icon", ICON)
    de.set_raw("Terminal", "false")
    de.set_raw("NoDisplay", "true")
    de.set_raw("X-KDE-autostart-after", "panel")
    de.set_raw("X-Cygnus-Managed", "true")
    return de.serialize()


def wanted() -> bool:
    return preferences.load().get(PREFERENCE) is True


def status() -> dict[str, Any]:
    """{"enabled" (chosen and set up), "entry" (the autostart file is there), "why" (when the choice and the file disagree)}."""
    entry_there = autostart_path().is_file()
    chosen = wanted()
    why = ""
    if chosen and not entry_there:
        why = "It is switched on, but its start-up entry is missing: switch it off and on again."
    elif entry_there and not chosen:
        why = "A start-up entry exists but the icon is switched off: it quits as soon as it starts."
    return {"enabled": chosen and entry_there, "entry": entry_there, "why": why}


def enable(*, start: bool = True, spawn=None) -> dict[str, Any]:
    """Switch the icon on: write the start-up entry (only ever in your own autostart folder), remember the choice, and start it now."""
    path = autostart_path()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CygnusError(f"{path} is not a plain file, so Cygnus will not write the tray icon's start-up entry there")
    argv = command()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, autostart_text(argv).encode())
    except OSError as exc:
        raise CygnusError(f"the start-up entry could not be written ({exc.strerror or exc})") from exc
    prefs = preferences.load()
    prefs[PREFERENCE] = True
    preferences.save(prefs)
    if start:
        try:
            (spawn or subprocess.Popen)(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except OSError as exc:
            raise CygnusError(f"the icon is switched on and will start at your next login, but it could not be started now ({exc.strerror or exc})") from exc
    return status()


def disable() -> dict[str, Any]:
    """Switch it off: the choice is forgotten and the start-up entry removed; a running icon notices within a minute and goes away. The entry
    is removed only if it is Cygnus's own (a file of that name that is not is left alone)."""
    prefs = preferences.load()
    prefs[PREFERENCE] = False
    preferences.save(prefs)
    path = autostart_path()
    if path.is_file():  # a link counts through to the file it leads to: the marker is read there
        try:
            marked = desktop_entry.parse(path.read_text(errors="replace")[:65536]).get("X-Cygnus-Managed") == "true"
        except OSError:
            marked = False
        if marked:
            path.unlink(missing_ok=True)  # the file, or the link itself: never what a link points at
    return status()
