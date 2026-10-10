"""Arch Linux news, shown before a system upgrade (so that an announcement that needs a manual step is not missed).

The feed is read over HTTPS from archlinux.org only, size-capped, and parsed strictly: a document type or an entity declaration is
refused outright (nothing in a news feed needs either), every item's text is reduced to plain text, and only links on archlinux.org
are kept. "Since your last upgrade" is the date of the last full upgrade in pacman's log. A feed that cannot be read never stops an
upgrade: the answer says so and the person decides (security updates must not wait for a web page).

This is Arch's news. A distribution built on Arch (CachyOS, for one) has announcements of its own that this does not cover.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable
from xml.parsers import expat

from cygnus.core.errors import CygnusError
from cygnus.core.util import http

FEED_URL = "https://archlinux.org/feeds/news/"
NEWS_HOST = "archlinux.org"
MAX_FEED = 1024 * 1024
MAX_ITEMS = 60  # items read from a feed
MAX_SHOWN = 10  # items shown at once
MAX_SUMMARY = 1500
MAX_LINES = 30  # lines of one item's text shown
FULL_TEXT = 200_000  # the text an item is searched in (the manual-step wording can be anywhere in it)
MAX_TITLE = 300
FALLBACK_DAYS = 30  # with no record of a last upgrade, the news of this many days
PACMAN_LOG = "/var/log/pacman.log"
LOG_TAIL = 16 * 1024 * 1024  # the end of pacman's log that is searched for the last upgrade
# Arch's own wording for an announcement that must be acted on ("X requires manual intervention"), and its near relatives.
_MANUAL = re.compile(
    r"manual(?:ly)?[ -](?:intervention|step|action|configuration|edit|update|upgrade|fix|work)\w*|manually (?:edit|remove|install|update|run|re-?enrol|re-?enroll|fix|change)\w*"
    r"|(?:intervention|action|attention|steps?) (?:is |are )?(?:needed|required|necessary)|requires? (?:manual|your attention|(?:an? )?action|intervention)"
    r"|needs? (?:manual|your attention|(?:an? )?action|intervention)|breaking change|before (?:you |the )?(?:updat|upgrad)\w*",
    re.IGNORECASE)
_DECLARATION = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_UPGRADE_LINE = re.compile(rb"^\[(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})([+-]\d{4})\] \[PACMAN\] starting full system upgrade$", re.MULTILINE)


class NewsError(CygnusError):
    """The feed could not be read, with the reason in words."""


@dataclass(frozen=True, slots=True)
class NewsItem:
    title: str
    link: str  # an https address on archlinux.org, or ""
    published: float | None  # epoch seconds
    summary: str  # plain text
    manual: bool  # says it needs a manual step

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "link": self.link, "published": self.published, "summary": self.summary, "manual": self.manual}


class _Text(HTMLParser):
    """The text of an HTML fragment: paragraphs and list items on their own lines, scripts and styles dropped (a script or style that is
    never closed would hide everything after it, so then what it swallowed is kept as plain text)."""

    _BREAKS = {"p", "br", "li", "div", "pre", "h1", "h2", "h3", "h4", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0
        self._hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BREAKS:
            self.out.append("\n" + ("• " if tag == "li" else ""))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
            if not self._skip:
                self._hidden = []  # closed: what it held was a script, not text
        elif tag in self._BREAKS and tag != "li":  # a list item opens a line; the next one (or the list's end) closes it
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            self._hidden.append(data)
        else:
            self.out.append(data)

    def finish(self) -> None:
        if self._skip:  # never closed: do not let it hide what follows
            self.out.append("\n" + "".join(self._hidden))


def plain(text: str, limit: int, lines: int | None = None) -> str:
    """Text safe to show anywhere: only printable characters (and newlines), runs of blank lines collapsed, cut to `limit` characters
    and (when given) `lines` lines."""
    text = "".join(c if (c == "\n" or c.isprintable()) else " " for c in text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    cut = False
    if lines is not None and text.count("\n") >= lines:
        text, cut = "\n".join(text.split("\n")[:lines]), True
    if len(text) > limit:
        text, cut = text[:limit - 1], True
    return text.rstrip() + "…" if cut else text


def html_text(html: str, limit: int = MAX_SUMMARY, lines: int | None = None) -> str:
    parser = _Text()
    try:
        parser.feed(html)
        parser.close()
        parser.finish()
    except Exception:  # noqa: BLE001 - a fragment that cannot be parsed is shown as the text it is, tags and all
        return plain(html, limit, lines)
    return plain("".join(parser.out), limit, lines)


_LINK = re.compile(r"https://(?:[a-z0-9-]+\.)*archlinux\.org(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/?#-]*)?", re.IGNORECASE)


def _link(value: str | None) -> str:
    """The item's address if it is an ordinary https address on archlinux.org (no user name, no port, no odd character: only what an
    address on that site is made of), else nothing. It is only ever opened by a click."""
    url = (value or "").strip()
    return url if len(url) <= 500 and _LINK.fullmatch(url) else ""


def _published(value: str | None) -> float | None:
    try:
        when = parsedate_to_datetime((value or "").strip())
    except (TypeError, ValueError, IndexError):
        return None
    return when.timestamp() if when.tzinfo is not None else None


def parse_feed(data: bytes) -> list[NewsItem]:
    """The items of an RSS feed, newest first as the feed gives them."""
    if len(data) > MAX_FEED:
        raise NewsError("the news feed is bigger than expected, so it was not read")
    if _DECLARATION.search(data):
        raise NewsError("the news feed contains a document type or entity declaration, which is never accepted")
    def refuse(*_args: Any) -> None:  # whatever the encoding of the document, expat reports a declaration here
        raise NewsError("the news feed contains a document type or entity declaration, which is never accepted")

    builder = ET.TreeBuilder()
    parser = expat.ParserCreate()
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(data, True)
        root = builder.close()
    except (expat.ExpatError, ValueError, LookupError) as exc:  # (an encoding expat cannot read: utf-16-le, or one nobody has heard of)
        raise NewsError(f"the news feed is not readable ({exc})") from exc
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise NewsError("this is not a news feed")
    items = []
    for node in channel.findall("item")[:MAX_ITEMS]:
        title = plain(node.findtext("title") or "", MAX_TITLE).replace("\n", " ")
        if not title:
            continue
        full = html_text(node.findtext("description") or "", FULL_TEXT)  # searched whole: what is shown is only the start of it
        items.append(NewsItem(title=title, link=_link(node.findtext("link")), published=_published(node.findtext("pubDate")),
                              summary=plain(full, MAX_SUMMARY, MAX_LINES), manual=bool(_MANUAL.search(title) or _MANUAL.search(full))))
    return items


def last_upgrade(log_path: str = PACMAN_LOG) -> float | None:
    """When the last full system upgrade started, from pacman's log (epoch seconds), or None when the log has none or cannot be read."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL))
            tail = fh.read(LOG_TAIL)
    except OSError:
        return None
    last = None
    for m in _UPGRADE_LINE.finditer(tail):
        last = m
    if last is None:
        return None
    try:
        return datetime.strptime("".join(part.decode() for part in last.groups()), "%Y-%m-%d%H:%M:%S%z").timestamp()
    except (ValueError, OverflowError):
        return None


