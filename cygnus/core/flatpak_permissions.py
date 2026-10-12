"""What an installed Flatpak application is allowed to do, in plain words (read-only: nothing here changes a permission).

A Flatpak runs in a sandbox, and what it may reach outside it is written in its own metadata (the `[Context]` and bus-policy groups); a person
or a tool such as Flatseal or `flatpak override` can add to that or take from it, in override files. This module reads the metadata through
libflatpak and the override files, puts them in the order Flatpak does (the application's own first, then the system-wide overrides, then yours; an
item written `!name` takes that item away), and describes the result, marking what lets a program leave the sandbox or reach a great deal.

The override files are read as the person's own files (a link into a dotfiles folder is followed), but only as regular files and only up to a size
limit. Nothing is run and nothing is written."""

from __future__ import annotations

import configparser
import os
import stat
from pathlib import Path
from typing import Any

from cygnus.core.errors import CygnusError

MAX_BYTES = 256 * 1024  # of one metadata or override file
MAX_ITEMS = 400  # entries kept per file: what a sane file holds is a few dozen
MAX_GROUPS = 400  # groups kept per file (an application with many extensions has a group for each)
LIST_KEYS = ("shared", "sockets", "devices", "features", "filesystems")
LEVEL_ORDER = {"escape": 0, "broad": 1, "normal": 2}

GROUPS = [("network", "Internet and other programs"), ("display", "Windows and sound"), ("devices", "Devices"), ("files", "Files"),
          ("system", "Other access"), ("bus", "Programs it can talk to"), ("env", "Settings it changes")]
GROUP_TITLES = dict(GROUPS)

# (text, level) for what the permission lets the program do. A level is "escape" (it can leave the sandbox: it amounts to the access of a
# normal program), "broad" (it reaches a great deal, or a lot of other programs' business) or "normal".
_SHARED = {"network": ("Use the internet and your local network", "normal"),
           "ipc": ("Share memory with other programs on the same display (older X11 programs need it)", "normal")}
_SOCKETS = {"x11": ("Show windows through X11, the older display system (a program there can watch and control other X11 programs)", "broad"),
            "fallback-x11": ("Use X11 only when Wayland is not available", "normal"),
            "wayland": ("Show windows through Wayland", "normal"),
            "pulseaudio": ("Play sound and use the microphone", "normal"),
            "session-bus": ("Talk to everything on your session's message bus, which includes starting other programs", "escape"),
            "system-bus": ("Talk to everything on the system's message bus", "escape"),
            "ssh-auth": ("Use your SSH keys (it cannot read them, but it can sign with them)", "broad"),
            "gpg-agent": ("Use your GPG keys", "broad"),
            "pcsc": ("Use smart cards", "broad"),
            "cups": ("Print", "normal"),
            "inherit-wayland-socket": ("Use the Wayland connection of the program that starts it", "normal")}
_DEVICES = {"dri": ("Use the graphics card (3D drawing, video)", "normal"), "all": ("Use every device in /dev", "broad"),
            "kvm": ("Use hardware virtualisation (for virtual machines)", "broad"), "shm": ("Use shared memory", "normal"),
            "input": ("Use game controllers and other input devices", "normal"), "usb": ("Use any USB device", "broad")}
_FEATURES = {"devel": ("Debugging features, which weaken the sandbox", "broad"), "multiarch": ("Run 32-bit programs", "normal"),
             "bluetooth": ("Use Bluetooth", "normal"), "canbus": ("Use CAN bus adapters", "normal"),
             "per-app-dev-shm": ("Its own shared memory", "normal")}
_FOLDERS = {"xdg-desktop": "Desktop", "xdg-documents": "Documents", "xdg-download": "Downloads", "xdg-music": "Music",
            "xdg-pictures": "Pictures", "xdg-public-share": "Public", "xdg-templates": "Templates", "xdg-videos": "Videos"}
_RUNTIME = {"pipewire-0": "The sound and video server's connection (PipeWire)", "speech-dispatcher": "The text-to-speech service's connection",
            "gvfs": "Your drives and network folders as the file manager sees them", "gvfsd": "Your drives and network folders as the file manager sees them",
            "dconf": "Your desktop settings database"}
