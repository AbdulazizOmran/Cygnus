"""Fedora's applications as a catalogue for Discover: the programs Fedora's AppStream data lists, joined to the release's package list (repodata) for
where each .rpm is, how big it is and its SHA-256.

All files come from dl.fedoraproject.org over HTTPS and are read as hostile data: decompressed with a limit, the package list is cut into its
`<package>` entries and only the entries of listed programs are parsed, and every field kept is cut to plain text and checked against a strict
pattern. The list of packages is checked against the checksum the release's own `repomd.xml` gives for it. A program whose package name is also
in the AUR is left out (the Arch way is preferred); the repositories' own names are compared when Discover's lists are built.

What a Fedora package is, here: a file built for Fedora, with Fedora's libraries and package names. Cygnus can try to convert it (the Install page
shows what it found), but it may not run. Nothing here checks Fedora's own signatures: the connection is HTTPS and the .rpm is checked against
the checksum in Fedora's package list, which came from the same host."""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from typing import Any

from cygnus.core import discover
from cygnus.core.backends import catalogue_common as common
from cygnus.core.errors import CygnusError
from cygnus.core.util import http
from cygnus.core.util.text import plain

ID = "fedora"
HOST = "dl.fedoraproject.org"
RELEASES = f"https://{HOST}/pub/fedora/linux/releases/"
SCREENSHOTS = f"https://{HOST}/pub/alt/screenshots/"
AUR_NAMES = common.AUR_NAMES
MAX_COMPRESSED = 64 * 1024 * 1024
MAX_TEXT = 1024 * 1024 * 1024  # of the package list, once decompressed (the real one is about 185 MB)
MAX_ENTRY = 4 * 1024 * 1024  # one package's entry; a real one is a few kilobytes
MAX_FILE = 2 * 1024**3
MAX_NAME, MAX_SUMMARY, MAX_URL = 120, 300, 300
MAX_DEPENDS = 40
MAX_APPS = 20_000

_RELEASE = re.compile(r"[1-9][0-9]{1,2}\Z")
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]{1,99}\Z")
_VERSION = re.compile(r"(?:[0-9]{1,5}:)?[A-Za-z0-9][A-Za-z0-9.+~_-]{0,79}\Z")
_LOCATION = re.compile(r"Packages/[a-z0-9]/[A-Za-z0-9][A-Za-z0-9._+~-]{0,199}\.(?:x86_64|noarch)\.rpm\Z")
_PRIMARY = re.compile(r"repodata/[0-9a-f]{64}-primary\.xml\.(zst|gz|xz)\Z")
_REQUIRE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]{1,99}\Z")
_NAME_IN_ENTRY = re.compile(rb"<name>([^<]{1,100})</name>")
_NEVER_IN_A_LIST = re.compile(rb"<!--|<!\[CDATA\[|<!DOCTYPE")
_RPM_NS = "urn:cygnus-rpm"


def host_ok(host: str) -> bool:
    return host == HOST


aur_host_ok = common.aur_host_ok
aur_names = common.aur_names


def tree(release: int) -> str:
    return f"{RELEASES}{release}/Everything/x86_64/os/"


def appstream_url(release: int) -> str:
    return f"{SCREENSHOTS}f{release}/fedora-{release}.xml.gz"


def file_url(release: int, location: str) -> str:
    """The address of an .rpm from its place in the release's package list (which has been checked against _LOCATION)."""
    return tree(release) + location


def releases_in(listing: str) -> list[int]:
    """The release numbers a directory listing links to, the newest first."""
    return sorted({int(m) for m in re.findall(r'href="([0-9]{2,3})/"', listing) if _RELEASE.fullmatch(m)}, reverse=True)


# -- the release's metadata -------------------------------------------------------------------------------------------------------------------

def primary_location(repomd: bytes) -> tuple[str, str, str]:
    """(the package list's place, its SHA-256, how it is compressed) from a release's repomd.xml; an error in words when the file is not one."""
    try:
        root = ET.fromstring(repomd)
    except ET.ParseError as exc:
        raise CygnusError("Fedora's release description could not be read") from exc
    for data in root:
        if data.tag.endswith("}data") and data.get("type") == "primary":
            ns = data.tag[:-len("data")]
            location, checksum = data.find(f"{ns}location"), data.find(f"{ns}checksum")
            href = location.get("href", "") if location is not None else ""
            digest = (checksum.text or "").strip() if checksum is not None and checksum.get("type") == "sha256" else ""
            match = _PRIMARY.fullmatch(href)
            if match and common.SHA256.fullmatch(digest) and href.startswith(f"repodata/{digest}-"):
                return href, digest, match.group(1)
    raise CygnusError("Fedora's release description has no package list Cygnus can read")


