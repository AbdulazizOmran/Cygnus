"""Fedora's applications read as a catalogue: the AppStream data for the release, joined to the release's package list (checked against the checksum
its repomd.xml gives), every kept field checked, and only a package with a real file for this processor listed. A real excerpt of the package list is
in tests/data; no test asks the internet."""

import gzip
import hashlib
from pathlib import Path

import pytest

from cygnus.core.backends import catalogue_common as common
from cygnus.core.backends import fedora_index as fi
from cygnus.core.errors import CygnusError

SAMPLE = (Path(__file__).parent / "data" / "fedora_primary_sample.xml").read_bytes()
SHA = "ab" * 32


def pieces(data: bytes, size: int = 997):
    """The bytes in odd-sized pieces, so no entry happens to end at the end of a piece."""
    return (data[i:i + size] for i in range(0, len(data), size))


def entry(name="demo", arch="x86_64", ver="1.0", rel="1.fc44", epoch="0", sha=SHA, size="1234", href=None, requires=("demo-data", "libc.so.6()(64bit)"), tag="package"):
    href = href if href is not None else f"Packages/{name[0]}/{name}-{ver}-{rel}.{arch}.rpm"
    reqs = "".join(f'<rpm:entry name="{r}"/>' for r in requires)
    return (f'<{tag} type="rpm"><name>{name}</name><arch>{arch}</arch><version epoch="{epoch}" ver="{ver}" rel="{rel}"/>'
            f'<checksum type="sha256" pkgid="YES">{sha}</checksum><packager>Fedora Project</packager><size package="{size}" installed="9"/><location href="{href}"/>'
            f"<format><rpm:requires>{reqs}</rpm:requires><rpm:provides><rpm:entry name=\"provided\"/></rpm:provides></format></{tag}>\n").encode()


def read(*blocks, wanted=("demo",)):
    return fi.packages(pieces(b"<metadata>" + b"".join(blocks) + b"</metadata>"), set(wanted))


# -- the real excerpt ----------------------------------------------------------------------------------------------------------------------------
def test_the_real_package_list_excerpt_gives_where_the_file_is_and_how_to_check_it():
    found = fi.packages(pieces(SAMPLE), {"0ad", "gimp", "0ad-data", "nothing-like-it"})
    assert "0ad" in found and "0ad-data" in found
    zero = found["0ad"]
    assert zero["version"] == "0.28.0-2.fc44" and zero["location"] == "Packages/0/0ad-0.28.0-2.fc44.x86_64.rpm"
    assert zero["size"] == 5777659 and zero["sha256"] == "0a43f7968af2f7b1f4cd9a109c695c5d001d92bdd9af5d698243d575e2ccef22"
    assert zero["depends"][:2] == ["0ad-data", "hicolor-icon-theme"] and not any(".so" in d or "/" in d or "(" in d for d in zero["depends"])  # packages, not files
    assert found["0ad-data"]["location"].endswith(".noarch.rpm")


def test_an_entry_in_the_real_list_is_found_whatever_size_the_pieces_are_read_in():
    expected = fi.packages(pieces(SAMPLE, 1 << 20), {"0ad", "0ad-data"})
    for size in (1, 7, 100, 4096, len(SAMPLE) + 1):
        assert fi.packages(pieces(SAMPLE, size), {"0ad", "0ad-data"}) == expected


def test_the_packager_element_is_not_taken_for_the_start_of_an_entry():
    """Found on the real data: '<packager>' begins with '<package', and the first version of the reader started every entry there and read none."""
    assert read(entry())["demo"]["version"] == "1.0-1.fc44"


# -- one entry --------------------------------------------------------------------------------------------------------------------------------------
def test_an_entry_is_read_with_its_version_size_checksum_place_and_needs():
    assert read(entry())["demo"] == {"version": "1.0-1.fc44", "location": "Packages/d/demo-1.0-1.fc44.x86_64.rpm", "sha256": SHA, "size": 1234, "depends": ["demo-data"]}
    assert read(entry(epoch="2"))["demo"]["version"] == "2:1.0-1.fc44"
    assert read(entry(arch="noarch"))["demo"]["location"].endswith(".noarch.rpm")