def since(items: list[NewsItem], when: float | None, now: float) -> list[NewsItem]:
    """The items published after `when`; with no `when`, those of the last FALLBACK_DAYS days. An item with no readable date is
    always included (showing too much is the safe mistake)."""
    cutoff = when if when is not None else now - FALLBACK_DAYS * 86400
    return [i for i in items if i.published is None or i.published > cutoff]


def check(*, fetch: Callable[..., bytes] | None = None, log_path: str = PACMAN_LOG, now: float | None = None) -> dict[str, Any]:
    """The news a person should see before upgrading: {"ok": True, "items", "total", "since", "needs_ack", "checked_at"} or
    {"ok": False, "error"}. Never raises: a feed that cannot be read must not stop an upgrade. `needs_ack` is true when an item says
    it needs a manual step."""
    now = time.time() if now is None else now
    try:
        data = (fetch or http.get)(FEED_URL, limit=MAX_FEED, timeout=15.0, host_ok=lambda h: h == NEWS_HOST or h.endswith("." + NEWS_HOST))
        items = parse_feed(data)
    except CygnusError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the upgrade is not stopped by it
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    when = last_upgrade(log_path)
    fresh = since(items, when, now)
    # The first MAX_SHOWN, and every item that needs a manual step wherever it comes in the feed: what the acknowledgement is for is
    # always on the screen.
    shown = [i for n, i in enumerate(fresh) if n < MAX_SHOWN or i.manual]
    return {"ok": True, "items": [i.as_dict() for i in shown], "total": len(fresh), "since": when,
            "needs_ack": any(i.manual for i in fresh), "checked_at": now}


def start(**kwargs: Any) -> Callable[[float], dict[str, Any]]:
    """Start `check` in a thread of its own and return a function that waits up to the given seconds for its answer (or says it did not
    come in time). The thread is a daemon: a feed that trickles in never holds the program up at exit, and the HTTP client's own
    deadline ends it anyway."""
    import threading

    box: dict[str, Any] = {}

    def run() -> None:
        box["answer"] = check(**kwargs)

    thread = threading.Thread(target=run, daemon=True, name="cygnus-news")
    thread.start()

    def wait(seconds: float) -> dict[str, Any]:
        thread.join(seconds)
        return box.get("answer") or {"ok": False, "error": "the news feed did not answer in time"}

    return wait


def check_within(seconds: float, **kwargs: Any) -> dict[str, Any]:
    """`check`, but whatever happens it answers within `seconds`."""
    return start(**kwargs)(seconds)
