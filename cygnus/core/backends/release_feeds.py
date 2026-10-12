"""Where a program's own download lives when its maker publishes it on a GitHub release page (an AppImage, a .deb or .rpm, an archive,
an Arch package), and how the newest file is found.

Discover lists these programs from a hand-checked catalogue (cygnus/core/release_catalog.py); this module is only asked when the person
presses a download button. It reads GitHub's answer for the project's newest release, picks the ONE file whose name matches the pattern
written for that format (no match, or more than one, is an error in words: never "the first one"), and checks that the download address is
that project's own release address. The file is then downloaded over HTTPS, checked against the SHA-256 GitHub publishes for it when there is
one, and handed to the Install page like any file the person chose. Nothing is installed by this module, and the answer is treated as hostile
input: every field is type-checked and size-limited.

What it cannot promise: GitHub vouches for who uploaded a file to the project, not for what is in it; the checksum only proves that the file
that arrived is the file GitHub holds. Updates are not tracked for these downloads (a .deb or .rpm has no update information of its own, and an
AppImage only has it when its maker put it inside)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from cygnus.core.errors import CygnusError
from cygnus.core.util import http

FORMATS = ("appimage", "deb", "rpm", "archive", "pkg")  # the order the buttons are shown in
LABELS = {"appimage": "AppImage", "deb": ".deb", "rpm": ".rpm", "archive": "archive", "pkg": "Arch package"}
LATEST = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
MAX_ANSWER = 8 * 1024 * 1024  # a release's text can be long; the list of files is what is read
MAX_ASSETS = 300
MAX_FILE = 3 * 1024**3  # a file declared larger is refused before anything is downloaded; the download itself is limited to the declared size
MAX_NAME = 200

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,60}\Z")
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,40}\Z")
V = r"[0-9][0-9A-Za-z.]*"  # a version inside a file name: digits and dots, no dash, so "1.5.0-x86_64-sciter" is not "1.5.0"


@dataclass(frozen=True)
class Project:
    key: str  # the catalogue's name for it (never a vendor feed's package name)
    owner: str
    repo: str
    assets: Mapping[str, str]  # format -> a regular expression the whole file name must match (x86_64 / amd64 only)


@dataclass(frozen=True)
class Asset:
    version: str  # the release's tag, as plain text
    name: str
    url: str
    sha256: str | None  # published by GitHub for the file; older uploads have none
    size: int
    format: str


def _p(key: str, owner: str, repo: str, **assets: str) -> Project:
    return Project(key, owner, repo, {fmt: pattern.replace("{v}", V) for fmt, pattern in assets.items()})


PROJECTS: dict[str, Project] = {p.key: p for p in (
    _p("helium", "imputnet", "helium-linux", appimage=r"helium-{v}-x86_64\.AppImage", deb=r"helium-bin_{v}-1_amd64\.deb",
       archive=r"helium-{v}-x86_64_linux\.tar\.xz"),
    _p("zen-browser", "zen-browser", "desktop", appimage=r"zen-x86_64\.AppImage", archive=r"zen\.linux-x86_64\.tar\.xz"),
    _p("obsidian", "obsidianmd", "obsidian-releases", appimage=r"Obsidian-{v}\.AppImage", deb=r"obsidian_{v}_amd64\.deb",
       archive=r"obsidian-{v}\.tar\.gz"),
    _p("heroic", "Heroic-Games-Launcher", "HeroicGamesLauncher", appimage=r"Heroic-{v}-linux-x86_64\.AppImage", deb=r"Heroic-{v}-linux-amd64\.deb",
       rpm=r"Heroic-{v}-linux-x86_64\.rpm", archive=r"Heroic-{v}-linux-x64\.tar\.xz"),
    _p("localsend", "localsend", "localsend", appimage=r"LocalSend-{v}-linux-x86-64\.AppImage", deb=r"LocalSend-{v}-linux-x86-64\.deb",
       archive=r"LocalSend-{v}-linux-x86-64\.tar\.gz"),
    _p("rustdesk", "rustdesk", "rustdesk", appimage=r"rustdesk-{v}-x86_64\.AppImage", deb=r"rustdesk-{v}-x86_64\.deb", rpm=r"rustdesk-{v}-0\.x86_64\.rpm",
       pkg=r"rustdesk-{v}-0-x86_64\.pkg\.tar\.zst"),
    _p("zettlr", "Zettlr", "Zettlr", appimage=r"Zettlr-{v}-x86_64\.AppImage", deb=r"Zettlr-{v}-amd64\.deb", rpm=r"Zettlr-{v}-x86_64\.rpm"),
    _p("logseq", "logseq", "logseq", appimage=r"Logseq-linux-x86_64-{v}\.AppImage"),
    _p("joplin", "laurent22", "joplin", appimage=r"Joplin-{v}\.AppImage", deb=r"Joplin-{v}\.deb"),
    _p("ferdium", "ferdium", "ferdium-app", appimage=r"Ferdium-linux-Portable-{v}-x86_64\.AppImage", deb=r"Ferdium-linux-{v}-amd64\.deb",
       rpm=r"Ferdium-linux-{v}-x86_64\.rpm", archive=r"Ferdium-linux-{v}\.tar\.gz"),
    _p("mailspring", "Foundry376", "Mailspring", deb=r"mailspring-{v}-amd64\.deb", rpm=r"mailspring-{v}-[0-9.]+\.x86_64\.rpm"),
    _p("beekeeper-studio", "beekeeper-studio", "beekeeper-studio", appimage=r"Beekeeper-Studio-{v}\.AppImage", deb=r"beekeeper-studio_{v}_amd64\.deb",
       rpm=r"beekeeper-studio-{v}\.x86_64\.rpm"),
    _p("dbeaver", "dbeaver", "dbeaver", deb=r"dbeaver-ce-{v}-linux-x86_64\.deb", rpm=r"dbeaver-ce-{v}-linux-x86_64\.rpm",
       archive=r"dbeaver-ce-{v}-linux-x86_64\.tar\.gz"),
    _p("anytype", "anyproto", "anytype-ts", appimage=r"Anytype-{v}\.AppImage", deb=r"anytype_{v}_amd64\.deb", rpm=r"anytype-{v}\.x86_64\.rpm",
       archive=r"anytype-{v}\.tar\.gz"),
    _p("cryptomator", "cryptomator", "cryptomator", appimage=r"cryptomator-{v}-x86_64\.AppImage", deb=r"cryptomator_{v}-[0-9a-z]+_amd64\.deb"),
    _p("balena-etcher", "balena-io", "etcher", deb=r"balena-etcher_{v}_amd64\.deb", rpm=r"balena-etcher-{v}-1\.x86_64\.rpm"),
    _p("tabby", "Eugeny", "tabby", appimage=r"tabby-{v}-linux-x64\.AppImage", deb=r"tabby-{v}-linux-x64\.deb", rpm=r"tabby-{v}-linux-x64\.rpm",
       archive=r"tabby-{v}-linux-x64\.tar\.gz"),
    _p("cherry-studio", "CherryHQ", "cherry-studio", appimage=r"Cherry-Studio-{v}-linux-x64\.AppImage", deb=r"Cherry-Studio-{v}-linux-x64\.deb",
       rpm=r"Cherry-Studio-{v}-linux-x64\.rpm"),
    _p("appflowy", "AppFlowy-IO", "AppFlowy", appimage=r"AppFlowy-{v}-linux-x86_64\.AppImage", deb=r"AppFlowy-{v}-linux-x86_64\.deb",
       rpm=r"AppFlowy-{v}-linux-x86_64\.rpm", archive=r"AppFlowy-{v}-linux-x86_64\.tar\.gz"),
    _p("cemu", "cemu-project", "Cemu", appimage=r"Cemu-{v}-x86_64\.AppImage"),
    _p("losslesscut", "mifi", "lossless-cut", appimage=r"LosslessCut-linux-x86_64\.AppImage", archive=r"LosslessCut-linux-x64\.tar\.bz2"),
    _p("vesktop", "Vencord", "Vesktop", appimage=r"Vesktop-{v}\.AppImage", deb=r"vesktop_{v}_amd64\.deb", rpm=r"vesktop-{v}\.x86_64\.rpm",
       archive=r"vesktop-{v}\.tar\.gz"),
    _p("ultimaker-cura", "UltiMaker", "Cura", appimage=r"UltiMaker-Cura-{v}-linux-X64\.AppImage"),
    _p("freecad", "FreeCAD", "FreeCAD", appimage=r"FreeCAD_{v}-Linux-x86_64-py[0-9]+\.AppImage"),
)}


def github_host(host: str) -> bool:
    """The hosts a release download may be on: GitHub's own, and the ones it redirects release files to (today release-assets.githubusercontent.com,
    earlier objects.githubusercontent.com: any host under githubusercontent.com)."""
    return host == "github.com" or host.endswith(".githubusercontent.com")


def formats_of(project: Project) -> list[str]:
    return [f for f in FORMATS if f in project.assets]


def _plain(text: str, limit: int = 80) -> str:
    return "".join(c if c.isprintable() else " " for c in text)[:limit]


def _is_own_release_file(url: object, project: Project, tag: str, name: str) -> bool:
    """Is `url` exactly https://github.com/<owner>/<repo>/releases/download/<tag>/<name>, the address GitHub gives a file of that release? Compared
    as a whole, not as a start: dot segments, escapes, queries and fragments are never part of what GitHub hands out, and any of them would let an
    address that begins like the project's reach somewhere else. Only the owner and repository may differ in capital letters (GitHub spells
    them as the project does, which is not always how it is written elsewhere)."""
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        return False
    middle = f"{project.owner}/{project.repo}/releases/download/"
    rest = url[len("https://github.com/"):]
    return rest[:len(middle)].lower() == middle.lower() and rest[len(middle):] == f"{tag}/{name}"


def newest(project: Project, fmt: str, *, fetch: Callable[..., object] = http.get_json) -> Asset:
    """The file of the project's newest release for `fmt`. Raises CygnusError, in words, when there is none, more than one, or the answer
    does not look like a release of that project."""
    pattern = project.assets.get(fmt)
    if pattern is None:
        raise CygnusError(f"{project.key} has no {LABELS.get(fmt, fmt)} download in Cygnus's list")
    if not (_OWNER.fullmatch(project.owner) and _REPO.fullmatch(project.repo)):
        raise CygnusError("that project's address does not look right")
    try:
        data = fetch(LATEST.format(owner=project.owner, repo=project.repo), headers={"Accept": "application/vnd.github+json"}, limit=MAX_ANSWER)
    except http.HttpError as exc:
        if exc.status in (403, 429):
            raise CygnusError("GitHub is limiting how often this connection may ask. Try again in a little while.") from exc
        if exc.status == 404:
            raise CygnusError(f"GitHub has no release of {project.owner}/{project.repo} to offer (the project may have moved)") from exc
        raise CygnusError(f"could not ask GitHub for the newest release: {exc}") from exc
    if not isinstance(data, dict) or data.get("draft") is True or data.get("prerelease") is True:
        raise CygnusError("GitHub's answer for the newest release was not a normal release")
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise CygnusError("GitHub's answer for the newest release had no usable version")
    listed = data.get("assets")
    if not isinstance(listed, list):
        raise CygnusError("GitHub's answer for the newest release listed no files")
    if len(listed) > MAX_ASSETS:  # "exactly one" can only be promised about a list that was looked at to the end
        raise CygnusError(f"the newest release of {project.key} lists more files than Cygnus will look through, so it will not choose one. "
                          "Open its release page instead.")
    regex = re.compile(pattern)
    found = [a for a in listed if isinstance(a, dict) and isinstance(a.get("name"), str) and len(a["name"]) <= MAX_NAME and regex.fullmatch(a["name"])]
    label = LABELS[fmt]
    if not found:
        raise CygnusError(f"the newest release of {project.key} ({_plain(tag, 40)}) has no {label} file Cygnus recognises. "
                          "The maker may have renamed it: open its release page instead.")
    if len(found) > 1:
        raise CygnusError(f"the newest release of {project.key} has more than one file that could be the {label} ({len(found)}), "
                          "so Cygnus will not guess which one. Open its release page instead.")
    item = found[0]
    name = item["name"]
    url = item.get("browser_download_url")
    if not _is_own_release_file(url, project, tag, name):
        raise CygnusError("the download address GitHub gave is not that project's own release address, so Cygnus will not use it")
    size = item.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_FILE:
        raise CygnusError("the file's size, as GitHub gives it, is missing or larger than Cygnus will download")
    if item.get("state") not in (None, "uploaded"):
        raise CygnusError("that file is not fully uploaded on GitHub yet; try again later")
    digest = item.get("digest")
    match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    return Asset(version=tag, name=name, url=url, sha256=match.group(1) if match else None, size=size, format=fmt)
