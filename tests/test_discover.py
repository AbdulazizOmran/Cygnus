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
