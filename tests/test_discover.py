"""Discover's data layer: AppStream files read as a stream (hostile input refused, every field capped), an index cached by the file's
identity, sources merged only by the exact id and the hand-checked vendor map, plain searching and paging. No test uses the machine's real
Flathub data or libflatpak; every source is a fixture."""

import gzip
import io
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cygnus.core import discover as d
from cygnus.core import paths


def component(id_="org.example.App", name="Example", *, kind=None, summary="Does examples", developer=None, pkgname=None, icon=None,
              url=None, categories=(), keywords=(), description=None, extra=""):
    attribute = f' type="{kind}"' if kind else ""
    parts = [f"<component{attribute}>", f"<id>{id_}</id>", f"<name>{name}</name>"]
    if summary is not None:
        parts.append(f"<summary>{summary}</summary>")
    if developer:
        parts.append(f"<developer_name>{developer}</developer_name>")
    if pkgname:
        parts.append(f"<pkgname>{pkgname}</pkgname>")
    for height, filename in (icon or {}).items():
        parts.append(f'<icon type="cached" width="{height}" height="{height}">{filename}</icon>')
    if url:
        parts.append(f'<url type="homepage">{url}</url>')
    if categories:
        parts.append("<categories>" + "".join(f"<category>{c}</category>" for c in categories) + "</categories>")
    if keywords:
        parts.append("<keywords>" + "".join(f"<keyword>{k}</keyword>" for k in keywords) + "</keywords>")
    if description is not None:
        parts.append(f"<description>{description}</description>")
    parts.append(extra + "</component>")
    return "".join(parts)


def document(*components, origin="test-origin"):
    return f'<?xml version="1.0" encoding="UTF-8"?><components version="0.16" origin="{origin}">' + "".join(components) + "</components>"


def read(text, encoding="utf-8"):
    return d.parse(io.BytesIO(text.encode(encoding) if isinstance(text, str) else text))


# -- reading one file ----------------------------------------------------------------------------------------------------------------
def test_an_application_is_read_with_everything_the_page_shows():
    apps, origin = read(document(component(developer="The Devs", pkgname="example-app", icon={"128": "big.png", "64": "small.png"},
                                           url="https://example.org/app", categories=("Network", "WebBrowser"), keywords=("web", "internet"),
                                           description="<p>First paragraph.</p><ul><li>one</li><li>two</li></ul>")))
    assert origin == "test-origin" and len(apps) == 1
    app = apps[0]
    assert (app["id"], app["name"], app["summary"], app["developer"], app["pkgname"]) == ("org.example.App", "Example", "Does examples", "The Devs", "example-app")
    assert app["icon"] == "small.png"  # 64 pixels is the size the list shows
    assert app["url"] == "https://example.org/app" and app["categories"] == ["Network", "WebBrowser"] and app["keywords"] == ["web", "internet"]
    assert "First paragraph." in app["description"] and "• one" in app["description"] and "<" not in app["description"]


def test_the_default_description_is_the_one_shown_whatever_order_the_translations_come_in():
    """Found on the real Flathub data: 933 of the 959 applications with several descriptions showed a translation (the last one read)."""
    def parsed(*descriptions):
        return read(document(component(description=None, extra="".join(descriptions))))[0][0]["description"]

    english = '<description><p>English text.</p></description>'
    french = '<description xml:lang="fr"><p>Texte francais.</p></description>'
    assert parsed(english, french) == "English text." and parsed(french, english) == "English text."
    assert parsed(french, '<description xml:lang="de"><p>Deutsch.</p></description>', english, french) == "English text."
    assert parsed('<description><p>First default.</p></description>', '<description><p>Second default.</p></description>') == "First default."
    assert parsed(french) == ""  # only translations: nothing is better than another language


def test_translated_keywords_are_not_mixed_into_the_keywords():
    xml = ('<components><component><id>a.B</id><name>B</name><keywords><keyword>editor</keyword></keywords>'
           '<keywords xml:lang="de"><keyword>bearbeiter</keyword></keywords><keywords><keyword>text</keyword></keywords></component></components>')
    assert read(xml)[0][0]["keywords"] == ["editor", "text"]


def test_the_first_well_formed_id_is_the_id_and_a_colon_cannot_be_part_of_one():
    assert read('<components><component><id>vendor:code</id><name>X</name></component></components>')[0] == []
    assert read('<components><component><id>a b</id><name>X</name></component></components>')[0] == []
    assert read('<components><component><id>a.B</id><id>c.D</id><name>X</name></component></components>')[0][0]["id"] == "a.B"


def test_a_component_with_a_colon_in_its_id_can_not_take_the_place_of_a_vendor_program():
    entries = d.build_entries([_src("flatpak", "f")], [_vendor("code", name="Visual Studio Code")],
                              loader=_loader(f={"apps": [d._Reader._finish({"id": "x.Y", "name": "Real", "summary": "", "developer": "", "pkgname": "", "icons": {},
                                                                              "url": "", "categories": [], "keywords": [], "description": ""})]}))
    assert sorted(e["key"] for e in entries) == ["vendor:code", "x.Y"]


def test_elements_nested_far_deeper_than_a_real_file_are_refused(monkeypatch):
    deep = "<components><component type='addon'>" + "<x>" * 40 + "</x>" * 40 + "</component></components>"
    with pytest.raises(d.DiscoverError, match="nested far more deeply"):
        read(deep)
    assert read("<components><component type='addon'>" + "<x>" * 20 + "</x>" * 20 + "</component></components>")[0] == []


def test_components_of_every_kind_count_not_only_the_applications(monkeypatch):
    monkeypatch.setattr(d, "MAX_ALL_COMPONENTS", 5)
    with pytest.raises(d.DiscoverError, match="more than 5 components"):
        read(document(*(component(f"a.A{i}", f"A{i}", kind="addon") for i in range(6))))


def test_a_flood_of_character_references_after_the_description_is_full_stays_fast():
    """The cost per piece of text used to grow with everything kept so far (8 seconds for 25,000 references)."""
    import time

    flood = "&#65;" * 60_000
    started = time.monotonic()
    app = read(document(component(description=f"<p>{flood}</p>")))[0][0]
    assert time.monotonic() - started < 4 and len(app["description"]) <= d.MAX_DESCRIPTION + 5
    blocks = read(document(component(description="<p>x</p>" + "<p/>" * 100_000)))[0][0]
    assert len(blocks["description"]) <= d.MAX_DESCRIPTION + 5


def test_a_gzip_with_a_valid_header_and_garbage_inside_is_a_plain_error(tmp_path):
    bad = tmp_path / "bad.xml.gz"
    good = gzip.compress(b"<components>" + b"<component><id>a.B</id><name>B</name></component>" * 2000 + b"</components>")
    bad.write_bytes(good[:20] + bytes(b ^ 0xFF for b in good[20:60]) + good[60:])
    with pytest.raises(d.DiscoverError, match="not readable"):
        with d._open(bad) as stream:
            d.parse(stream)


def test_a_translated_variant_never_replaces_the_default_text():
    xml = ('<components origin="o"><component><id>a.b.C</id><name xml:lang="de">Deutsch</name><name>English</name>'
           '<summary xml:lang="fr">Français</summary><summary>Plain</summary></component></components>')
    app = read(xml)[0][0]
    assert (app["name"], app["summary"]) == ("English", "Plain")


def test_only_applications_are_kept_not_add_ons_runtimes_or_fonts():
    apps, _ = read(document(component("a.A", "A"), component("a.R", "R", kind="runtime"), component("a.F", "F", kind="font"),
                            component("a.C", "C", kind="console-application"), component("a.D", "D", kind="desktop"), component("a.X", "X", kind="addon")))
    assert [a["id"] for a in apps] == ["a.A", "a.C", "a.D"]


def test_a_component_without_an_id_or_a_name_is_left_out():
    apps, _ = read(document(component("", "Nameless id"), component("a.B", ""), component("a.C", "Fine")))
    assert [a["id"] for a in apps] == ["a.C"]


def test_the_developer_name_inside_a_developer_element_is_read_too():
    xml = '<components><component><id>a.B</id><name>B</name><developer id="x"><name>Dev Inc</name></developer></component></components>'
    assert read(xml)[0][0]["developer"] == "Dev Inc"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_a_document_type_declaration_is_refused_in_every_encoding(encoding):
    xml = '<!DOCTYPE components [<!ENTITY x "boom">]><components><component><id>a.B</id><name>&x;</name></component></components>'
    data = xml.encode(encoding)  # "utf-16" carries its own byte-order mark; the others are recognised from the first characters
    with pytest.raises(d.DiscoverError, match="document type or entity declaration"):
        d.parse(io.BytesIO(data))


