"""Making a web browser the one that opens links (the "default browser").

A program that came as an AppImage or an archive is not known to the desktop until Cygnus gives it a menu entry, and the
desktop only treats an entry as a browser when it says so: it has to claim http, https and html. This module checks that,
makes the choice through `xdg-settings` (the program browsers run for their own "make default" button, so the result is the
same as theirs), and then READS THE RESULT BACK from every place the desktop looks: a choice that did not stick is worse than
one that failed loudly. What was set before is remembered, so one click puts it back.

It changes your own settings only: the browser lines of `~/.config/mimeapps.list` and, on KDE, `BrowserApplication` in
`~/.config/kdeglobals`. Nothing system-wide, and nothing that needs administrator rights."""

from __future__ import annotations

import os
import re
import shutil
import stat
import threading
from pathlib import Path
from typing import Any

from cygnus.core import handlers, preferences
from cygnus.core.desktop import entry as desktop_entry
from cygnus.core.desktop import integrate
from cygnus.core.errors import CygnusError
from cygnus.core.util import proc

# What a menu entry must claim before the desktop will let it be the browser: xdg-settings sets all three together and puts
# everything back when one of them does not stick (xdg-mime ignores a default for a type the entry does not claim).
MIME_TYPES = integrate.BROWSER_MIME_TYPES
# xdg-settings also hands a browser the "about:" and "unknown scheme" links (outside KDE, at least): they are not part of what
# an entry must claim, but they are changed, so they are remembered and put back like the others.
TRACKED = MIME_TYPES + ("x-scheme-handler/about", "x-scheme-handler/unknown")
_WORDS = {"x-scheme-handler/http": "http links", "x-scheme-handler/https": "https links", "text/html": "web page files",
          "x-scheme-handler/about": "about: links", "x-scheme-handler/unknown": "links of unknown kinds"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,200}\.desktop\Z")
# A name in a list that is only read and written back: GLib accepts more than a menu entry's own id (an "@", say), and dropping such a name
# from your list when the choice is undone would lose it.
_LISTED = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@%:~-]{0,200}\Z")
MAX_FILES, MAX_FOUND, MAX_ENTRY_BYTES = 3000, 100, 262144
# The variables `xdg-settings` reads to know which desktop it is on. The process helper passes almost nothing on purpose, and
# without KDE_SESSION_VERSION `xdg-settings` runs a command that does not exist (`kwriteconfig`), fails, and never writes
# KDE's own browser setting: the choice would look made in the menu and not be.
_SESSION = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS", "XDG_CACHE_HOME", "XDG_CURRENT_DESKTOP",
            "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "KDE_SESSION_VERSION", "KDE_FULL_SESSION")
_LOCK = threading.Lock()  # a click on "make default" and one on "put back" must not interleave


def _env() -> dict[str, str]:
    return {k: os.environ[k] for k in _SESSION if k in os.environ}


def _plain(text: str, limit: int = 120) -> str:
    """A name from a file that is not ours, safe to show: no control characters, bounded."""
    return "".join(c if c.isprintable() else " " for c in text).strip()[:limit]


def _application_dirs() -> list[Path]:
    dirs = [integrate.xdg_data_home(), *(Path(d) for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d)]
    return [d / "applications" for d in dirs]


def find_entry(desktop_id: str) -> Path | None:
    """The menu entry with this name that the desktop would use (your own folder first), or None."""
    if not _ID.match(desktop_id):
        return None
    for folder in _application_dirs():
        path = folder / desktop_id
        if path.is_file():
            return path
    return None


