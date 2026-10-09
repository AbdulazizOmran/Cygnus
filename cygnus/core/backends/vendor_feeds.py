"""Where a newer version of a converted .deb/.rpm program comes from.

A converted program is not updated by pacman: the vendor's own repository setup is deliberately not carried over, so nothing
would ever tell it that Chrome 156 exists. For the vendors that publish the package list apt itself reads (a `Packages` file
that names every version, its file and its SHA-256) Cygnus can read the newest version from there, without downloading the
program, and when you ask it to update, download exactly that file over HTTPS, check it against the published SHA-256, and
hand it to the same analysis and confirmation as any file you open yourself. Nothing is installed by this module.

The package list is fetched over HTTPS (TLS only; Cygnus does not check the vendor's own GPG signature on it), so what it
protects against is a corrupted or mixed-up download, not a vendor whose server has been taken over: the same protection you
have when you download the file from the vendor's website yourself.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin

from cygnus.core.errors import CygnusError
from cygnus.core.util import http

MAX_COMPRESSED = 8 * 1024 * 1024
MAX_INDEX = 64 * 1024 * 1024  # the list as text; a decompression bomb stops here


@dataclass(frozen=True)
class Feed:
    package: str  # the vendor's package name (the `Package:` line, and what the converted package is called)
    index: str  # https address of the package list (`Packages.gz`)
    base: str  # https folder the `Filename:` of a package is relative to
    rpm: str | None = None  # https address of the newest .rpm, for a program that was converted from the vendor's rpm


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    sha256: str | None  # published for the .deb; the vendor's rpm address has none
    size: int | None
    format: str  # "deb" or "rpm"


def _chrome(channel: str) -> Feed:
    return Feed(f"google-chrome-{channel}", "https://dl.google.com/linux/chrome/deb/dists/stable/main/binary-amd64/Packages.gz",
                "https://dl.google.com/linux/chrome/deb/",
                rpm=f"https://dl.google.com/linux/direct/google-chrome-{channel}_current_x86_64.rpm")


FEEDS: dict[str, Feed] = {f.package: f for f in (
    _chrome("stable"), _chrome("beta"), _chrome("unstable"),
    Feed("microsoft-edge-stable", "https://packages.microsoft.com/repos/edge/dists/stable/main/binary-amd64/Packages.gz",
         "https://packages.microsoft.com/repos/edge/"),
    Feed("vivaldi-stable", "https://repo.vivaldi.com/archive/deb/dists/stable/main/binary-amd64/Packages.gz",
         "https://repo.vivaldi.com/archive/deb/"),
    Feed("brave-browser", "https://brave-browser-apt-release.s3.brave.com/dists/stable/main/binary-amd64/Packages.gz",
         "https://brave-browser-apt-release.s3.brave.com/"),
    Feed("opera-stable", "https://deb.opera.com/opera-stable/dists/stable/non-free/binary-amd64/Packages.gz",
         "https://deb.opera.com/opera-stable/"),
    Feed("signal-desktop", "https://updates.signal.org/desktop/apt/dists/xenial/main/binary-amd64/Packages.gz",
         "https://updates.signal.org/desktop/apt/"),
    Feed("1password", "https://downloads.1password.com/linux/debian/amd64/dists/stable/main/binary-amd64/Packages.gz",
         "https://downloads.1password.com/linux/debian/amd64/"),
    Feed("teamviewer", "https://linux.teamviewer.com/deb/dists/stable/main/binary-amd64/Packages.gz",
         "https://linux.teamviewer.com/deb/"),
    Feed("slack-desktop", "https://packagecloud.io/slacktechnologies/slack/debian/dists/jessie/main/binary-amd64/Packages.gz",
         "https://packagecloud.io/slacktechnologies/slack/debian/"),
    Feed("code-insiders", "https://packages.microsoft.com/repos/code/dists/stable/main/binary-amd64/Packages.gz",
         "https://packages.microsoft.com/repos/code/"),
    Feed("google-earth-pro-stable", "https://dl.google.com/linux/earth/deb/dists/stable/main/binary-amd64/Packages.gz",
         "https://dl.google.com/linux/earth/deb/"),
)}


_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,100}")
_HTTPS = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%+/@:-]*)?")


def user_feeds() -> dict[str, Feed]:
    """The sources the person added themselves (`cygnus feed add`), read from the preferences. Entries that do not look right
    are ignored."""
    from cygnus.core import preferences

    raw = preferences.load().get("vendor_feeds")
    out: dict[str, Feed] = {}
    for package, entry in (raw.items() if isinstance(raw, dict) else ()):
        if not (isinstance(package, str) and _PACKAGE.fullmatch(package) and isinstance(entry, dict)):
            continue
        index, base, rpm = entry.get("index"), entry.get("base"), entry.get("rpm")
        if all(isinstance(u, str) and _HTTPS.fullmatch(u) for u in (index, base)) and base.endswith("/") \
                and (rpm is None or (isinstance(rpm, str) and _HTTPS.fullmatch(rpm))):
            out[package] = Feed(package, index, base, rpm)
    return out


def feed_for(package: str) -> Feed | None:
    """Where newer versions of `package` are published: a source the person added wins over the built-in list."""
    return user_feeds().get(package) or FEEDS.get(package)


def add_user_feed(feed: Feed) -> None:
    from cygnus.core import preferences

    if not (_PACKAGE.fullmatch(feed.package) and _HTTPS.fullmatch(feed.index) and _HTTPS.fullmatch(feed.base)
            and feed.base.endswith("/") and (feed.rpm is None or _HTTPS.fullmatch(feed.rpm))):
        raise CygnusError("a package name and https addresses are needed (the base address ends in a slash)")
    prefs = preferences.load()
    feeds = dict(prefs.get("vendor_feeds") or {})
    feeds[feed.package] = {"index": feed.index, "base": feed.base, **({"rpm": feed.rpm} if feed.rpm else {})}
    prefs["vendor_feeds"] = feeds
    preferences.save(prefs)


def remove_user_feed(package: str) -> bool:
    from cygnus.core import preferences

    prefs = preferences.load()
    feeds = dict(prefs.get("vendor_feeds") or {})
    if package not in feeds:
        return False
    del feeds[package]
    if feeds:
        prefs["vendor_feeds"] = feeds
    else:
        prefs.pop("vendor_feeds", None)
    preferences.save(prefs)
    return True


# -- version order -------------------------------------------------------------------------------------------------------
def _rpmvercmp(a: str, b: str) -> int:
    """pacman's (libalpm's) comparison of two version or release strings, step for step."""
    if a == b:
        return 0
    i = j = 0
    prev_i = prev_j = 0  # where the last segment ended: the separators in between are measured from here
    while i < len(a) and j < len(b):
        while i < len(a) and not a[i].isalnum():
            i += 1
        while j < len(b) and not b[j].isalnum():
            j += 1
        if i >= len(a) or j >= len(b):
            break
        if (i - prev_i) != (j - prev_j):  # different separators in front of the segment: that decides it
            return -1 if (i - prev_i) < (j - prev_j) else 1
        start_i, start_j = i, j
        numeric = a[i].isdigit()
        if numeric:
            while i < len(a) and a[i].isdigit():
                i += 1
            while j < len(b) and b[j].isdigit():
                j += 1
        else:
            while i < len(a) and a[i].isalpha():
                i += 1
            while j < len(b) and b[j].isalpha():
                j += 1
        prev_i, prev_j = i, j
        seg_a, seg_b = a[start_i:i], b[start_j:j]
        if not seg_b:  # the other side is of the other kind: a number is newer than letters
            return 1 if numeric else -1
        if numeric:
            seg_a, seg_b = seg_a.lstrip("0"), seg_b.lstrip("0")
            if len(seg_a) != len(seg_b):
                return 1 if len(seg_a) > len(seg_b) else -1
        if seg_a != seg_b:
            return 1 if seg_a > seg_b else -1
    rest_a, rest_b = a[i:], b[j:]
    if not rest_a and not rest_b:
        return 0
    # the last word: letters left over never beat nothing (1.0a is older than 1.0)
    if (not rest_a and not rest_b[:1].isalpha()) or rest_a[:1].isalpha():
        return -1
    return 1


def _evr(v: str) -> tuple[str, str, str | None]:
    epoch = "0"
    if ":" in v and v.split(":", 1)[0].isdigit():
        epoch, v = v.split(":", 1)
    release = None
    if "-" in v:
        v, release = v.rsplit("-", 1)
    return epoch, v, release


def vercmp(a: str, b: str) -> int:
    """-1, 0 or 1 as `a` is older than, the same as or newer than `b`: pacman's order (epoch, version, then release)."""
    if a == b:
        return 0
    (ea, va, ra), (eb, vb, rb) = _evr(a), _evr(b)
    for first, second in ((ea, eb), (va, vb)):
        got = _rpmvercmp(first, second)
        if got:
            return got
    return _rpmvercmp(ra, rb) if ra is not None and rb is not None else 0


