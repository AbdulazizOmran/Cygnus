"""KDE / freedesktop desktop integration for user-scope applications (architecture §15).

Everything written here lives under the user's XDG directories, is recorded as an owned
artifact (with its SHA-256), and is removed on uninstall only if it is still unchanged.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
from dataclasses import dataclass, field
from pathlib import Path

from cygnus import APP_NAME
from cygnus.core.desktop import entry as desktop_entry
from cygnus.core.errors import CygnusError
from cygnus.core.util import proc
from cygnus.core.util.fs import atomic_write

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


def xdg_data_home() -> Path:
    v = os.environ.get("XDG_DATA_HOME", "")
    return Path(v) if v and os.path.isabs(v) else Path.home() / ".local/share"


def xdg_config_home() -> Path:
    v = os.environ.get("XDG_CONFIG_HOME", "")
    return Path(v) if v and os.path.isabs(v) else Path.home() / ".config"


def shim_dir() -> Path:
    # ~/.local/libexec/cygnus/launch for the default XDG_DATA_HOME (~/.local/share).
    return xdg_data_home().parent / "libexec/cygnus/launch"


def allowed_roots() -> list[Path]:
    """The only places desktop integration may write."""
    return [xdg_data_home() / "applications", xdg_data_home() / "icons", xdg_data_home() / "metainfo",
            xdg_config_home() / "autostart", shim_dir()]


def check_owned_path(path: Path) -> Path:
    path = Path(os.path.normpath(path))
    for root in allowed_roots():
        root = Path(os.path.normpath(root))
        if path.is_relative_to(root) and path != root:
            # No symlinks anywhere between the root and the file.
            cur = root
            for part in path.relative_to(root).parts[:-1]:
                cur = cur / part
                if cur.is_symlink():
                    raise CygnusError(f"refusing to write through a symbolic link: {cur}")
            if path.is_symlink():
                raise CygnusError(f"refusing to overwrite a symbolic link: {path}")
            return path
    raise CygnusError(f"desktop integration may not write to {path}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(slots=True, kw_only=True)
class FileToWrite:
    path: Path
    data: bytes
    mode: int = 0o644
    kind: str = "file"  # desktop_entry | icon | launcher_shim | autostart | metainfo

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.data)


@dataclass(slots=True, kw_only=True)
class IntegrationPlan:
    desktop_id: str
    files: list[FileToWrite] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    shadowed_system_entry: str | None = None


def system_desktop_dirs() -> list[Path]:
    dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    return [Path(d) / "applications" for d in dirs if d]


def desktop_id_taken(desktop_id: str, *, own: Path | None = None) -> str | None:
    """Return the path of another entry with this desktop-file ID, if any (our own file excluded)."""
    for d in [xdg_data_home() / "applications", *system_desktop_dirs()]:
        p = d / desktop_id
        if p.exists() and (own is None or p != own):
            return str(p)
    return None


def _sq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _one_line(text: str) -> str:
    """Control characters (newlines, carriage returns, NUL, escapes) become spaces: a name written into a
    script comment must never be able to start a new line of the script."""
    return "".join(" " if ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F else c for c in text)


def launcher_shim(app_key: str, display_name: str, target: str, fs_uuid: str | None, location_label: str) -> str:
    """POSIX sh launcher: checks the drive *without touching the mountpoint*, then execs the app."""
    display_name, location_label = _one_line(display_name), _one_line(location_label)
    title = f"{display_name} is not available"
    body = f"It is stored on {location_label}, which is not connected."
    return (
        "#!/bin/sh\n"
        f"# Managed by {APP_NAME}: launches {display_name} from {location_label}.\n"
        f"TARGET={_sq(target)}\n"
        f"DRIVE_UUID={_sq(fs_uuid or '')}\n"
        'if [ -n "$DRIVE_UUID" ] && [ ! -e "/dev/disk/by-uuid/$DRIVE_UUID" ]; then\n'
        f"    notify-send --app-name={_sq(APP_NAME)} --icon=drive-harddisk {_sq(title)} {_sq(body)} "
        "2>/dev/null || true\n"
        "    exit 1\n"
        "fi\n"
        'exec "$TARGET" "$@"\n'
    )


def _rewrite_exec(exec_line: str, program: Path) -> str:
    """Replace the program of an (unescaped) Exec value, keeping arguments and field codes; quoted
    as the Desktop Entry Specification says (store it with DesktopEntry.set, which escapes it)."""
    try:
        argv = desktop_entry.split_exec(exec_line)
    except ValueError as exc:
        raise CygnusError(f"cannot parse Exec line {exec_line!r}") from exc
    if not argv:
        raise CygnusError("empty Exec line")
    return " ".join([desktop_entry.exec_program(str(program))] + [desktop_entry.quote_exec_arg(a) for a in argv[1:]])


def png_size(data: bytes) -> int | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return w if w == h else max(w, h)
    return None


def plan_appimage_integration(*, app_key: str, display_name: str, appimage_path: str, fs_uuid: str | None,
                              location_label: str, upstream_desktop: dict[str, str] | None,
                              upstream_actions: dict[str, dict[str, str]] | None = None,
                              upstream_desktop_id: str | None, icon: bytes | None, icon_kind: str | None,
                              autostart_entries: list[Path] = ()) -> IntegrationPlan:
    if not _SAFE_KEY.match(app_key):
        raise CygnusError(f"unsafe application key {app_key!r}")
    shim = shim_dir() / app_key
    own_entry = None
    desktop_id = upstream_desktop_id if upstream_desktop_id and re.fullmatch(r"[A-Za-z0-9._-]+\.desktop",
                                                                             upstream_desktop_id) else None
    plan = IntegrationPlan(desktop_id="")
    if desktop_id:
        own_entry = xdg_data_home() / "applications" / desktop_id
        taken = desktop_id_taken(desktop_id)
        if taken and not _is_ours(Path(taken), app_key):
            plan.shadowed_system_entry = taken
            plan.notes.append(f"{taken} already uses the menu id {desktop_id}; using a Cygnus-specific id instead")
            desktop_id = None
    if not desktop_id:
        desktop_id = f"cygnus-{app_key}.desktop"
    plan.desktop_id = desktop_id
    icon_name = desktop_id.removesuffix(".desktop")

    de = desktop_entry.DesktopEntry()
    src = dict(upstream_desktop or {})
    de.set_raw("Type", "Application")
    de.set("Name", desktop_entry.unescape(src.get("Name", display_name)))
    for key in ("GenericName", "Comment", "Categories", "MimeType", "Keywords", "StartupNotify", "Terminal"):
        if src.get(key):
            de.set_raw(key, src[key].replace("\n", " "))
    for key, value in src.items():  # localized names/comments
        if re.fullmatch(r"(Name|GenericName|Comment|Keywords)\[[A-Za-z_@.-]+\]", key):
            de.set_raw(key, value.replace("\n", " "))
    de.set("Exec", _rewrite_exec(desktop_entry.unescape(src.get("Exec", "app %U")), shim))
    de.set("Icon", icon_name if icon else src.get("Icon", "application-x-executable"))
    wm_class = src.get("StartupWMClass") or (upstream_desktop_id or "").removesuffix(".desktop") or None
    if wm_class:
        de.set("StartupWMClass", wm_class)
    actions = [a for a in desktop_entry.split_list(src.get("Actions", "")) if re.fullmatch(r"[A-Za-z0-9-]+", a)]
    if actions and upstream_actions:
        kept = []
        for a in actions:
            act = upstream_actions.get(a)
            if not act or not act.get("Exec"):
                continue
            group = f"Desktop Action {a}"
            de.set("Name", desktop_entry.unescape(act.get("Name", a)), group)
            de.set("Exec", _rewrite_exec(desktop_entry.unescape(act["Exec"]), shim), group)
            kept.append(a)
        if kept:
            de.set_raw("Actions", ";".join(kept) + ";")
    de.set_raw("X-Cygnus-Managed", "true")
    de.set("X-Cygnus-AppId", app_key)
    # Keep the Actions groups after the main group.
    main = de.groups.pop(desktop_entry.MAIN_GROUP)
    de.groups = {desktop_entry.MAIN_GROUP: main, **de.groups}

    plan.files.append(FileToWrite(path=shim, data=launcher_shim(app_key, display_name, appimage_path, fs_uuid,
                                                                location_label).encode(),
                                  mode=0o755, kind="launcher_shim"))
    plan.files.append(FileToWrite(path=xdg_data_home() / "applications" / desktop_id,
                                  data=de.serialize().encode(), kind="desktop_entry"))
    if icon:
        if icon_kind == "svg":
            ipath = xdg_data_home() / "icons/hicolor/scalable/apps" / f"{icon_name}.svg"
        else:
            size = png_size(icon) or 256
            ipath = xdg_data_home() / f"icons/hicolor/{size}x{size}/apps" / f"{icon_name}.png"
        plan.files.append(FileToWrite(path=ipath, data=icon, kind="icon"))
    for auto in autostart_entries:
        rewritten = rewrite_autostart(auto, shim)
        if rewritten is not None:
            plan.files.append(FileToWrite(path=auto, data=rewritten, kind="autostart"))
    return plan


def _is_ours(path: Path, app_key: str | None = None) -> bool:
    """Marked as Cygnus's in its [Desktop Entry] group (not merely mentioned somewhere in the file). With
    `app_key`, it must also be THIS application's entry: another Cygnus-managed app that happens to ship the
    same desktop-file name does not own it for us to overwrite."""
    try:
        entry = desktop_entry.parse(path.read_text(errors="replace")[:262144])
    except (OSError, ValueError):
        return False
    return entry.get("X-Cygnus-Managed") == "true" and (app_key is None or entry.get("X-Cygnus-AppId") == app_key)


def rewrite_autostart(path: Path, shim: Path) -> bytes | None:
    """Point an existing autostart entry at the shim (so moves never break it)."""
    try:
        de = desktop_entry.parse(path.read_text(errors="replace")[:262144])
    except OSError:
        return None
    exec_line = de.get("Exec")
    if not exec_line:
        return None
    new = _rewrite_exec(exec_line, shim)
    if new == exec_line:
        return None
    de.set("Exec", new)
    de.set_raw("X-Cygnus-Managed", "true")
    return de.serialize().encode()


def release_autostart(text: str, program: Path) -> bytes | None:
    """An autostart entry Cygnus pointed at its launcher, pointed straight at `program` again (used when
    the launcher goes but you keep the entry)."""
    de = desktop_entry.parse(text[:262144])
    exec_line = de.get("Exec")
    if not exec_line:
        return None
    de.set("Exec", _rewrite_exec(exec_line, program))
    de.remove("X-Cygnus-Managed")
    return de.serialize().encode()


def validate_desktop_file(data: bytes) -> list[str]:
    """Run desktop-file-validate on the content; returns error lines (warnings are tolerated)."""
    tool = proc.which("desktop-file-validate")
    if tool is None:
        return []
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".desktop") as tmp:
        tmp.write(data)
        tmp.flush()
        res = proc.run([tool, tmp.name], timeout=30)
    return [line for line in (res.stdout + res.stderr).splitlines() if "error:" in line]


def refresh_caches() -> list[str]:
    """Make KDE pick up changes immediately; failures are reported but non-fatal."""
    problems = []
    apps = xdg_data_home() / "applications"
    for argv in (["update-desktop-database", "-q", str(apps)], ["kbuildsycoca6"]):
        if proc.which(argv[0]) is None:
            continue
        try:
            res = proc.run(argv, timeout=120)
            if res.returncode != 0:
                problems.append(f"{argv[0]}: {res.stderr.strip()[:200]}")
        except proc.CommandTimeout as exc:
            problems.append(str(exc))
    return problems


# -- executor handlers --------------------------------------------------------------------------------
def write_owned(params: dict) -> "object":
    """Executor handler: atomically write an integration file; compensation restores the previous state."""
    from cygnus.core.executor import Step, StepOutcome

    path = check_owned_path(Path(params["path"]))
    data = bytes.fromhex(params["data_hex"])
    previous = path.read_bytes() if path.exists() else None
    previous_mode = stat.S_IMODE(path.stat().st_mode) if previous is not None else None
    # The file was not there when this step was planned but already holds this content: an earlier run of this
    # step wrote it and died before journaling, so undoing it means removing the file, not "restoring" itself.
    landed_earlier = previous == data and params.get("existed") is False
    atomic_write(path, data, mode=int(params.get("mode", 0o644)))
    comp = (Step(kind="fs.restore_owned", params={"path": str(path), "data_hex": previous.hex(),
                                                   "expect_sha256": sha256_bytes(data), "mode": previous_mode})
            if previous is not None and not landed_earlier else
            Step(kind="fs.remove_owned", params={"path": str(path), "expect_sha256": sha256_bytes(data)}))
    return StepOutcome(result={"path": str(path), "sha256": sha256_bytes(data),
                               "replaced": previous is not None and not landed_earlier},
                       compensation=comp)


def remove_owned(params: dict) -> "object":
    """Remove a file Cygnus created — only if it is still exactly what Cygnus wrote."""
    from cygnus.core.executor import StepOutcome

    path = check_owned_path(Path(params["path"]))
    if not path.exists():
        return StepOutcome(result={"path": str(path), "removed": False, "reason": "already gone"})
    data = path.read_bytes()
    if sha256_bytes(data) != params["expect_sha256"]:
        if params.get("keep_if_changed"):  # an uninstall: your edit stays, the rest of the uninstall goes on
            return StepOutcome(result={"path": str(path), "removed": False, "kept": True,
                                       "reason": "you changed it after Cygnus wrote it; left in place"})
        raise CygnusError(f"{path} was changed after Cygnus wrote it; leaving it in place")
    mode = path.stat().st_mode & 0o777
    path.unlink()
    from cygnus.core.executor import Step

    return StepOutcome(result={"path": str(path), "removed": True},
                       compensation=Step(kind="fs.write_owned", params={"path": str(path), "data_hex": data.hex(),
                                                                        "mode": mode}))


def restore_owned(params: dict) -> "object":
    from cygnus.core.executor import StepOutcome

    path = check_owned_path(Path(params["path"]))
    if path.exists() and sha256_bytes(path.read_bytes()) != params["expect_sha256"]:
        raise CygnusError(f"{path} was changed after Cygnus wrote it; not restoring the old version")
    mode = params.get("mode")  # journals from older versions lack it: keep what the file has now
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    atomic_write(path, bytes.fromhex(params["data_hex"]), mode=int(mode))
    return StepOutcome(result={"path": str(path), "restored": True})


def write_owned_abort(params: dict) -> "object":
    """The process died while writing: a file that did not exist when the step was planned and now holds
    exactly what was being written is removed; a replaced file cannot be restored, which is reported."""
    from cygnus.core.executor import StepOutcome

    try:
        path = check_owned_path(Path(params["path"]))
    except CygnusError:  # a link or a place Cygnus may not write: the step refused before writing anything
        return StepOutcome(result={"untouched": params["path"]})
    for leftover in path.parent.glob(f".{path.name}.tmp-*"):  # the temporary file of an interrupted atomic write
        leftover.unlink(missing_ok=True)
    if not path.exists() or sha256_bytes(path.read_bytes()) != sha256_bytes(bytes.fromhex(params["data_hex"])):
        return StepOutcome(result={"untouched": str(path)})  # the atomic write never landed
    if params.get("existed") is False:
        path.unlink()
        return StepOutcome(result={"removed": str(path)})
    raise CygnusError(f"{path} was rewritten and its previous version could not be restored")


HANDLERS = {"fs.write_owned": write_owned, "fs.write_owned.abort": write_owned_abort,
            "fs.remove_owned": remove_owned, "fs.restore_owned": restore_owned}