@pytest.mark.parametrize("over", [
    {"arch": "i686"}, {"arch": "aarch64"}, {"arch": "src"}, {"arch": ""},
    {"arch": "i686", "href": "Packages/d/demo-1.0-1.fc44.x86_64.rpm"}, {"arch": "aarch64", "href": "Packages/d/demo-1.0-1.fc44.noarch.rpm"},  # the arch element counts on its own
    {"href": "Packages/d/../../x/demo-1.0-1.fc44.x86_64.rpm"}, {"href": "Packages/d/demo-1.0-1.fc44.i686.rpm"}, {"href": "/Packages/d/demo-1.0-1.fc44.x86_64.rpm"},
    {"href": "https://evil.example/demo-1.0-1.fc44.x86_64.rpm"}, {"href": "Packages/d/demo.x86_64.rpm.sig"}, {"href": "other/d/demo-1.0-1.fc44.x86_64.rpm"},
    {"href": "Packages/dd/demo-1.0-1.fc44.x86_64.rpm"}, {"href": "Packages/d/demo 1.x86_64.rpm"}, {"href": "Packages/D/demo-1.0-1.fc44.x86_64.rpm"},
    {"href": "Packages/d//demo-1.0-1.fc44.x86_64.rpm"},
    {"sha": "AB" * 32}, {"sha": "ab" * 31}, {"sha": "zz" * 32}, {"sha": ""},
    {"size": "0"}, {"size": "-5"}, {"size": "12abc"}, {"size": str(fi.MAX_FILE + 1)}, {"size": ""}, {"size": "1e3"},
    {"ver": "bad version"}, {"rel": "1/../x"}, {"ver": ""}, {"epoch": "x"},
])
def test_an_entry_that_is_not_a_clean_file_for_this_processor_is_left_out(over):
    assert read(entry(**over)) == {}


def test_an_entry_with_a_checksum_of_another_kind_is_left_out():
    bad = entry().replace(b'type="sha256"', b'type="md5"')
    assert read(bad) == {}


def test_only_wanted_packages_are_parsed_and_the_first_entry_of_a_name_wins():
    found = read(entry("other"), entry("demo", ver="1.0"), entry("demo", ver="2.0"), wanted=("demo",))
    assert list(found) == ["demo"] and found["demo"]["version"] == "1.0-1.fc44"
    assert read(entry(), wanted=()) == {}


def test_an_entry_that_is_not_xml_is_skipped_and_the_ones_after_it_are_read():
    broken = b'<package type="rpm"><name>demo</name><arch>x86_64</arch><version></package>\n'
    assert list(read(broken, entry("other"), wanted=("demo", "other"))) == ["other"]
    assert read(b'<package type="rpm"><name>demo</name>&xxe;</package>\n', entry("other"), wanted=("demo", "other")).keys() == {"other"}


@pytest.mark.parametrize("marker", [b'<!DOCTYPE x [<!ENTITY a "aaaa">]>', b"<!-- a comment -->", b"<![CDATA[ text ]]>"])
def test_a_package_list_with_a_document_type_a_comment_or_cdata_is_not_read_at_all(marker):
    """A real list is escaped text. One with these can hide a whole entry of another program inside a description, where cutting at the first
    "</package>" would find it and give it the wrong program's name."""
    with pytest.raises(CygnusError, match="constructs a real one never has"):
        read(entry("other"), b'<package type="rpm">' + marker + b"<name>demo</name></package>\n", wanted=("demo", "other"))


def test_an_entry_hidden_in_a_description_is_never_taken_for_the_real_one():
    decoy = (b'<package type="rpm"><name>evil</name><arch>x86_64</arch><description><![CDATA[' + entry("firefox", sha="cd" * 32, href="Packages/e/evil-1.0-1.fc44.x86_64.rpm").strip()
             + b"]]></description></package>\n")
    for size in (7, 97, 10**6):
        with pytest.raises(CygnusError, match="constructs a real one never has"):
            fi.packages(pieces(b"<metadata>" + decoy + b"</metadata>", size), {"firefox"})
    commented = b'<package type="rpm"><!-- <name>firefox</name> --><name>evil</name></package>\n'
    with pytest.raises(CygnusError, match="constructs a real one never has"):
        read(commented, wanted=("firefox", "evil"))