def _load(path: Path) -> desktop_entry.DesktopEntry | None:
    """A menu entry's content: a regular file only (a named pipe with that name must not hold the listing up), read up to a limit."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return desktop_entry.parse(fh.read(MAX_ENTRY_BYTES).decode("utf-8", "replace"))
    except OSError:
        return None
    finally:
        os.close(fd)


def _claims(entry: desktop_entry.DesktopEntry) -> set[str]:
    return set(entry.get_list("MimeType"))


def _is_browser_entry(entry: desktop_entry.DesktopEntry) -> bool:
    return set(MIME_TYPES[:2]) <= _claims(entry) and "WebBrowser" in entry.get_list("Categories")


def _drive_note(program: str) -> str:
    """Cygnus's launcher for a program on a drive that can be unplugged says so inside; that matters here, because every
    link you click would hit it."""
    try:
        path = Path(program)
        if path.parent != integrate.shim_dir() or not path.is_file():
            return ""
        with open(path, "rb") as fh:
            head = fh.read(8192).decode("utf-8", "replace")
    except OSError:
        return ""
    m = re.search(r"^DRIVE_UUID='([^']+)'", head, re.M)
    return ("It is stored on a drive that can be unplugged: while that drive is not connected, links will not open and you "
            "will get a notice instead.") if m else ""


def assess(desktop_id: str) -> dict:
    """Can this menu entry be the default browser, and if not, why. {"desktop_id", "name", "path", "managed", "repairable",
    "browser" (it files itself as one or claims web links: worth offering the choice), "blockers": [...], "notes": [...]}:
    a blocker stops the choice, a note is only something worth knowing."""
    out = {"desktop_id": desktop_id, "name": _plain(desktop_id), "path": "", "managed": False, "repairable": False,
           "browser": False, "blockers": [], "notes": []}
    path = find_entry(desktop_id)
    if path is None:
        out["blockers"].append(f"No menu entry named {_plain(desktop_id)} is installed.")
        return out
    entry = _load(path)
    if entry is None:
        out["blockers"].append("Its menu entry cannot be read.")
        return out
    out["path"] = str(path)
    out["name"] = _plain(entry.get("Name") or desktop_id)
    out["managed"] = entry.get("X-Cygnus-Managed") == "true"
    categories = entry.get_list("Categories")
    out["repairable"] = out["managed"] and "WebBrowser" in categories
    out["browser"] = "WebBrowser" in categories or bool(set(MIME_TYPES[:2]) & _claims(entry))
    if entry.get("Type") != "Application" or entry.get("Hidden") == "true":
        out["blockers"].append("Its menu entry is hidden or is not a program.")
    missing = [_WORDS[m] for m in MIME_TYPES if m not in _claims(entry)]
    if missing:
        out["blockers"].append("Its menu entry does not say it opens " + ", ".join(missing) + ", so the desktop would ignore "
                               "the choice." + (" Repairing the program in Cygnus adds what a browser needs." if out["repairable"] else ""))
    try:
        argv = desktop_entry.split_exec(entry.get("Exec") or "")
    except ValueError:
        argv = []
    program = argv[0] if argv else ""
    if not program:
        out["blockers"].append("Its menu entry has no program to start.")
    else:
        found = os.path.isfile(program) and os.access(program, os.X_OK) if os.path.isabs(program) else proc.which(program)
        if not found:
            out["blockers"].append(f"The program its menu entry starts ({_plain(program)}) is not there"
                                   + ("." if os.path.isabs(program) else " (a name without a folder must be in /usr/bin, /usr/local/bin or /bin, "
                                                                          "where the desktop tools look; a full path always works)."))
        elif note := _drive_note(program):
            out["notes"].append(note)
    if "WebBrowser" not in categories:
        out["notes"].append("KDE's own \"Default applications\" page lists only programs filed as web browsers, so it will not "
                            "show this one; the choice itself still works.")
    if out["managed"] and desktop_id.startswith("cygnus-"):
        out["notes"].append("Another program already uses this browser's own menu name, so Cygnus named its entry differently. "
                            "A browser that checks by its own name may keep saying it is not the default even though it is.")
    return out


def candidates() -> list[dict]:
    """The web browsers on this computer: [{"desktop_id", "name", "path", "managed"}]. A menu entry in your own folder hides
    one of the same name in the system's, as the desktop does."""
    seen: set[str] = set()
    found: list[dict] = []
    scanned = 0
    for folder in _application_dirs():
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".desktop"))
        except OSError:
            continue
        for name in names:
            scanned += 1
            if scanned > MAX_FILES or len(found) >= MAX_FOUND:
                return sorted(found, key=lambda b: b["name"].casefold())
            if name in seen or not _ID.match(name):
                continue
            seen.add(name)
            entry = _load(folder / name)
            if entry is None or entry.get("Type") != "Application" or "true" in (entry.get("NoDisplay"), entry.get("Hidden")):
                continue
            if _is_browser_entry(entry):
                found.append({"desktop_id": name, "name": _plain(entry.get("Name") or name), "path": str(folder / name),
                              "managed": entry.get("X-Cygnus-Managed") == "true"})
    return sorted(found, key=lambda b: b["name"].casefold())


