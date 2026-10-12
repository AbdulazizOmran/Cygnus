"""What the Debian and Fedora catalogues share: compressed text read with a limit (xz, gzip and zstd), lines that cross the pieces it is read in, the
patterns for kept fields, and the AUR's names."""

import gzip
import lzma

import pytest
from compression import zstd

from cygnus.core.backends import catalogue_common as common
from cygnus.core.errors import CygnusError

PACK = {"xz": lzma.compress, "gz": gzip.compress, "zst": zstd.compress}


@pytest.mark.parametrize("kind", ["xz", "gz", "zst"])
def test_every_kind_of_compression_is_read_back_whole(kind):
    data = bytes(range(256)) * 1000
    assert b"".join(common.chunks(PACK[kind](data), kind, 10**9, "Test")) == data


@pytest.mark.parametrize("kind", ["xz", "gz", "zst"])
def test_lines_that_cross_the_pieces_are_not_cut_and_the_last_line_needs_no_line_end(kind):
    lines = [f"line {i} " + "x" * (i % 97) for i in range(5000)]
    text = "\n".join(lines).encode()  # no line end after the last one
    assert list(common.text_lines(PACK[kind](text), kind, 10**9, "Test")) == lines


def test_line_ends_are_removed_whatever_they_are_and_bad_bytes_do_not_stop_the_reading():
    assert list(common.text_lines(gzip.compress(b"one\r\ntwo\nthree\xff\n\nlast"), "gz", 10**9, "Test")) == ["one", "two", "three�", "", "last"]
    assert list(common.text_lines(gzip.compress(b""), "gz", 10**9, "Test")) == []


@pytest.mark.parametrize("kind", ["xz", "gz", "zst"])
def test_a_small_download_that_would_become_an_enormous_text_is_refused_naming_whose_list_it_was(kind):
    bomb = PACK[kind](b"x" * 5_000_000)
    assert len(bomb) < 100_000
    with pytest.raises(CygnusError, match="a list from Somebody is larger than Cygnus will read"):
        list(common.chunks(bomb, kind, 1_000_000, "Somebody"))


@pytest.mark.parametrize("kind", ["xz", "gz", "zst"])
@pytest.mark.parametrize("data", [b"not compressed at all", b""])
def test_data_that_is_not_what_it_claims_is_an_error_in_words(kind, data):
    with pytest.raises(CygnusError, match="a list from Somebody could not be unpacked"):
        list(common.chunks(data, kind, 10**9, "Somebody"))


@pytest.mark.parametrize("kind", ["xz", "gz", "zst"])
def test_a_cut_short_file_is_an_error_in_words(kind):
    whole = PACK[kind](bytes(range(256)) * 5000)
    with pytest.raises(CygnusError, match="could not be unpacked"):
        list(common.chunks(whole[:len(whole) // 2], kind, 10**9, "Somebody"))


def test_an_unknown_kind_is_a_programming_error_not_a_silent_empty_list():
    with pytest.raises(CygnusError):
        list(common.chunks(b"x", "bz2", 100, "Somebody"))


def test_the_aur_names_are_one_per_line_without_comments_or_blanks():
    assert common.aur_names(["# AUR package list", "alpha", "beta", "", "   ", "  gamma  ", "#delta"]) == {"alpha", "beta", "gamma"}
    assert common.aur_names([]) == set()


def test_the_aur_is_asked_over_https_on_its_own_host_only():
    assert common.AUR_NAMES == "https://aur.archlinux.org/packages.gz"
    assert common.aur_host_ok("aur.archlinux.org") and not common.aur_host_ok("archlinux.org") and not common.aur_host_ok("evil.example") and not common.aur_host_ok("x.aur.archlinux.org")


@pytest.mark.parametrize("pattern, good, bad", [
    (common.CATEGORY, ["Office", "Net-work", "A"], ["", "9x", "a b", "../x", "x" * 31]),
    (common.APP_ID, ["org.gnome.gitg", "demo", "a+b_c-d.e"], ["", " x", "a b", "a/b", "-x", "x" * 201]),
    (common.HTTPS, ["https://demo.example", "https://demo.example/a/b?x=1#y", "https://demo.example:8443/x"], ["http://demo.example", "https://bad host", "javascript:x", "https://", "ftp://x.example"]),
    (common.SHA256, ["ab" * 32], ["AB" * 32, "ab" * 31, "zz" * 32, "", "ab" * 32 + "\n"]),
])
def test_the_patterns_for_kept_fields(pattern, good, bad):
    assert all(pattern.fullmatch(g) for g in good) and not any(pattern.fullmatch(b) for b in bad)
