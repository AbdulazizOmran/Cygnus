"""What the assistant gets to read. Cygnus fetches it itself, from a short allow-list, and the model only ever sees the text:
the vendor's own site (taken from the application's metadata, or typed by the person), the project's page on a code forge,
and Flathub's description. Only https, never an address that is not on the public internet, never a host that is not on the
list, and redirects are checked at every step."""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

from cygnus.core.ai.text import plain
from cygnus.core.errors import CygnusError
from cygnus.core.util import http

MAX_PAGES = 4
MAX_RAW = 400 * 1024
MAX_PAGE_TEXT = 20_000
FORGES = {"github.com": "raw.githubusercontent.com", "gitlab.com": "gitlab.com"}
# Hosts where every subdomain belongs to someone else: a program's page on one of these is read as that exact host only, never as
# "that host and everything under it" (a page at sourceforge.net must not let another project's sourceforge.net site be read).
SHARED_HOSTS = frozenset({
    "sourceforge.net", "sourceforge.io", "github.io", "gitlab.io", "bitbucket.io", "codeberg.page", "workers.dev", "pages.dev",
    "netlify.app", "vercel.app", "herokuapp.com", "fly.dev", "onrender.com", "glitch.me", "web.app", "firebaseapp.com", "appspot.com",
    "azurewebsites.net", "azureedge.net", "amazonaws.com", "cloudfront.net", "blogspot.com", "wordpress.com", "tumblr.com", "weebly.com",
    "wixsite.com", "myshopify.com", "readthedocs.io", "readthedocs.org", "gitbook.io", "notion.site", "itch.io", "fandom.com", "medium.com",
})
# What the hosted service refuses as well, so that a page which would be sent is never one that is turned away.
_NOT_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SourceError(CygnusError):
    pass


@dataclass(frozen=True)
class Page:
    url: str
    text: str


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:  # a malformed address, such as an unclosed bracket
        return ""


def vendor_hosts(urls: list[str]) -> set[str]:
    """The hosts that may be read for an application: each address it (or the person) names, with the "www." dropped."""
    hosts: set[str] = set()
    for url in urls:
        host = _host(url)
        if host:
            hosts.add(host.removeprefix("www."))
            hosts.add("www." + host.removeprefix("www."))
            if host in FORGES:
                hosts.add(FORGES[host])
    return hosts


def _allowed(host: str, allowed: set[str]) -> bool:
    host = host.lower().rstrip(".")
    exact_only = set(FORGES) | set(FORGES.values()) | SHARED_HOSTS  # their subdomains belong to other people
    return any(host == a or host.endswith("." + a) for a in allowed if a not in exact_only) or host in allowed


def _public(host: str) -> bool:
    """True when `host` is on the public internet (an address literal, or every address its name leads to)."""
    try:
        return _global(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        pass
    try:
        found = {info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        return True  # it does not resolve: the request will fail by itself
    return bool(found) and all(_global(ipaddress.ip_address(a.split("%")[0])) for a in found)


def _global(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """On the public internet. An IPv6 address that only carries an IPv4 one (mapped, the old "compatible" form, 6to4) is judged by
    the IPv4 address inside it."""
    if isinstance(ip, ipaddress.IPv6Address):
        if int(ip) < 2**32:  # ::/96: "::" itself and the deprecated IPv4-compatible form (::127.0.0.1)
            return False
        for inner in (ip.ipv4_mapped, ip.sixtofour):
            if inner is not None:
                return _global(inner)
    return ip.is_global


def host_check(allowed: set[str]) -> Callable[[str], bool]:
    return lambda host: bool(host) and _allowed(host, allowed) and _public(host)


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "pre", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def to_text(raw: bytes) -> str:
    """Readable text from a page: markup, scripts and styles dropped, spaces tidied, cut to a sensible length."""
    if raw.count(b"\x00") > 8:
        return ""  # not text
    text = raw.decode("utf-8", errors="replace")
    if "<" in text and ">" in text:
        parser = _Text()
        try:
            parser.feed(text)
            parser.close()
        except Exception:  # noqa: BLE001 - broken markup: what was read so far is still text
            pass
        text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return _NOT_TEXT.sub("", text)[:MAX_PAGE_TEXT]  # a stray control character would make the hosted service turn the page away


def readme_url(url: str) -> str | None:
    """The README of a project on GitHub or GitLab, from the address of its page."""
    parts = urlparse(url)
    segments = [s for s in parts.path.split("/") if s]
    if parts.scheme != "https" or len(segments) < 2:
        return None
    owner, repo = segments[0], re.sub(r"\.git$", "", segments[1])
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo):
        return None
    host = parts.hostname or ""
    if host == "github.com":
        return f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md"
    if host == "gitlab.com":
        return f"https://gitlab.com/{owner}/{repo}/-/raw/HEAD/README.md"
    return None


def clean_url(url: object) -> str | None:
    """An https address a person or a package gave, or None when it is anything else: no user name, no port but the usual one, no
    trailing dot, no space, control, invisible or markup character, and nothing the HTTP layer would read differently."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if len(url) > 300 or any(c.isspace() or c in '"<>\\' or unicodedata.category(c)[0] == "C" for c in url):
        return None
    try:
        parts = urlparse(url)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if parts.scheme != "https" or not host or host.endswith(".") or parts.username is not None or parts.password is not None \
            or port not in (None, 443):
        return None
    try:
        http._checked_host(url)
    except http.HttpError:
        return None
    return url


def candidate_urls(homepages: list[str]) -> list[str]:
    """The pages to read for these addresses: each page itself, and the README when one is a forge project."""
    out: list[str] = []
    for url in homepages:
        for u in (url, readme_url(url)):
            if u and u not in out:
                out.append(u)
    return out[:MAX_PAGES]


def fetch_pages(homepages: list[str], *, get: Callable[..., bytes] = http.get) -> list[Page]:
    """Read the pages. A page that cannot be read is skipped; if none can, that is said."""
    homepages = [u for u in (clean_url(h) for h in homepages) if u]
    if not homepages:
        raise SourceError("there is no website to read for this application: type its address and try again")
    allowed = vendor_hosts(homepages)
    check = host_check(allowed)
    pages: list[Page] = []
    problems: list[str] = []
    for url in candidate_urls(homepages):
        if not check(_host(url)):
            problems.append(f"{plain(_host(url), 80)} is not an address Cygnus will read")
            continue
        try:
            raw = get(url, limit=MAX_RAW, timeout=20.0, host_ok=check)
        except CygnusError as exc:
            problems.append(plain(str(exc), 150))  # a hostile server or redirect chose some of these words
            continue
        text = to_text(raw)
        if text:
            pages.append(Page(url, text))
    if not pages:
        raise SourceError("nothing could be read from the application's website" + (f" ({problems[0]})" if problems else ""))
    return pages