_BUS_NAMES = {"org.kde.StatusNotifierWatcher": "Put an icon in the system tray", "org.freedesktop.Notifications": "Show notifications",
              "org.a11y.Bus": "Work with screen readers and other accessibility tools", "com.canonical.AppMenu.Registrar": "Show its menu in the global menu",
              "org.freedesktop.ScreenSaver": "Keep the screen from locking", "org.freedesktop.secrets": "Read and store passwords in your wallet",
              "org.kde.kwalletd5": "Read and store passwords in your wallet", "org.kde.kwalletd6": "Read and store passwords in your wallet",
              "org.mpris.MediaPlayer2": "Be controlled by media keys", "org.freedesktop.portal.Fcitx": "Use the input method",
              "org.freedesktop.Flatpak": "Start programs outside the sandbox", "org.freedesktop.portal.Flatpak": "Start programs outside the sandbox",
              "org.freedesktop.systemd1": "Start and stop system services and programs", "org.freedesktop.PolicyKit1": "Ask for administrator rights"}
_ESCAPE_BUS = ("org.freedesktop.Flatpak", "org.freedesktop.portal.Flatpak", "org.freedesktop.systemd1", "org.freedesktop.PolicyKit1")
_SYSTEM_PREFIXES = ("/usr", "/etc", "/var", "/boot", "/dev", "/proc", "/sys", "/bin", "/sbin", "/lib", "/opt", "/root")  # not /run: USB drives are there


def _clean(text: str, limit: int = 160) -> str:
    """Text from a file that is not ours, safe to show."""
    return "".join(c if c.isprintable() else " " for c in text).strip()[:limit]


def parse_keyfile(text: str) -> dict[str, dict[str, str]]:
    """{group: {key: value}} of a keyfile; text that is not one gives {} (a damaged file must not stop the page)."""
    parser = configparser.ConfigParser(interpolation=None, strict=False, default_section="\x00no-default")  # GKeyFile has no inheriting [DEFAULT] group
    parser.optionxform = str  # keys are case-sensitive (environment variable names)
    try:
        parser.read_string(text[:MAX_BYTES])
    except configparser.Error:
        return {}
    out: dict[str, dict[str, str]] = {}
    for group in parser.sections()[:MAX_GROUPS]:
        out[group] = dict(list(parser.items(group))[:MAX_ITEMS])
    return out


def _split(value: str) -> list[str]:
    return [x.strip() for x in value.split(";") if x.strip()][:MAX_ITEMS]


def _canon_fs(name: str) -> str:
    """One spelling for one place: "~", "/home/you" and "home" are the home folder, "~/.config/x" and "/home/you/.config/x" are "xdg-config/x"."""
    n = name.rstrip("/") or "/"
    home = os.path.expanduser("~").rstrip("/")
    if n == "~" or (home and n == home):
        return "home"
    if n.startswith("~/"):
        rest = n[2:]
    elif home and n.startswith(home + "/"):
        rest = n[len(home) + 1:]
    else:
        return n
    for prefix, key in ((".config", "xdg-config"), (".local/share", "xdg-data"), (".cache", "xdg-cache")):
        if rest == prefix or rest.startswith(prefix + "/"):
            return key + rest[len(prefix):]
    return "~/" + rest


def _fs_name(item: str) -> tuple[str, str]:
    name, _, mode = item.rpartition(":")
    return (name, mode) if name and mode in ("ro", "rw", "create", "reset") else (item, "")