def test_a_bare_doctype_and_a_billion_laughs_document_are_refused():
    for xml in ('<!DOCTYPE components><components/>',
                '<!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;">]><components><component><id>x.Y</id><name>&c;</name></component></components>'):
        with pytest.raises(d.DiscoverError):
            read(xml)


@pytest.mark.parametrize("bad", ["", "not xml at all", "<components><component></components>", "<?xml version='1.0' encoding='no-such-codec'?><components/>",
                                 "<components>" + "\x00" + "</components>"])
def test_a_document_that_cannot_be_read_is_a_plain_error_not_a_crash(bad):
    with pytest.raises(d.DiscoverError, match="not readable"):
        read(bad)


def test_a_stream_that_fails_while_being_read_is_a_plain_error():
    class Broken:
        def read(self, _n):
            raise OSError("disk went away")

    with pytest.raises(d.DiscoverError, match="not readable"):
        d.parse(Broken())


def test_too_many_applications_and_too_many_bytes_stop_the_reading(monkeypatch):
    monkeypatch.setattr(d, "MAX_COMPONENTS", 3)
    with pytest.raises(d.DiscoverError, match="more than 3 applications"):
        read(document(*(component(f"a.A{i}", f"A{i}") for i in range(4))))
    assert len(read(document(*(component(f"a.A{i}", f"A{i}") for i in range(3))))[0]) == 3
    monkeypatch.setattr(d, "MAX_COMPONENTS", 60_000)
    monkeypatch.setattr(d, "MAX_BYTES", 100)
    with pytest.raises(d.DiscoverError, match="far larger"):
        read(document(component(description="x" * 500)))


def test_every_field_is_cut_to_its_limit():
    long = "w" * 5000
    assert read(document(component(id_="a." + "i" * 500)))[0] == []  # an id that long is not an id: the component is left out, not cut into another one
    app = read(document(component(id_="a." + "i" * 190, name=long, summary=long, developer=long, url="https://example.org/" + "p" * 400,
                                  categories=[long] * 60, keywords=[long] * 60, description=long)))[0][0]
    assert len(app["id"]) <= d.MAX_ID and len(app["name"]) <= d.MAX_NAME and len(app["summary"]) <= d.MAX_SUMMARY
    assert len(app["developer"]) <= d.MAX_DEVELOPER and app["url"] == ""  # a long address is dropped, not cut into another one
    assert len(app["categories"]) == d.MAX_CATEGORIES and all(len(c) <= 40 for c in app["categories"])
    assert len(app["keywords"]) == d.MAX_KEYWORDS and all(len(k) <= d.MAX_KEYWORD for k in app["keywords"])
    assert len(app["description"]) <= d.MAX_DESCRIPTION + 5


def test_control_characters_and_markup_in_text_never_reach_the_page():
    app = read(document(component(name="Ev‮il​", summary="line\nbreak\ttab", description="<p>one</p><script>x</script> &amp; two")))[0][0]
    assert "‮" not in app["name"] and "​" not in app["name"] and "\n" not in app["summary"] and "\t" not in app["summary"]
    assert "<" not in app["description"] and "&amp;" not in app["description"]


@pytest.mark.parametrize("url,kept", [("https://example.org/a?b=c#d", True), ("http://example.org/", False), ("javascript:alert(1)", False),
                                      ("https://exa mple.org/", False), ("ftp://example.org/", False),
                                      ("https://user:pw@example.org/", False), ("https://example.org:99999999/", False)])
def test_only_an_https_address_is_kept_as_the_website(url, kept):
    assert read(document(component(url=url)))[0][0]["url"] == (url if kept else "")


@pytest.mark.parametrize("pkg,kept", [("firefox", True), ("lib32-foo.bar+baz", True), ("-rf", False), ("Foo", False), ("a b", False), ("a;b", False), ("", False)])
def test_a_package_name_must_look_like_one(pkg, kept):
    assert read(document(component(pkgname=pkg)))[0][0]["pkgname"] == (pkg if kept else "")


@pytest.mark.parametrize("icon,kept", [("firefox.png", True), ("a.b-c_d+e.png", True), ("x.jxl", True), ("x.webp", True), ("../x.png", False),
                                       ("a/b.png", False), (".hidden.png", False), ("x.svg", False), ("x.png/", False), ("x" * 200 + ".png", False),
                                       ("", False), ("x.PNG", False)])
def test_an_icon_is_only_ever_a_bare_file_name_of_a_known_kind(icon, kept):
    assert read(document(component(icon={"64": icon})))[0][0]["icon"] == (icon if kept else "")


def test_the_icon_size_preference_is_64_then_128_and_the_size_travels_with_the_name():
    both = read(document(component(icon={"128": "b.png", "64": "s.png"})))[0][0]
    assert (both["icon"], both["icon_size"]) == ("s.png", "64")
    big = read(document(component(icon={"256": "x.png", "128": "b.png"})))[0][0]
    assert (big["icon"], big["icon_size"]) == ("b.png", "128")
    odd = read(document(component(icon={"256": "x.png"})))[0][0]  # only the two sizes Discover looks for are used
    assert (odd["icon"], odd["icon_size"]) == ("", "")


# -- opening a file --------------------------------------------------------------------------------------------------------------------
def test_a_plain_and_a_compressed_file_are_both_read(tmp_path):
    xml = document(component())
    (tmp_path / "a.xml").write_text(xml)
    (tmp_path / "a.xml.gz").write_bytes(gzip.compress(xml.encode()))
    for name in ("a.xml", "a.xml.gz"):
        with d._open(tmp_path / name) as stream:
            assert len(d.parse(stream)[0]) == 1


def test_a_link_or_a_folder_or_a_pipe_is_never_opened(tmp_path):
    (tmp_path / "real.xml").write_text(document(component()))
    (tmp_path / "link.xml").symlink_to(tmp_path / "real.xml")
    (tmp_path / "dir.xml").mkdir()
    os.mkfifo(tmp_path / "pipe.xml")
    for name in ("link.xml", "dir.xml", "pipe.xml"):
        with pytest.raises((d.DiscoverError, OSError)):
            d._open(tmp_path / name)


# -- where the data is ---------------------------------------------------------------------------------------------------------------
class _Remote:
    def __init__(self, name, folder, disabled=False, broken=False):
        self.name, self.folder, self.disabled, self.broken = name, folder, disabled, broken

    def get_name(self):
        return self.name

    def get_disabled(self):
        return self.disabled

    def get_appstream_dir(self, arch):
        if self.broken:
            raise RuntimeError("no such data")
        return None if self.folder is None else NS(get_path=lambda: str(self.folder))


def _env(*remotes):
    return NS(Flatpak=NS(get_default_arch=lambda: "x86_64"), remotes=lambda: [("system", r) for r in remotes])


def test_each_enabled_remote_is_found_once_and_the_compressed_file_is_preferred(tmp_path):
    for name in ("one", "two", "three"):
        (tmp_path / name).mkdir()
    (tmp_path / "one" / "appstream.xml").write_text("<components/>")
    (tmp_path / "one" / "appstream.xml.gz").write_bytes(gzip.compress(b"<components/>"))
    (tmp_path / "two" / "appstream.xml").write_text("<components/>")
    (tmp_path / "three" / "appstream.xml").write_text("<components/>")
    sources = d.flatpak_sources(_env(_Remote("one", tmp_path / "one"), _Remote("two", tmp_path / "two"), _Remote("off", tmp_path / "three", disabled=True),
                                     _Remote("one", tmp_path / "two"), _Remote("empty", None), _Remote("broken", None, broken=True),
                                     _Remote("nodata", tmp_path / "nowhere")))
    assert [(s.label, s.xml.name, s.kind) for s in sources] == [("one", "appstream.xml.gz", "flatpak"), ("two", "appstream.xml", "flatpak")]
    assert sources[0].icons == tmp_path / "one" / "icons" and sources[0].remote == "one"


def test_a_data_file_that_is_a_link_is_not_listed(tmp_path):
    (tmp_path / "real.xml").write_text("<components/>")
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "appstream.xml").symlink_to(tmp_path / "real.xml")
    assert d.flatpak_sources(_env(_Remote("r", tmp_path / "r"))) == []


