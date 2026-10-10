"""Arch news before an upgrade: a strict reader (no declarations, capped, plain text, only archlinux.org links), the date of the last
upgrade taken from pacman's log, and a check that never raises."""

from datetime import datetime, timezone
from xml.sax.saxutils import escape

import pytest

from cygnus.core import news
from cygnus.core.util import http

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp()


def feed(*items, extra=""):
    body = "".join(items)
    return (f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
            f"<title>Arch Linux: Recent news updates</title>{extra}{body}</channel></rss>").encode()


def item(title="A thing", description="&lt;p&gt;Text&lt;/p&gt;", link="https://archlinux.org/news/a-thing/", date="Tue, 22 Sep 2026 09:09:27 +0000"):
    return (f"<item><title>{title}</title><link>{link}</link><description>{description}</description>"
            f"<pubDate>{date}</pubDate></item>")


# -- reading a feed -----------------------------------------------------------------------------------------------------------------------

def test_a_feed_is_read_into_items():
    items = news.parse_feed(feed(item("First", "&lt;p&gt;One&lt;/p&gt;&lt;p&gt;Two &lt;code&gt;x&lt;/code&gt;&lt;/p&gt;"), item("Second")))
    assert [i.title for i in items] == ["First", "Second"]
    assert items[0].summary == "One\n\nTwo x" and items[0].link == "https://archlinux.org/news/a-thing/"
    assert items[0].published == datetime(2026, 9, 22, 9, 9, 27, tzinfo=timezone.utc).timestamp()
    assert items[0].manual is False


@pytest.mark.parametrize("declaration", [
    '<!DOCTYPE rss [<!ENTITY a "aaaa">]>', '<!doctype rss>', '<! DOCTYPE rss>', '<!ENTITY x SYSTEM "file:///etc/passwd">',
])
def test_a_document_type_or_entity_declaration_is_never_accepted(declaration):
    data = feed(item()).replace(b"<rss", declaration.encode() + b"<rss", 1)
    with pytest.raises(news.NewsError, match="declaration"):
        news.parse_feed(data)


def test_a_feed_that_is_too_big_or_not_a_feed_is_refused():
    with pytest.raises(news.NewsError, match="bigger than expected"):
        news.parse_feed(b"x" * (news.MAX_FEED + 1))
    for bad in (b"", b"<html><body>hi</body></html>", b"<rss version='2.0'></rss>", b"<rss><channel><item><title>unclosed</rss>", b"not xml at all"):
        with pytest.raises(news.NewsError):
            news.parse_feed(bad)


def test_a_feed_with_no_items_is_an_empty_list_and_an_item_without_a_title_is_skipped():
    assert news.parse_feed(feed()) == []
    assert [i.title for i in news.parse_feed(feed(item(title=""), item("Kept")))] == ["Kept"]


def test_only_the_first_items_of_a_long_feed_are_read():
    items = news.parse_feed(feed(*[item(f"n{i}") for i in range(news.MAX_ITEMS + 25)]))
    assert len(items) == news.MAX_ITEMS


@pytest.mark.parametrize("link, kept", [
    ("https://archlinux.org/news/x/", True), ("https://www.archlinux.org/news/x/", True),
    ("javascript:alert(1)", False), ("http://archlinux.org/news/x/", False), ("https://evil.example/archlinux.org", False),
    ("https://archlinux.org.evil.example/", False), ("https://notarchlinux.org/news/x/", False), ("https://user:pw@archlinux.org/", False), ("https://archlinux.org/a b", False),
    ("https://archlinux.org/‮", False), ("file:///etc/passwd", False), ("", False), ("https://archlinux.org/" + "a" * 600, False),
])
def test_only_ordinary_links_on_archlinux_org_are_kept(link, kept):
    got = news.parse_feed(feed(item(link=link)))[0].link
    assert (got == link) if kept else got == ""


