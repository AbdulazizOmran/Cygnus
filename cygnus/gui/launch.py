"""What Cygnus opens when something outside hands it an argument: a browser's Install button on Flathub, the file manager.

Every such argument is hostile input. It is validated strictly, a `.flatpakref` is read with a size cap, and the
repository it names is mapped only to a remote that is ALREADY configured (Cygnus never adds a repository a file names).
An argument only ever opens the Install page with something to analyse; it never installs anything."""

from __future__ import annotations

import configparser
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit, urlunsplit

from cygnus.core.errors import CygnusError
from cygnus.core.util.fs import open_regular

MAX_REF_BYTES = 64 * 1024  # Flathub's own .flatpakref is about 5 KB (it carries the repository's signing key)
APP_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+")
BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
FLATHUB_URL = "https://dl.flathub.org/repo"  # Cygnus adds this one itself when it is not there yet
# What Flathub's Install button opens: the address of an app's reference file, with the scheme `flatpak+https`. Only Flathub's own
# standard address is followed, and nothing is fetched: the app id in it is all that is needed (the same as an `appstream:` link).
FLATHUB_REF_LINK = re.compile(r"(?i:flatpak\+https)://(?i:dl\.flathub\.org)/repo/appstream/([A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+)\.flatpakref")


@dataclass(frozen=True)
class Launch:
    properties: dict[str, str] = field(default_factory=dict)  # for the Install page
    notice: str = ""  # shown on the page: what was understood, or why nothing could be opened


def valid_app_id(name: str) -> bool:
    return len(name) <= 255 and bool(APP_ID.fullmatch(name))  # Flatpak's own limit


def normal_url(url: str) -> str:
    """A repository address in a form two spellings of the same one share: the scheme and the host are not case sensitive,
    the path is (a repository at /Repo is not the one at /repo), and a trailing slash does not matter."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, parts.fragment))


def _shown(text: str, limit: int = 120) -> str:
    """Text from a file, made safe to show: no control characters and no right-to-left or other invisible formatting
    characters that could make an address read differently from what it is."""
    return "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp"))[:limit]


def _read_ref(path: Path) -> configparser.SectionProxy:
    fd = open_regular(path)
    import os

    with os.fdopen(fd, "rb") as fh:
        data = fh.read(MAX_REF_BYTES + 1)
    if len(data) > MAX_REF_BYTES:
        raise CygnusError("this .flatpakref file is far larger than a real one, so Cygnus does not read it")
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(data.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise CygnusError("this .flatpakref file is not readable") from exc
    if not parser.has_section("Flatpak Ref"):
        raise CygnusError("this is not a Flatpak reference file")
    return parser["Flatpak Ref"]


def _from_ref(path: Path, remotes: Callable[[], dict[str, str]]) -> Launch:
    try:
        ref = _read_ref(path)
    except CygnusError as exc:
        return Launch(notice=str(exc))
    name, branch = ref.get("Name", "").strip(), (ref.get("Branch") or "stable").strip()
    if not valid_app_id(name) or not BRANCH.fullmatch(branch):
        return Launch(notice="this Flatpak reference does not name a valid application")
    if ref.get("IsRuntime", "").strip().lower() == "true":
        return Launch(notice=f"{name} is a runtime (a part other apps use), not an application to install here")
    url = normal_url(ref.get("Url", ""))
    if url == FLATHUB_URL:
        remote = "flathub"
    else:
        remote = next(iter(sorted(n for u, n in remotes().items() if u == url)), "")
    if not remote:
        return Launch(notice=f"This file installs {name} from {_shown(ref.get('Url', '?'))}, a repository Cygnus does not have "
                             "set up. Cygnus never adds a repository that a file names, because that decides where your "
                             "software comes from. Add it yourself with `flatpak remote-add`, then open the file again.")
    return Launch({"appSpec": f"{remote}:{name}//{branch}"})


def _path_of(arg: str, cwd: str) -> Path | None:
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", arg):  # any address with a scheme: only local file:// ones are files
        if not arg.lower().startswith("file:"):
            return None
        parsed = urlparse(arg)
        if parsed.scheme.lower() != "file" or parsed.netloc not in ("", "localhost"):
            return None
        path = Path(unquote(parsed.path))
        return path if path.is_absolute() else None  # "file:name" is not a place
    path = Path(arg)
    return path if path.is_absolute() else Path(cwd) / path


def resolve(arg: str, cwd: str, remotes: Callable[[], dict[str, str]] = lambda: {}) -> Launch:
    """The Install page's properties for one command-line argument (or the reason there are none)."""
    arg = arg.strip()
    if arg.lower().startswith("appstream:"):  # appstream:org.example.App and appstream://org.example.App
        app_id = arg[len("appstream:"):].lstrip("/").rstrip("/")
        if valid_app_id(app_id):
            return Launch({"appSpec": f"flathub:{app_id}"})
        return Launch(notice="Cygnus did not understand this software link")
    if arg.lower().startswith("flatpak+https:"):  # the Install button on flathub.org
        m = FLATHUB_REF_LINK.fullmatch(arg)
        if m and valid_app_id(m.group(1)):
            return Launch({"appSpec": f"flathub:{m.group(1)}"})
        host = urlparse(arg.replace("flatpak+https:", "https:", 1)).netloc if "\0" not in arg else ""
        return Launch(notice=f"This link asks Cygnus to fetch a Flatpak reference from {_shown(host, 80) or 'another address'}. "
                             "Cygnus only follows Flathub's own links, because following one decides where your software "
                             "comes from. Download the .flatpakref file from that site and open it with Cygnus instead.")
    try:
        path = _path_of(arg, cwd) if "\0" not in arg and "%00" not in arg.lower() else None
    except ValueError:  # an address Python cannot take apart
        path = None
    if path is None:
        return Launch(notice="Cygnus only opens local files and software links, not other addresses")
    suffix = path.suffix.lower()
    if suffix == ".flatpakref":
        try:
            return _from_ref(path, remotes)
        except (OSError, ValueError):  # a file that vanished, or a name the system will not take
            return Launch(notice="this .flatpakref file could not be read")
    if suffix == ".flatpakrepo":
        return Launch(notice="This file adds a software repository. Cygnus never adds a repository that a file names, "
                             "because that decides where your software comes from. Add it yourself with "
                             "`flatpak remote-add`, then install from it here.")
    return Launch({"fileUrl": path.as_uri()})