def test_without_libflatpak_nothing_is_listed_from_it():
    class Nope:
        Flatpak = NS(get_default_arch=lambda: (_ for _ in ()).throw(RuntimeError("no libflatpak")))

        def remotes(self):
            return []

    assert d.flatpak_sources(Nope()) == []


def test_the_repositories_data_is_found_only_where_the_package_puts_it(tmp_path):
    base = tmp_path / "swcatalog"
    (base / "xml").mkdir(parents=True)
    (base / "xmls").mkdir()
    (base / "xml" / "extra.xml.gz").write_bytes(gzip.compress(b"<components/>"))
    (base / "xml" / "core.xml").write_text("<components/>")
    (base / "xml" / "notes.txt").write_text("x")
    (base / "xml" / "linked.xml").symlink_to(base / "xml" / "core.xml")
    (base / "xml" / "folder.xml").mkdir()
    (base / "xmls" / "old.xml").write_text("<components/>")
    sources = d.repo_sources((str(base), str(tmp_path / "missing")))
    assert sorted(s.xml.name for s in sources) == ["core.xml", "extra.xml.gz", "old.xml"]
    assert all(s.kind == "repo" and s.icons == base / "icons" and s.remote == "" for s in sources)
    assert {s.label for s in sources} == {"core", "extra", "old"}


# -- the cached index ------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def source(tmp_path):
    d._MEMORY.clear()
    path = tmp_path / "data" / "appstream.xml"
    path.parent.mkdir()
    path.write_text(document(component("a.One", "One"), component("a.Two", "Two")))
    return d.Source("flatpak", "remote", path, path.parent / "icons", "remote")


def _cache_files():
    folder = paths.cache_dir() / "discover"
    return sorted(folder.glob("*.json")) if folder.exists() else []


def test_the_index_is_built_once_saved_privately_and_then_served_from_memory(source, monkeypatch):
    index = d.load_index(source)
    assert [a["id"] for a in index["apps"]] == ["a.One", "a.Two"] and index["schema"] == d.SCHEMA
    (cache,) = _cache_files()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600 and stat.S_IMODE(cache.parent.stat().st_mode) == 0o700
    monkeypatch.setattr(d, "parse", lambda *_a: (_ for _ in ()).throw(AssertionError("parsed again")))
    assert d.load_index(source) is index


def test_after_a_restart_the_saved_index_is_used_while_the_file_is_unchanged(source, monkeypatch):
    d.load_index(source)
    d._MEMORY.clear()  # a new process
    monkeypatch.setattr(d, "parse", lambda *_a: (_ for _ in ()).throw(AssertionError("parsed again")))
    assert [a["id"] for a in d.load_index(source)["apps"]] == ["a.One", "a.Two"]


def test_a_changed_file_is_read_again(source):
    d.load_index(source)
    d._MEMORY.clear()
    source.xml.write_text(document(component("a.Three", "Three")))
    assert [a["id"] for a in d.load_index(source)["apps"]] == ["a.Three"]
    assert [a["id"] for a in d.load_index(source)["apps"]] == ["a.Three"]


def test_a_replaced_file_of_the_same_size_and_date_is_still_noticed(source):
    d.load_index(source)
    d._MEMORY.clear()
    stat_before = source.xml.stat()
    other = source.xml.parent / "other.xml"
    other.write_text(source.xml.read_text().replace("One", "Uno"))
    os.utime(other, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    os.replace(other, source.xml)  # a new inode with the same size and time
    assert [a["name"] for a in d.load_index(source)["apps"]] == ["Uno", "Two"]


@pytest.mark.parametrize("damage", ["not json", "[]", '{"schema": 1}', '{"schema": 2, "stamp": [], "apps": []}', '{"schema": 2}'])
def test_a_damaged_or_foreign_saved_index_is_ignored(source, damage):
    d.load_index(source)
    (cache,) = _cache_files()
    d._MEMORY.clear()
    cache.write_text(damage)
    assert [a["id"] for a in d.load_index(source)["apps"]] == ["a.One", "a.Two"]  # built again from the file


@pytest.mark.parametrize("apps", [[{"id": "org.x.App"}], ["text"], [{"id": "a.B", "name": "B", "summary": "", "developer": "", "pkgname": "", "icon": "",
                                                                   "icon_size": "", "url": "", "description": "", "categories": "WebBrowser", "keywords": []}],
                                  [{"id": "a.B", "name": "B", "summary": 5, "developer": "", "pkgname": "", "icon": "", "icon_size": "", "url": "",
                                    "description": "", "categories": [], "keywords": []}],
                                  [{"id": "a.B", "name": "B", "summary": "x" * 100000, "developer": "", "pkgname": "", "icon": "", "icon_size": "", "url": "",
                                    "description": "", "categories": [], "keywords": []}]])
def test_a_saved_index_with_the_right_stamp_but_apps_of_the_wrong_shape_is_not_trusted(source, apps):
    d.load_index(source)
    (cache,) = _cache_files()
    saved = json.loads(cache.read_text())
    saved["apps"] = apps
    cache.write_text(json.dumps(saved))
    d._MEMORY.clear()
    assert [a["id"] for a in d.load_index(source)["apps"]] == ["a.One", "a.Two"]  # built again from the file instead of failing for ever


def test_a_replaced_file_is_noticed_even_in_the_same_process_without_a_restart(source):
    assert [a["id"] for a in d.load_index(source)["apps"]] == ["a.One", "a.Two"]
    source.xml.write_text(document(component("a.New", "New one")))  # nothing clears the memory in between
    assert [a["id"] for a in d.load_index(source)["apps"]] == ["a.New"]


def test_a_file_whose_only_change_is_its_date_is_read_again(source, monkeypatch):
    calls = []
    real = d.parse
    monkeypatch.setattr(d, "parse", lambda stream: (calls.append(1), real(stream))[1])
    d.load_index(source)
    info = source.xml.stat()
    os.utime(source.xml, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))  # same size, same inode, a new date
    d.load_index(source)
    assert len(calls) == 2


def test_a_cache_folder_that_cannot_be_written_only_costs_speed(source, tmp_path, monkeypatch):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the cache folder should be")
    monkeypatch.setattr(paths, "cache_dir", lambda: blocker)
    assert len(d.load_index(source)["apps"]) == 2


def test_a_file_that_is_not_readable_is_not_cached_as_empty(source):
    source.xml.write_text("<components><component>")
    with pytest.raises(d.DiscoverError):
        d.load_index(source)
    assert _cache_files() == []


# -- icons -----------------------------------------------------------------------------------------------------------------------------
def test_a_flatpak_icon_is_found_in_its_size_folder_and_only_there(tmp_path):
    icons = tmp_path / "icons"
    (icons / "64x64").mkdir(parents=True)
    (icons / "64x64" / "app.png").write_bytes(b"png")
    (icons / "128x128").mkdir()
    (icons / "128x128" / "big.png").write_bytes(b"png")
    src = d.Source("flatpak", "r", tmp_path / "x.xml", icons, "r")
    assert d.icon_path(src, "", "app.png") == str(icons / "64x64" / "app.png")
    assert d.icon_path(src, "", "big.png") is None and d.icon_path(src, "", "missing.png") is None


def test_a_repository_icon_lives_under_the_origin_the_data_names(tmp_path):
    icons = tmp_path / "icons"
    (icons / "arch-extra" / "64x64").mkdir(parents=True)
    (icons / "arch-extra" / "64x64" / "a.jxl").write_bytes(b"jxl")
    src = d.Source("repo", "extra", tmp_path / "x.xml", icons, "")
    assert d.icon_path(src, "arch-extra", "a.jxl") == str(icons / "arch-extra" / "64x64" / "a.jxl")
    assert d.icon_path(src, "other", "a.jxl") is None


def test_a_128_pixel_icon_is_found_when_that_is_the_size_the_data_names(tmp_path):
    icons = tmp_path / "icons"
    (icons / "128x128").mkdir(parents=True)
    (icons / "128x128" / "big.png").write_bytes(b"png")
    src = d.Source("flatpak", "r", tmp_path / "x.xml", icons, "r")
    assert d.icon_path(src, "", "big.png", "128") == str(icons / "128x128" / "big.png")
    assert d.icon_path(src, "", "big.png") is None and d.icon_path(src, "", "big.png", "256") is None and d.icon_path(src, "", "big.png", "../x") is None