# -- putting the layers in order ---------------------------------------------------------------------------------------------------------
def merge(layers: list[tuple[str, dict[str, dict[str, str]]]]) -> dict[str, Any]:
    """The permissions after every layer, first layer first ((source, parsed keyfile), source "app" for the application's own and "you" for
    an override). -> {"lists": {key: {item: (mode, source)}}, "removed": [(key, item, source)], "bus": {"session"|"system": {name: (policy,
    source)}}, "env": {name: (value, source)}}. An item written "!name" takes that item away; a later layer overrides an earlier one."""
    lists: dict[str, dict[str, tuple[str, str]]] = {k: {} for k in LIST_KEYS}
    removed: list[tuple[str, str, str]] = []
    bus: dict[str, dict[str, tuple[str, str]]] = {"session": {}, "system": {}}
    env: dict[str, tuple[str, str]] = {}
    for source, data in layers:
        context = data.get("Context", {})
        for key in LIST_KEYS:
            for item in _split(context.get(key, "")):
                if item.startswith("!"):
                    name, mode = _fs_name(item[1:]) if key == "filesystems" else (item[1:], "")
                    if key == "filesystems" and mode == "reset":  # "!host:reset": every filesystem permission that came before is dropped
                        removed.extend(("filesystems", n, source) for n in lists[key])
                        lists[key].clear()
                        continue
                    name = _canon_fs(name) if key == "filesystems" else name
                    if name in lists[key]:
                        removed.append((key, name, source))
                        del lists[key][name]
                    continue
                name, mode = _fs_name(item) if key == "filesystems" else (item, "")
                lists[key][_canon_fs(name) if key == "filesystems" else name] = (mode, source)
        for kind, group in (("session", "Session Bus Policy"), ("system", "System Bus Policy")):
            for name, policy in list(data.get(group, {}).items())[:MAX_ITEMS]:
                policy = policy.strip().lower()
                if policy == "none":
                    if name in bus[kind]:
                        removed.append((kind + "-bus", name, source))
                        del bus[kind][name]
                elif policy in ("see", "talk", "own"):
                    bus[kind][name] = (policy, source)
        for name, value in list(data.get("Environment", {}).items())[:MAX_ITEMS]:
            env[name] = (value, source)
    return {"lists": lists, "removed": removed, "bus": bus, "env": env}


# -- saying what each one means ---------------------------------------------------------------------------------------------------------
def _mode_text(mode: str) -> str:
    return {"ro": " (read only)", "create": " (read and write, and it creates the folder if it is missing)"}.get(mode, " (read and write)")


def describe_filesystem(name: str, mode: str) -> tuple[str, str, str]:
    """(text, level, group) of one filesystem permission."""
    name = _canon_fs(name)
    tail = _mode_text(mode)
    if name == "host-root":
        return "Every file on your computer, from the root, including other drives" + tail, "escape", "files"
    if name == "host":
        return "Every file on your computer" + tail, "escape", "files"
    if name == "host-os":
        return "The system's own programs and libraries (/usr, /bin, /lib…)" + tail, "escape", "files"
    if name == "host-etc":
        return "The system's settings folder (/etc)" + tail, "escape", "files"
    if name in ("home", "~", "~/"):
        return "Your whole home folder" + tail, "broad", "files"
    if name == "/":
        return "Every file on your computer" + tail, "escape", "files"
    if name in _FOLDERS:
        return f"Your {_FOLDERS[name]} folder" + tail, "normal", "files"
    if name == "xdg-config/autostart" or name.startswith("xdg-config/autostart/"):
        return "The list of programs that start when you log in (it could add itself and run outside the sandbox)" + tail, "escape", "files"
    if name == "xdg-data/applications" or name.startswith("xdg-data/applications/"):
        return "The launchers in your application menu (a launcher it adds runs outside the sandbox when you click it)" + tail, "broad", "files"
    if name in ("~/.ssh", "~/.gnupg") or name.startswith(("~/.ssh/", "~/.gnupg/")):
        return f"Your {'SSH' if 'ssh' in name else 'GPG'} keys folder (it can read your private keys)" + tail, "broad", "files"
    for prefix, folder in (("xdg-config", "~/.config"), ("xdg-cache", "~/.cache"), ("xdg-data", "~/.local/share")):
        if name == prefix or name.startswith(prefix + "/"):
            return f"Your {folder}{name[len(prefix):]} folder" + tail, "broad" if name == prefix else "normal", "files"
    if name.startswith("xdg-run/"):
        known = _RUNTIME.get(name[8:])
        return (f"{known}" if known else f"The runtime file {_clean(name[8:], 80)}") + tail, "normal", "files"
    shown = _clean(name, 120)
    if name.startswith("/") and name.startswith(_SYSTEM_PREFIXES):
        return f"The system folder {shown}" + tail, "broad", "files"
    return f"The folder {shown}" + tail, "normal", "files"


