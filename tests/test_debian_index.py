"""Debian's applications read as a catalogue: DEP-11 (a small corner of YAML) and the package list, both as hostile text, decompressed with a limit,
every kept field checked, and only a package with a real file for this processor listed. Real excerpts of both files are in tests/data; no test
asks the internet."""

import gzip
import io
import lzma
from pathlib import Path

import pytest

from cygnus.core.backends import debian_index as di
from cygnus.core.errors import CygnusError

DATA = Path(__file__).parent / "data"
DEP11 = (DATA / "debian_dep11_sample.yml").read_text().splitlines()
PACKAGES = (DATA / "debian_packages_sample.txt").read_text().splitlines()
SHA = "ab" * 32


def doc(*lines):
    return ["---", *lines]


def app_doc(package="demo", id_="demo.desktop", name="Demo", summary="Does demos", type_="desktop-application", extra=()):
    return doc(f"Type: {type_}", f"ID: {id_}", f"Package: {package}", "Name:", f"  C: {name}", "Summary:", f"  C: {summary}", *extra)


# -- the real excerpts ----------------------------------------------------------------------------------------------------------------------
def test_the_real_dep11_excerpt_gives_the_desktop_applications_and_leaves_out_add_ons_and_fonts():
    found = di.apps(DEP11)
    assert sorted(found) == ["abiword", "gitg", "lxi-tools", "starfighter"]  # an add-on and a font are in the file and are not applications
    assert found["abiword"] == {"id": "abiword", "ids": ["abiword"], "package": "abiword", "name": "AbiWord", "summary": "Compose, edit, and view documents",
                                "categories": ["Office", "WordProcessor"], "url": ""}
    assert found["gitg"]["id"] == "org.gnome.gitg" and found["gitg"]["url"] == "https://wiki.gnome.org/Apps/Gitg"  # the homepage, not the bug tracker
    assert found["starfighter"]["name"] == "Project: Starfighter"  # a 'single quoted' value
    assert found["lxi-tools"]["summary"] == "LXI-Tools is a collection of open source tools for managing network connected LXI compatible test instruments."


def test_the_real_package_list_excerpt_gives_where_the_file_is_and_how_to_check_it():
    found = di.packages(PACKAGES, {"abiword", "gitg", "starfighter", "lxi-tools", "nothing-like-it"})
    assert sorted(found) == ["gitg", "lxi-tools", "abiword", "starfighter"] or sorted(found) == sorted(["abiword", "gitg", "lxi-tools", "starfighter"])
    abiword = found["abiword"]
    assert abiword["version"] == "3.0.6~dfsg-1" and abiword["filename"] == "pool/main/a/abiword/abiword_3.0.6~dfsg-1_amd64.deb"
    assert abiword["size"] == 946980 and abiword["sha256"] == "ee1a7508971aa5de9a8548866915a5f421e8ce9b1c31dc2fcf2ccaaba4900983"
    assert abiword["depends"][:3] == ["abiword-common", "fonts-urw-base35", "libabiword-3.0"] and "gsfonts" not in abiword["depends"]  # the first of "a | b"
    assert "libc6" in abiword["depends"] and not any("(" in d or " " in d for d in abiword["depends"])  # no version conditions


def test_the_two_real_files_join_into_catalogue_records():
    recs = di.records(di.apps(DEP11), di.packages(PACKAGES, set(di.apps(DEP11))))
    assert [r["package"] for r in recs] == ["abiword", "gitg", "lxi-tools", "starfighter"]
    one = recs[0]
    assert set(one) == {"package", "id", "ids", "name", "summary", "categories", "url", "version", "filename", "sha256", "size", "depends"}
    assert di.clean(recs) == recs  # what is made passes the check that reads it back


