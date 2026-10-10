"""What will protect you if a system update goes wrong: is a snapshot taken, and by what?

This only LOOKS (files and settings; nothing is run, nothing is changed) and says so in words. Cygnus does not take snapshots
itself:
- snap-pac (Snapper's pacman hooks) snapshots around every pacman transaction, the helper's included, so there is nothing to add;
  this is judged from the hook files pacman will really use (a same-named file in a hook folder replaces the shipped one, and a link to
  /dev/null switches it off) and from snap-pac's own settings. It is said with "normally": a hook can still fail or be removed later.
- Snapper without snap-pac, and Timeshift, are named, with what to do by hand before updating. Cygnus does not run either of them.
"""

from __future__ import annotations

import configparser
import os
import shutil
from typing import Any

SHIPPED_HOOKS = "usr/share/libalpm/hooks"
SNAP_PAC_HOOKS = ("05-snap-pac-pre.hook", "zz-snap-pac-post.hook")
SNAP_PAC_INI = "etc/snap-pac.ini"
SNAPPER_CONF = "etc/conf.d/snapper"  # snap-pac snapshots the configurations listed in SNAPPER_CONFIGS there (and only those)
SNAPPER_CONFIGS = "etc/snapper/configs"
_OFF = {"false", "no", "off", "0"}


def _under(root: str, path: str) -> str:
    return os.path.join(root, path.lstrip("/"))


def _hook_state(name: str, hookdirs: tuple[str, ...], root: str) -> str:
    """"shipped" (the package's own hook will run), "replaced" (another file of that name takes its place), "disabled" (a link to
    /dev/null takes its place), or "missing"."""
    for folder in (*hookdirs, "/" + SHIPPED_HOOKS):
        path = os.path.join(_under(root, folder), name)
        if not os.path.lexists(path):
            continue
        if os.path.islink(path) and (os.readlink(path) == "/dev/null" or os.path.realpath(path) == "/dev/null"):
            return "disabled"
        return "shipped" if os.path.dirname(path) == _under(root, "/" + SHIPPED_HOOKS) else "replaced"
    return "missing"


def _snap_pac_snapshots_root(root: str) -> bool | None:
    """Does snap-pac's own configuration snapshot the `root` Snapper configuration? snap-pac sets root's `snapshot` to true in its own code
    and then reads the ini, so only a `snapshot` line in the ini's [root] section can turn it off (a [DEFAULT] section never reaches root).
    True by default; None when the file cannot be read."""
    path = _under(root, SNAP_PAC_INI)
    if not os.path.exists(path):
        return True
    parser = configparser.ConfigParser(interpolation=None, strict=False, default_section="\0unused")  # [DEFAULT] is then an ordinary section
    try:
        parser.read(path)
    except (configparser.Error, OSError, UnicodeDecodeError):
        return None
    value = parser["root"].get("snapshot") if parser.has_section("root") else None
    return True if value is None else value.strip().lower() not in _OFF