@pytest.mark.parametrize("origin", ["../outside", "/abs/dir", "..", ".", "a/b", "x" * 81, "a b", "a\nb"])
def test_a_repository_origin_that_is_not_a_folder_name_is_refused(tmp_path, origin):
    icons = tmp_path / "icons"
    for sub in (icons / "64x64", tmp_path / "outside" / "64x64"):
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "evil.png").write_bytes(b"png")
    src = d.Source("repo", "extra", tmp_path / "x.xml", icons, "")
    assert d.icon_path(src, origin, "evil.png") is None


def test_an_icon_that_is_a_link_or_a_folder_or_has_an_odd_name_is_refused(tmp_path):
    icons = tmp_path / "icons"
    folder = icons / "64x64"
    folder.mkdir(parents=True)
    (tmp_path / "secret.png").write_bytes(b"secret")
    (folder / "link.png").symlink_to(tmp_path / "secret.png")
    (folder / "dir.png").mkdir()
    src = d.Source("flatpak", "r", tmp_path / "x.xml", icons, "r")
    for name in ("link.png", "dir.png", "../secret.png", "..", "a/b.png", "/etc/passwd", "", None, "x.svg", "x.png\n"):
        assert d.icon_path(src, "", name) is None, name


# -- groups ----------------------------------------------------------------------------------------------------------------------------
def test_groups_follow_the_categories_and_a_browser_is_not_also_internet():
    assert d.groups_of(["Network", "WebBrowser"]) == ["browsers"]
    assert d.groups_of(["Network", "Email"]) == ["chat", "internet"]
    assert d.groups_of(["Game", "Utility"]) == ["games", "utility"]
    assert d.groups_of(["Nothing", "Known"]) == ["other"] and d.groups_of([]) == ["other"]


# -- the merged catalogue --------------------------------------------------------------------------------------------------------------
def _app(id_, name, **over):
    base = {"id": id_, "name": name, "summary": f"{name} summary", "developer": "", "pkgname": "", "icon": "", "url": "", "categories": [],
            "keywords": [], "description": ""}
    return {**base, **over}


def _loader(**by_label):
    return lambda source: by_label[source.label]


def test_translated_phrases_inside_the_default_description_are_left_out():
    """Found by review on the real Flathub data (MenuLibre has 13 <em xml:lang> labels in its default text) and in upstream metainfo files, which put
    <p xml:lang="..."> beside the default <p>."""
    def parsed(description):
        return read(document(component(description=description)))[0][0]["description"]

    assert parsed('<p>English.</p><p xml:lang="de">Deutsch.</p><p xml:lang="fr">Francais.</p><p>More English.</p>') == "English.\nMore English."
    assert parsed('<p>Features: <em>fast</em><em xml:lang="ca">rapid</em> and small.</p>') == "Features: fast and small."
    assert parsed('<p>One.</p><ul><li>a</li><li xml:lang="de">b <em>c</em></li><li>d</li></ul><p>Two.</p>') == "One.\n\n• a\n• d\nTwo."
    assert parsed('<p xml:lang="de">Hallo Welt</p>') == ""  # only a translation: nothing is better than another language
    apps = read(document(component(description='<p xml:lang="de">Hallo</p>', extra='<description><p>Second.</p></description>')))[0]
    assert apps[0]["description"] == "Second."  # an empty default does not stop the next one


def _src(kind, label):
    return d.Source(kind, label, Path(f"/nonexistent/{label}.xml"), Path("/nonexistent/icons"), label if kind == "flatpak" else "")


def _vendor(package="vendor-app", **over):
    base = {"package": package, "formats": ["deb"], "name": "Vendor App", "summary": "A vendor's program", "categories": ["Network"], "flathub_id": "",
            "developer": "Vendor", "url": "https://vendor.example/", "icon_name": "vendor", "note": ""}
    return {**base, **over}


def test_the_same_id_in_two_places_is_one_application_with_two_ways_to_install_it():
    entries = d.build_entries([_src("flatpak", "flathub"), _src("repo", "extra")], [], loader=_loader(
        flathub={"origin": "", "apps": [_app("org.mozilla.firefox", "Firefox", categories=["WebBrowser"])]},
        extra={"origin": "o", "apps": [_app("org.mozilla.firefox", "Firefox", pkgname="firefox", summary="Browse the web", url="https://firefox.example/")]}))
    assert len(entries) == 1
    entry = entries[0]
    assert [s["kind"] for s in entry["sources"]] == ["flatpak", "repo"]
    assert entry["sources"][0]["id"] == "org.mozilla.firefox" and entry["sources"][1]["package"] == "firefox"
    assert entry["url"] == "https://firefox.example/" and entry["groups"] == ["browsers"]  # what the first source left out is filled in


def test_one_id_published_several_times_in_one_source_is_one_way_to_get_it():
    """Found by review on the real Flathub data: org.winehq.Wine has nine components (one per branch) and showed nine identical cards."""
    entries = d.build_entries([_src("flatpak", "flathub")], [], loader=_loader(
        flathub={"apps": [_app("org.winehq.Wine", "Wine") for _ in range(9)]}))
    assert len(entries) == 1 and entries[0]["sources"] == [{"kind": "flatpak", "remote": "flathub", "id": "org.winehq.Wine", "label": "flathub (Flatpak)"}]
    both = d.build_entries([_src("repo", "extra")], [], loader=_loader(
        extra={"apps": [_app("org.x.App", "App", pkgname="app"), _app("org.x.App", "App", pkgname="app"), _app("org.x.App", "App", pkgname="app-git")]}))
    assert [s["package"] for s in both[0]["sources"]] == ["app", "app-git"]  # different packages are different ways


def _release(key="demo", flathub_id="", formats=("appimage", "deb"), **over):
    base = {"key": key, "formats": list(formats), "name": "Demo", "summary": "Does demos.", "categories": ["Network"], "developer": "Acme", "url": "https://demo.example/",
            "flathub_id": flathub_id, "icon_name": "demo", "page": "https://github.com/acme/demo/releases"}
    return {**base, **over}


def test_a_release_download_joins_the_flatpak_only_when_its_exact_flathub_id_is_in_the_data():
    flathub = {"flathub": {"apps": [_app("org.acme.Demo", "Demo")]}}
    joined = d.build_entries([_src("flatpak", "flathub")], [], loader=_loader(**flathub), releases=[_release(flathub_id="org.acme.Demo")])
    assert len(joined) == 1 and [s["kind"] for s in joined[0]["sources"]] == ["flatpak", "release"]
    source = joined[0]["sources"][1]
    assert source == {"kind": "release", "key": "demo", "formats": ["appimage", "deb"], "page": "https://github.com/acme/demo/releases",
                      "label": "The maker's own download (GitHub)"}
    apart = d.build_entries([_src("flatpak", "flathub")], [], loader=_loader(**flathub), releases=[_release(flathub_id="org.acme.Demo2")])
    assert sorted(e["key"] for e in apart) == ["org.acme.Demo", "release:demo"]  # a near miss is never merged
    alone = next(e for e in apart if e["key"] == "release:demo")
    assert alone["name"] == "Demo" and alone["sources"][0]["kind"] == "release" and alone["groups"] == ["internet"] and alone["icon_name"] == "demo"
    assert not any(s["kind"] == "release" for s in next(e for e in apart if e["key"] == "org.acme.Demo")["sources"])


def test_a_release_download_is_not_merged_into_an_entry_that_only_a_repository_lists():
    entries = d.build_entries([_src("repo", "extra")], [], loader=_loader(extra={"apps": [_app("org.acme.Demo", "Demo", pkgname="demo")]}),
                              releases=[_release(flathub_id="org.acme.Demo")])
    assert sorted(e["key"] for e in entries) == ["org.acme.Demo", "release:demo"]


def test_releases_default_to_none_and_the_search_can_ask_for_them():
    assert d.build_entries([], [], loader=_loader()) == []
    entries = d.build_entries([_src("flatpak", "flathub")], [_vendor()], loader=_loader(flathub={"apps": [_app("org.x.Other", "Other")]}),
                              releases=[_release(), _release("second", name="Second")])
    assert [e["key"] for e in d.search(entries, kind="release")["entries"]] == ["release:demo", "release:second"]
    assert [e["key"] for e in d.search(entries, kind="vendor")["entries"]] == ["vendor:vendor-app"]
    assert d.search(entries, "second")["entries"][0]["key"] == "release:second"