def test_a_marker_split_across_two_pieces_is_found_too():
    data = b"<metadata>" + entry("other") + b"<!-- comment --></metadata>"
    at = data.index(b"<!--") + 2
    with pytest.raises(CygnusError, match="constructs a real one never has"):
        fi.packages(iter([data[:at], data[at:]]), {"other"})


def test_an_entry_that_starts_with_a_plain_package_tag_is_read_too():
    assert read(entry().replace(b' type="rpm"', b""))["demo"]["version"] == "1.0-1.fc44"


def test_the_real_list_excerpt_has_none_of_those_constructs():
    assert not fi._NEVER_IN_A_LIST.search(SAMPLE)


def test_a_package_of_several_applications_lists_every_id_and_the_first_one_stays_the_id():
    [one] = fi.apps([app("a.desktop", "First App", "multi"), app("b.desktop", "Second App", "multi"), app("a.desktop", "First App", "multi")]).values()
    assert one["id"] == "a" and one["ids"] == ["a", "b"] and one["summary"] == "Installs 2 programs: First App, Second App"


def test_a_long_list_of_requirements_is_cut_early_and_quickly():
    import time

    started = time.monotonic()
    assert len(fi._requires(f"pkg{i:06}" for i in range(200_000))) == fi.MAX_DEPENDS
    assert time.monotonic() - started < 1


def test_an_entry_that_never_ends_is_an_error_and_not_endless_memory(monkeypatch):
    monkeypatch.setattr(fi, "MAX_ENTRY", 1000)
    with pytest.raises(CygnusError, match="far larger than expected"):
        fi.packages(pieces(b"<package type=\"rpm\">" + b"x" * 5000), {"demo"})


def test_what_a_package_requires_is_package_names_only_in_order_without_repeats_and_not_too_many():
    names = fi._requires(["/usr/bin/sh", "a-lib", "libfoo.so.1()(64bit)", "rpmlib(PayloadIsZstd)", "(foo if bar)", "python(abi)", "perl(Foo)", "other-pkg", "a-lib", "x", "Bad Name",
                          "pkg+plus", "../x", ""])
    assert names == ["a-lib", "other-pkg", "pkg+plus"]
    assert len(fi._requires([f"pkg{i:03}" for i in range(100)])) == fi.MAX_DEPENDS


# -- the release's metadata ---------------------------------------------------------------------------------------------------------------------
def repomd(href=None, digest=SHA, kind="sha256", location_extra=""):
    href = href if href is not None else f"repodata/{digest}-primary.xml.zst"
    return (f'<?xml version="1.0"?><repomd xmlns="http://linux.duke.edu/metadata/repo" xmlns:rpm="http://linux.duke.edu/metadata/rpm"><revision>1</revision>'
            f'<data type="filelists"><checksum type="sha256">{"cd" * 32}</checksum><location href="repodata/{"cd" * 32}-filelists.xml.zst"/></data>'
            f'<data type="primary"><checksum type="{kind}">{digest}</checksum><location href="{href}"{location_extra}/></data></repomd>').encode()


def test_the_package_list_is_found_in_the_release_description_with_its_checksum_and_compression():
    assert fi.primary_location(repomd()) == (f"repodata/{SHA}-primary.xml.zst", SHA, "zst")
    assert fi.primary_location(repomd(href=f"repodata/{SHA}-primary.xml.gz"))[2] == "gz"
    assert fi.primary_location(repomd(href=f"repodata/{SHA}-primary.xml.xz"))[2] == "xz"


@pytest.mark.parametrize("data", [
    repomd(href="repodata/other-primary.xml.zst"), repomd(href=f"repodata/{'cd' * 32}-primary.xml.zst"),  # the name must carry the checksum it is checked against
    repomd(href=f"../repodata/{SHA}-primary.xml.zst"), repomd(href=f"https://evil.example/{SHA}-primary.xml.zst"), repomd(href=f"repodata/{SHA}-primary.xml.bz2"),
    repomd(href=f"repodata/{SHA}-primary.xml.zst/../x"), repomd(href=f"/repodata/{SHA}-primary.xml.zst"),
    repomd(digest="AB" * 32, href="repodata/" + "AB" * 32 + "-primary.xml.zst"), repomd(kind="md5"), repomd(digest="short", href="repodata/short-primary.xml.zst"),
    b"not xml", b"", b"<repomd/>", b'<repomd xmlns="x"><data type="filelists"/></repomd>',
])
def test_a_release_description_that_does_not_name_a_clean_package_list_is_an_error_in_words(data):
    with pytest.raises(CygnusError, match="Fedora's release description"):
        fi.primary_location(data)


