"""Browse applications (the Discover page): what Flathub and the Arch repositories advertise through AppStream, and the programs that
vendors publish as .deb or .rpm, which Cygnus can convert.

This module only LISTS. It installs nothing, downloads nothing and runs nothing; the page hands a choice over to the flows that already
exist (the Flatpak install on the Install page, a repository package on the Packages page, a vendor's file on the Install page after a
checked download), each with its own plan and confirmation.

Where the data comes from, and how it is read:
- Flathub (or any Flatpak remote): libflatpak says where each remote keeps its AppStream data, in the system and the user installation.
- The Arch repositories: the `archlinux-appstream-data` package, when it is installed (`/usr/share/swcatalog`).
- The AppStream files are read as a stream (never as one document in memory): a document type or entity declaration is refused in any
  encoding, every field is capped, and what is kept is a compact index in Cygnus's cache folder, keyed by the file's identity and date.
- Text is reduced to plain text. Icons are used only as a bare file name inside the folder the data names, a regular file that is not a
  link. Screenshots are not fetched.
- Sources are merged only by the exact AppStream id, and by a small hand-checked map from a vendor's package to its Flathub id
  (cygnus.core.vendor_catalog); never by a similar name.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import stat
import threading
import zlib
from pathlib import Path
from typing import Any, Callable, NamedTuple
from xml.parsers import expat

from cygnus.core import news, paths
from cygnus.core.errors import CygnusError

SCHEMA = 3
MAX_COMPONENTS = 60_000  # applications per AppStream file
MAX_ALL_COMPONENTS = 4 * MAX_COMPONENTS  # components of every kind (add-ons, runtimes, fonts...): they are skipped, but they are still read
MAX_DEPTH = 32  # how deeply the elements may be nested: a real file is about 6
MAX_BYTES = 768 * 1024 * 1024  # of one file, once uncompressed
MAX_ID, MAX_NAME, MAX_SUMMARY, MAX_DEVELOPER, MAX_URL, MAX_DESCRIPTION = 200, 120, 300, 120, 300, 1500
MAX_KEYWORDS, MAX_KEYWORD, MAX_CATEGORIES = 12, 40, 20
PAGE = 40
KINDS = {"desktop-application", "desktop", "console-application"}  # what counts as an application (not an add-on, runtime, font...)
_ICON = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,150}\.(?:png|jxl|webp)\Z")
_PKG = re.compile(r"[a-z0-9@_+][a-z0-9@._+-]{0,99}\Z")
_ID_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}\Z")  # an AppStream id: never a ":" (it would collide with Cygnus's own "vendor:" keys) or a space
_ORIGIN = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")
_HTTPS = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/?#-]*)?\Z")
REPO_BASES = ("/usr/share/swcatalog", "/var/lib/swcatalog", "/usr/share/app-info")

# The kinds of program people look for, in the order they are shown, and the freedesktop categories that put an application in each.
GROUPS = [
    ("browsers", "Web browsers", {"WebBrowser"}),
    ("chat", "Chat and mail", {"InstantMessaging", "Chat", "Email", "VideoConference", "IRCClient", "Telephony", "ContactManagement"}),
    ("development", "Development", {"Development", "IDE", "Debugger", "TextEditor", "RevisionControl", "WebDevelopment", "GUIDesigner", "Building"}),
    ("games", "Games", {"Game", "ArcadeGame", "ActionGame", "StrategyGame", "RolePlaying", "BoardGame", "CardGame", "PuzzleGame", "SportsGame",
                        "SimulationGame", "BlocksGame", "AdventureGame", "KidsGame", "LogicGame", "Emulator", "Shooter"}),
    ("graphics", "Graphics and photos", {"Graphics", "Photography", "2DGraphics", "3DGraphics", "RasterGraphics", "VectorGraphics", "Scanning", "OCR",
                                         "Viewer", "Publishing"}),
    ("media", "Music and video", {"AudioVideo", "Audio", "Video", "Music", "Player", "Recorder", "Midi", "Mixer", "Sequencer", "Tuner", "TV",
                                  "AudioVideoEditing", "DiscBurning"}),
    ("office", "Office", {"Office", "WordProcessor", "Spreadsheet", "Presentation", "Calendar", "ProjectManagement", "Finance", "Dictionary"}),
    ("learn", "Education and science", {"Education", "Science", "Math", "Astronomy", "Chemistry", "Physics", "Biology", "Geography", "Languages",
                                        "Literature", "ArtificialIntelligence", "DataVisualization", "Engineering"}),
    ("system", "System", {"System", "Monitor", "Security", "PackageManager", "Settings", "FileManager", "TerminalEmulator", "Filesystem", "Accessibility"}),
    ("internet", "Internet", {"Network", "FileTransfer", "P2P", "RemoteAccess", "News", "Feed", "WebDevelopment", "Dialup", "HamRadio"}),
    ("utility", "Utilities", {"Utility", "Archiving", "Compression", "Calculator", "Clock", "TextTools", "Maps"}),
]
GROUP_IDS = [g[0] for g in GROUPS] + ["other"]
GROUP_NAMES = {**{g[0]: g[1] for g in GROUPS}, "other": "Other"}


class DiscoverError(CygnusError):
    """The application data could not be read, with the reason in words."""


class Source(NamedTuple):
    kind: str  # "flatpak" (a Flatpak remote's AppStream data) or "repo" (the Arch repositories')
    label: str  # the remote's name, or the file's
    xml: Path
    icons: Path  # the folder with the icon sizes in it; for the repositories the origin is added when the data says it
    remote: str  # the Flatpak remote's name, "" for the repositories


def groups_of(categories: list[str]) -> list[str]:
    """The kinds an application belongs to, from its freedesktop categories ("other" when none fits)."""
    wanted = set(categories)
    found = [gid for gid, _name, cats in GROUPS if wanted & cats]
    if "browsers" in found and "internet" in found:
        found.remove("internet")  # a browser is a browser, not also "Internet"
    return found or ["other"]


# -- reading one AppStream file ------------------------------------------------------------------------------------------------------

def _cut(text: str, limit: int) -> str:
    text = " ".join(("".join(c if c.isprintable() else " " for c in text)).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def cur_has_description(cur: dict[str, Any] | None) -> bool:
    return bool(cur and cur["description"])


class _Reader:
    """Collects the applications of one AppStream document while it is parsed (nothing is kept but what the index needs)."""

    _SIMPLE = {"id", "name", "summary", "developer_name", "pkgname", "icon", "url", "category", "keyword"}
    _BLOCKS = {"p", "ul", "ol", "li", "br"}

    def __init__(self) -> None:
        self.apps: list[dict[str, Any]] = []
        self.origin = ""
        self.stack: list[str] = []
        self.cur: dict[str, Any] | None = None
        self.skip = False
        self.collect: tuple[str, dict[str, str]] | None = None
        self.buf: list[str] = []
        self.size = 0
        self.desc: list[str] | None = None
        self.desc_skip = 0  # the depth of a translated child (<p xml:lang="de">) of a default description while inside it, else 0
        self.desc_len = self.buf_len = 0  # running lengths: summing the lists at every piece of text would cost a hostile file its time
        self.in_developer = False
        self.skip_keywords = False  # inside a translated <keywords xml:lang="...">
        self.components = 0

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        self.stack.append(tag)
        depth = len(self.stack)
        if depth > MAX_DEPTH:
            raise DiscoverError("the application data is nested far more deeply than a real file is")
        if depth == 1:
            self.origin = _cut(attrs.get("origin", ""), 80)
        elif depth == 2 and tag == "component":
            self.components += 1
            if self.components > MAX_ALL_COMPONENTS:
                raise DiscoverError(f"the application data holds more than {MAX_ALL_COMPONENTS} components, which is more than expected")
            self.skip = attrs.get("type", "desktop-application") not in KINDS
            self.cur = None if self.skip else {"id": "", "name": "", "summary": "", "developer": "", "pkgname": "", "icons": {}, "url": "",
                                                "categories": [], "keywords": [], "description": ""}
            if self.cur is not None and len(self.apps) >= MAX_COMPONENTS:
                raise DiscoverError(f"the application data holds more than {MAX_COMPONENTS} applications, which is more than expected")
        elif self.cur is None:
            return
        elif tag == "developer" and depth == 3:
            self.in_developer = True
        elif tag == "description" and depth == 3:
            # only the default text: a translated <description xml:lang="..."> (and everything inside it) is not read, and the first default one wins
            self.desc = [] if "xml:lang" not in attrs and not cur_has_description(self.cur) else None
            self.desc_len, self.desc_skip = 0, 0
        elif tag == "keywords" and depth == 3:
            self.skip_keywords = "xml:lang" in attrs
        elif self.desc is not None:
            if self.desc_skip:
                return
            if "xml:lang" in attrs:  # a translated paragraph or phrase inside the default text: it and everything in it is left out
                self.desc_skip = depth
                return
            if tag in self._BLOCKS and self.desc_len < 20 * MAX_DESCRIPTION:
                piece = "\n• " if tag == "li" else "\n"
                self.desc.append(piece)
                self.desc_len += len(piece)
        elif tag in self._SIMPLE and "xml:lang" not in attrs and not (tag == "keyword" and self.skip_keywords):
            expected = {"category": 4, "keyword": 4}.get(tag, 4 if (tag == "name" and self.in_developer) else 3)
            if depth == expected:
                self.collect, self.buf, self.buf_len = (tag, attrs), [], 0

    def data(self, text: str) -> None:
        self.size += len(text)
        if self.collect is not None:
            if self.buf_len < 2 * MAX_DESCRIPTION:
                self.buf.append(text)
                self.buf_len += len(text)
        elif self.desc is not None and self.cur is not None and not self.desc_skip:
            if self.desc_len < 20 * MAX_DESCRIPTION:
                self.desc.append(text)
                self.desc_len += len(text)

    def end(self, tag: str) -> None:
        depth = len(self.stack)
        cur = self.cur
        if self.desc_skip == depth:
            self.desc_skip = 0
        if cur is not None and self.collect is not None and self.collect[0] == tag:
            name, attrs = self.collect
            text = "".join(self.buf)
            self.collect, self.buf = None, []
            self._take(cur, name, attrs, text)
        elif cur is not None and tag == "description" and depth == 3 and self.desc is not None:
            cur["description"] = news.plain("".join(self.desc), MAX_DESCRIPTION, 30)
            self.desc = None
        elif tag == "keywords" and depth == 3:
            self.skip_keywords = False
        elif tag == "developer" and depth == 3:
            self.in_developer = False
        elif tag == "component" and depth == 2:
            if cur is not None and cur["id"] and cur["name"]:
                self.apps.append(self._finish(cur))
            self.cur, self.skip = None, False
        self.stack.pop()

    def _take(self, cur: dict[str, Any], name: str, attrs: dict[str, str], text: str) -> None:
        if name == "id":
            cur["id"] = _cut(text, MAX_ID) if _ID_OK.match(text.strip()) and not cur["id"] else cur["id"]  # the first, and only a well-formed one
        elif name == "name":
            if self.in_developer:
                cur["developer"] = cur["developer"] or _cut(text, MAX_DEVELOPER)
            else:
                cur["name"] = cur["name"] or _cut(text, MAX_NAME)
        elif name == "summary":
            cur["summary"] = cur["summary"] or _cut(text, MAX_SUMMARY)
        elif name == "developer_name":
            cur["developer"] = cur["developer"] or _cut(text, MAX_DEVELOPER)
        elif name == "pkgname":
            cur["pkgname"] = cur["pkgname"] or text.strip()
        elif name == "icon":
            if attrs.get("type") == "cached" and attrs.get("height", "").isdigit():
                cur["icons"].setdefault(attrs["height"], text.strip())
        elif name == "url":
            if attrs.get("type") == "homepage" and not cur["url"] and _HTTPS.match(text.strip()) and len(text.strip()) <= MAX_URL:
                cur["url"] = text.strip()
        elif name == "category":
            if len(cur["categories"]) < MAX_CATEGORIES:
                cur["categories"].append(_cut(text, 40))
        elif name == "keyword":
            if len(cur["keywords"]) < MAX_KEYWORDS:
                cur["keywords"].append(_cut(text, MAX_KEYWORD))

    @staticmethod
    def _finish(cur: dict[str, Any]) -> dict[str, Any]:
        size = "64" if cur["icons"].get("64") else "128" if cur["icons"].get("128") else ""  # the two sizes Discover looks for; any other is not shown
        icon = cur["icons"].get(size, "") if size else ""
        return {"id": cur["id"], "name": cur["name"], "summary": cur["summary"], "developer": cur["developer"],
                "pkgname": cur["pkgname"] if _PKG.match(cur["pkgname"]) else "", "icon": icon if _ICON.match(icon) else "", "icon_size": size,
                "url": cur["url"], "categories": cur["categories"], "keywords": cur["keywords"], "description": cur["description"]}


def parse(stream: Any) -> tuple[list[dict[str, Any]], str]:
    """(the applications, the origin the file names) read from a stream of AppStream XML. A document type or entity declaration is refused
    in any encoding; a document that is too large, or not readable, is a DiscoverError."""
    reader = _Reader()

    def refuse(*_args: Any) -> None:
        raise DiscoverError("the application data contains a document type or entity declaration, which is never accepted")

    parser = expat.ParserCreate()
    parser.StartElementHandler = reader.start
    parser.EndElementHandler = reader.end
    parser.CharacterDataHandler = reader.data
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    total = 0
    try:
        while True:
            chunk = stream.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise DiscoverError("the application data is far larger than expected, so it was not read")
            parser.Parse(chunk, False)
        parser.Parse(b"", True)
    except (expat.ExpatError, ValueError, LookupError, EOFError, OSError, zlib.error) as exc:
        raise DiscoverError(f"the application data is not readable ({exc})") from exc
    return reader.apps, reader.origin


def _open(path: Path):
    """The file's bytes (a regular file, never a link), decompressed when it is a .gz."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)  # a named pipe must not hold the opening for ever
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DiscoverError(f"{path} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    raw = os.fdopen(fd, "rb")
    return gzip.GzipFile(fileobj=raw) if str(path).endswith(".gz") else raw


# -- where the data is -----------------------------------------------------------------------------------------------------------------

def flatpak_sources(env: Any = None) -> list[Source]:
    """The AppStream data of every enabled Flatpak remote, as libflatpak says where each keeps it (system installations first, then the
    user's); a remote with no data yet is left out, and a remote of the same name is used once."""
    try:
        if env is None:
            from cygnus.core.backends import flatpak as fb

            env = fb.FlatpakEnv()
        arch = env.Flatpak.get_default_arch()
        remotes = env.remotes()
    except Exception:  # noqa: BLE001 - no Flatpak here, or libflatpak unreadable: then there is nothing to list from it
        return []
    out: list[Source] = []
    seen: set[str] = set()
    for _inst, remote in remotes:
        try:
            name = remote.get_name()
            if remote.get_disabled() or name in seen:
                continue
            directory = remote.get_appstream_dir(arch)
            base = Path(directory.get_path()) if directory is not None and directory.get_path() else None
        except Exception:  # noqa: BLE001
            continue
        if base is None:
            continue
        for filename in ("appstream.xml.gz", "appstream.xml"):
            candidate = base / filename
            try:
                if stat.S_ISREG(os.lstat(candidate).st_mode):
                    out.append(Source("flatpak", name, candidate, base / "icons", name))
                    seen.add(name)
                    break
            except OSError:
                continue
    return out


def repo_sources(bases: tuple[str, ...] = REPO_BASES) -> list[Source]:
    """The Arch repositories' AppStream data (the archlinux-appstream-data package), when it is installed."""
    out: list[Source] = []
    for base in bases:
        for sub in ("xml", "xmls"):
            folder = Path(base) / sub
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            for name in names:
                if not (name.endswith(".xml") or name.endswith(".xml.gz")):
                    continue
                try:
                    if stat.S_ISREG(os.lstat(folder / name).st_mode):
                        out.append(Source("repo", name.split(".xml")[0], folder / name, Path(base) / "icons", ""))
                except OSError:
                    continue
    return out


def _stamp(path: Path) -> list[Any]:
    st = os.lstat(path)
    return [str(path), st.st_size, st.st_mtime_ns, st.st_ino]


def signature(sources: list[Source], vendors: list[dict[str, Any]]) -> str:
    """What the merged catalogue is made of: each data file as it is now (a file that vanished is left out) and the vendors' entries. A
    change in any of them is a different signature, so the catalogue is built again."""
    parts: list[Any] = []
    for source in sources:
        try:
            parts.append([source.kind, source.label, _stamp(source.xml)])
        except OSError:
            continue
    return hashlib.sha256(json.dumps([parts, vendors], sort_keys=True, default=str).encode()).hexdigest()


_MEMORY: dict[str, tuple[list[Any], dict[str, Any]]] = {}
_BUILD = threading.Lock()


_TEXT_FIELDS = ("id", "name", "summary", "developer", "pkgname", "icon", "icon_size", "url", "description")


def _valid_app(app: Any) -> bool:
    """One application of a saved index has the shape and limits the reader gives it (the file is in the person's own cache folder, but whatever is
    in it must not be able to break the list)."""
    return (isinstance(app, dict) and all(isinstance(app.get(k), str) and len(app[k]) <= MAX_DESCRIPTION + 5 for k in _TEXT_FIELDS)
            and bool(app["id"]) and bool(app["name"])
            and all(isinstance(app.get(k), list) and len(app[k]) <= MAX_CATEGORIES and all(isinstance(x, str) and len(x) <= 120 for x in app[k])
                    for k in ("categories", "keywords")))


def load_index(source: Source) -> dict[str, Any]:
    """The compact index of one AppStream file: from memory, else from the cache folder (when the file is still the one it was made
    from), else built now by reading the file (and saved for next time)."""
    stamp = _stamp(source.xml)
    key = str(source.xml)
    hit = _MEMORY.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    with _BUILD:
        hit = _MEMORY.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        cache = paths.cache_dir() / "discover" / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
        index = None
        try:
            loaded = json.loads(cache.read_text(encoding="utf-8"))
            if (isinstance(loaded, dict) and loaded.get("schema") == SCHEMA and loaded.get("stamp") == stamp and isinstance(loaded.get("apps"), list)
                    and isinstance(loaded.get("origin", ""), str) and all(_valid_app(a) for a in loaded["apps"])):
                index = loaded
        except (OSError, ValueError):
            pass
        if index is None:
            with _open(source.xml) as stream:
                apps, origin = parse(stream)
            index = {"schema": SCHEMA, "stamp": stamp, "origin": origin, "apps": apps}
            try:
                from cygnus.core.util.fs import atomic_write

                cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write(cache, json.dumps(index, ensure_ascii=False).encode(), 0o600)
            except OSError:
                pass  # the index still works from memory; it is only built again next time
        _MEMORY[key] = (stamp, index)
        return index


def icon_path(source: Source, origin: str, basename: str, size: str = "64") -> str | None:
    """The file an icon is, or None: only a bare file name of a known kind, in the size folder (64x64 or 128x128) of the folder the data names, a
    regular file that is not a link. A repository's origin comes from the data and is a folder name there: anything else is refused."""
    if not _ICON.match(basename or "") or size not in ("64", "128"):
        return None
    if source.kind == "repo" and origin and (not _ORIGIN.match(origin) or origin in (".", "..")):
        return None
    folder = source.icons / origin / f"{size}x{size}" if source.kind == "repo" and origin else source.icons / f"{size}x{size}"
    path = folder / basename
    try:
        return str(path) if stat.S_ISREG(os.lstat(path).st_mode) else None
    except OSError:
        return None


# -- the merged catalogue ----------------------------------------------------------------------------------------------------------------

def _app_key(app_id: str) -> str:
    return app_id[:-len(".desktop")] if app_id.endswith(".desktop") else app_id


def build_entries(sources: list[Source], vendors: list[dict[str, Any]], *, loader: Callable[[Source], dict[str, Any]] = load_index) -> list[dict[str, Any]]:
    """One entry per application: what the Flatpak remotes and the repositories say, merged by the exact AppStream id, plus the vendors'
    .deb/.rpm programs (merged into a Flatpak entry only where the vendor's Flathub id is in that data; otherwise listed on their own).
    Each entry: key, name, summary, developer, groups, categories, icon (a file path or ""), icon_name (a theme icon for the fallback),
    url, description, sources: [{kind, label, ...}]."""
    entries: dict[str, dict[str, Any]] = {}
    for source in sources:
        index = loader(source)
        origin = index.get("origin", "")
        for app in index["apps"]:
            key = _app_key(app["id"])
            entry = entries.get(key)
            if entry is None:
                entry = entries[key] = {"key": key, "name": app["name"], "summary": app["summary"], "developer": app["developer"],
                                        "categories": list(app["categories"]), "keywords": list(app["keywords"]), "url": app["url"],
                                        "description": app["description"], "icon": "", "icon_name": "package-x-generic", "sources": []}
            for field in ("summary", "developer", "url", "description"):  # fill what the first source did not say
                if not entry[field] and app[field]:
                    entry[field] = app[field]
            for field in ("categories", "keywords"):  # the first source that says something about the kind of program
                if not entry[field] and app[field]:
                    entry[field] = list(app[field])
            if not entry["icon"]:
                entry["icon"] = icon_path(source, origin, app["icon"], app.get("icon_size") or "64") or ""
            if source.kind == "flatpak":
                record = {"kind": "flatpak", "remote": source.remote, "id": key, "label": f"{source.remote} (Flatpak)"}  # the reference's own name has no ".desktop"
            elif app["pkgname"]:
                record = {"kind": "repo", "package": app["pkgname"], "label": "Your repositories (pacman)"}
            else:
                continue
            if record not in entry["sources"]:  # one program published on several branches (Wine has nine) is still one way to get it
                entry["sources"].append(record)
    for vendor in vendors:
        flathub_id = vendor.get("flathub_id") or ""
        target = entries.get(flathub_id) if flathub_id else None
        source = {"kind": "vendor", "package": vendor["package"], "formats": vendor["formats"], "label": "The vendor's own package (.deb / .rpm), converted by Cygnus",
                  "note": vendor.get("note", "")}
        if target is not None and any(s["kind"] == "flatpak" for s in target["sources"]):
            target["sources"].append(source)
            continue
        key = f"vendor:{vendor['package']}"
        entries[key] = {"key": key, "name": vendor["name"], "summary": vendor["summary"], "developer": vendor.get("developer", ""),
                        "categories": list(vendor["categories"]), "keywords": [], "url": vendor.get("url", ""), "description": vendor.get("description", ""),
                        "icon": "", "icon_name": vendor.get("icon_name", "package-x-generic"), "sources": [source]}
    out = []
    for entry in entries.values():
        if not entry["sources"]:
            continue  # an entry only listed by a source that cannot install it (a repository component with no package name)
        entry["groups"] = groups_of(entry["categories"])
        out.append(entry)
    out.sort(key=lambda e: (e["name"].lower(), e["key"]))
    return out


# -- searching -----------------------------------------------------------------------------------------------------------------------------

def _rank(entry: dict[str, Any], terms: list[str]) -> int | None:
    name, summary = entry["name"].lower(), entry["summary"].lower()
    keywords = " ".join(entry["keywords"]).lower()
    other = f"{entry['developer']} {entry['key']}".lower()
    for term in terms:
        if not (term in name or term in summary or term in keywords or term in other):
            return None
    joined = " ".join(terms)
    if name == joined:
        return 0
    if all(t in name for t in terms):
        return 1 if name.startswith(terms[0]) else 2
    if all(t in name or t in keywords for t in terms):
        return 3
    return 4


def search(entries: list[dict[str, Any]], query: str = "", group: str = "", kind: str = "", offset: int = 0, limit: int = PAGE) -> dict[str, Any]:
    """One page of results, best first: {"total", "offset", "entries"}, and how many applications each group has among all the matches of the
    query ("groups"). An empty query lists everything (the popular order is not known, so alphabetical)."""
    terms = [t for t in query.lower().split() if t][:8]
    ranked = []
    for entry in entries:
        rank = _rank(entry, terms) if terms else 0
        if rank is not None and (not kind or any(s["kind"] == kind for s in entry["sources"])):
            ranked.append((rank, entry))
    counts = {gid: 0 for gid in GROUP_IDS}
    for _rank_, entry in ranked:
        for gid in entry["groups"]:
            counts[gid] += 1
    chosen = [(r, e) for r, e in ranked if not group or group in e["groups"]]
    if terms:
        chosen.sort(key=lambda p: (p[0], len(p[1]["name"]), p[1]["name"].lower()))
    page = chosen[max(0, offset):max(0, offset) + max(1, min(limit, 200))]
    return {"total": len(chosen), "offset": max(0, offset), "entries": [e for _r, e in page],
            "groups": [{"id": gid, "name": GROUP_NAMES[gid], "count": counts[gid]} for gid in GROUP_IDS if counts[gid] or gid == group]}