def test_the_search_does_not_match_the_kind_written_in_a_key():
    """Found by review: searching "release" or "vendor" listed every program of that kind, because the key (release:helium) was searched."""
    entries = d.build_entries([], [_vendor("some-tool", name="Some Tool", summary="Does tool things.", developer="Acme")], loader=_loader(),
                              releases=[_release("helium", name="Helium", summary="A browser.", developer="Imput")])
    assert d.search(entries, "release")["entries"] == [] and d.search(entries, "vendor")["entries"] == []
    assert [e["key"] for e in d.search(entries, "helium")["entries"]] == ["release:helium"]  # the program's own name in the key still finds it
    assert [e["key"] for e in d.search(entries, "some-tool")["entries"]] == ["vendor:some-tool"]


def test_the_signature_changes_when_the_releases_do():
    base = d.signature([], [], [_release()])
    assert base == d.signature([], [], [_release()]) and base != d.signature([], [], [_release(name="Other")]) and base != d.signature([], [])


def test_a_desktop_suffix_on_an_id_does_not_make_a_second_application():
    entries = d.build_entries([_src("flatpak", "f"), _src("repo", "r")], [], loader=_loader(
        f={"apps": [_app("org.gnome.Calc.desktop", "Calculator")]}, r={"apps": [_app("org.gnome.Calc", "Calculator", pkgname="gnome-calculator")]}))
    assert len(entries) == 1 and entries[0]["key"] == "org.gnome.Calc" and len(entries[0]["sources"]) == 2
    assert entries[0]["sources"][0]["id"] == "org.gnome.Calc"  # what the Install page is handed is the reference's name, not the menu entry's


def test_similar_names_are_never_merged():
    entries = d.build_entries([_src("flatpak", "f")], [], loader=_loader(
        f={"apps": [_app("org.a.Firefox", "Firefox"), _app("org.b.Firefox", "Firefox"), _app("org.a.firefox", "Firefox"), _app("org.a.Firefox2", "Firefox")]}))
    assert len(entries) == 4


def test_a_vendor_program_joins_the_flatpak_only_when_its_flathub_id_is_in_the_data():
    flathub = {"apps": [_app("com.google.Chrome", "Google Chrome"), _app("com.google.ChromeDev", "Google Chrome (unstable)")]}
    entries = d.build_entries([_src("flatpak", "flathub")], [
        _vendor("google-chrome-stable", name="Google Chrome", flathub_id="com.google.Chrome"),
        _vendor("google-chrome-beta", name="Google Chrome (Beta)", flathub_id=""),
        _vendor("ghost", name="Ghost", flathub_id="com.example.NotThere")], loader=_loader(flathub=flathub))
    by_key = {e["key"]: e for e in entries}
    assert [s["kind"] for s in by_key["com.google.Chrome"]["sources"]] == ["flatpak", "vendor"]
    assert [s["kind"] for s in by_key["com.google.ChromeDev"]["sources"]] == ["flatpak"]  # the beta is NOT folded into the developer channel
    assert [s["kind"] for s in by_key["vendor:google-chrome-beta"]["sources"]] == ["vendor"]
    assert [s["kind"] for s in by_key["vendor:ghost"]["sources"]] == ["vendor"]  # an id that is not in the data merges nothing


def test_categories_and_keywords_come_from_the_first_source_that_has_them():
    entries = d.build_entries([_src("flatpak", "f"), _src("repo", "r")], [], loader=_loader(
        f={"apps": [_app("a.B", "B")]}, r={"apps": [_app("a.B", "B", pkgname="b-pkg", categories=["WebBrowser"], keywords=["web"])]}))
    assert entries[0]["groups"] == ["browsers"] and entries[0]["keywords"] == ["web"]


def test_a_vendor_program_is_not_folded_into_an_entry_that_has_no_flatpak():
    """The map says the program is on Flathub; if the data only has a repository entry under that id, nothing merges: the vendor's file is a
    different thing from a repository package, and the entry must not look like it has a Flatpak."""
    entries = d.build_entries([_src("repo", "extra")], [_vendor("google-chrome-stable", name="Google Chrome", flathub_id="com.google.Chrome")],
                              loader=_loader(extra={"apps": [_app("com.google.Chrome", "Chrome from the repository", pkgname="chrome-pkg")]}))
    by_key = {e["key"]: e for e in entries}
    assert [s["kind"] for s in by_key["com.google.Chrome"]["sources"]] == ["repo"]
    assert [s["kind"] for s in by_key["vendor:google-chrome-stable"]["sources"]] == ["vendor"]


def test_a_vendor_program_is_listed_alone_when_there_is_no_data_at_all():
    entries = d.build_entries([], [_vendor("a-app", name="A App"), _vendor("b-app", name="B App")], loader=_loader())
    assert [e["key"] for e in entries] == ["vendor:a-app", "vendor:b-app"]
    assert entries[0]["sources"][0]["formats"] == ["deb"] and entries[0]["icon_name"] == "vendor" and entries[0]["groups"] == ["internet"]


def test_a_repository_component_without_a_package_name_cannot_be_installed_and_is_not_listed():
    entries = d.build_entries([_src("repo", "extra")], [], loader=_loader(extra={"apps": [_app("a.B", "B"), _app("a.C", "C", pkgname="c-pkg")]}))
    assert [e["key"] for e in entries] == ["a.C"]


def test_the_icon_comes_from_the_first_source_that_has_one(tmp_path):
    for sub in ("one", "two"):
        (tmp_path / sub / "icons" / "64x64").mkdir(parents=True)
    (tmp_path / "two" / "icons" / "64x64" / "app.png").write_bytes(b"png")
    s1, s2 = d.Source("flatpak", "one", tmp_path / "one" / "x.xml", tmp_path / "one" / "icons", "one"), d.Source("flatpak", "two", tmp_path / "two" / "x.xml", tmp_path / "two" / "icons", "two")
    entries = d.build_entries([s1, s2], [], loader=_loader(one={"apps": [_app("a.B", "B", icon="app.png")]}, two={"apps": [_app("a.B", "B", icon="app.png")]}))
    assert entries[0]["icon"] == str(tmp_path / "two" / "icons" / "64x64" / "app.png")


def test_entries_are_sorted_by_name_ignoring_case():
    entries = d.build_entries([_src("flatpak", "f")], [], loader=_loader(f={"apps": [_app("a.Z", "zebra"), _app("a.A", "Apple"), _app("a.M", "mango")]}))
    assert [e["name"] for e in entries] == ["Apple", "mango", "zebra"]


# -- searching -------------------------------------------------------------------------------------------------------------------------
def _catalogue():
    def entry(key, name, summary="", keywords=(), developer="", groups=("other",), kinds=("flatpak",)):
        return {"key": key, "name": name, "summary": summary, "keywords": list(keywords), "developer": developer, "groups": list(groups),
                "sources": [{"kind": k} for k in kinds]}
    return [entry("org.mozilla.firefox", "Firefox", "Fast web browser", ["web"], "Mozilla", ["browsers"]),
            entry("org.example.Fire", "Fireworks", "Draw explosions", [], "", ["graphics"]),
            entry("org.example.Wall", "Firewall tool", "Manage the network", ["firefox", "security"], "", ["system"]),
            entry("org.example.Notes", "Notes", "Take notes with Firefox bookmarks", [], "", ["office"], ("repo",)),
            entry("org.example.Chess", "Chess", "A board game", ["fire"], "", ["games"], ("flatpak", "repo")),
            entry("vendor:chrome", "Chrome", "Google's browser", [], "Google", ["browsers"], ("vendor",))]


def test_the_best_match_comes_first():
    names = [e["name"] for e in d.search(_catalogue(), "firefox")["entries"]]
    assert names == ["Firefox", "Firewall tool", "Notes"]  # exact name, then the keyword, then the summary
    assert [e["name"] for e in d.search(_catalogue(), "fire")["entries"]][:3] == ["Firefox", "Fireworks", "Firewall tool"]  # names that start with it, shortest first