# -- what the desktop has now --------------------------------------------------------------------------------------
def _default_list(path: Path, mime: str) -> list[str]:
    """The programs a mimeapps-style file lists as the default for `mime`, first one first."""
    try:
        with open(path, "rb") as fh:
            text = fh.read(1 << 20).decode("utf-8", "surrogateescape")
    except OSError:
        return []
    section = ""
    found: list[str] = []
    for line in handlers._lines(text):
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            continue
        key, _, value = stripped.partition("=")
        if section == "[Default Applications]" and key.strip() == mime:
            found = [v.strip() for v in value.split(";") if _LISTED.match(v.strip())]  # a later line for the same type wins, as in GLib's key files
    return found


def _config_home() -> Path:
    return integrate.xdg_config_home()


def _explicit(mime: str) -> str:
    """What YOU (or a program on your behalf) set for `mime` in your own mimeapps.list; "" when nothing is set and the
    desktop just takes whichever program registered first."""
    listed = _default_list(_config_home() / "mimeapps.list", mime)
    return listed[0] if listed else ""


def _overridden_by(mime: str, desktop_id: str) -> str:
    """A desktop-specific list in your settings folder (kde-mimeapps.list) is read before mimeapps.list: when one names a
    different program for `mime`, setting it in mimeapps.list changes nothing. The file's name, or ""."""
    for desktop in (os.environ.get("XDG_CURRENT_DESKTOP") or "").split(":"):
        if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", desktop):
            name = f"{desktop.lower()}-mimeapps.list"
            listed = _default_list(_config_home() / name, mime)
            if listed and listed[0] != desktop_id:
                return name
    return ""


def _kde_tool(kind: str) -> str | None:
    version = os.environ.get("KDE_SESSION_VERSION", "")
    return f"{kind}{version}" if version in ("5", "6") and shutil.which(f"{kind}{version}") else None


def _kde_value(run) -> str | None:
    """KDE's own browser setting ("" when unset), or None when this is not a KDE session."""
    tool = _kde_tool("kreadconfig")
    if tool is None:
        return None
    result = run([tool, "--file", "kdeglobals", "--group", "General", "--key", "BrowserApplication"], timeout=20, env=_env())
    return result.stdout.strip() if result.returncode == 0 else None


def _kde_set(value: str, run) -> bool:
    """Write KDE's own browser setting (or remove it); False when that could not be done."""
    tool = _kde_tool("kwriteconfig")
    if tool is None:
        return True
    argv = [tool, "--file", "kdeglobals", "--group", "General", "--key", "BrowserApplication"]
    try:
        return run(argv + ([value] if value else ["--delete"]), timeout=20, env=_env()).returncode == 0
    except CygnusError:  # a timeout
        return False


_KDE_VALUE = re.compile(r"[^\x00-\x1f\x7f-][^\x00-\x1f\x7f]{0,300}\Z")  # KDE's own value: text, never starting with a dash (it is passed as an argument)


def _lists_ok(lists: Any) -> bool:
    return isinstance(lists, dict) and all(isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) and _LISTED.match(x) for x in v)
                                           for k, v in lists.items())


def _backup() -> dict | None:
    """What was set before Cygnus changed anything, or None. A hand-edited file that no longer has the right shape, or holds a name that could
    not be a menu entry or a value that could be read as an option, counts as nothing remembered."""
    raw = preferences.load().get("browser_backup")
    if not isinstance(raw, dict) or not isinstance(raw.get("mime"), dict) or not isinstance(raw.get("chosen"), str) or not _ID.match(raw["chosen"]):
        return None
    if not all(isinstance(k, str) and isinstance(v, str) and (v == "" or _LISTED.match(v)) for k, v in raw["mime"].items()):
        return None
    if not _lists_ok(raw.get("lists", {})) or not _lists_ok(raw.get("after", {})):  # "lists": before the choice; "after": what the choice left
        return None
    kde = raw.get("kde", "")
    if not isinstance(kde, str) or (kde != "" and not _KDE_VALUE.match(kde)):
        return None
    return raw


