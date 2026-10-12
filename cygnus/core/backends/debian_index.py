"""Debian's applications as a catalogue for Discover: the programs Debian's AppStream data (DEP-11) lists as desktop applications, joined to
Debian's package list for where each .deb is, how big it is and its SHA-256.

Both files come from deb.debian.org over HTTPS and are read as hostile text: they are decompressed with a limit, read line by line (never as one
document), and every field kept is cut to plain text and checked against a strict pattern. A package is listed only when it has a file in the
package list for this processor; a program whose package name is also in the AUR is left out (the Arch way is preferred), and the repositories'
own names are compared when Discover's lists are built (cygnus.core.discover).

What a Debian package is, here: a file built for Debian, with Debian's libraries and package names. Cygnus can try to convert it (the Install page
shows what it found), but it may not run: it can need other Debian packages that Arch does not have, or libraries in another version. Nothing here
checks Debian's own signature: the connection is HTTPS and the .deb is checked against the checksum in Debian's list, which came from the same host."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from cygnus.core.backends import catalogue_common as common
from cygnus.core.errors import CygnusError
from cygnus.core.util import http
from cygnus.core.util.text import plain

ID = "debian"
HOST = "deb.debian.org"
BASE = f"https://{HOST}/debian/"
SUITE = "stable"
APPSTREAM = f"{BASE}dists/{SUITE}/main/dep11/Components-amd64.yml.xz"
PACKAGES = f"{BASE}dists/{SUITE}/main/binary-amd64/Packages.xz"
AUR_NAMES = common.AUR_NAMES
MAX_COMPRESSED = 48 * 1024 * 1024
MAX_TEXT = 640 * 1024 * 1024  # of one file, once decompressed (the real ones are about 40 MB)
MAX_FILE = 2 * 1024**3
MAX_NAME, MAX_SUMMARY, MAX_URL = 120, 300, 300
MAX_DEPENDS = 40
MAX_APPS = 20_000

_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]{1,99}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+~:_-]{0,79}\Z")
_FILENAME = re.compile(r"pool/main/[a-z0-9][a-z0-9+.-]{0,99}/[a-z0-9][a-z0-9+.-]{1,99}/[A-Za-z0-9][A-Za-z0-9.+~_-]{0,199}_(?:amd64|all)\.deb\Z")
_SHA256 = common.SHA256
_CATEGORY, _APP_ID, _HTTPS = common.CATEGORY, common.APP_ID, common.HTTPS
_DEP_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]{1,99}(?::[a-z0-9]+)?\Z")


def host_ok(host: str) -> bool:
    return host == HOST


aur_host_ok = common.aur_host_ok
aur_names = common.aur_names


def file_url(filename: str) -> str:
    """The address of a .deb from its name in the package list (which has been checked against _FILENAME)."""
    return BASE + filename


# -- decompressing with a limit -----------------------------------------------------------------------------------------------------------

def text_lines(data: bytes, kind: str, limit: int = MAX_TEXT) -> Iterator[str]:
    """The lines of a compressed text file ("xz" or "gz"), decompressed in pieces; more than `limit` bytes, or damaged data, is an error in words,
    so a small download cannot become an enormous text."""
    return common.text_lines(data, kind, limit, "Debian")


# -- Debian's AppStream data (DEP-11, a small corner of YAML) ------------------------------------------------------------------------------

def _scalar(first: str, more: list[str]) -> str:
    """The text of a YAML scalar that starts after "key:" and may go on over following lines (plain, 'single' or "double" quoted, or a block)."""
    first = first.strip()
    parts = [first] + [m.strip() for m in more]
    if first[:1] in (">", "|"):
        return " ".join(p for p in parts[1:] if p)
    text = " ".join(p for p in parts if p)
    if text[:1] == "'":
        body = text[1:]
        body = body[:-1] if body.endswith("'") else body
        return body.replace("''", "'")
    if text[:1] == '"':
        body = text[1:]
        body = body[:-1] if body.endswith('"') and not body.endswith('\\"') else body
        out, i = [], 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                n = body[i + 1]
                if n in "nrt":
                    out.append(" ")
                elif n in '"\\/':
                    out.append(n)
                elif n in "uU":
                    width = 4 if n == "u" else 8
                    digits = body[i + 2:i + 2 + width]
                    try:
                        out.append(chr(int(digits, 16)))
                        i += width
                    except (ValueError, OverflowError):
                        pass
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out)
    return text


MAX_DOC_LINES = 20_000  # of one document: a real one has up to a few hundred; the rest of a longer one is read past and not kept


def _documents(lines: Iterable[str]) -> Iterator[list[str]]:
    doc: list[str] = []
    for line in lines:
        if line == "---":
            if doc:
                yield doc
            doc = []
        elif len(doc) < MAX_DOC_LINES:
            doc.append(line)
    if doc:
        yield doc


def _read_app(doc: list[str]) -> dict[str, Any] | None:
    """What Discover needs of one DEP-11 document: the application's id, package, name, summary, categories and website, or None when it is not a
    desktop application (an add-on, a font, a codec...) or does not say which package it is in."""
    top: dict[str, str] = {}
    maps: dict[str, dict[str, str]] = {}
    categories: list[str] = []
    key = ""
    child = 0  # how far the entries of the current key are indented (read from its first one: files differ)
    pending: tuple[str, str, list[str]] | None = None  # (map name, entry name, the first line of its value, lines that continue it)

    def close() -> None:
        nonlocal pending
        if pending is not None:
            name, entry, first_and_more = pending[0], pending[1], pending[2]
            if entry in ("C", "homepage") and entry not in maps.setdefault(name, {}):
                maps[name][entry] = _scalar(first_and_more[0], first_and_more[1:])
            pending = None

    for line in doc:
        if not line or line.startswith("#"):
            continue
        if not line[0].isspace() and not line.startswith("- "):
            close()
            name, _, value = line.partition(":")
            key, child = name, 0
            if value.strip():
                top[name] = _scalar(value, [])
            continue
        stripped = line.lstrip()
        if key == "Categories" and stripped.startswith("- "):
            categories.append(stripped[2:].strip())
            continue
        if key in ("Name", "Summary", "Url"):
            indent = len(line) - len(stripped)
            child = child or indent
            if indent == child:
                close()
                entry, _, value = stripped.partition(":")
                pending = (key, entry.strip("'\""), [value])
            elif pending is not None and indent > child:
                pending[2].append(line)
    close()
    if top.get("Type") != "desktop-application" or not _PACKAGE.fullmatch(top.get("Package", "")):
        return None
    app_id = top.get("ID", "")
    if not _APP_ID.fullmatch(app_id):
        return None
    url = maps.get("Url", {}).get("homepage", "")
    return {"id": app_id.removesuffix(".desktop"), "package": top["Package"], "name": plain(maps.get("Name", {}).get("C", ""), MAX_NAME),
            "summary": plain(maps.get("Summary", {}).get("C", ""), MAX_SUMMARY),
            "categories": [c for c in categories if _CATEGORY.fullmatch(c)][:12], "url": url if len(url) <= MAX_URL and _HTTPS.fullmatch(url) else ""}


def apps(lines: Iterable[str]) -> dict[str, dict[str, Any]]:
    """The desktop applications of a DEP-11 file by package name. A package with several applications is one entry (see `catalogue_common.Group`)."""
    found: dict[str, common.Group] = {}
    for doc in _documents(lines):
        app = _read_app(doc)
        if app is None or not app["name"]:
            continue
        if app["package"] in found:
            found[app["package"]].add(app["name"], app["id"])
        elif len(found) < MAX_APPS:
            found[app["package"]] = common.Group(app, app["name"], app["id"])
    return {package: group.entry() for package, group in found.items()}


# -- Debian's package list ------------------------------------------------------------------------------------------------------------------

def _dependency_names(*fields: str) -> list[str]:
    """The package names a Depends-style field mentions (the first of each "a | b" choice), in order, without version conditions."""
    names: dict[str, None] = {}
    for field in fields:
        for part in field.split(","):
            first = part.split("|")[0].strip().split(" ")[0].split("(")[0].strip()
            if _DEP_NAME.fullmatch(first):
                names.setdefault(first, None)
                if len(names) >= MAX_DEPENDS:
                    return list(names)
    return list(names)


def packages(lines: Iterable[str], wanted: set[str]) -> dict[str, dict[str, Any]]:
    """The entries of Debian's package list for the packages in `wanted` that have a file for this processor: where it is, its size and SHA-256, its
    version and what it says it needs."""
    found: dict[str, dict[str, Any]] = {}
    stanza: dict[str, str] = {}

    def finish() -> None:
        name = stanza.get("Package", "")
        if name in wanted and name not in found and stanza.get("Architecture") in ("amd64", "all"):
            record = _package_record(stanza)
            if record is not None:
                found[name] = record

    for line in lines:
        if not line:
            finish()
            stanza = {}
        elif line[0] in " \t":
            continue  # a field that goes on over several lines is not one of the fields read here
        else:
            field, _, value = line.partition(":")
            if field == "Package" and "Package" in stanza:  # a new entry without the blank line before it: nothing of the last one carries over
                finish()
                stanza = {}
            if field in ("Package", "Version", "Architecture", "Filename", "Size", "SHA256", "Depends", "Pre-Depends"):
                stanza[field] = value.strip()
    finish()
    return found


def _package_record(stanza: dict[str, str]) -> dict[str, Any] | None:
    filename, sha = stanza.get("Filename", ""), stanza.get("SHA256", "")
    size = stanza.get("Size", "")
    version = stanza.get("Version", "")
    if not (_FILENAME.fullmatch(filename) and _SHA256.fullmatch(sha) and _VERSION.fullmatch(version) and size.isdecimal() and 0 < int(size) <= MAX_FILE):
        return None
    return {"version": version, "filename": filename, "sha256": sha, "size": int(size), "depends": _dependency_names(stanza.get("Pre-Depends", ""), stanza.get("Depends", ""))}


def records(app_by_package: dict[str, dict[str, Any]], package_by_name: dict[str, dict[str, Any]], excluded: set[str] = frozenset()) -> list[dict[str, Any]]:
    """Catalogue records: each application that has a file in the package list and whose package is not one the AUR has."""
    out = []
    for name in sorted(app_by_package):
        pkg, app = package_by_name.get(name), app_by_package[name]
        if pkg is None or name in excluded:
            continue
        out.append({"package": name, "id": app["id"], "ids": app.get("ids") or [app["id"]], "name": app["name"], "summary": app["summary"],
                    "categories": app["categories"], "url": app["url"], **pkg})
    return out


def clean(raw: Any) -> list[dict[str, Any]]:
    """Records read back from the cache folder, checked again as strictly as when they were made."""
    out, seen = [], set()
    for rec in raw if isinstance(raw, list) else []:
        if not isinstance(rec, dict):
            continue
        strings = ("package", "id", "name", "summary", "url", "version", "filename", "sha256")
        if not all(isinstance(rec.get(k), str) for k in strings):
            continue
        size = rec.get("size")
        deps, cats = rec.get("depends"), rec.get("categories")
        if (not _PACKAGE.fullmatch(rec["package"]) or rec["package"] in seen or not _APP_ID.fullmatch(rec["id"]) or not rec["name"].strip()
                or not _FILENAME.fullmatch(rec["filename"]) or not _SHA256.fullmatch(rec["sha256"]) or not _VERSION.fullmatch(rec["version"])
                or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_FILE):
            continue
        seen.add(rec["package"])
        ids = rec.get("ids")
        ids = [i for i in ids if isinstance(i, str) and _APP_ID.fullmatch(i)][:8] if isinstance(ids, list) else []
        out.append({"package": rec["package"], "id": rec["id"], "ids": ids or [rec["id"]], "name": plain(rec["name"], MAX_NAME), "summary": plain(rec["summary"], MAX_SUMMARY),
                    "categories": [c for c in cats if isinstance(c, str) and _CATEGORY.fullmatch(c)][:12] if isinstance(cats, list) else [],
                    "url": rec["url"] if len(rec["url"]) <= MAX_URL and _HTTPS.fullmatch(rec["url"]) else "", "version": rec["version"],
                    "filename": rec["filename"], "sha256": rec["sha256"], "size": size,
                    "depends": [d for d in deps if isinstance(d, str) and _DEP_NAME.fullmatch(d)][:MAX_DEPENDS] if isinstance(deps, list) else []})
    return out


def fetch(progress: Callable[[str], None] = lambda _: None, *, get: Callable[..., bytes] = http.get) -> list[dict[str, Any]]:
    """Download Debian's application data, package list and the AUR's package names, and join them (about fifteen megabytes in all)."""
    progress("Reading Debian's list of applications…")
    app_by_package = apps(text_lines(get(APPSTREAM, limit=MAX_COMPRESSED, host_ok=host_ok, timeout=60), "xz"))
    progress("Reading Debian's package list…")
    package_by_name = packages(text_lines(get(PACKAGES, limit=MAX_COMPRESSED, host_ok=host_ok, timeout=60), "xz"), set(app_by_package))
    progress("Leaving out what the AUR already has…")
    excluded = aur_names(text_lines(get(AUR_NAMES, limit=MAX_COMPRESSED, host_ok=aur_host_ok, timeout=60), "gz"))
    return records(app_by_package, package_by_name, excluded)
