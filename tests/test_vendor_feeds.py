"""Where newer versions of a converted program come from: the vendor's own package list, read safely."""

import gzip
import itertools
import zlib

import pytest

from cygnus.core.backends import vendor_feeds as vf
from cygnus.core.errors import CygnusError

SHA = "c58aa0f2cd66179c9f050e062c882d27aa9b9f8c2b7c73fee3498560b5ed0b38"
FEED = vf.FEEDS["google-chrome-stable"]


def stanza(package="google-chrome-stable", version="155.0.8059.39-1", filename=None, sha=SHA, arch="amd64", size="143552428"):
    filename = filename or f"pool/main/g/{package}/{package}_{version}_{arch}.deb"
    return (f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\nFilename: {filename}\nSize: {size}\nSHA256: {sha}\n"
            f"Description: Test\n second line\n")


def index(*stanzas):
    return gzip.compress("\n".join(stanzas).encode())


def fetch_of(raw):
    return lambda url, **kw: raw


def test_the_newest_deb_is_found_with_its_address_and_published_checksum():
    raw = index(stanza("google-chrome-beta", "156.0.1-1"), stanza(version="154.0.1-1"), stanza(version="155.0.8059.39-1"),
                stanza(version="9.0.1-1"))
    rel = vf.newest(FEED, "deb", fetch=fetch_of(raw))
    assert rel == vf.Release("155.0.8059.39-1", "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/"
                             "google-chrome-stable_155.0.8059.39-1_amd64.deb", SHA, 143552428, "deb")


def test_an_rpm_converted_program_gets_the_vendors_direct_rpm_address_and_the_same_version_number():
    rel = vf.newest(FEED, "rpm", fetch=fetch_of(index(stanza())))
    assert rel.format == "rpm" and rel.sha256 is None and rel.version == "155.0.8059.39-1"
    assert rel.url == "https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm"
    with pytest.raises(CygnusError, match="no download address"):
        vf.newest(vf.FEEDS["microsoft-edge-stable"], "rpm",
                  fetch=fetch_of(index(stanza("microsoft-edge-stable", "1.0-1"))))


@pytest.mark.parametrize("filename", ["../../etc/passwd", "pool/../../x.deb", "//evil.example/x.deb", "https://evil.example/x.deb",
                                      "pool/x deb", "-rf", "pool/\x00x.deb"])
def test_a_filename_that_leads_outside_the_vendors_folder_is_refused(filename):
    with pytest.raises(CygnusError):
        vf.newest(FEED, "deb", fetch=fetch_of(index(stanza(filename=filename))))


def test_an_entry_without_a_filename_is_refused():
    text = stanza().replace("Filename: pool/main/g/google-chrome-stable/google-chrome-stable_155.0.8059.39-1_amd64.deb\n", "")
    with pytest.raises(CygnusError, match="cannot read safely"):
        vf.newest(FEED, "deb", fetch=fetch_of(gzip.compress(text.encode())))


@pytest.mark.parametrize("fields", [{"sha": "nothex"}, {"sha": SHA[:-1]}, {"version": "x y"}, {"version": ""}, {"size": "12ab"}])
def test_an_entry_that_is_not_in_the_expected_form_is_refused(fields):
    with pytest.raises(CygnusError, match="cannot read safely"):
        vf.newest(FEED, "deb", fetch=fetch_of(index(stanza(**fields))))


def test_nothing_that_cannot_be_read_is_ever_taken_for_an_answer():
    for raw, match in ((b"not gzip at all", "not readable"), (index(stanza("something-else")), "does not mention"),
                       (index(stanza(arch="arm64")), "does not mention"),
                       (gzip.compress(b"x" * (vf.MAX_INDEX + 10)), "far larger")):
        with pytest.raises(CygnusError, match=match):
            vf.newest(FEED, "deb", fetch=fetch_of(raw))


def test_a_failed_download_of_the_list_is_an_error_not_an_answer():
    def broken(url, **kw):
        raise CygnusError("network error")

    with pytest.raises(CygnusError, match="network error"):
        vf.newest(FEED, "deb", fetch=broken)


def test_every_bundled_feed_is_https_and_keeps_its_files_under_its_own_folder():
    for name, feed in vf.FEEDS.items():
        assert name == feed.package and feed.index.startswith("https://") and feed.base.startswith("https://")
        assert feed.index.startswith(feed.base.rsplit("/", 2)[0]) and feed.base.endswith("/")
        assert feed.rpm is None or feed.rpm.startswith("https://")


VERSIONS = ["1.0", "1.0.1", "1.0a", "1.0-1", "1.0-2", "1.0-10", "2:0.1", "1:9", "155.0.8059.39-1", "155.0.8059.39-3", "155.0.8060.1-1",
            "156.0.8078.12-1", "1.0rc1", "1.0.0", "0.9.9", "1.10", "1.9", "1.0+git20240101", "20240101", "1.0b-1", "2.0~rc1", "1.00",
            "1.0.", "", "9", "10", "1.0-1.1", "1.0-1a"]


@pytest.mark.parametrize("a, b", list(itertools.permutations(VERSIONS, 2)))
def test_the_version_order_is_pacmans(a, b):
    pyalpm = pytest.importorskip("pyalpm")
    assert vf.vercmp(a, b) == pyalpm.vercmp(a, b), (a, b)