def _tools_missing() -> str:
    missing = [t for t in ("xdg-settings", "xdg-mime", "gio") if shutil.which(t) is None]
    return f"{' and '.join(missing)} (packages xdg-utils and glib2) not installed" if missing else ""


def status(run=proc.run) -> dict:
    """{"available", "why", "default" (the menu entry links open with now), "current": {mime: entry}, "explicit": {mime: entry
    you set}, "kde": KDE's own setting or None, "consistent", "chosen_here" (the entry Cygnus set, or "")}."""
    why = _tools_missing()
    if why:
        return {"available": False, "why": why, "default": "", "current": {}, "explicit": {}, "kde": None, "consistent": False,
                "chosen_here": ""}
    current = {m: handlers._query(m, run) for m in MIME_TYPES}
    backup = _backup()
    return {"available": True, "why": "", "default": current["x-scheme-handler/https"], "current": current,
            "explicit": {m: _explicit(m) for m in MIME_TYPES}, "kde": _kde_value(run), "consistent": len(set(current.values())) == 1,
            "chosen_here": backup["chosen"] if backup else ""}


def _problems(desktop_id: str, run) -> list[str]:
    """Every place the desktop looks, checked for `desktop_id`: what is not as it should be, in plain words."""
    bad = []
    for mime in MIME_TYPES:
        now = handlers._query(mime, run)
        if now != desktop_id:
            other = _overridden_by(mime, desktop_id)
            bad.append(f"{_WORDS[mime]} still open with {now or 'nothing'}" +
                       (f" (the list {other} in your settings folder takes priority: take its line out)" if other else ""))
        elif _explicit(mime) != desktop_id:
            bad.append(f"{_WORDS[mime]} open with it only because nothing else claims them, not because it was chosen")
    kde = _kde_value(run)
    if kde is not None and kde.removesuffix(".desktop") != desktop_id.removesuffix(".desktop"):
        bad.append(f"KDE's own browser setting says {kde or 'nothing'}")
    checked = run(["xdg-settings", "check", "default-web-browser", desktop_id], timeout=30, env=_env())
    if checked.stdout.strip() != "yes":
        bad.append("xdg-settings, the program browsers ask, does not agree")
    return bad


def _wanted(before: dict, mime: str) -> list[str]:
    """The default list `mime` had in the remembered state: the whole list when it was saved, else the one name."""
    listed = (before.get("lists") or {}).get(mime)
    if isinstance(listed, list):
        return [x for x in listed if isinstance(x, str) and _LISTED.match(x)]
    one = before["mime"].get(mime, "")
    return [one] if one and _LISTED.match(one) else []


def _set_list(mime: str, wanted: list[str]) -> bool:
    """Make the default list of `mime` exactly `wanted` and say whether it is so now (written here, not through xdg-mime, which edits one
    line and would leave a line spaced by hand, or a second line for the type, behind)."""
    try:
        handlers._write_default(mime, wanted)
    except OSError:  # a settings folder that cannot be written: reported as "could not be put back", never raised in the middle of an undo
        return False
    return _default_list(_config_home() / "mimeapps.list", mime) == wanted


def _put_back(before: dict, chosen: str, run) -> tuple[list[str], list[str]]:
    """Return every browser setting to `before` ({"mime", "lists", "kde"}), but only where it is still `chosen`: one you or another program
    changed since is left as it is. -> (left as someone set it, could not be put back)."""
    left, failed = [], []
    for mime in TRACKED:
        listed = _default_list(_config_home() / "mimeapps.list", mime)
        wanted = _wanted(before, mime)
        if listed == wanted:
            continue  # already as it was (a first attempt put this one back)
        now = listed[0] if listed else ""
        if now != chosen:
            if now:
                left.append(_WORDS[mime])
            continue
        after = (before.get("after") or {}).get(mime)
        if isinstance(after, list) and listed != after:  # a name was added or taken out after the choice: that list is yours now
            left.append(_WORDS[mime])
            continue
        if wanted != [chosen] and not _set_list(mime, wanted):
            failed.append(_WORDS[mime])
    kde = _kde_value(run)
    if kde is not None and kde.removesuffix(".desktop") == chosen.removesuffix(".desktop") and not _kde_set(before.get("kde", ""), run):
        failed.append("KDE's own browser setting")
    return left, failed