@pytest.mark.parametrize("title, summary, manual", [
    ("Mkinitcpio >=42 requires manual intervention for TPM2", "x", True),
    ("virtualbox-ext-vnc >= 7.2.12-2 requires manual intervention", "x", True),
    ("Something", "Please be aware: manual intervention is needed.", True),
    ("Action required: update your keyring", "x", True),
    ("Before you upgrade, read this", "x", True),
    ("New release of the installation media", "A new ISO.", False),
    ("Python 3.14 in the repositories", "Rebuild your AUR packages.", False),
])
def test_an_item_that_says_it_needs_a_manual_step_is_marked(title, summary, manual):
    assert news.parse_feed(feed(item(title, f"&lt;p&gt;{summary}&lt;/p&gt;")))[0].manual is manual


def test_item_text_is_reduced_to_safe_plain_text():
    html = ("&lt;p&gt;Run &lt;code&gt;sudo pacman -Syu&lt;/code&gt;&lt;/p&gt;&lt;script&gt;alert(1)&lt;/script&gt;&lt;style&gt;p{x:y}&lt;/style&gt;"
            "&lt;ul&gt;&lt;li&gt;one&lt;/li&gt;&lt;li&gt;two&lt;/li&gt;&lt;/ul&gt;&lt;a href=\"javascript:x\"&gt;link&lt;/a&gt;")
    summary = news.parse_feed(feed(item(description=html)))[0].summary
    assert summary == "Run sudo pacman -Syu\n\n• one\n• two\nlink"
    assert "<" not in summary and "alert" not in summary and "p{x:y}" not in summary


def test_characters_a_terminal_or_screen_would_obey_are_removed():
    assert news.plain("a\x1b[31mred\x1b[0m‮flip\r\nnext", 100) == "a [31mred [0m flip \nnext".replace(" \n", "\n")
    assert all(c == "\n" or c.isprintable() for c in news.plain("x\x00y\x07z​w", 100))


def test_long_text_is_cut_with_a_mark():
    cut = news.html_text("<p>" + "word " * 1000 + "</p>", 50)
    assert len(cut) == 50 and cut.endswith("…")
    assert len(news.parse_feed(feed(item(title="t" * 1000)))[0].title) == news.MAX_TITLE


@pytest.mark.parametrize("date, expected", [
    ("Tue, 22 Sep 2026 09:09:27 +0000", datetime(2026, 9, 22, 9, 9, 27, tzinfo=timezone.utc).timestamp()),
    ("Tue, 22 Sep 2026 12:09:27 +0300", datetime(2026, 9, 22, 9, 9, 27, tzinfo=timezone.utc).timestamp()),
    ("", None), ("yesterday", None), ("Tue, 99 Sep 2026 09:09:27 +0000", None),
])
def test_dates_are_read_or_left_unknown(date, expected):
    assert news.parse_feed(feed(item(date=date)))[0].published == expected


# -- since the last upgrade --------------------------------------------------------------------------------------------------------------

LOG = """[2026-10-08T19:52:55+0300] [PACMAN] Running 'pacman -Syu'
[2026-10-08T19:52:55+0300] [PACMAN] starting full system upgrade
[2026-10-08T19:55:00+0300] [ALPM] upgraded foo (1 -> 2)
[2026-10-09T14:27:50+0300] [PACMAN] starting full system upgrade
[2026-10-09T14:30:00+0300] [ALPM] installed bar (1)
[2026-10-09T14:31:00+0300] [PACMAN] 'starting full system upgrade' mentioned in a message
"""


def test_the_last_upgrade_is_the_last_full_upgrade_in_the_log(tmp_path):
    log = tmp_path / "pacman.log"
    log.write_text(LOG)
    assert news.last_upgrade(str(log)) == datetime(2026, 10, 9, 11, 27, 50, tzinfo=timezone.utc).timestamp()  # +0300 -> UTC