def test_the_release_numbers_are_the_links_to_two_and_three_digit_folders_newest_first():
    listing = '<a href="10/">10/</a> <a href="7/">7/</a> <a href="44/">44/</a> <a href="9/">9/</a> <a href="43/">43/</a> <a href="../">..</a> <a href="rawhide/">r</a> <a href="44/">dup</a>'
    assert fi.releases_in(listing) == [44, 43, 10]  # a one-digit folder is not a release this is made for
    assert fi.releases_in("") == [] and fi.releases_in("<html></html>") == []
    assert fi.releases_in('<a href="1000/">x</a> <a href="045/">x</a>') == []  # no leading zero, no four digits


def test_the_addresses_are_https_on_fedoras_mirror_and_an_rpms_address_is_built_from_a_checked_place_only():
    assert fi.tree(44) == "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/os/"
    assert fi.appstream_url(44) == "https://dl.fedoraproject.org/pub/alt/screenshots/f44/fedora-44.xml.gz"
    assert fi.file_url(44, "Packages/d/demo-1.0-1.fc44.x86_64.rpm") == fi.tree(44) + "Packages/d/demo-1.0-1.fc44.x86_64.rpm"
    assert fi.host_ok("dl.fedoraproject.org") and not fi.host_ok("evil.example") and not fi.host_ok("x.dl.fedoraproject.org") and not fi.host_ok("fedoraproject.org")


# -- the applications ------------------------------------------------------------------------------------------------------------------------------
def app(id_="demo.desktop", name="Demo", pkgname="demo", **over):
    return {**{"id": id_, "name": name, "summary": "Does demos.", "developer": "", "pkgname": pkgname, "icon": "", "icon_size": "", "url": "https://demo.example/",
               "categories": ["Office"], "keywords": [], "description": ""}, **over}


def test_applications_are_kept_by_package_name_with_plain_checked_fields():
    out = fi.apps([app(), app("other.desktop", "Other", "other")])
    assert sorted(out) == ["demo", "other"] and out["demo"]["name"] == "Demo" and out["demo"]["id"] == "demo"  # ".desktop" removed
    assert out["demo"] == {"id": "demo", "ids": ["demo"], "package": "demo", "name": "Demo", "summary": "Does demos.", "categories": ["Office"], "url": "https://demo.example/"}


@pytest.mark.parametrize("over", [{"pkgname": ""}, {"pkgname": "Bad Name"}, {"pkgname": "../x"}, {"pkgname": "a"}, {"id_": "bad id"}, {"id_": ""}, {"id_": "a/b"}, {"name": ""}, {"name": "  "}])
def test_an_application_without_a_clean_package_id_and_name_is_left_out(over):
    assert fi.apps([app(**over)]) == {}


def test_urls_categories_and_text_of_an_application_are_checked_and_cut():
    [one] = fi.apps([app(url="http://plain.example", categories=["Office", "../x", "a b", "9x"] + [f"C{i}" for i in range(30)], name="N‮" * 100, summary="s\x1b" * 400)]).values()
    assert one["url"] == "" and one["categories"][0] == "Office" and len(one["categories"]) == 12
    assert len(one["name"]) <= fi.MAX_NAME and len(one["summary"]) <= fi.MAX_SUMMARY and all(c.isprintable() for c in one["name"] + one["summary"])
    assert fi.apps([app(url="https://" + "a" * 400 + ".example")])["demo"]["url"] == ""


def test_a_huge_list_of_applications_is_cut_off(monkeypatch):
    monkeypatch.setattr(fi, "MAX_APPS", 3)
    assert len(fi.apps([app(f"p{i:02}.desktop", f"App {i}", f"pkg{i:02}") for i in range(10)])) == 3