def _snapper_configs(root: str) -> list[str] | None:
    """The Snapper configurations snap-pac works through, read the way snap-pac reads them (the line that STARTS with SNAPPER_CONFIGS in
    /etc/conf.d/snapper). None when the file cannot be read or has no such line: snap-pac then fails (its hook exits with an error, and
    pacman does not go on after a failing pre hook)."""
    try:
        text = open(_under(root, SNAPPER_CONF), encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.split("\n"):
        if line.startswith("SNAPPER_CONFIGS"):
            return line.rstrip("\n").rstrip('"').split("=")[-1].lstrip('"').split()
    return None


def _installed(program: str, root: str) -> bool:
    if root == "/":
        return shutil.which(program) is not None
    return any(os.path.exists(os.path.join(_under(root, d), program)) for d in ("usr/bin", "bin", "usr/sbin"))


def _snap_pac(states: dict[str, str], root: str) -> dict[str, Any]:
    pre, post = states[SNAP_PAC_HOOKS[0]], states[SNAP_PAC_HOOKS[1]]
    if pre in ("disabled", "missing"):
        return {"state": "snap-pac-off", "text": ("snap-pac is installed but its snapshot hook is switched off here, so no snapshot is "
                                                  "taken before this update. Take one yourself first if you want one.")}
    ini = _snap_pac_snapshots_root(root)
    configs = _snapper_configs(root)
    if ini is None:
        return {"state": "snap-pac-unclear", "text": ("snap-pac is installed, but its settings file (/etc/snap-pac.ini) cannot be read, so "
                                                      "Cygnus cannot tell whether a snapshot is taken before this update (snap-pac reads that file "
                                                      "too, and may fail on it). Take one yourself first if you want to be sure.")}
    if ini is False:
        return {"state": "snap-pac-off", "text": ("snap-pac is installed but its settings (/etc/snap-pac.ini) say not to snapshot the root "
                                                  "configuration, so none is taken before this update. Take one yourself first if you want one.")}
    if configs is None:
        return {"state": "snap-pac-unclear", "text": ("snap-pac is installed, but Snapper's list of configurations (/etc/conf.d/snapper) cannot be "
                                                      "read or has no SNAPPER_CONFIGS line. snap-pac needs that line and fails without it, and then "
                                                      "pacman may refuse to change anything. Cygnus cannot tell what will happen; fix that file, or "
                                                      "take a snapshot yourself first if you want one.")}
    if "root" not in configs:
        return {"state": "snap-pac-off", "text": ("snap-pac is installed, but Snapper's configurations (/etc/conf.d/snapper) do not include "
                                                  "\"root\", so no snapshot of the system is taken before this update. Take one yourself first "
                                                  "if you want one.")}
    note = " (its hook has been replaced by one of your own, which Cygnus cannot judge)" if pre == "replaced" else ""
    when = ("before and after every package change" if post not in ("disabled", "missing")
            else "before every package change (its \"after\" hook is switched off)")
    return {"state": "snap-pac", "text": (f"Snapper's snap-pac normally takes a snapshot {when}, including this update{note}. Cygnus adds none of "
                                          "its own. (If snapper itself fails, snap-pac does not notice, and the update goes on without a snapshot.)")}


def situation(hookdirs: tuple[str, ...] = ("/etc/pacman.d/hooks/",), *, root: str = "/") -> dict[str, Any]:
    """{"state": "snap-pac" | "snap-pac-off" | "snap-pac-unclear" | "snapper" | "timeshift" | "none", "text"} for the computer at `root`.
    snap-pac counts as installed only when the hook its package ships is there (a file of the same name in a hook folder of your own
    proves nothing about it)."""
    states = {name: _hook_state(name, hookdirs, root) for name in SNAP_PAC_HOOKS}
    if os.path.exists(os.path.join(_under(root, "/" + SHIPPED_HOOKS), SNAP_PAC_HOOKS[0])):
        return _snap_pac(states, root)
    if _installed("snapper", root):
        has_config = os.path.isdir(_under(root, SNAPPER_CONFIGS)) and any(True for _ in os.scandir(_under(root, SNAPPER_CONFIGS)))
        what = ("Take one yourself before updating (for example `sudo snapper create --description \"before update\"`), or install snap-pac "
                "so that one is taken every time." if has_config else
                "Snapper has no configuration yet (see `snapper create-config`), so there is nothing to snapshot with.")
        return {"state": "snapper", "text": "Snapper is installed, but nothing takes a snapshot before a package change (snap-pac is not installed). " + what}
    if _installed("timeshift", root):
        return {"state": "timeshift", "text": ("Timeshift is installed. Cygnus does not run it: take a snapshot with Timeshift yourself before "
                                               "updating.")}
    return {"state": "none", "text": "No snapshot tool was found (Snapper, snap-pac or Timeshift), so there is nothing to roll back to if an update goes wrong."}