def test_no_upgrade_in_the_log_or_no_log_is_unknown(tmp_path):
    log = tmp_path / "pacman.log"
    log.write_text("[2026-10-09T14:30:00+0300] [ALPM] installed bar (1)\n")
    assert news.last_upgrade(str(log)) is None
    assert news.last_upgrade(str(tmp_path / "missing.log")) is None
    log.write_bytes(b"\xff\xfe garbage \x00\n")
    assert news.last_upgrade(str(log)) is None


def test_only_the_end_of_a_huge_log_is_read(tmp_path, monkeypatch):
    log = tmp_path / "pacman.log"
    old = "[2020-01-01T00:00:00+0000] [PACMAN] starting full system upgrade\n"
    log.write_text(old + "[2026-10-09T14:30:00+0300] [ALPM] filler line to push the old one out of the tail\n" * 50)
    monkeypatch.setattr(news, "LOG_TAIL", 500)
    assert news.last_upgrade(str(log)) is None  # the old line is outside what is searched
    monkeypatch.setattr(news, "LOG_TAIL", 10**7)
    assert news.last_upgrade(str(log)) == datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()


def test_news_since_a_date_with_a_fallback_when_there_is_none():
    day = 86400
    items = [news.NewsItem("new", "", NOW - 1 * day, "", False), news.NewsItem("old", "", NOW - 40 * day, "", False),
             news.NewsItem("undated", "", None, "", False), news.NewsItem("middle", "", NOW - 10 * day, "", False)]
    assert [i.title for i in news.since(items, NOW - 5 * day, NOW)] == ["new", "undated"]
    assert [i.title for i in news.since(items, None, NOW)] == ["new", "undated", "middle"]  # the last 30 days, and what has no date
    assert [i.title for i in news.since(items, NOW, NOW)] == ["undated"]


# -- the check -----------------------------------------------------------------------------------------------------------------------------

def _check(tmp_path, data, log=LOG, **kw):
    path = tmp_path / "pacman.log"
    path.write_text(log)
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if isinstance(data, Exception):
            raise data
        return data

    return news.check(fetch=fetch, log_path=str(path), now=NOW, **kw), calls


def test_the_check_asks_archlinux_org_only_and_with_limits(tmp_path):
    answer, calls = _check(tmp_path, feed(item()))
    url, kwargs = calls[0]
    assert url == "https://archlinux.org/feeds/news/" and kwargs["limit"] == news.MAX_FEED and kwargs["timeout"] <= 20
    ok = kwargs["host_ok"]
    assert ok("archlinux.org") and ok("www.archlinux.org")
    assert not ok("evil.example") and not ok("archlinux.org.evil.example") and not ok("notarchlinux.org")


def test_the_check_returns_the_news_since_the_last_upgrade(tmp_path):
    old = item("Old", date="Tue, 22 Sep 2026 09:09:27 +0000")
    fresh = item("Fresh", date="Sat, 10 Oct 2026 08:00:00 +0000")
    answer, _ = _check(tmp_path, feed(fresh, old))
    assert answer["ok"] and [i["title"] for i in answer["items"]] == ["Fresh"] and answer["total"] == 1 and answer["needs_ack"] is False
    assert answer["since"] == datetime(2026, 10, 9, 11, 27, 50, tzinfo=timezone.utc).timestamp()


def test_an_item_that_needs_a_manual_step_needs_an_acknowledgement(tmp_path):
    answer, _ = _check(tmp_path, feed(item("foo requires manual intervention", date="Sat, 10 Oct 2026 08:00:00 +0000")))
    assert answer["needs_ack"] is True and answer["items"][0]["manual"] is True
    old_manual, _ = _check(tmp_path, feed(item("foo requires manual intervention", date="Tue, 22 Sep 2026 09:09:27 +0000")))
    assert old_manual["needs_ack"] is False and old_manual["items"] == []  # already past: read before the last upgrade


def test_a_long_list_is_cut_and_says_how_many_there_were(tmp_path):
    answer, _ = _check(tmp_path, feed(*[item(f"n{i}", date="Sat, 10 Oct 2026 08:00:00 +0000") for i in range(25)]))
    assert len(answer["items"]) == news.MAX_SHOWN and answer["total"] == 25