# -- Fedora's AppStream data --------------------------------------------------------------------------------------------------------------------

def apps(parsed: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The applications Fedora's AppStream data lists, by package name. A package with several applications is one entry (`catalogue_common.Group`)."""
    found: dict[str, common.Group] = {}
    for app in parsed:
        package = app.get("pkgname", "")
        app_id = app["id"].removesuffix(".desktop")
        if not (_PACKAGE.fullmatch(package) and common.APP_ID.fullmatch(app_id) and app["name"].strip()):
            continue
        name = plain(app["name"], MAX_NAME)
        if package in found:
            found[package].add(name, app_id)
        elif len(found) < MAX_APPS:
            first = {"id": app_id, "package": package, "name": name, "summary": plain(app["summary"], MAX_SUMMARY),
                     "categories": [c for c in app["categories"] if common.CATEGORY.fullmatch(c)][:12],
                     "url": app["url"] if len(app["url"]) <= MAX_URL and common.HTTPS.fullmatch(app["url"]) else ""}
            found[package] = common.Group(first, name, app_id)
    return {package: group.entry() for package, group in found.items()}


# -- the package list -----------------------------------------------------------------------------------------------------------------------------

def _requires(names: Iterable[str]) -> list[str]:
    """The package names a package requires, in order: not library files, other files, rpm's own and "(…)" conditions, and not twice."""
    out: dict[str, None] = {}
    for name in names:
        if _REQUIRE.fullmatch(name) and ".so" not in name:
            out.setdefault(name, None)
            if len(out) >= MAX_DEPENDS:
                break
    return list(out)


def _entry(block: bytes) -> dict[str, Any] | None:
    """One package's entry of the list, or None when it is not a clean file for this processor."""
    try:
        root = ET.fromstring(b'<list xmlns:rpm="' + _RPM_NS.encode() + b'">' + block + b"</list>")
    except ET.ParseError:
        return None
    package = root.find("package")
    if package is None:
        return None
    version, checksum, size, location = package.find("version"), package.find("checksum"), package.find("size"), package.find("location")
    if version is None or checksum is None or size is None or location is None or package.findtext("arch") not in ("x86_64", "noarch"):
        return None
    epoch = version.get("epoch", "0")
    text = f"{version.get('ver', '')}-{version.get('rel', '')}"
    text = text if epoch in ("0", "") else f"{epoch}:{text}"
    href, digest, length = location.get("href", ""), (checksum.text or "").strip(), size.get("package", "")
    if not (_VERSION.fullmatch(text) and _LOCATION.fullmatch(href) and checksum.get("type") == "sha256" and common.SHA256.fullmatch(digest)
            and length.isdecimal() and 0 < int(length) <= MAX_FILE):
        return None
    requires = package.find(f"format/{{{_RPM_NS}}}requires")
    names = [e.get("name", "") for e in requires if e.tag == f"{{{_RPM_NS}}}entry"] if requires is not None else []
    return {"version": text, "location": href, "sha256": digest, "size": int(length), "depends": _requires(names)}


def packages(pieces: Iterable[bytes], wanted: set[str]) -> dict[str, dict[str, Any]]:
    """The entries of a release's package list for the packages in `wanted` that have a file for this processor. The list is read in pieces and cut at
    each `</package>`; only the entries of wanted packages are parsed (the list holds tens of thousands)."""
    found: dict[str, dict[str, Any]] = {}
    pending = b""
    end = b"</package>"
    for piece in pieces:
        pending += piece
        # A real list never has a comment, a CDATA section or a document type (its text is escaped). One that does could hide a whole entry of another program
        # inside a description, where cutting at "</package>" would find it: such a list is not read.
        if _NEVER_IN_A_LIST.search(pending):
            raise CygnusError("Fedora's package list has constructs a real one never has, so it was not used")
        while True:
            at = pending.find(end)
            if at < 0:
                break
            block, pending = pending[:at + len(end)], pending[at + len(end):]
            start = max(block.rfind(b"<package "), block.rfind(b"<package>"))  # not "<packager>", which is an element inside the entry
            named = _NAME_IN_ENTRY.search(block, start) if start >= 0 else None
            if named is None:
                continue
            name = named.group(1).decode("utf-8", "replace")
            if name in wanted and name not in found:
                entry = _entry(block[start:])
                if entry is not None:
                    found[name] = entry
        if len(pending) > MAX_ENTRY:
            raise CygnusError("Fedora's package list has an entry that is far larger than expected, so it was not read")
    return found


def records(app_by_package: dict[str, dict[str, Any]], package_by_name: dict[str, dict[str, Any]], release: int, excluded: set[str] = frozenset()
            ) -> list[dict[str, Any]]:
    """Catalogue records: each application that has a file in the package list and whose package is not one the AUR has."""
    out = []
    for name in sorted(app_by_package):
        pkg, app = package_by_name.get(name), app_by_package[name]
        if pkg is None or name in excluded:
            continue
        out.append({"package": name, "id": app["id"], "ids": app.get("ids") or [app["id"]], "name": app["name"], "summary": app["summary"],
                    "categories": app["categories"], "url": app["url"], "release": release, **pkg})
    return out


def clean(raw: Any) -> list[dict[str, Any]]:
    """Records read back from the cache folder, checked again as strictly as when they were made."""
    out, seen = [], set()
    for rec in raw if isinstance(raw, list) else []:
        if not isinstance(rec, dict):
            continue
        strings = ("package", "id", "name", "summary", "url", "version", "location", "sha256")
        if not all(isinstance(rec.get(k), str) for k in strings):
            continue
        size, release = rec.get("size"), rec.get("release")
        deps, cats = rec.get("depends"), rec.get("categories")
        if (not _PACKAGE.fullmatch(rec["package"]) or rec["package"] in seen or not common.APP_ID.fullmatch(rec["id"]) or not rec["name"].strip()
                or not _LOCATION.fullmatch(rec["location"]) or not common.SHA256.fullmatch(rec["sha256"]) or not _VERSION.fullmatch(rec["version"])
                or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_FILE
                or isinstance(release, bool) or not isinstance(release, int) or not _RELEASE.fullmatch(str(release))):
            continue
        seen.add(rec["package"])
        ids = rec.get("ids")
        ids = [i for i in ids if isinstance(i, str) and common.APP_ID.fullmatch(i)][:8] if isinstance(ids, list) else []
        out.append({"package": rec["package"], "id": rec["id"], "ids": ids or [rec["id"]], "name": plain(rec["name"], MAX_NAME), "summary": plain(rec["summary"], MAX_SUMMARY),
                    "categories": [c for c in cats if isinstance(c, str) and common.CATEGORY.fullmatch(c)][:12] if isinstance(cats, list) else [],
                    "url": rec["url"] if len(rec["url"]) <= MAX_URL and common.HTTPS.fullmatch(rec["url"]) else "", "release": release, "version": rec["version"],
                    "location": rec["location"], "sha256": rec["sha256"], "size": size,
                    "depends": _requires(d for d in deps if isinstance(d, str)) if isinstance(deps, list) else []})
    return out


def fetch(progress: Callable[[str], None] = lambda _: None, *, get: Callable[..., bytes] = http.get) -> list[dict[str, Any]]:
    """Download Fedora's application data and package list for the newest release, and the AUR's package names, and join them (about thirty
    megabytes in all)."""
    progress("Finding Fedora's newest release…")
    listing = get(RELEASES, limit=2 * 1024 * 1024, host_ok=host_ok).decode("utf-8", "replace")
    found = None
    for candidate in releases_in(listing)[:4]:
        try:  # a release that is only being prepared has a folder and nothing in it yet: the one before it is used
            found = (candidate, get(f"{tree(candidate)}repodata/repomd.xml", limit=1024 * 1024, host_ok=host_ok),
                     get(appstream_url(candidate), limit=common.CHUNK * 1024, host_ok=host_ok, timeout=60))
            break
        except http.HttpError:
            continue
    if found is None:
        raise CygnusError("Cygnus could not find a Fedora release with a package list and a list of applications")
    release, repomd, appstream = found
    href, digest, kind = primary_location(repomd)
    progress(f"Reading Fedora {release}'s list of applications…")
    parsed, _origin = discover.parse(gzip.GzipFile(fileobj=io.BytesIO(appstream)))
    app_by_package = apps(parsed)
    progress(f"Reading Fedora {release}'s package list…")
    primary = get(tree(release) + href, limit=MAX_COMPRESSED, host_ok=host_ok, timeout=120)
    if hashlib.sha256(primary).hexdigest() != digest:
        raise CygnusError("Fedora's package list does not match the checksum its release gives for it, so it was not used")
    package_by_name = packages(common.chunks(primary, kind, MAX_TEXT, "Fedora"), set(app_by_package))
    progress("Leaving out what the AUR already has…")
    excluded = aur_names(common.text_lines(get(AUR_NAMES, limit=common.CHUNK * 1024, host_ok=aur_host_ok, timeout=60), "gz", 64 * 1024 * 1024, "the AUR"))
    return records(app_by_package, package_by_name, release, excluded)