# -- reading the YAML ---------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("first, more, text", [
    ("plain text", [], "plain text"),
    ("  padded  ", [], "padded"),
    ("'single ''quoted'' text'", [], "single 'quoted' text"),
    ('"double \\"quoted\\" \\\\ text"', [], 'double "quoted" \\ text'),
    ('"line one\\nline two"', [], "line one line two"),
    ('"accent \\u00e9 and \\U0001F600"', [], "accent é and \U0001F600"),
    ("'folded", ["  over two", "  lines'"], "folded over two lines"),
    ('"folded', ["  over two", '  lines"'], "folded over two lines"),
    ("plain", ["  goes on"], "plain goes on"),
    (">-", ["  block one", "  block two"], "block one block two"),
    ("|", ["  literal"], "literal"),
    ('"bad \\u12 escape"', [], "bad 12 escape"),  # an escape that is not one is dropped and the text goes on
    ('"bad \\uZZZZ"', [], "bad ZZZZ"),
    ('"huge \\UFFFFFFFF end"', [], "huge FFFFFFFF end"),
    ("", [], ""),
    ('"unterminated', [], "unterminated"),
    ("'", [], ""),
])
def test_a_yaml_scalar_is_read_as_text(first, more, text):
    assert di._scalar(first, more) == text


def test_an_application_needs_a_valid_package_id_and_name():
    ok = di.apps(app_doc())
    assert list(ok) == ["demo"]
    assert di.apps(app_doc(package="Bad Package")) == {} and di.apps(app_doc(package="a")) == {} and di.apps(app_doc(package="../x")) == {}
    assert di.apps(app_doc(id_="bad id")) == {} and di.apps(app_doc(id_="a/b")) == {} and di.apps(app_doc(id_="")) == {}
    assert di.apps(app_doc(name="")) == {} and di.apps(app_doc(name="   ")) == {}
    assert di.apps(doc("Type: desktop-application", "ID: x.desktop", "Name:", "  C: X")) == {}  # which package is it in?
    assert di.apps(app_doc(type_="console-application")) == {} and di.apps(app_doc(type_="addon")) == {} and di.apps(app_doc(type_="generic")) == {}


def test_only_the_default_language_is_read_and_the_other_ones_never_win():
    lines = doc("Type: desktop-application", "ID: d.desktop", "Package: demo", "Name:", "  de: Demo auf Deutsch", "  C: Demo", "  fr: Demo en francais",
                "Summary:", "  'no': Norsk", "  C: Does demos", "  nl: Doet demos")
    [app] = di.apps(lines).values()
    assert app["name"] == "Demo" and app["summary"] == "Does demos"
    assert di.apps(doc("Type: desktop-application", "ID: d.desktop", "Package: demo", "Name:", "  de: Nur Deutsch")) == {}  # no default name: not listed


def test_a_homepage_is_kept_only_when_it_is_an_https_address_and_the_other_links_never():
    def url(text):
        return list(di.apps(app_doc(extra=["Url:", f"  homepage: {text}", "  bugtracker: https://bugs.example/x"])).values())[0]["url"]

    assert url("https://demo.example/page?x=1") == "https://demo.example/page?x=1"
    for bad in ("http://demo.example", "javascript:alert(1)", "https://bad host/x", "ftp://x", "https://" + "a" * 400 + ".example", "//demo.example"):
        assert url(bad) == ""
    only_bugs = list(di.apps(app_doc(extra=["Url:", "  bugtracker: https://bugs.example/x"])).values())[0]
    assert only_bugs["url"] == ""


def test_categories_are_plain_words_and_not_too_many():
    extra = ["Categories:", "- Office", "- ../x", "- a b", "- Net-work", "- 9bad", "- " + "x" * 40] + [f"- C{i}" for i in range(30)]
    [app] = di.apps(app_doc(extra=extra)).values()
    assert app["categories"][:2] == ["Office", "Net-work"] and len(app["categories"]) == 12


def test_hostile_text_in_a_name_or_summary_is_made_plain_and_cut():
    [app] = di.apps(app_doc(name='"N\\u202eame \\u001b[31m' + "x" * 300 + '"', summary='"' + "s\\n" * 300 + '"')).values()
    assert all(c.isprintable() for c in app["name"] + app["summary"]) and len(app["name"]) <= di.MAX_NAME and len(app["summary"]) <= di.MAX_SUMMARY


