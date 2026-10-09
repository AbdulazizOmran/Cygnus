"""A text report to paste into a bug report: versions, tools, where things stand. Nothing here changes anything, it never
starts the root helper, and it leaves out what is private: the user name and the home folder are replaced, and no application
names, file names or command lines are listed (only counts)."""

from __future__ import annotations

import os
import platform
import pwd
import re
import sys
from pathlib import Path

from cygnus import APP_ID, __version__
from cygnus.core.util import proc

TOOLS = {
    "pacman": "pacman", "pacman-conf": "pacman", "flatpak": "flatpak", "ostree": "ostree", "unsquashfs": "squashfs-tools",
    "zsync": "zsync", "bsdtar": "libarchive", "desktop-file-validate": "desktop-file-utils", "kbuildsycoca6": "kservice",
    "update-desktop-database": "desktop-file-utils", "fakeroot": "fakeroot", "makepkg": "pacman", "gio": "glib2",
    "xdg-mime": "xdg-utils", "gpg": "gnupg",
}
MODULES = ("pyalpm", "gi", "cryptography", "systemd.journal", "PySide6", "requests", "pydantic")
PATHS = {
    "helper bus service": "/usr/share/dbus-1/system-services/io.github.omranabdulaziz.Cygnus.Helper1.service",
    "polkit policy": "/usr/share/polkit-1/actions/io.github.omranabdulaziz.Cygnus.policy",
    "helper bus policy": "/usr/share/dbus-1/system.d/io.github.omranabdulaziz.Cygnus.Helper1.conf",
    "desktop entry": "/usr/share/applications/io.github.omranabdulaziz.Cygnus.desktop",
}


def _first_line(argv: list[str]) -> str:
    try:
        res = proc.run(argv, timeout=15)
    except Exception as exc:  # noqa: BLE001 - a report must never fail because one tool does
        return f"unavailable ({type(exc).__name__})"
    lines = [ln.strip() for ln in (res.stdout or res.stderr).splitlines() if ln.strip()]
    return lines[0] if lines else "no output"


def _pacman_version() -> str:
    if not proc.which("pacman"):
        return "missing"
    try:
        found = re.search(r"Pacman v[0-9.]+ - libalpm v[0-9.]+", proc.run(["pacman", "--version"], timeout=15).stdout)
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({type(exc).__name__})"
    return found.group(0) if found else "unknown version"


def _os_release() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def redact(text: str) -> str:
    """Replace the home folder and the user name wherever they appear."""
    home = str(Path.home())
    user = os.environ.get("USER") or ""
    try:
        user = user or pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        pass
    if home and home != "/":
        text = text.replace(home, "~")
    if user and len(user) > 2:
        text = re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(user)}(?![A-Za-z0-9_-])", "<user>", text)
    return text


def _section(title: str, rows: list[str]) -> str:
    return f"## {title}\n" + "\n".join(rows) + "\n"


def build(registry_path: str | None = None) -> str:
    parts = [f"# Cygnus report\nCygnus {__version__} ({APP_ID})\n"]
    qt = "not installed"
    try:
        import PySide6
        from PySide6 import QtCore

        qt = f"PySide6 {PySide6.__version__}, Qt {QtCore.qVersion()}"
    except Exception as exc:  # noqa: BLE001
        qt = f"unavailable ({type(exc).__name__})"
    parts.append(_section("System", [
        f"system: {_os_release()}", f"kernel: {platform.release()}", f"python: {sys.version.split()[0]}", f"qt: {qt}",
        f"desktop: {os.environ.get('XDG_CURRENT_DESKTOP', '?')} / {os.environ.get('XDG_SESSION_TYPE', '?')}",
        f"pacman: {_pacman_version()}",
        f"package: {_first_line(['pacman', '-Q', 'cygnus']) if proc.which('pacman') else 'unknown'}",
        f"flatpak: {_first_line(['flatpak', '--version']) if proc.which('flatpak') else 'missing'}",
    ]))
    parts.append(_section("Tools", [f"{tool:<24} {'ok' if proc.which(tool) else 'MISSING (package ' + pkg + ')'}"
                                    for tool, pkg in TOOLS.items()]))
    rows = []
    for module in MODULES:
        try:
            __import__(module)
            rows.append(f"{module:<24} ok")
        except Exception as exc:  # noqa: BLE001
            rows.append(f"{module:<24} MISSING ({type(exc).__name__})")
    parts.append(_section("Python modules", rows))
    rows = [f"{name:<24} {'present' if Path(path).exists() else 'MISSING'}" for name, path in PATHS.items()]
    try:
        reader = pwd.getpwnam("cygnus-reader")
        rows.append(f"{'reader account':<24} present (uid {reader.pw_uid}, shell {reader.pw_shell})")
    except KeyError:
        rows.append(f"{'reader account':<24} MISSING (the package's sysusers file creates it)")
    parts.append(_section("Installation", rows))
    parts.append(_section("Your setup", _setup(registry_path)))
    parts.append("## Helper log\nIf the problem involved administrator actions, add the output of: "
                 "journalctl -t cygnus-helper -n 50\n")
    return redact("\n".join(parts))


def _setup(registry_path: str | None) -> list[str]:
    rows: list[str] = []
    try:
        from cygnus.core import handlers, updates
        from cygnus.core.registry import db as regdb
        from cygnus.core.registry import open_registry

        reg = open_registry(registry_path)
        installs = regdb.list_installations(reg)
        by_format: dict[str, int] = {}
        for row in installs:
            by_format[row["format"]] = by_format.get(row["format"], 0) + 1
        rows.append("managed installations: " + (", ".join(f"{n} {fmt}" for fmt, n in sorted(by_format.items())) or "none"))
        converted = sum(1 for row in installs if updates.is_converted(row))
        if converted:
            rows.append(f"  of the pacman ones, converted from a .deb/.rpm: {converted}")
        statuses: dict[str, int] = {}
        for st in updates.last_known(reg):
            statuses[st.status] = statuses.get(st.status, 0) + 1
        rows.append("last known update states: " + (", ".join(f"{n} {s}" for s, n in sorted(statuses.items())) or "none"))
        for loc in reg.list_locations():
            rows.append(f"storage: class {loc.location_class}, filesystem {loc.fs_type or '?'}"
                        + (", default" if loc.is_default else ""))
        rows.append(f"interrupted operations: {len(reg.conn.execute('SELECT id FROM operation WHERE state IN (?, ?)', ('running', 'interrupted')).fetchall())}")
        state = handlers.status()
        rows.append("default program for Flathub links and software files: "
                    + ("Cygnus" if state["enabled"] else ("not set" if state["available"] else f"unavailable ({state['why']})")))
    except Exception as exc:  # noqa: BLE001 - say what could not be read, never fail
        rows.append(f"could not read Cygnus's own data: {type(exc).__name__}: {exc}")
    return rows