def _restore_exactly(before: dict, run) -> list[str]:
    """After a choice that failed: every browser setting back to exactly what it was a moment ago (xdg-settings leaves its own
    partial changes behind when it fails). -> what could not be put back."""
    failed = []
    for mime in TRACKED:
        wanted = _wanted(before, mime)
        if _default_list(_config_home() / "mimeapps.list", mime) != wanted and not _set_list(mime, wanted):
            failed.append(_WORDS[mime])
    kde = _kde_value(run)
    if kde is not None and kde != before.get("kde", "") and not _kde_set(before.get("kde", ""), run):
        failed.append("KDE's own browser setting")
    return failed


def enable(desktop_id: str, run=proc.run) -> dict:
    """Make `desktop_id` the browser links open with, and check that it took. Everything is put back if it did not."""
    with _LOCK:
        state = status(run)
        if not state["available"]:
            raise CygnusError(state["why"])
        verdict = assess(desktop_id)
        if verdict["blockers"]:
            raise CygnusError(f"{verdict['name']} cannot be the default browser. " + " ".join(verdict["blockers"]))
        if not _problems(desktop_id, run):
            return {**status(run), "changed": False}  # already so, and chosen on purpose
        before = {"mime": {m: _explicit(m) for m in TRACKED}, "lists": {m: _default_list(_config_home() / "mimeapps.list", m) for m in TRACKED},
                  "kde": state["kde"] or ""}
        prefs = preferences.load()
        earlier = _backup()
        # The very first memory is the one worth keeping: what it was before Cygnus ever touched it.
        prefs["browser_backup"] = {**{k: v for k, v in earlier.items() if k != "after"}, "chosen": desktop_id} if earlier else {**before, "chosen": desktop_id}
        preferences.save(prefs)  # remembered before anything changes

        def give_up(reasons: list[str]) -> CygnusError:
            try:
                failed = _restore_exactly(before, run)
            except (OSError, CygnusError):  # whatever goes wrong while undoing is said in words, not raised over the real reason
                failed = ["the browser settings"]
            if not failed:  # nothing is left to put back: the memory of the choice goes too. Otherwise it stays, so "Put back" can try again.
                try:
                    again = preferences.load()
                    again.pop("browser_backup", None)
                    if earlier:
                        again["browser_backup"] = earlier
                    preferences.save(again)
                except (OSError, CygnusError):
                    pass
            return CygnusError(f"{verdict['name']} could not be made the default browser: " + "; ".join(reasons) + ". "
                               + (f"Some settings could not be put back: {', '.join(failed)}." if failed else
                                  "Your settings were put back as they were."))

        try:
            result = run(["xdg-settings", "set", "default-web-browser", desktop_id], timeout=60, env=_env())
            bad = _problems(desktop_id, run)
        except CygnusError as exc:  # a timeout, or the program could not start
            raise give_up([str(exc)]) from exc
        if result.returncode != 0 or bad:
            lines = (result.stderr or result.stdout or "").strip().splitlines()
            raise give_up(bad or [lines[-1][:200] if lines else "xdg-settings failed"])
        try:  # what the choice left in each list, so that "Put back" can tell a name added afterwards (yours) from what it wrote
            again = preferences.load()
            if isinstance(again.get("browser_backup"), dict):
                again["browser_backup"]["after"] = {m: _default_list(_config_home() / "mimeapps.list", m) for m in TRACKED}
                preferences.save(again)
        except (OSError, CygnusError):
            pass  # without it the first name alone decides, as before
        return {**status(run), "changed": True}


def restore(run=proc.run) -> dict:
    """Put the browser settings back to what they were before Cygnus chose one. A setting changed since is left alone."""
    with _LOCK:
        why = _tools_missing()
        if why:
            raise CygnusError(why)
        backup = _backup()
        if backup is None:
            raise CygnusError("Cygnus has not changed your default browser, so there is nothing to put back.")
        left, failed = _put_back(backup, backup["chosen"], run)
        if failed:  # the memory stays: the next try puts back what is left (what was put back is recognised as it was)
            raise CygnusError("These could not be put back: " + ", ".join(failed) + ". Cygnus still remembers your choice, so you can try again, "
                              "or set the browser you want in your desktop's default applications settings.")
        prefs = preferences.load()
        prefs.pop("browser_backup", None)
        preferences.save(prefs)
        return {**status(run), "left_alone": left}