def test_a_huge_file_is_cut_off(monkeypatch):
    monkeypatch.setattr(di, "MAX_APPS", 3)
    many = sum((app_doc(package=f"p{i:02}", id_=f"p{i:02}.desktop", name=f"App {i}") for i in range(10)), [])
    assert len(di.apps(many)) == 3


def test_stray_lines_comments_and_blank_lines_do_not_stop_the_reading():
    lines = ["# a comment", "", *app_doc(extra=["", "# another", "Unknown:", "  nested: x", "- stray"]), "", "---"]
    assert list(di.apps(lines)) == ["demo"]


# -- reading the package list -----------------------------------------------------------------------------------------------------------------------
def stanza(**over):
    fields = {"Package": "demo", "Version": "1.0-1", "Architecture": "amd64", "Depends": "libc6 (>= 2.34), demo-data", "Filename": "pool/main/d/demo/demo_1.0-1_amd64.deb",
              "Size": "1234", "SHA256": SHA}
    fields.update(over)
    return [f"{k}: {v}" for k, v in fields.items() if v is not None] + [""]


def one(**over):
    return di.packages(stanza(**over), {"demo"}).get("demo")


def test_a_package_entry_is_read_with_its_file_size_checksum_and_needs():
    assert one() == {"version": "1.0-1", "filename": "pool/main/d/demo/demo_1.0-1_amd64.deb", "sha256": SHA, "size": 1234, "depends": ["libc6", "demo-data"]}
    assert one(Architecture="all", Filename="pool/main/d/demo/demo_1.0-1_all.deb")["size"] == 1234


@pytest.mark.parametrize("over", [
    {"Architecture": "arm64"}, {"Architecture": "i386"}, {"Architecture": None},
    {"Filename": "pool/main/../../x/demo_1.0-1_amd64.deb"}, {"Filename": "pool/contrib/d/demo/demo_1.0-1_amd64.deb"}, {"Filename": "/pool/main/d/demo/demo_1.0-1_amd64.deb"},
    {"Filename": "pool/main/d/demo/demo_1.0-1_amd64.deb\x00x"}, {"Filename": "pool/main/d/demo/demo_1.0-1_arm64.deb"}, {"Filename": "pool/main/d/demo/demo_1.0-1_amd64.deb.sig"},
    {"Filename": "https://evil.example/demo_1.0-1_amd64.deb"}, {"Filename": "pool/main/d/demo/demo 1_amd64.deb"}, {"Filename": "pool/main/d//demo_1.0-1_amd64.deb"},
    {"Filename": None}, {"SHA256": None}, {"SHA256": "AB" * 32}, {"SHA256": "ab" * 31}, {"SHA256": "zz" * 32},
    {"Size": None}, {"Size": "0"}, {"Size": "-5"}, {"Size": "12abc"}, {"Size": str(di.MAX_FILE + 1)}, {"Size": "1e3"}, {"Size": " "},
    {"Version": None}, {"Version": "bad version"}, {"Version": "1.0/../x"}, {"Version": "a" * 100},
])
def test_a_package_entry_that_is_not_a_clean_file_for_this_processor_is_left_out(over):
    assert one(**over) is None


def test_only_the_wanted_packages_are_kept_and_the_first_entry_of_a_name_wins():
    lines = stanza(Package="other") + stanza(Version="1.0-1") + stanza(Version="2.0-1")
    found = di.packages(lines, {"demo"})
    assert list(found) == ["demo"] and found["demo"]["version"] == "1.0-1"
    assert di.packages(lines, set()) == {}


def test_a_package_that_is_last_in_the_list_without_a_blank_line_after_it_is_kept():
    assert "demo" in di.packages(stanza()[:-1], {"demo"})