def describe_bus(kind: str, name: str, policy: str) -> tuple[str, str]:
    """(text, level) of one bus-name permission."""
    what = _BUS_NAMES.get(name) or next((v for k, v in _BUS_NAMES.items() if name.startswith(k + ".")), "")
    shown = _clean(name, 120)
    verb = {"see": "See that", "talk": "Talk to", "own": "Own the name"}[policy]
    bus = "the system" if kind == "system" else "your session"
    if policy == "see":  # it can only tell that the name exists
        return f"See that {shown} exists on {bus}'s message bus", "normal"
    text = f"{what} ({shown})" if what else f"{verb} {shown} on {bus}'s message bus"
    prefix = name[:-1] if name.endswith(".*") else None  # "org.freedesktop.*" reaches every name that starts with it
    reaches = any((e.startswith(prefix) or prefix.startswith(e + ".")) if prefix is not None else (name == e or name.startswith(e + ".")) for e in _ESCAPE_BUS)
    return text, "escape" if reaches and policy in ("talk", "own") else "normal"


def _condition(item: str) -> tuple[str, str]:
    """Flatpak 1.17 can grant a permission only under a condition: "if:session-bus:has-foo". -> (the permission, the condition text or "")."""
    if item.startswith("if:"):
        parts = item.split(":", 2)
        if len(parts) == 3 and parts[1].lstrip("!"):
            return parts[1].lstrip("!"), _clean(parts[2], 60)
    return item, ""


def _item(group: str, text: str, level: str, source: str, raw: str) -> dict[str, str]:
    return {"group": group, "text": text, "level": level, "source": source, "raw": raw}


def describe(merged: dict[str, Any]) -> list[dict[str, str]]:
    """Every permission as {"group", "text", "level", "source" ("app" or "you"), "raw"}, the worst first inside each group."""
    items: list[dict[str, str]] = []
    for item, (mode, source) in merged["lists"]["shared"].items():
        text, level = _SHARED.get(item, (f"Shares {_clean(item, 60)} with other programs (a permission Cygnus does not know)", "normal"))
        items.append(_item("network", text, level, source, f"shared={item}"))
    for item, (mode, source) in merged["lists"]["sockets"].items():
        perm, cond = _condition(item)
        text, level = _SOCKETS.get(perm, (f"Uses {_clean(perm, 60)} (a permission Cygnus does not know)", "normal"))
        group = "display" if perm in ("x11", "fallback-x11", "wayland", "pulseaudio", "inherit-wayland-socket") else "system"
        items.append(_item(group, text + (f" (only when {cond})" if cond else ""), level, source, f"sockets={item}"))
    for item, (mode, source) in merged["lists"]["devices"].items():
        perm, cond = _condition(item)
        text, level = _DEVICES.get(perm, (f"Uses the device group {_clean(perm, 60)} (a permission Cygnus does not know)", "normal"))
        items.append(_item("devices", text + (f" (only when {cond})" if cond else ""), level, source, f"devices={item}"))
    for item, (mode, source) in merged["lists"]["features"].items():
        perm, cond = _condition(item)
        text, level = _FEATURES.get(perm, (f"The feature {_clean(perm, 60)} (Cygnus does not know it)", "normal"))
        items.append(_item("system", text + (f" (only when {cond})" if cond else ""), level, source, f"features={item}"))
    for item, (mode, source) in merged["lists"]["filesystems"].items():
        text, level, group = describe_filesystem(item, mode)
        items.append(_item(group, text, level, source, f"filesystems={item}" + (f":{mode}" if mode else "")))
    for kind in ("session", "system"):
        for name, (policy, source) in merged["bus"][kind].items():
            text, level = describe_bus(kind, name, policy)
            items.append(_item("bus", text, level, source, f"{kind} bus {policy} {name}"))
    for name, (value, source) in merged["env"].items():
        items.append(_item("env", f"Sets {_clean(name, 60)} to {_clean(value, 100)}", "normal", source, f"{name}={value}"))
    items.sort(key=lambda i: ([g for g, _t in GROUPS].index(i["group"]), LEVEL_ORDER[i["level"]], i["text"].lower()))
    return items