# -- the package list ----------------------------------------------------------------------------------------------------
_VERSION = re.compile(r"[0-9][A-Za-z0-9.+~:_-]{0,100}")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~:/-]{0,300}")


def parse_index(text: str, package: str) -> list[dict[str, str]]:
    """The stanzas of `package` in an apt `Packages` list (a list names every version it still carries)."""
    found: list[dict[str, str]] = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if line[:1] in (" ", "\t") or ":" not in line:
                continue  # a continuation line, or not a field
            key, _, value = line.partition(":")
            fields.setdefault(key.strip(), value.strip())
        if fields.get("Package") == package:
            found.append(fields)
    return found


def _deb_release(fields: dict[str, str], feed: Feed) -> Release:
    version, filename, sha = fields.get("Version", ""), fields.get("Filename", ""), fields.get("SHA256", "").lower()
    size = fields.get("Size", "")
    if not _VERSION.fullmatch(version) or not _FILENAME.fullmatch(filename) or ".." in filename.split("/") \
            or not re.fullmatch(r"[0-9a-f]{64}", sha) or not re.fullmatch(r"[0-9]{0,15}", size):
        raise CygnusError("the vendor's package list has an entry Cygnus cannot read safely")
    url = urljoin(feed.base, filename)
    if not url.startswith(feed.base):  # a name like //other.example/x or an absolute address leads elsewhere
        raise CygnusError("the vendor's package list points outside the vendor's own folder")
    return Release(version, url, sha, int(size) if size else None, "deb")