def test_what_a_package_needs_is_a_list_of_names_without_versions_alternatives_or_junk():
    names = di._dependency_names("libc6 (>= 2.34), aa | bb (>= 1), python3:any, Bad Name, ../x, , lib+x, cc (<< 2) | dd, x", "pre-dep")
    assert names == ["libc6", "aa", "python3:any", "lib+x", "cc", "pre-dep"]  # a name of one letter is not a package name here either
    assert di._dependency_names("libc6, libc6") == ["libc6"]
    assert di._dependency_names("aa|bb, cc |dd, ee| ff") == ["aa", "cc", "ee"]  # the first of a choice, whatever the spaces around the bar
    assert len(di._dependency_names(", ".join(f"p{i:03}" for i in range(100)))) == di.MAX_DEPENDS
    assert di._dependency_names("") == []


def test_a_multi_line_field_is_not_mistaken_for_a_field_and_a_colon_inside_a_value_stays_in_it():
    lines = stanza(Description="x") + [" Version: 9.9", "\tSize: 1"]
    assert di.packages(stanza() + [" Version: 9.9"], {"demo"})["demo"]["version"] == "1.0-1"
    lines = ["Package: demo", "Version: 1:2.0-1", "Architecture: amd64", "Filename: pool/main/d/demo/demo_2.0-1_amd64.deb", "Size: 5", f"SHA256: {SHA}", ""]
    assert di.packages(lines, {"demo"})["demo"]["version"] == "1:2.0-1"


def test_the_aur_names_are_one_per_line_without_the_comment():
    assert di.aur_names(["# AUR package list", "alpha", "beta", "", "  gamma  "]) == {"alpha", "beta", "gamma"}


def test_a_program_the_aur_has_is_not_listed_and_one_without_a_file_is_not_either():
    apps = di.apps(app_doc("one", "one.desktop", "One") + app_doc("two", "two.desktop", "Two") + app_doc("three", "three.desktop", "Three"))
    pkgs = di.packages(stanza(Package="one", Filename="pool/main/o/one/one_1_amd64.deb") + stanza(Package="two", Filename="pool/main/t/two/two_1_amd64.deb"), set(apps))
    assert [r["package"] for r in di.records(apps, pkgs)] == ["one", "two"]  # "three" has no file in the list
    assert [r["package"] for r in di.records(apps, pkgs, {"two"})] == ["one"]


# -- reading back from the cache ---------------------------------------------------------------------------------------------------------------
def good_record(**over):
    return {"package": "demo", "id": "demo", "name": "Demo", "summary": "s", "categories": ["Office"], "url": "https://demo.example", "version": "1.0-1",
            "filename": "pool/main/d/demo/demo_1.0-1_amd64.deb", "sha256": SHA, "size": 10, "depends": ["libc6"], **over}


@pytest.mark.parametrize("over", [{"package": "Bad"}, {"package": 5}, {"id": "bad id"}, {"name": " "}, {"filename": "pool/main/../x_amd64.deb"}, {"sha256": "x"},
                                  {"version": "bad version"}, {"size": 0}, {"size": True}, {"size": "10"}, {"size": di.MAX_FILE + 1}, {"summary": None}, {"url": 5}])
def test_a_cached_record_that_is_not_clean_is_dropped(over):
    assert di.clean([good_record(**over)]) == []


def test_cached_records_are_cut_again_deduplicated_and_junk_lists_are_nothing():
    rec = good_record(name="N\x1b[0m" * 100, summary="s" * 1000, url="http://plain.example", categories=["ok", "../x", 5], depends=["libc6", "Bad Name", 5, "x" * 500])
    out = di.clean([rec, good_record(), "x", None, 5])
    assert [r["package"] for r in out] == ["demo"] and len(out[0]["name"]) <= di.MAX_NAME and len(out[0]["summary"]) <= di.MAX_SUMMARY
    assert out[0]["url"] == "" and out[0]["categories"] == ["ok"] and out[0]["depends"] == ["libc6"]
    assert di.clean(None) == [] and di.clean({"a": 1}) == [] and di.clean("x") == []


# -- decompressing with a limit ----------------------------------------------------------------------------------------------------------------------
def test_compressed_text_is_read_line_by_line_whatever_the_compression():
    text = "one\ntwo\r\nthree"
    assert list(di.text_lines(lzma.compress(text.encode()), "xz")) == ["one", "two", "three"]
    assert list(di.text_lines(gzip.compress(text.encode()), "gz")) == ["one", "two", "three"]
    assert list(di.text_lines(lzma.compress(b"bad \xff byte\n"), "xz")) == ["bad � byte"]