def test_better_matches_come_first_even_when_the_catalogue_is_in_another_order():
    """The catalogue is sorted by name, not by how well a name matches: on the real data, without the ranking, "firefox" listed Firefox third."""
    entries = [{"key": k, "name": n, "summary": s, "keywords": [], "developer": "", "groups": ["other"], "sources": [{"kind": "flatpak"}]}
               for k, n, s in (("a.Add", "Add Water", "A theme for Firefox"), ("a.Dat", "Datcord", "Chat about firefox"), ("a.Fox", "Firefox", "Web browser"))]
    assert [e["name"] for e in d.search(entries, "firefox")["entries"]] == ["Firefox", "Datcord", "Add Water"]


def test_every_word_must_match_somewhere():
    assert [e["name"] for e in d.search(_catalogue(), "web browser")["entries"]] == ["Firefox"]
    assert d.search(_catalogue(), "web zzz")["total"] == 0
    assert [e["name"] for e in d.search(_catalogue(), "mozilla")["entries"]] == ["Firefox"]  # the developer counts
    assert [e["name"] for e in d.search(_catalogue(), "ORG.EXAMPLE.WALL")["entries"]] == ["Firewall tool"]  # so does the id


def test_what_is_typed_is_never_a_pattern():
    for hostile in (".*", "(", "[a-", "\\", "a|b", "%", "'; drop table", "\x00"):
        assert d.search(_catalogue(), hostile)["total"] == 0


def test_an_empty_query_lists_everything_by_name_and_counts_the_groups():
    result = d.search(_catalogue(), "")
    assert result["total"] == 6 and [e["name"] for e in result["entries"]] == ["Firefox", "Fireworks", "Firewall tool", "Notes", "Chess", "Chrome"]
    assert {g["id"]: g["count"] for g in result["groups"]} == {"browsers": 2, "graphics": 1, "system": 1, "office": 1, "games": 1}
    assert [g["id"] for g in result["groups"]] == ["browsers", "games", "graphics", "office", "system"]  # always in the order they are shown


def test_a_group_filter_narrows_the_results_but_not_the_group_counts():
    result = d.search(_catalogue(), "fire", group="browsers")
    assert [e["name"] for e in result["entries"]] == ["Firefox"] and result["total"] == 1
    assert sum(g["count"] for g in result["groups"]) >= 3  # the other groups still show how many matches they hold


def test_a_chosen_group_keeps_its_button_even_when_no_match_is_in_it():
    result = d.search(_catalogue(), "notes", group="browsers")  # the words match no browser, but the person chose Browsers
    assert result["entries"] == [] and [g["id"] for g in result["groups"]] == ["browsers", "office"]
    assert {g["id"]: g["count"] for g in result["groups"]}["browsers"] == 0
    assert [g["id"] for g in d.search(_catalogue(), "notes")["groups"]] == ["office"]  # and not when no group is chosen


def test_a_kind_filter_keeps_only_what_can_be_installed_that_way():
    assert [e["name"] for e in d.search(_catalogue(), "", kind="vendor")["entries"]] == ["Chrome"]
    assert {e["name"] for e in d.search(_catalogue(), "", kind="repo")["entries"]} == {"Notes", "Chess"}
    assert d.search(_catalogue(), "", kind="nonsense")["total"] == 0


def test_results_come_in_pages_and_a_wild_request_is_kept_in_bounds():
    many = [{"key": f"a.A{i:03}", "name": f"App {i:03}", "summary": "", "keywords": [], "developer": "", "groups": ["other"], "sources": [{"kind": "flatpak"}]} for i in range(450)]
    first, second = d.search(many, "", offset=0, limit=40), d.search(many, "", offset=40, limit=40)
    assert first["total"] == 450 and len(first["entries"]) == 40 and first["entries"][0]["name"] == "App 000" and second["entries"][0]["name"] == "App 040"
    assert len(d.search(many, "", limit=100000)["entries"]) == 200  # never more than the cap
    assert len(d.search(many, "", limit=0)["entries"]) == 1 and len(d.search(many, "", limit=-5)["entries"]) == 1
    assert d.search(many, "", offset=-10)["offset"] == 0 and d.search(many, "", offset=10_000)["entries"] == []
    assert [e["name"] for e in d.search(many, "", offset=-10, limit=3)["entries"]] == ["App 000", "App 001", "App 002"]  # a negative start is the first page


def test_only_the_first_eight_words_of_a_query_are_used():
    entries = [{"key": "k", "name": "alpha", "summary": "", "keywords": [], "developer": "", "groups": ["other"], "sources": [{"kind": "flatpak"}]}]
    assert d.search(entries, " ".join(["alpha"] * 8 + ["missing"]))["total"] == 1


def test_a_whole_catalogue_goes_through_search_end_to_end(tmp_path):
    d._MEMORY.clear()
    path = tmp_path / "appstream.xml"
    path.write_text(document(component("org.mozilla.firefox", "Firefox", summary="A web browser", pkgname="firefox", categories=("Network", "WebBrowser"), keywords=("web",)),
                             component("org.example.Calc", "Calculator", categories=("Utility", "Calculator"))))
    flathub = d.Source("flatpak", "flathub", path, tmp_path / "icons", "flathub")
    entries = d.build_entries([flathub], [_vendor("google-chrome-stable", name="Google Chrome", categories=["Network", "WebBrowser"])])
    result = d.search(entries, "browser web")
    assert [e["name"] for e in result["entries"]] == ["Firefox"]
    assert json.dumps(result)  # everything in a result can be sent to the window as JSON
    assert [e["name"] for e in d.search(entries, "", group="browsers")["entries"]] == ["Firefox", "Google Chrome"]


# -- the AUR's answer ------------------------------------------------------------------------------------------------------------------

def _aur(name="vlc-git", **over):
    return {"Name": name, "Version": "4.0-1", "Description": "A player", "NumVotes": 5, "Popularity": 1.5, "Maintainer": "somebody",
            "URL": "https://example.org/vlc", "OutOfDate": None, **over}


def test_an_aur_answer_is_shaped_for_the_page_most_popular_first():
    out = d.aur_results([_aur("b", Popularity=0.1), _aur("a", Popularity=2.0), _aur("c", Popularity=2.0, NumVotes=9)], {"a"})
    assert [e["name"] for e in out["entries"]] == ["c", "a", "b"] and out["total"] == 3  # popularity, then votes, then name
    assert [e["installed"] for e in out["entries"]] == [False, True, False]
    one = out["entries"][1]
    assert one == {"name": "a", "version": "4.0-1", "summary": "A player", "votes": 5, "popularity": 2.0, "maintainer": "somebody", "out_of_date": False,
                   "url": "https://example.org/vlc", "installed": True}


def test_an_aur_answer_is_written_by_strangers_and_nothing_in_it_is_trusted():
    hostile = [_aur("fine"), {"Name": "BAD NAME"}, {"Name": "../x"}, {"Name": 5}, {"Name": "-lead"}, "text", None, {"Description": "no name"},
               _aur("ctl", Description="a\x1b[31mred‮text\nline", Version="1\x00", Maintainer="x\x07y"),
               _aur("nums", NumVotes=True, Popularity="high"), _aur("neg", NumVotes=-4, Popularity=-1.0), _aur("inf", Popularity=float("inf")),
               _aur("url1", URL="http://plain.example"), _aur("url2", URL="https://bad host/x"), _aur("url3", URL="javascript:alert(1)")]
    out = d.aur_results(hostile)
    names = [e["name"] for e in out["entries"]]
    assert "fine" in names and not {"BAD NAME", "../x", "-lead"} & set(names) and out["total"] == len(names)
    by = {e["name"]: e for e in out["entries"]}
    assert all(c.isprintable() for e in out["entries"] for c in e["summary"] + e["version"] + e["maintainer"])
    assert "‮" not in by["ctl"]["summary"] and "\n" not in by["ctl"]["summary"]
    assert by["nums"]["votes"] == 0 and by["nums"]["popularity"] == 0.0  # a bool is not a number of votes; text is not a popularity
    assert by["neg"]["votes"] == 0 and by["neg"]["popularity"] == 0.0 and by["inf"]["popularity"] == 0.0
    assert by["url1"]["url"] == "" and by["url2"]["url"] == "" and by["url3"]["url"] == ""  # only an https address is ever offered as a link


def test_an_aur_answer_that_is_not_a_list_is_nothing_and_a_long_one_is_cut_with_the_total_said():
    assert d.aur_results({"results": []}) == {"total": 0, "entries": []} and d.aur_results(None) == {"total": 0, "entries": []}
    many = [_aur(f"pkg{i:03}", Popularity=float(i)) for i in range(100)]
    out = d.aur_results(many)
    assert out["total"] == 100 and len(out["entries"]) == d.AUR_RESULTS == 30 and out["entries"][0]["name"] == "pkg099"