def summary(items: list[dict[str, str]]) -> dict[str, int]:
    return {"escape": sum(1 for i in items if i["level"] == "escape"), "broad": sum(1 for i in items if i["level"] == "broad"), "total": len(items)}


def removed_text(merged: dict[str, Any]) -> list[str]:
    """What an override took away, in words (only things that were there: "you removed network access")."""
    out = []
    for key, name, source in merged["removed"]:
        if key == "filesystems":
            what = describe_filesystem(name, "")[0].split(" (")[0]
        elif key in ("session-bus", "system-bus"):
            what = describe_bus(key.split("-")[0], name, "talk")[0]
        else:
            what = {"shared": _SHARED, "sockets": _SOCKETS, "devices": _DEVICES, "features": _FEATURES}[key].get(name, (_clean(name, 60), ""))[0]
        out.append(f"{'You' if source == 'you' else 'The system'} removed: {what}")
    return out


# -- reading the files -------------------------------------------------------------------------------------------------------------------
def read_text(path: Path) -> str | None:
    """A regular file's text up to the size limit (a link is followed: it is the person's own file), or None when it is not there or not
    readable. A file over the limit is refused rather than half read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(MAX_BYTES + 1).decode("utf-8", "replace")
    finally:
        os.close(fd)


def override_files(installation_path: Path, app_id: str, user_path: Path) -> list[tuple[str, Path]]:
    """The override files in the order Flatpak applies them: the installation's, then yours (when the application is in the system's)."""
    folders = [("system", installation_path / "overrides")] if installation_path != user_path else []
    folders.append(("you", user_path / "overrides"))
    return [(who, folder / name) for who, folder in folders for name in ("global", app_id)]


def read(app_id: str, env: Any, *, user_path: Path, installation: str | None = None) -> dict[str, Any]:
    """The permissions of an installed application, described. `env` is a backends.flatpak.FlatpakEnv. `installation` is "user" or "system"
    when the copy is known (the one Cygnus manages); without it the per-user copy is the one shown, because that is the one `flatpak run` starts.
    Raises CygnusError when the application is not installed."""
    matches = [(inst, ref) for inst, ref in env.installed_refs()
               if ref.get_name() == app_id and ref.get_kind() == env.Flatpak.RefKind.APP]
    if installation in ("user", "system"):
        matches = [(i, r) for i, r in matches if bool(i.get_is_user()) == (installation == "user")] or matches
    matches.sort(key=lambda m: not m[0].get_is_user())  # per-user copies first (stable: the system's keep their order)
    found = matches[0] if matches else None
    if found is None:
        raise CygnusError(f"{_clean(app_id)} is not installed as a Flatpak, so there are no permissions to show")
    inst, ref = found
    meta = ref.load_metadata(None).get_data()
    notes: list[str] = []
    if len(meta) > MAX_BYTES:
        raise CygnusError("the application's permission file is far larger than expected, so it was not read")
    layers: list[tuple[str, dict[str, dict[str, str]]]] = [("app", parse_keyfile(bytes(meta).decode("utf-8", "replace")))]
    changed = False
    for who, path in override_files(Path(inst.get_path().get_path()), app_id, user_path):
        text = read_text(path)
        if text is None:
            if path.exists() or path.is_symlink():
                notes.append(f"A permissions file ({path.name}) could not be read, so what it changes is not shown.")
            continue
        layer = parse_keyfile(text)
        if layer:
            layers.append((who, layer))
            changed = changed or who == "you"
        elif any(line.strip() and not line.strip().startswith("#") for line in text.splitlines()):
            notes.append(f"A permissions file ({path.name}) could not be understood, so what it changes is not shown.")
    merged = merge(layers)
    items = describe(merged)
    return {"app": _clean(app_id), "name": _clean(ref.get_appdata_name() or app_id, 80), "origin": _clean(ref.get_origin() or "", 60),
            "items": items, "summary": summary(items), "removed": removed_text(merged), "changed_by_you": changed, "notes": notes,
            "groups": [{"id": g, "title": t} for g, t in GROUPS if any(i["group"] == g for i in items)]}