def test_an_item_that_needs_a_manual_step_is_always_among_the_shown_ones_wherever_it_comes_in_the_feed(tmp_path):
    items = [item(f"n{i}", date="Sat, 10 Oct 2026 08:00:00 +0000") for i in range(news.MAX_SHOWN + 3)]
    items.append(item("late one requires manual intervention", date="Sat, 10 Oct 2026 08:00:00 +0000"))
    answer, _ = _check(tmp_path, feed(*items))
    assert answer["needs_ack"] is True and answer["total"] == news.MAX_SHOWN + 4
    manual = [i for i in answer["items"] if i["manual"]]
    assert [i["title"] for i in manual] == ["late one requires manual intervention"]  # what the acknowledgement is for is on the screen
    assert len(answer["items"]) == news.MAX_SHOWN + 1  # the first ones, and that one


def test_what_is_shown_never_has_an_acknowledgement_without_a_marked_item(tmp_path):
    for count in (1, 5, news.MAX_SHOWN, news.MAX_SHOWN + 7):
        items = [item(f"n{i} requires manual intervention" if i == count - 1 else f"n{i}", date="Sat, 10 Oct 2026 08:00:00 +0000") for i in range(count)]
        answer, _ = _check(tmp_path, feed(*items))
        assert answer["needs_ack"] is any(i["manual"] for i in answer["items"])


def test_a_manual_step_named_after_the_first_1500_characters_is_still_found(tmp_path):
    body = "&lt;p&gt;" + "Ordinary text. " * 150 + "&lt;/p&gt;&lt;p&gt;Action is required: edit /etc/foo before updating.&lt;/p&gt;"
    parsed = news.parse_feed(feed(item("A long one", body)))[0]
    assert len(parsed.summary) <= news.MAX_SUMMARY and parsed.summary.endswith("…") and "Action is required" not in parsed.summary
    assert parsed.manual is True  # searched whole, shown cut


@pytest.mark.parametrize("text", [
    "Please do this before you update.", "Before updating, read this.", "before the upgrade", "Manual steps are required",
    "This needs manual action", "You must manually edit your config", "intervention needed", "Breaking change in foo 2",
    "Manual intervention is required", "foo requires manual intervention", "Action required", "needs your attention",
])
def test_more_of_the_wording_arch_uses_is_recognised(text):
    assert news.parse_feed(feed(item(text)))[0].manual is True


@pytest.mark.parametrize("text", ["A new ISO is available", "Python 3.14 rebuilds", "The manual pages moved", "Please update your mirrorlist at your leisure"])
def test_ordinary_news_is_not_marked(text):
    assert news.parse_feed(feed(item(text)))[0].manual is False


REFUSED = "document type or entity declaration"


def _in(encoding: str, text: str) -> bytes:
    """The feed in the given encoding, with its XML declaration saying so (a UTF-16 feed whose declaration says UTF-8 is a different
    kind of broken)."""
    return text.replace('encoding="utf-8"', f'encoding="{encoding}"').encode(encoding)


def test_a_declaration_is_refused_whatever_the_encoding_of_the_feed():
    body = feed(item("T")).decode()
    with_entity = body.replace("<rss", '<!DOCTYPE rss [<!ENTITY a "EXPANDED">]><rss', 1).replace("<title>T</title>", "<title>&a;</title>")
    for encoding in ("utf-16", "utf-8", "iso-8859-1"):
        with pytest.raises(news.NewsError, match=REFUSED):
            news.parse_feed(_in(encoding, with_entity))


def test_an_encoding_expat_cannot_read_is_a_news_error_not_a_crash():
    body = feed(item("T")).decode()
    for encoding in ("utf-16-le", "shift_jis", "no-such-encoding"):
        with pytest.raises(news.NewsError, match="not readable"):
            news.parse_feed(_in(encoding, body) if encoding != "no-such-encoding" else body.replace("utf-8", encoding).encode())