def newest(feed: Feed, fmt: str, *, fetch: Callable[..., bytes] = http.get) -> Release:
    """The newest release of `feed.package` as a file of kind `fmt` ("deb" or "rpm"). Raises CygnusError when it cannot be
    found out; that is never to be read as "up to date"."""
    raw = fetch(feed.index, limit=MAX_COMPRESSED)
    try:
        unpacker = zlib.decompressobj(wbits=31)  # gzip
        text_bytes = unpacker.decompress(raw, MAX_INDEX)
        if unpacker.unconsumed_tail:
            raise CygnusError("the vendor's package list is far larger than a real one")
        if not unpacker.eof:  # cut short (even at a clean line): the newest version may be in what is missing
            raise CygnusError("the vendor's package list is incomplete")
    except zlib.error as exc:
        raise CygnusError("the vendor's package list is not readable") from exc
    stanzas = [s for s in parse_index(text_bytes.decode("utf-8", errors="replace"), feed.package)
               if s.get("Architecture") in ("amd64", "all")]
    if not stanzas:
        raise CygnusError(f"the vendor's package list does not mention {feed.package}")
    best = stanzas[0]
    for other in stanzas[1:]:
        if vercmp(other.get("Version", "0"), best.get("Version", "0")) > 0:
            best = other
    release = _deb_release(best, feed)
    if fmt == "rpm":
        if not feed.rpm:
            raise CygnusError(f"Cygnus knows no download address for {feed.package} as an rpm")
        return Release(release.version, feed.rpm, None, None, "rpm")  # same version number in both kinds of package
    return release