# -- joining and reading back --------------------------------------------------------------------------------------------------------------------------
def test_a_program_the_aur_has_is_not_listed_and_one_without_a_file_is_not_either():
    apps = fi.apps([app("one.desktop", "One", "one"), app("two.desktop", "Two", "two"), app("three.desktop", "Three", "three")])
    pkgs = read(entry("one"), entry("two"), wanted=("one", "two", "three"))
    assert [r["package"] for r in fi.records(apps, pkgs, 44)] == ["one", "two"] and all(r["release"] == 44 for r in fi.records(apps, pkgs, 44))
    assert [r["package"] for r in fi.records(apps, pkgs, 44, {"two"})] == ["one"]


def good_record(**over):
    return {"package": "demo", "id": "demo", "name": "Demo", "summary": "s", "categories": ["Office"], "url": "https://demo.example", "release": 44, "version": "1.0-1.fc44",
            "location": "Packages/d/demo-1.0-1.fc44.x86_64.rpm", "sha256": SHA, "size": 10, "depends": ["demo-data"], **over}


def test_what_is_made_passes_the_check_that_reads_it_back():
    recs = fi.records(fi.apps([app()]), read(entry()), 44)
    assert fi.clean(recs) == recs


@pytest.mark.parametrize("over", [{"package": "Bad Name"}, {"package": 5}, {"id": "bad id"}, {"name": " "}, {"location": "Packages/d/../x.x86_64.rpm"}, {"sha256": "x"},
                                  {"version": "bad version"}, {"size": 0}, {"size": True}, {"size": "10"}, {"size": fi.MAX_FILE + 1}, {"summary": None}, {"url": 5},
                                  {"release": 4}, {"release": True}, {"release": "44"}, {"release": 1000}, {"release": None}])
def test_a_cached_record_that_is_not_clean_is_dropped(over):
    assert fi.clean([good_record(**over)]) == []


def test_cached_records_are_cut_again_deduplicated_and_junk_lists_are_nothing():
    rec = good_record(name="N\x1b[0m" * 100, summary="s" * 1000, url="http://plain.example", categories=["ok", "../x", 5], depends=["demo-data", "Bad Name", 5, "libfoo.so.1", "x" * 500])
    out = fi.clean([rec, good_record(), "x", None, 5])
    assert [r["package"] for r in out] == ["demo"] and len(out[0]["name"]) <= fi.MAX_NAME and len(out[0]["summary"]) <= fi.MAX_SUMMARY
    assert out[0]["url"] == "" and out[0]["categories"] == ["ok"] and out[0]["depends"] == ["demo-data"]
    assert fi.clean(None) == [] and fi.clean({"a": 1}) == [] and fi.clean("x") == []


# -- fetching -------------------------------------------------------------------------------------------------------------------------------------------
APPSTREAM = (b'<components origin="fedora"><component type="desktop-application"><id>demo.desktop</id><name>Demo</name><summary>Does demos</summary>'
             b"<pkgname>demo</pkgname><categories><category>Office</category></categories></component>"
             b'<component type="desktop-application"><id>two.desktop</id><name>Two</name><summary>Does two</summary><pkgname>two</pkgname></component></components>')


def primary_for(*blocks):
    from compression import zstd

    data = zstd.compress(b"<metadata>" + b"".join(blocks) + b"</metadata>")
    return data, hashlib.sha256(data).hexdigest()


def fake_get(files):
    asked = []

    def get(url, **kw):
        asked.append((url, kw))
        value = files[url]
        if isinstance(value, Exception):
            raise value
        return value

    get.asked = asked
    return get


def standard(release=44):
    data, digest = primary_for(entry("demo"), entry("two"))
    base = fi.tree(release)
    files = {fi.RELEASES: b'<a href="43/">43/</a> <a href="44/">44/</a>', base + "repodata/repomd.xml": repomd(href=f"repodata/{digest}-primary.xml.zst", digest=digest),
             base + f"repodata/{digest}-primary.xml.zst": data, fi.appstream_url(release): gzip.compress(APPSTREAM),
             common.AUR_NAMES: gzip.compress(b"# AUR\ntwo\nother\n")}
    return files