def test_a_script_that_is_never_closed_does_not_hide_what_follows_it():
    body = "&lt;p&gt;ok&lt;/p&gt;&lt;script&gt;x&lt;/script&gt;&lt;p&gt;fine&lt;/p&gt;&lt;script&gt;Manual intervention is required before updating."
    parsed = news.parse_feed(feed(item("Plain title", body)))[0]
    assert parsed.summary == "ok\n\nfine\n\nManual intervention is required before updating."  # the closed script is gone, the unclosed one's text is kept
    assert parsed.manual is True
    closed = news.parse_feed(feed(item("Plain title", "&lt;p&gt;ok&lt;/p&gt;&lt;script&gt;alert(1)&lt;/script&gt;")))[0]
    assert "alert" not in closed.summary  # a script that IS closed is still dropped


def test_a_bare_document_type_is_refused_in_any_encoding_too():
    body = feed(item("T")).decode().replace("<rss", "<!DOCTYPE rss><rss", 1)  # no entity in it: the document type alone is not accepted
    for encoding in ("utf-16", "utf-8"):
        with pytest.raises(news.NewsError, match=REFUSED):
            news.parse_feed(_in(encoding, body))


def test_the_same_feed_without_a_declaration_is_read_in_those_encodings():
    body = feed(item("A title")).decode()
    for encoding in ("utf-16", "utf-8", "iso-8859-1"):
        assert news.parse_feed(_in(encoding, body))[0].title == "A title"  # so the refusals above are about the declaration, nothing else


def test_a_summary_has_a_limit_on_lines_too():
    html = "&lt;br&gt;x" * 600
    summary = news.parse_feed(feed(item("Many lines", html)))[0].summary
    assert summary.count("\n") < news.MAX_LINES and summary.endswith("…")


def test_the_link_is_only_what_an_address_on_the_site_is_made_of():
    for bad in ("https://evil.example\\.archlinux.org/news/", "https://archlinux.org:8443/x", "https://archlinux.org/a\\b", 'https://archlinux.org/"x',
                "https://archlinux.org/<x>", "https://archlinux.org/x y", "HTTP://archlinux.org/", "https://archlinux.org@evil.example/"):
        assert news.parse_feed(feed(item(link=escape(bad))))[0].link == "", bad
    ok = "https://archlinux.org/news/foo-1.2-requires-manual-intervention/?a=b&c=d"
    assert news.parse_feed(feed(item(link=escape(ok))))[0].link == ok


def test_the_news_check_runs_in_a_daemon_thread_that_never_holds_the_program_up(monkeypatch):
    import threading

    release = threading.Event()
    seen = []
    monkeypatch.setattr(news, "check", lambda **kw: seen.append(threading.current_thread()) or release.wait(30) and {"ok": True})
    wait = news.start()
    answer = wait(0.3)
    release.set()
    assert answer["ok"] is False and "in time" in answer["error"] and seen and seen[0].daemon is True


def test_a_feed_that_cannot_be_read_is_an_answer_never_an_exception(tmp_path):
    for failure in (http.HttpError("network error for https://archlinux.org/feeds/news/: no route"), news.NewsError("bad feed"),
                    RuntimeError("something odd"), OSError("the tests do not use the internet")):
        answer, _ = _check(tmp_path, failure)
        assert answer["ok"] is False and answer["error"]
    answer, _ = _check(tmp_path, http.HttpError("network error for https://archlinux.org/feeds/news/: no route"))
    assert answer["error"] == "network error for https://archlinux.org/feeds/news/: no route"  # a Cygnus error is said as it is, without a class name
    answer, _ = _check(tmp_path, b"<html>captive portal</html>")
    assert answer["ok"] is False and "not a news feed" in answer["error"]
    answer, _ = _check(tmp_path, feed(extra="<!DOCTYPE x>"))
    assert answer["ok"] is False