def test_orphaned_and_out_of_date_packages_are_told_apart():
    out = d.aur_results([_aur("orphan", Maintainer=None), _aur("late", OutOfDate=1700000000), _aur("ok", OutOfDate=None), _aur("odd", OutOfDate=True, Maintainer="")])
    by = {e["name"]: e for e in out["entries"]}
    assert by["orphan"]["maintainer"] == "" and by["late"]["out_of_date"] is True and by["ok"]["out_of_date"] is False
    assert by["odd"]["out_of_date"] is False and by["odd"]["maintainer"] == ""  # a bool is not a date


# -- programs from an online catalogue (AppImageHub) -------------------------------------------------------------------------------------

def _image(repo="acme/tool", name="Tool", **over):
    return {**{"repo": repo, "name": name, "summary": "Does tool things.", "categories": ["Utility"], "developer": "Acme"}, **over}


def test_a_community_appimage_is_listed_on_its_own_with_a_source_that_says_nobody_checked_it():
    [entry] = d.build_entries([], [], loader=_loader(), appimages=[_image()])
    assert entry["key"] == "appimage:acme/tool" and entry["name"] == "Tool" and entry["groups"] == ["utility"] and entry["developer"] == "Acme"
    assert entry["url"] == "https://github.com/acme/tool" and entry["icon_name"] == "application-x-executable" and entry["icon"] == ""
    assert entry["sources"] == [{"kind": "appimage", "repo": "acme/tool", "formats": ["appimage"], "page": "https://github.com/acme/tool/releases",
                                 "label": "A community listing (AppImageHub), not checked by Cygnus"}]


def test_a_community_appimage_gives_way_to_a_program_that_is_already_listed_by_the_same_name():
    """Prefer what the system's own lists carry: the version from Flathub or the repositories is shown, not a second entry for the same program."""
    entries = d.build_entries([_src("flatpak", "flathub"), _src("repo", "extra")], [_vendor(name="Vendor App")], loader=_loader(
        flathub={"apps": [_app("org.mozilla.firefox", "Firefox")]}, extra={"apps": [_app("org.vlc.VLC", "VLC media player", pkgname="vlc")]}),
        releases=[_release(name="Demo Release")],
        appimages=[_image("a/firefox", "firefox"), _image("a/vlc", "VLC Media-Player"), _image("a/vendor", "vendor_app"), _image("a/demo", "Demo  Release!"),
                   _image("a/new", "Brand New")])
    assert sorted(e["key"] for e in entries) == ["appimage:a/new", "org.mozilla.firefox", "org.vlc.VLC", "release:demo", "vendor:vendor-app"]
    firefox = next(e for e in entries if e["key"] == "org.mozilla.firefox")
    assert [s["kind"] for s in firefox["sources"]] == ["flatpak"]  # never attached to the other program's entry by a similar name


def test_a_community_appimage_of_a_project_that_is_already_a_makers_own_download_is_left_out_whatever_the_capitals():
    entries = d.build_entries([], [], loader=_loader(), releases=[_release(repo="Acme/Demo-App", name="Maker's Name")],
                              appimages=[_image("acme/demo-app", "A Different Name"), _image("other/demo-app", "Another")])
    assert sorted(e["key"] for e in entries) == ["appimage:other/demo-app", "release:demo"]


def test_two_community_appimages_of_one_name_show_the_first_and_the_same_project_twice_shows_once():
    entries = d.build_entries([], [], loader=_loader(), appimages=[_image("a/one", "Same Name"), _image("b/two", "same-name"), _image("a/one", "Other Name")])
    assert [e["key"] for e in entries] == ["appimage:a/one"]


def test_a_name_with_no_letters_is_never_taken_for_another_programs():
    entries = d.build_entries([], [], loader=_loader(), appimages=[_image("a/one", "!!!"), _image("b/two", "???")])
    assert len(entries) == 2  # both reduce to nothing: that is no reason to hide either


def test_the_search_finds_a_community_appimage_by_name_developer_and_project_but_not_by_its_kind_prefix():
    entries = d.build_entries([], [], loader=_loader(), appimages=[_image("zed/widget", "Widget", developer="Zed Corp"), _image("q/other", "Other")])
    assert [e["name"] for e in d.search(entries, "widget")["entries"]] == ["Widget"]
    assert [e["name"] for e in d.search(entries, "zed")["entries"]] == ["Widget"]
    assert d.search(entries, "appimage")["total"] == 0  # "appimage:" is the kind of key, not something written about the program
    assert d.search(entries, kind="appimage")["total"] == 2 and d.search(entries, kind="flatpak")["total"] == 0


def test_the_signature_changes_when_an_online_catalogue_does():
    one = d.signature([], [], [], [["appimagehub", 10, 1, 5]])
    assert one != d.signature([], [], [], [["appimagehub", 11, 1, 5]]) != d.signature([], [], [], [["appimagehub", 10, 2, 5]])
    assert one != d.signature([], [], [], []) and d.signature([], [], []) == d.signature([], [], [], ())


# -- Debian's programs ----------------------------------------------------------------------------------------------------------------------------

def _deb(package="demo", name="Demo", **over):
    return {**{"package": package, "id": package, "name": name, "summary": "Does demo things.", "categories": ["Office"], "url": "https://demo.example", "version": "1.0-1",
               "filename": f"pool/main/{package[0]}/{package}/{package}_1.0-1_amd64.deb", "sha256": "ab" * 32, "size": 1000, "depends": ["libc6"]}, **over}


def test_a_debian_program_is_listed_on_its_own_with_everything_the_download_needs_and_a_warning_in_its_label():
    [entry] = d.build_entries([], [], loader=_loader(), debs=[_deb(depends=["libc6", "demo-data"])])
    assert entry["key"] == "debian:demo" and entry["name"] == "Demo" and entry["groups"] == ["office"] and entry["url"] == "https://demo.example"
    assert entry["icon"] == "" and entry["icon_name"] == "package-x-generic" and entry["developer"] == ""
    assert entry["sources"] == [{"kind": "debian", "package": "demo", "version": "1.0-1", "size": 1000, "filename": "pool/main/d/demo/demo_1.0-1_amd64.deb",
                                 "sha256": "ab" * 32, "formats": ["deb"], "needs": ["demo-data"], "needs_more": 0, "label": "Debian's own package, converted by Cygnus (it may not work)"}]


def test_a_debian_program_gives_way_to_whatever_the_repositories_flathub_or_the_makers_already_have():
    """The Arch version is preferred: Debian's is not listed when the repositories have a package of that name, when the id is already an entry, or when the
    name is already the name of a listed program."""
    entries = d.build_entries([_src("flatpak", "flathub")], [_vendor(name="Vendor App")], loader=_loader(
        flathub={"apps": [_app("org.gnome.gitg", "gitg"), _app("org.mozilla.firefox", "Firefox")]}), releases=[_release(name="Demo Release")], appimages=[_image("a/tool", "Some Tool")],
        arch_names={"firefox", "in-repo"},
        debs=[_deb("in-repo", "In Repo"), _deb("gitg", "Gitg Under Another Name", id="org.gnome.gitg"), _deb("fire", "FIREFOX"), _deb("vendor", "vendor-app"),
              _deb("rel", "Demo  Release!"), _deb("img", "Some_Tool"), _deb("fresh", "Fresh Program")])
    assert sorted(e["key"] for e in entries) == ["appimage:a/tool", "debian:fresh", "org.gnome.gitg", "org.mozilla.firefox", "release:demo", "vendor:vendor-app"]
    gitg = next(e for e in entries if e["key"] == "org.gnome.gitg")
    assert [s["kind"] for s in gitg["sources"]] == ["flatpak"]  # a Debian package is never attached to another program's entry


def test_two_debian_programs_of_one_name_show_the_first_and_one_package_shows_once():
    entries = d.build_entries([], [], loader=_loader(), debs=[_deb("one", "Same Name"), _deb("two", "same-name"), _deb("one", "Other"), _deb("three", "Third")])
    assert [e["key"] for e in entries] == ["debian:one", "debian:three"]