def test_a_compressed_list_is_decompressed_with_a_cap_not_in_full():
    bomb = zlib.compress(b"\0" * (vf.MAX_INDEX * 2), 9)
    assert len(bomb) < vf.MAX_COMPRESSED
    with pytest.raises(CygnusError, match="far larger"):
        vf.newest(FEED, "deb", fetch=fetch_of(gzip.compress(b"\0" * (vf.MAX_INDEX * 2))))


# -- sources the person adds themselves ----------------------------------------------------------------------------------
def test_a_source_added_by_the_person_is_kept_listed_used_and_removable():
    from cygnus.core import preferences

    assert vf.feed_for("my-tool") is None
    mine = vf.Feed("my-tool", "https://example.org/apt/dists/stable/main/binary-amd64/Packages.gz", "https://example.org/apt/",
                   "https://example.org/my-tool-latest.rpm")
    vf.add_user_feed(mine)
    assert vf.feed_for("my-tool") == mine and "my-tool" in vf.user_feeds() and "my-tool" not in vf.FEEDS
    assert preferences.load()["vendor_feeds"]["my-tool"]["base"] == "https://example.org/apt/"
    assert vf.remove_user_feed("my-tool") is True and vf.feed_for("my-tool") is None
    assert vf.remove_user_feed("my-tool") is False and "vendor_feeds" not in preferences.load()


def test_a_source_the_person_added_wins_over_the_built_in_one():
    own = vf.Feed("google-chrome-stable", "https://mirror.example/Packages.gz", "https://mirror.example/")
    vf.add_user_feed(own)
    assert vf.feed_for("google-chrome-stable") == own


@pytest.mark.parametrize("feed", [
    vf.Feed("x", "http://example.org/Packages.gz", "https://example.org/"),  # not https
    vf.Feed("x", "https://example.org/Packages.gz", "https://example.org"),  # the base must end in a slash
    vf.Feed("../x", "https://example.org/Packages.gz", "https://example.org/"),
    vf.Feed("x", "https://exa mple.org/Packages.gz", "https://example.org/"),
    vf.Feed("x", "https://example.org/Packages.gz", "https://example.org/", "ftp://example.org/x.rpm")])
def test_a_source_that_is_not_https_or_not_well_formed_is_refused(feed):
    with pytest.raises(CygnusError, match="https addresses"):
        vf.add_user_feed(feed)


def test_a_hand_edited_preferences_entry_that_does_not_look_right_is_ignored():
    from cygnus.core import preferences

    preferences.save({"vendor_feeds": {"good": {"index": "https://e.org/P.gz", "base": "https://e.org/"},
                                       "bad1": {"index": "http://e.org/P.gz", "base": "https://e.org/"},
                                       "bad2": "text", "../bad3": {"index": "https://e.org/P.gz", "base": "https://e.org/"},
                                       "bad4": {"index": "https://e.org/P.gz", "base": "https://e.org/", "rpm": 5}}})
    assert list(vf.user_feeds()) == ["good"]


def test_the_cli_adds_only_a_source_that_can_be_read(monkeypatch, capsys):
    import argparse

    from cygnus.cli import main as cli

    args = argparse.Namespace(sub="add", package="google-chrome-stable", index="https://example.org/Packages.gz",
                              base="https://example.org/pool", rpm=None)
    monkeypatch.setattr(vf, "newest", lambda feed, fmt, **kw: (_ for _ in ()).throw(CygnusError("the vendor's package list is not readable")))
    with pytest.raises(CygnusError):
        cli.cmd_feed(args)
    assert vf.user_feeds() == {}
    monkeypatch.setattr(vf, "newest", lambda feed, fmt, **kw: vf.Release("9.9-1", feed.base + "x.deb", SHA, 1, "deb"))
    assert cli.cmd_feed(args) == 0
    assert vf.user_feeds()["google-chrome-stable"].base == "https://example.org/pool/"  # the slash was added
    assert "added: google-chrome-stable is published as 9.9-1" in capsys.readouterr().out
    assert cli.cmd_feed(argparse.Namespace(sub="list")) == 0 and "added by you" in capsys.readouterr().out
    assert cli.cmd_feed(argparse.Namespace(sub="remove", package="google-chrome-stable")) == 0
    assert cli.cmd_feed(argparse.Namespace(sub="remove", package="google-chrome-stable")) == 1


def test_a_package_list_that_ends_early_is_not_taken_for_the_whole_list():
    full = "\n".join([stanza(version="150.0.1-1"), stanza(version="151.0.0-1")]).encode()
    whole = gzip.compress(full)
    assert vf.newest(FEED, "deb", fetch=fetch_of(whole)).version == "151.0.0-1"
    cut = zlib.compressobj(wbits=31)
    partial = cut.compress(stanza(version="150.0.1-1").encode() + b"\n") + cut.flush(zlib.Z_SYNC_FLUSH)  # a clean cut: no end marker
    with pytest.raises(CygnusError, match="incomplete"):
        vf.newest(FEED, "deb", fetch=fetch_of(partial))