def test_a_small_download_that_would_become_an_enormous_text_is_refused():
    bomb = lzma.compress(b"x" * 10_000 + b"\n")
    with pytest.raises(CygnusError, match="larger than Cygnus will read"):
        list(di.text_lines(bomb, "xz", limit=5000))
    with pytest.raises(CygnusError, match="larger than Cygnus will read"):
        list(di.text_lines(gzip.compress(b"x\n" * 5000), "gz", limit=100))


@pytest.mark.parametrize("data, kind", [(b"not compressed at all", "xz"), (b"not compressed at all", "gz"), (b"", "xz"), (lzma.compress(b"abc" * 1000)[:20], "xz"),
                                        (gzip.compress(b"abc" * 1000)[:20], "gz")])
def test_damaged_data_is_an_error_in_words_not_a_crash(data, kind):
    with pytest.raises(CygnusError, match="could not be unpacked"):
        list(di.text_lines(data, kind))


# -- fetching -------------------------------------------------------------------------------------------------------------------------------------
def test_the_three_lists_are_fetched_from_their_hosts_with_limits_joined_and_the_aur_ones_left_out():
    asked = []
    files = {di.APPSTREAM: lzma.compress("\n".join(app_doc("one", "one.desktop", "One") + app_doc("two", "two.desktop", "Two")).encode()),
             di.PACKAGES: lzma.compress("\n".join(stanza(Package="one", Filename="pool/main/o/one/one_1_amd64.deb") + stanza(Package="two", Filename="pool/main/t/two/two_1_amd64.deb")).encode()),
             di.AUR_NAMES: gzip.compress(b"# AUR\ntwo\nother\n")}

    def get(url, **kw):
        asked.append((url, kw))
        return files[url]

    lines = []
    out = di.fetch(lines.append, get=get)
    assert [r["package"] for r in out] == ["one"] and len(lines) == 3
    assert [u for u, _ in asked] == [di.APPSTREAM, di.PACKAGES, di.AUR_NAMES]
    assert all(kw["limit"] == di.MAX_COMPRESSED for _u, kw in asked)
    deb, aur = asked[0][1]["host_ok"], asked[2][1]["host_ok"]
    assert deb("deb.debian.org") and not deb("aur.archlinux.org") and not deb("evil.example") and not deb("x.deb.debian.org") and not deb("debian.org")
    assert aur("aur.archlinux.org") and not aur("deb.debian.org") and not aur("archlinux.org")
    assert asked[1][1]["host_ok"] is deb


def test_the_addresses_are_https_on_the_debian_mirror_and_a_package_address_is_built_from_a_checked_name_only():
    assert di.APPSTREAM.startswith("https://deb.debian.org/debian/dists/stable/main/dep11/") and di.PACKAGES.startswith("https://deb.debian.org/debian/dists/stable/main/binary-amd64/")
    assert di.file_url("pool/main/d/demo/demo_1.0-1_amd64.deb") == "https://deb.debian.org/debian/pool/main/d/demo/demo_1.0-1_amd64.deb"
    assert di.AUR_NAMES == "https://aur.archlinux.org/packages.gz"


def test_a_download_that_is_not_what_it_should_be_is_an_error_in_words():
    with pytest.raises(CygnusError, match="could not be unpacked"):
        di.fetch(get=lambda url, **kw: b"<html>an error page</html>")