def test_what_debian_says_a_program_needs_leaves_out_libraries_debian_basics_and_what_the_repositories_have():
    needs = d.debian_needs(["abiword-common", "libc6", "libgtk-3-0t64", "libabiword-3.0", "zlib1g", "python3:any", "debconf", "dpkg", "git", "git", "libreoffice-core",
                            "fonts-urw-base35", "coreutils", "libstdc++6", "lib-nodigit"], {"git"})
    assert needs == ["abiword-common", "python3", "libreoffice-core", "fonts-urw-base35", "lib-nodigit"]
    assert d.debian_needs([], set()) == [] and d.debian_needs(["libc6"], set()) == []
    assert len(d.debian_needs([f"pkg{i:02}-data" for i in range(30)], set())) == 10


def test_a_name_with_the_repositories_name_does_not_hide_a_program_by_its_dependencies():
    entries = d.build_entries([], [], loader=_loader(), arch_names={"demo-data"}, debs=[_deb(depends=["demo-data", "other-data"])])
    assert entries[0]["sources"][0]["needs"] == ["other-data"]  # the repositories' package of that name satisfies the first


def test_the_search_finds_a_debian_program_by_name_and_summary_and_not_by_its_kind_and_can_filter_for_it():
    entries = d.build_entries([], [], loader=_loader(), debs=[_deb("zeta", "Zeta Editor", summary="Edits zeta files"), _deb("other", "Other")])
    assert [e["name"] for e in d.search(entries, "zeta")["entries"]] == ["Zeta Editor"]
    assert d.search(entries, "debian")["total"] == 0
    assert d.search(entries, kind="debian")["total"] == 2 and d.search(entries, kind="appimage")["total"] == 0


def test_an_appimage_is_listed_before_a_debian_package_of_the_same_name_so_the_self_contained_one_wins():
    entries = d.build_entries([], [], loader=_loader(), appimages=[_image("a/demo", "Demo")], debs=[_deb("demo", "Demo")])
    assert [e["key"] for e in entries] == ["appimage:a/demo"]


# -- Fedora's programs ------------------------------------------------------------------------------------------------------------------------------

def _rpm(package="demo", name="Demo", **over):
    return {**{"package": package, "id": package, "name": name, "summary": "Does demo things.", "categories": ["Office"], "url": "https://demo.example", "release": 44,
               "version": "1.0-1.fc44", "location": f"Packages/{package[0]}/{package}-1.0-1.fc44.x86_64.rpm", "sha256": "cd" * 32, "size": 2000, "depends": ["demo-data"]}, **over}


def test_a_fedora_program_is_listed_on_its_own_with_everything_the_download_needs():
    [entry] = d.build_entries([], [], loader=_loader(), rpms=[_rpm(depends=["demo-data", "shadow-utils", "bash"])], arch_names={"bash"})
    assert entry["key"] == "fedora:demo" and entry["name"] == "Demo" and entry["groups"] == ["office"] and entry["url"] == "https://demo.example"
    assert entry["sources"] == [{"kind": "fedora", "package": "demo", "version": "1.0-1.fc44", "size": 2000, "release": 44, "location": "Packages/d/demo-1.0-1.fc44.x86_64.rpm",
                                 "sha256": "cd" * 32, "formats": ["rpm"], "needs": ["demo-data"], "needs_more": 0, "label": "Fedora's own package, converted by Cygnus (it may not work)"}]


def test_a_fedora_program_gives_way_to_everything_listed_before_it_including_debian_and_appimages():
    entries = d.build_entries([_src("flatpak", "flathub")], [], loader=_loader(flathub={"apps": [_app("org.mozilla.firefox", "Firefox")]}), arch_names={"in-repo"},
                              appimages=[_image("a/tool", "Tool")], debs=[_deb("same", "Shared Name")],
                              rpms=[_rpm("in-repo", "In Repo"), _rpm("fire", "firefox"), _rpm("tool", "Tool"), _rpm("shared", "Shared Name"), _rpm("only", "Only Fedora"),
                                    _rpm("byid", "Another Name", id="org.mozilla.firefox")])
    assert sorted(e["key"] for e in entries) == ["appimage:a/tool", "debian:same", "fedora:only", "org.mozilla.firefox"]


def test_what_fedora_says_a_program_needs_leaves_out_fedoras_basics_and_what_the_repositories_have():
    needs = d.rpm_needs(["demo-libs", "shadow-utils", "bash", "demo-libs", "python3-gobject", "rpm", "fedora-logos", "git"], {"bash", "git"})
    assert needs == ["demo-libs", "python3-gobject"]
    assert d.rpm_needs([], set()) == [] and len(d.rpm_needs([f"pkg{i:02}" for i in range(30)], set())) == 10


def test_the_search_finds_a_fedora_program_and_can_filter_for_it_but_not_by_its_kind_prefix():
    entries = d.build_entries([], [], loader=_loader(), rpms=[_rpm("zeta", "Zeta Editor", summary="Edits zeta files"), _rpm("other", "Other")])
    assert [e["name"] for e in d.search(entries, "zeta")["entries"]] == ["Zeta Editor"]
    assert d.search(entries, "fedora")["total"] == 0 and d.search(entries, kind="fedora")["total"] == 2 and d.search(entries, kind="debian")["total"] == 0


def test_a_community_appimage_whose_repository_is_named_like_a_repository_package_gives_way_to_it():
    entries = d.build_entries([], [], loader=_loader(), arch_names={"anki", "tool"}, appimages=[_image("ankitects/anki", "Anki Desktop"), _image("a/Tool", "Tool"),
                                                                                                  _image("a/new", "Brand New"), _image("a/anki-sync", "Anki Sync")])
    assert sorted(e["key"] for e in entries) == ["appimage:a/anki-sync", "appimage:a/new"]  # only the whole repository name counts, whatever its capitals


def test_a_debian_package_of_several_programs_is_left_out_only_when_every_one_of_them_is_already_listed():
    """Found by review: the id of the first program of a package decided for all of them, so a package of Chess (on Flathub) and Mines (not) showed neither."""
    flathub = {"flathub": {"apps": [_app("org.gnome.Chess", "Chess"), _app("org.gnome.Mines", "Mines")]}}
    both = d.build_entries([_src("flatpak", "flathub")], [], loader=_loader(**flathub), debs=[_deb("games", "games", id="org.gnome.Chess", ids=["org.gnome.Chess", "org.gnome.Mines"])])
    assert [e["key"] for e in both] == ["org.gnome.Chess", "org.gnome.Mines"]
    one_taken = d.build_entries([_src("flatpak", "flathub")], [], loader=_loader(flathub={"apps": [_app("org.gnome.Chess", "Chess")]}),
                                debs=[_deb("games", "games", id="org.gnome.Chess", ids=["org.gnome.Chess", "org.gnome.Mines"])])
    assert sorted(e["key"] for e in one_taken) == ["debian:games", "org.gnome.Chess"]
    rpm = d.build_entries([], [], loader=_loader(), rpms=[_rpm("games", "games", id="x", ids=["x", "y"])], debs=[])
    assert [e["key"] for e in rpm] == ["fedora:games"]


def test_a_record_from_an_older_copy_without_ids_still_works():
    assert [e["key"] for e in d.build_entries([], [], loader=_loader(), debs=[_deb()])] == ["debian:demo"]


@pytest.mark.parametrize("name, library", [("libc6", True), ("libgtk-3-0t64", True), ("libglib2.0-0t64", True), ("libjpeg62-turbo", True), ("libstdc++6", True), ("zlib1g", True),
                                           ("libabiword-3.0", True), ("libwmflite-0.2-7", True), ("libssl3t64", True),
                                           ("libxml2-utils", False), ("libreoffice-core", False), ("libfakeroot", False), ("libgnome-keyring-common", False),
                                           ("python3-gi", False), ("abiword-common", False)])
def test_a_debian_package_is_a_library_by_the_shape_of_its_name(name, library):
    assert bool(d._DEBIAN_LIBRARY.fullmatch(name)) is library


def test_the_needs_cut_to_ten_say_how_many_more_there_are():
    many = [f"pkg{i:02}-data" for i in range(15)]
    [debian] = d.build_entries([], [], loader=_loader(), debs=[_deb(depends=many)])
    [fedora] = d.build_entries([], [], loader=_loader(), rpms=[_rpm(depends=many)])
    for entry in (debian, fedora):
        [source] = entry["sources"]
        assert source["needs"] == many[:10] and source["needs_more"] == 5
    assert d.debian_needs(many, set(), None) == many and d.rpm_needs(many, set(), None) == many