def test_the_lists_are_fetched_from_fedoras_host_with_limits_checked_joined_and_the_aur_ones_left_out():
    get = fake_get(standard())
    lines = []
    out = fi.fetch(lines.append, get=get)
    assert [r["package"] for r in out] == ["demo"] and out[0]["release"] == 44 and len(lines) == 4
    urls = [u for u, _ in get.asked]
    assert urls[0] == fi.RELEASES and urls[1] == fi.tree(44) + "repodata/repomd.xml" and urls[2] == fi.appstream_url(44) and urls[-1] == common.AUR_NAMES
    assert all(kw["limit"] > 0 for _u, kw in get.asked)
    fedora = get.asked[0][1]["host_ok"]
    assert fedora("dl.fedoraproject.org") and not fedora("evil.example") and get.asked[-1][1]["host_ok"]("aur.archlinux.org")
    assert all(kw["host_ok"] is get.asked[0][1]["host_ok"] for u, kw in get.asked[:-1])


def test_a_release_that_has_a_folder_and_no_packages_yet_is_passed_over_for_the_one_before():
    files = standard(43)
    files[fi.RELEASES] = b'<a href="43/">43/</a> <a href="44/">44/</a> <a href="45/">45/</a>'
    from cygnus.core.util import http

    files[fi.tree(45) + "repodata/repomd.xml"] = http.HttpError("HTTP 404", 404)
    files[fi.tree(44) + "repodata/repomd.xml"] = http.HttpError("HTTP 404", 404)
    out = fi.fetch(get=fake_get(files))
    assert out[0]["release"] == 43


def test_when_no_release_has_a_package_list_it_is_an_error_in_words():
    from cygnus.core.util import http

    files = {fi.RELEASES: b'<a href="44/">44/</a>', fi.tree(44) + "repodata/repomd.xml": http.HttpError("HTTP 404", 404)}
    with pytest.raises(CygnusError, match="could not find a Fedora release with a package list"):
        fi.fetch(get=fake_get(files))
    with pytest.raises(CygnusError, match="could not find a Fedora release"):
        fi.fetch(get=fake_get({fi.RELEASES: b"nothing here"}))


def test_a_package_list_that_does_not_match_the_checksum_the_release_gives_is_not_used():
    files = standard()
    data, digest = primary_for(entry("demo"))
    files[fi.tree(44) + f"repodata/{digest}-primary.xml.zst"] = data + b"x"  # the same name, other bytes
    files[fi.tree(44) + "repodata/repomd.xml"] = repomd(href=f"repodata/{digest}-primary.xml.zst", digest=digest)
    with pytest.raises(CygnusError, match="does not match the checksum"):
        fi.fetch(get=fake_get(files))


def test_a_damaged_package_list_is_an_error_in_words():
    files = standard()
    junk = b"not zstd at all"
    digest = hashlib.sha256(junk).hexdigest()
    files[fi.tree(44) + "repodata/repomd.xml"] = repomd(href=f"repodata/{digest}-primary.xml.zst", digest=digest)
    files[fi.tree(44) + f"repodata/{digest}-primary.xml.zst"] = junk
    with pytest.raises(CygnusError, match="could not be unpacked"):
        fi.fetch(get=fake_get(files))


def test_a_package_that_holds_several_applications_is_one_entry_named_after_the_package_with_the_programs_listed():
    [one] = fi.apps([app("a.desktop", "First App", "multi"), app("b.desktop", "Second App", "multi"), app("c.desktop", "First App", "multi")]).values()
    assert one["name"] == "multi" and one["summary"] == "Installs 2 programs: First App, Second App" and one["id"] == "a" and one["package"] == "multi"
    assert fi.apps([app("s.desktop", "Solo One", "solo")])["solo"]["name"] == "Solo One"


def test_a_release_whose_list_of_applications_is_not_there_yet_is_passed_over_for_the_one_before():
    from cygnus.core.util import http

    files = standard(43)
    files[fi.RELEASES] = b'<a href="43/">43/</a> <a href="44/">44/</a>'
    files[fi.tree(44) + "repodata/repomd.xml"] = repomd(href=f"repodata/{SHA}-primary.xml.zst")
    files[fi.appstream_url(44)] = http.HttpError("HTTP 404", 404)
    assert fi.fetch(get=fake_get(files))[0]["release"] == 43