def test_a_package_that_holds_several_applications_is_one_entry_named_after_the_package_with_the_programs_listed():
    two = app_doc("multi", "a.desktop", "First App", extra=["Categories:", "- Office"]) + app_doc("multi", "b.desktop", "Second App") + app_doc("multi", "c.desktop", "First App")
    [app] = di.apps(two).values()
    assert app["name"] == "multi" and app["summary"] == "Installs 2 programs: First App, Second App"  # a name twice is one program
    assert app["id"] == "a" and app["categories"] == ["Office"] and app["package"] == "multi"
    many = sum((app_doc("multi", f"p{i}.desktop", f"Program {i}") for i in range(7)), [])
    assert di.apps(many)["multi"]["summary"] == "Installs 7 programs: Program 0, Program 1, Program 2, Program 3, …"
    assert di.apps(app_doc("solo", "s.desktop", "Solo One"))["solo"]["name"] == "Solo One"  # a package with one application keeps its name


def test_a_line_with_no_end_is_refused_at_a_limit_and_a_long_line_is_not_read_slowly():
    import time

    long = lzma.compress(b"x" * (3 * 1024 * 1024) + b"\nnext\n")
    started = time.monotonic()
    with pytest.raises(CygnusError, match="line far longer than expected"):
        list(di.text_lines(long, "xz"))
    assert time.monotonic() - started < 2
    just_under = lzma.compress(b"y" * (1024 * 1024 - 10) + b"\nnext")
    lines = list(di.text_lines(just_under, "xz"))
    assert len(lines) == 2 and lines[1] == "next"


def test_a_document_that_never_ends_keeps_only_a_bounded_number_of_lines(monkeypatch):
    monkeypatch.setattr(di, "MAX_DOC_LINES", 50)
    kept = []
    for doc in di._documents(["Type: desktop-application"] + [f"Key{i}: v" for i in range(10_000)] + ["---", "Next: doc"]):
        kept.append(len(doc))
    assert kept == [50, 1]  # what comes after the limit is read past, and the next document is read normally


def test_a_package_with_a_thousand_applications_is_named_quickly_and_lists_a_few():
    import time

    many = sum((app_doc("big", f"p{i}.desktop", f"Program {i}") for i in range(3000)), [])
    started = time.monotonic()
    out = di.apps(many)["big"]
    assert time.monotonic() - started < 2
    assert out["summary"].startswith("Installs 200 programs: Program 0, Program 1") and out["name"] == "big" and out["ids"] == [f"p{i}" for i in range(8)]


def test_a_long_dependency_list_is_cut_early_and_quickly():
    import time

    started = time.monotonic()
    assert len(di._dependency_names(", ".join(f"pkg{i:06}" for i in range(200_000)))) == di.MAX_DEPENDS
    assert time.monotonic() - started < 1


def test_a_new_package_line_without_the_blank_line_before_it_starts_a_new_entry_and_takes_nothing_from_the_last():
    lines = ["Package: aa", "Version: 1.0-1", "Architecture: amd64", "Filename: pool/main/a/aa/aa_1.0-1_amd64.deb", "Size: 10", f"SHA256: {SHA}",
             "Package: bb", "Version: 2.0-1", "Architecture: amd64", ""]
    found = di.packages(lines, {"aa", "bb"})
    assert list(found) == ["aa"]  # bb has no file of its own, and aa's is not given to it


def test_a_package_of_several_applications_lists_every_id_and_the_first_one_stays_the_id():
    two = app_doc("multi", "a.desktop", "First App") + app_doc("multi", "b.desktop", "Second App") + app_doc("multi", "a.desktop", "First App")
    [app] = di.apps(two).values()
    assert app["id"] == "a" and app["ids"] == ["a", "b"]


@pytest.mark.parametrize("indent", [1, 2, 3, 4, 8])
def test_the_names_summaries_and_categories_are_read_whatever_the_indent_of_the_file_is(indent):
    pad = " " * indent
    lines = doc("Type: desktop-application", "ID: d.desktop", "Package: demo", "Name:", f"{pad}C: Demo", f"{pad}de: Demo auf Deutsch", "Summary:", f"{pad}C: Does demos",
                f"{pad}{pad}goes on", "Categories:", f"{pad}- Office", "- Network", "Url:", f"{pad}homepage: https://demo.example")
    [app] = di.apps(lines).values()
    assert app["name"] == "Demo" and app["summary"] == "Does demos goes on" and app["categories"] == ["Office", "Network"] and app["url"] == "https://demo.example"
