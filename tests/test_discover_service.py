"""Discover as the window uses it: the catalogue is built once and again when a data file changes, a bad data file costs only itself, what is
installed is marked, every argument is checked, and a vendor's package is downloaded into a folder of its own (checked against the
published checksum, never converted or installed there). No test reads the machine's real Flathub data."""

import gzip
import os
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cygnus.core import discover, packages, paths, preferences
from cygnus.core.backends import vendor_feeds
from cygnus.core.errors import CygnusError
from cygnus.gui import service


def _xml(*apps):
    body = "".join(f"<component><id>{i}</id><name>{n}</name><summary>{n} summary</summary>"
                   + (f"<pkgname>{p}</pkgname>" if p else "") + "<categories><category>Network</category></categories></component>" for i, n, p in apps)
    return f'<components origin="o">{body}</components>'


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    """A Flathub-like source and a repository-like source made of files in the test's own folder."""
    discover._MEMORY.clear()
    service._CATALOGUE.update(signature="", entries=[], sources=[], problems=[], repo_missing=True)
    service._MARKS.update(at=0.0, value=None)
    flathub = tmp_path / "flathub.xml"
    flathub.write_text(_xml(("org.mozilla.firefox", "Firefox", ""), ("com.google.Chrome", "Google Chrome", ""), ("org.example.Calc", "Calculator", "")))
    extra = tmp_path / "extra.xml"
    extra.write_text(_xml(("org.mozilla.firefox", "Firefox", "firefox"), ("org.example.Edit", "Editor", "editor-pkg")))
    sources = {"flatpak": [discover.Source("flatpak", "flathub", flathub, tmp_path / "icons", "flathub")],
               "repo": [discover.Source("repo", "extra", extra, tmp_path / "icons", "")]}
    monkeypatch.setattr(discover, "flatpak_sources", lambda *a, **k: list(sources["flatpak"]))
    monkeypatch.setattr(discover, "repo_sources", lambda *a, **k: list(sources["repo"]))
    monkeypatch.setattr(service, "_installed_marks", lambda: {"flatpak": set(), "pacman": set(), "vendor": set()})
    monkeypatch.setattr(service, "_repository_names", lambda: frozenset())  # no test asks the real pacman databases
    monkeypatch.setattr(service, "_repository_lists_stamp", lambda: 1.0)
    return NS(flathub=flathub, extra=extra, sources=sources)


def test_the_overview_says_what_it_read_and_that_vendors_are_included(catalogue):
    overview = service.discover_overview()
    assert [s["label"] for s in overview["sources"]] == ["flathub", "extra"] and overview["repo_missing"] is False
    assert overview["problems"] == [] and overview["vendors"] >= 1
    assert overview["total"] > 4  # the four applications plus the vendors' programs


def test_without_the_repositories_data_the_overview_says_so_instead_of_pretending(catalogue):
    catalogue.sources["repo"].clear()
    overview = service.discover_overview()
    assert overview["repo_missing"] is True and [s["label"] for s in overview["sources"]] == ["flathub"]


def test_the_catalogue_is_built_once_and_again_only_when_something_changed(catalogue, monkeypatch):
    built = []
    real = discover.build_entries
    monkeypatch.setattr(discover, "build_entries", lambda *a, **k: (built.append(1), real(*a, **k))[1])
    service.discover_overview()
    service.discover_search("firefox")
    service.discover_app("org.mozilla.firefox")
    assert len(built) == 1
    catalogue.flathub.write_text(_xml(("org.new.App", "Brand New", "")))
    assert any(e["key"] == "org.new.App" for e in service.discover_search("brand")["entries"]) and len(built) == 2


def test_a_vendor_source_you_add_appears_without_a_restart(catalogue):
    before = service.discover_overview()["total"]
    preferences.save({"vendor_feeds": {"my-tool": {"index": "https://example.com/Packages.gz", "base": "https://example.com/"}}})
    assert service.discover_overview()["total"] == before + 1
    assert service.discover_search("my-tool")["entries"][0]["key"] == "vendor:my-tool"


def test_a_data_file_that_cannot_be_read_costs_only_itself(catalogue):
    catalogue.extra.write_text("<components><component>")
    overview = service.discover_overview()
    assert [s["label"] for s in overview["sources"]] == ["flathub"] and len(overview["problems"]) == 1 and "extra" in overview["problems"][0]
    assert [e["key"] for e in service.discover_search("calculator")["entries"]] == ["org.example.Calc"]


def test_an_unexpected_failure_reading_a_data_file_is_named_and_logged_not_hidden(catalogue, monkeypatch, caplog):
    real = discover.load_index

    def broken(source):
        if source.label == "extra":
            raise KeyError("boom")
        return real(source)

    monkeypatch.setattr(discover, "load_index", broken)
    with caplog.at_level("WARNING"):
        overview = service.discover_overview()
    assert [s["label"] for s in overview["sources"]] == ["flathub"] and "could not be read (KeyError)" in overview["problems"][0]
    assert "boom" not in overview["problems"][0]  # the message shown stays short; the cause is in the log
    assert any(r.exc_info and r.exc_info[0] is KeyError for r in caplog.records)


def test_a_data_file_replaced_while_the_catalogue_is_built_does_not_break_it(catalogue, monkeypatch):
    """A remote's data refresh can replace the file between reading it and building: the catalogue is built from what was read, once."""
    real = discover.build_entries

    def replaced(sources, vendors, **kw):
        catalogue.flathub.unlink()
        return real(sources, vendors, **kw)

    monkeypatch.setattr(discover, "build_entries", replaced)
    overview = service.discover_overview()
    assert overview["problems"] == [] and {s["label"] for s in overview["sources"]} == {"flathub", "extra"}


def test_a_data_file_that_vanished_is_not_a_crash(catalogue):
    catalogue.flathub.unlink()
    assert [s["label"] for s in service.discover_overview()["sources"]] == ["extra"]


def test_one_application_from_two_places_shows_both_ways_to_install_it(catalogue):
    entry = service.discover_app("org.mozilla.firefox")
    assert [s["kind"] for s in entry["sources"]] == ["flatpak", "repo"]
    assert entry["sources"][0] == {"kind": "flatpak", "remote": "flathub", "id": "org.mozilla.firefox", "label": "flathub (Flatpak)", "installed": False}
    assert entry["sources"][1]["package"] == "firefox" and entry["installed"] is False and entry["icon_url"] == ""


def test_what_is_installed_is_marked_on_each_way_of_installing_it(catalogue, monkeypatch):
    monkeypatch.setattr(service, "_installed_marks", lambda: {"flatpak": {"org.mozilla.firefox"}, "pacman": {"editor-pkg"}, "vendor": set()})
    firefox = service.discover_app("org.mozilla.firefox")
    assert [s["installed"] for s in firefox["sources"]] == [True, False] and firefox["installed"] is True
    assert service.discover_app("org.example.Edit")["installed"] is True
    assert service.discover_app("org.example.Calc")["installed"] is False


def test_a_vendor_program_counts_as_installed_when_cygnus_converted_it_or_pacman_has_the_package(catalogue, monkeypatch):
    monkeypatch.setattr(service, "_installed_marks", lambda: {"flatpak": set(), "pacman": {"brave-browser"}, "vendor": {"vivaldi-stable"}})
    by_package = {s["package"]: s["installed"] for e in service.discover_search("", kind="vendor")["entries"] for s in e["sources"] if s["kind"] == "vendor"}
    assert by_package["brave-browser"] is True and by_package["vivaldi-stable"] is True and by_package["opera-stable"] is False


def test_a_vendor_program_that_is_on_flathub_is_one_entry_with_both(catalogue):
    entry = service.discover_app("com.google.Chrome")
    assert [s["kind"] for s in entry["sources"]] == ["flatpak", "vendor"]
    assert entry["sources"][1]["package"] == "google-chrome-stable" and entry["sources"][1]["formats"] == ["deb", "rpm"]
    assert service.discover_app("vendor:google-chrome-beta")["sources"][0]["kind"] == "vendor"  # no Flathub id: listed alone, never folded in


def test_the_icon_is_given_as_an_address_not_a_path(catalogue, tmp_path):
    (tmp_path / "icons" / "64x64").mkdir(parents=True)
    (tmp_path / "icons" / "64x64" / "calc.png").write_bytes(b"png")
    catalogue.flathub.write_text(catalogue.flathub.read_text().replace("<name>Calculator</name>", "<name>Calculator</name><icon type=\"cached\" height=\"64\">calc.png</icon>"))
    assert service.discover_app("org.example.Calc")["icon_url"] == (tmp_path / "icons" / "64x64" / "calc.png").as_uri()


@pytest.mark.parametrize("args", [(None, None, None, None), (5, 5, 5, "x"), ({}, [], (), 1.5), ("x" * 100000, "nonsense", "nonsense", True),
                                  ("firefox", "browsers", "flatpak", -5), ("firefox", "", "", 10**9)])
def test_whatever_the_window_sends_the_search_answers_in_bounds(catalogue, args):
    result = service.discover_search(*args)
    assert isinstance(result["entries"], list) and result["total"] >= 0 and result["offset"] >= 0


def test_a_group_or_source_that_does_not_exist_is_ignored_not_turned_into_an_empty_list(catalogue):
    everything = service.discover_search("")
    assert service.discover_search("", "nonsense", "nonsense")["total"] == everything["total"] > 0
    assert service.discover_search("", ["browsers"], {"kind": "vendor"})["total"] == everything["total"]


def test_searching_with_a_group_and_a_kind_narrows_what_is_shown(catalogue):
    shown = service.discover_search("", "internet", "flatpak")
    assert {e["key"] for e in shown["entries"]} == {"org.mozilla.firefox", "com.google.Chrome", "org.example.Calc"}
    assert all("internet" in e["groups"] and any(s["kind"] == "flatpak" for s in e["sources"]) for e in shown["entries"])
    browsers = service.discover_search("", "browsers", "vendor")  # the vendors' own browsers come with their categories
    assert browsers["entries"] and all(e["key"].startswith("vendor:") for e in browsers["entries"])
    assert {g["id"] for g in service.discover_search("")["groups"]} >= {"internet", "browsers"}


def test_an_application_that_is_gone_from_the_list_is_said_in_words(catalogue):
    for key in ("org.nothing.Here", None, 5, "vendor:../x"):
        with pytest.raises(CygnusError, match="no longer in the list"):
            service.discover_app(key)


def test_what_is_installed_is_asked_of_flatpak_pacman_and_the_registry(monkeypatch):
    service._MARKS.update(at=0.0, value=None)
    app = NS(get_kind=lambda: "APP", get_name=lambda: "org.mozilla.firefox")
    runtime = NS(get_kind=lambda: "RUNTIME", get_name=lambda: "org.freedesktop.Platform")

    class Env:
        Flatpak = NS(RefKind=NS(APP="APP"))

        def installed_refs(self):
            return [(None, app), (None, runtime)]

    from cygnus.core.backends import flatpak as fb

    monkeypatch.setattr(fb, "FlatpakEnv", Env)
    monkeypatch.setattr(packages, "installed_versions", lambda **k: {"bash": "5", "firefox": "157"})
    service.record_package_install("google-chrome-stable", "155-1", origin="converted", source={"file": "/x.deb", "vendor_package": "google-chrome-stable", "vendor_format": "deb"})
    service.record_package_install("local-tool", "1-1", origin="local", source={"file": "/y.pkg.tar.zst"})
    marks = service._installed_marks()
    assert marks["flatpak"] == {"org.mozilla.firefox"} and marks["pacman"] == {"bash", "firefox"} and marks["vendor"] == {"google-chrome-stable"}


def test_what_cannot_be_asked_is_simply_not_marked(monkeypatch):
    service._MARKS.update(at=0.0, value=None)
    from cygnus.core.backends import flatpak as fb

    monkeypatch.setattr(fb, "FlatpakEnv", lambda: (_ for _ in ()).throw(RuntimeError("no libflatpak")))
    monkeypatch.setattr(packages, "installed_versions", lambda **k: (_ for _ in ()).throw(CygnusError("databases unreadable")))
    assert service._installed_marks() == {"flatpak": set(), "pacman": set(), "vendor": set(), "apps": set()}


def test_installed_marks_are_trusted_for_a_few_seconds_only(monkeypatch):
    service._MARKS.update(at=0.0, value=None)
    asked = []
    monkeypatch.setattr(packages, "installed_versions", lambda **k: (asked.append(1), {})[1])
    from cygnus.core.backends import flatpak as fb

    monkeypatch.setattr(fb, "FlatpakEnv", lambda: NS(Flatpak=NS(RefKind=NS(APP="APP")), installed_refs=lambda: []))
    service._installed_marks()
    service._installed_marks()
    assert len(asked) == 1
    service._MARKS["at"] -= service.MARKS_SECONDS + 1
    service._installed_marks()
    assert len(asked) == 2


# -- fetching a vendor's package ---------------------------------------------------------------------------------------------------------
def _release(fmt="deb", sha="ab" * 32, url="https://dl.example.com/pool/app_1.0_amd64.deb"):
    return vendor_feeds.Release("1.0-1", url, sha, 100, fmt)


def _fetch(package="google-chrome-stable", fmt="deb", *, release=None, downloaded=None, lines=None):
    got = {}

    def download(url, folder, *, expected_sha256, progress):
        got.update(url=url, folder=Path(folder), sha=expected_sha256)
        path = Path(folder) / url.rsplit("/", 1)[-1]
        path.write_bytes(b"package")
        return path

    result = service.fetch_vendor_package(package, fmt, (lines if lines is not None else []).append, newest=lambda feed, f: release or _release(f),
                                          download=downloaded or download)
    return result, got


def test_a_vendors_deb_is_downloaded_with_its_published_checksum_and_handed_over_as_a_file(tmp_path):
    lines = []
    result, got = _fetch(lines=lines)
    assert result["ok"] and result["checked"] is True and result["version"] == "1.0-1" and result["format"] == "deb"
    assert got["sha"] == "ab" * 32 and got["url"].startswith("https://")
    assert result["file_url"] == Path(result["path"]).as_uri() and Path(result["path"]).read_bytes() == b"package"
    assert got["folder"] == paths.cache_dir() / "downloads" / "vendor" / "google-chrome-stable"
    assert any("Looking for" in l for l in lines) and any("Downloading app_1.0_amd64.deb" in l for l in lines)


def test_a_download_that_finishes_after_the_page_was_left_is_announced_by_the_window():
    result, _ = _fetch()
    assert "finished downloading" in result["late_attention"] and "google-chrome-stable" in result["late_attention"] and ".deb" in result["late_attention"]
    # choosing the program again in Discover downloads it afresh (the old file is replaced): the notice must send the person to the file instead
    assert result["path"] in result["late_attention"] and "Install page" in result["late_attention"] and "choose it again" not in result["late_attention"]


def test_the_vendors_rpm_has_no_published_checksum_and_the_answer_says_so(tmp_path):
    result, got = _fetch(fmt="rpm", release=_release("rpm", sha=None, url="https://dl.example.com/google-chrome-stable_current_x86_64.rpm"))
    assert result["ok"] and result["checked"] is False and got["sha"] is None


def test_a_package_the_vendor_does_not_publish_as_rpm_is_refused_in_words():
    with pytest.raises(CygnusError, match="publishes no .rpm"):
        _fetch("microsoft-edge-stable", "rpm")


@pytest.mark.parametrize("package,fmt", [("../../etc", "deb"), ("", "deb"), (None, "deb"), (5, "deb"), ("google-chrome-stable", "exe"),
                                         ("google-chrome-stable", None), ("google-chrome-stable; rm -rf /", "deb"), ("-x", "deb")])
def test_what_the_window_sends_is_checked_before_anything_is_fetched(package, fmt):
    with pytest.raises(CygnusError, match="not a program and package format Cygnus knows"):  # this very check, not the unknown-vendor one after it
        service.fetch_vendor_package(package, fmt, lambda line: None, newest=lambda *a: pytest.fail("looked for a version"),
                                     download=lambda *a, **k: pytest.fail("downloaded"))


def test_a_vendor_without_a_source_is_refused():
    with pytest.raises(CygnusError, match="no source for that vendor"):
        service.fetch_vendor_package("unknown-vendor", "deb", lambda line: None)


def test_an_address_that_is_not_https_is_never_downloaded():
    result, got = _fetch(release=_release(url="http://dl.example.com/app.deb"))
    assert result["ok"] is False and "usable download address" in result["error"] and not got


def test_a_second_download_at_the_same_time_is_turned_away():
    assert service._FETCH_LOCK.acquire(blocking=False)
    try:
        result, _ = _fetch()
        assert result["ok"] is False and "still going" in result["error"]
    finally:
        service._FETCH_LOCK.release()
    assert _fetch()[0]["ok"]  # and the lock is free again afterwards


def test_the_lock_is_released_when_the_download_fails():
    def broken(url, folder, **kw):
        raise OSError(28, "No space left on device")

    with pytest.raises(CygnusError, match="could not be saved: No space left"):
        _fetch(downloaded=broken)
    assert service._FETCH_LOCK.acquire(blocking=False)
    service._FETCH_LOCK.release()


def test_an_earlier_download_of_the_same_program_goes_but_other_programs_and_the_updates_folder_stay():
    root = paths.cache_dir() / "downloads"
    (root / "updates" / "dl-1").mkdir(parents=True)
    (root / "updates" / "dl-1" / "keep.deb").write_bytes(b"an update being reviewed")
    _fetch("google-chrome-stable")
    (root / "vendor" / "google-chrome-stable" / "stale.deb").write_bytes(b"old")
    _fetch("brave-browser", release=_release(url="https://dl.example.com/brave.deb"))
    _fetch("google-chrome-stable")
    assert not (root / "vendor" / "google-chrome-stable" / "stale.deb").exists()
    assert (root / "vendor" / "brave-browser" / "brave.deb").exists()
    assert (root / "updates" / "dl-1" / "keep.deb").read_bytes() == b"an update being reviewed"


def test_only_the_three_newest_programs_downloads_are_kept():
    for i, package in enumerate(("google-chrome-stable", "brave-browser", "vivaldi-stable", "opera-stable")):
        _fetch(package, release=_release(url=f"https://dl.example.com/{package}.deb"))
        os.utime(paths.cache_dir() / "downloads" / "vendor" / package, (1000 + i, 1000 + i))
    _fetch("signal-desktop", release=_release(url="https://dl.example.com/signal.deb"))
    kept = sorted(p.name for p in (paths.cache_dir() / "downloads" / "vendor").iterdir())
    assert len(kept) == 3 and "signal-desktop" in kept and "google-chrome-stable" not in kept


def test_a_link_where_a_download_folder_belongs_is_never_followed(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious").write_text("do not delete")
    root = paths.cache_dir() / "downloads" / "vendor"
    root.mkdir(parents=True)
    (root / "google-chrome-stable").symlink_to(victim)
    result, got = _fetch()
    assert result["ok"] and (victim / "precious").read_text() == "do not delete" and got["folder"].is_dir() and not got["folder"].is_symlink()
    (paths.cache_dir() / "downloads" / "vendor").rename(paths.cache_dir() / "downloads" / "moved")
    (paths.cache_dir() / "downloads" / "vendor").symlink_to(victim)
    with pytest.raises(CygnusError, match="not a plain folder"):
        _fetch()
    assert (victim / "precious").read_text() == "do not delete"


def test_the_download_is_not_made_by_the_updates_page_helper(monkeypatch):
    monkeypatch.setattr(service, "_fresh_updates_folder", lambda: pytest.fail("the Updates page's helper deletes earlier downloads"))
    assert _fetch()[0]["ok"]


# -- fetching a maker's own download from its release page --------------------------------------------------------------------------------
PROGRAM = b"program"


def _asset(fmt="appimage", sha="cd" * 32, name="Demo-1.2.3-x86_64.AppImage"):
    from cygnus.core.backends import release_feeds as rf

    return rf.Asset("v1.2.3", name, f"https://github.com/acme/demo/releases/download/v1.2.3/{name}", sha, len(PROGRAM), fmt)


def _fetch_release(key="obsidian", fmt="appimage", *, asset=None, downloaded=None, lines=None):
    got = {}

    def download(url, folder, *, expected_sha256, limit, host_ok, progress):
        got.update(url=url, folder=Path(folder), sha=expected_sha256, limit=limit, host_ok=host_ok)
        path = Path(folder) / url.rsplit("/", 1)[-1]
        path.write_bytes(PROGRAM)
        return path

    result = service.fetch_release_download(key, fmt, (lines if lines is not None else []).append, newest=lambda project, f: asset or _asset(f),
                                            download=downloaded or download)
    return result, got


def test_a_release_file_is_downloaded_from_githubs_hosts_with_its_published_checksum_and_handed_over_as_a_file():
    from cygnus.core.backends import release_feeds as rf

    lines = []
    result, got = _fetch_release(lines=lines)
    assert result["ok"] and result["checked"] is True and result["version"] == "v1.2.3" and result["format"] == "appimage"
    assert result["format_label"] == "AppImage" and result["package"] == "obsidian"
    assert got["sha"] == "cd" * 32 and got["limit"] == len(PROGRAM) and got["host_ok"] is rf.github_host and got["url"].startswith("https://github.com/")
    assert got["limit"] != rf.MAX_FILE  # the size GitHub declares is the limit of the download, not the largest size Cygnus would ever accept
    assert result["file_url"] == Path(result["path"]).as_uri() and Path(result["path"]).read_bytes() == b"program"
    assert got["folder"] == paths.cache_dir() / "downloads" / "release" / "obsidian"  # its own folder, never the vendors' or the updates'
    assert any("Looking for" in l for l in lines) and any("Downloading Demo-1.2.3-x86_64.AppImage" in l for l in lines)
    assert "Install page" in result["late_attention"] and result["path"] in result["late_attention"]
    assert result["late_attention"].startswith("The AppImage finished downloading")  # the window adds the program's name in front
    assert (paths.cache_dir() / "downloads" / "release" / "obsidian").stat().st_mode & 0o777 == 0o700


def test_a_file_without_a_published_checksum_says_it_was_not_checked():
    result, got = _fetch_release(asset=_asset(sha=None))
    assert result["ok"] and result["checked"] is False and got["sha"] is None


def test_only_a_known_program_and_one_of_its_download_kinds_is_fetched():
    for key, fmt in (("nothing", "appimage"), ("../x", "appimage"), (None, "appimage"), (5, "deb"), ("obsidian", "rpm"), ("obsidian", "../x"), ("obsidian", None),
                     ("logseq", "deb"), ("obsidian", "pkg")):
        with pytest.raises(CygnusError, match="not a program and download kind"):
            _fetch_release(key, fmt)


def test_release_and_vendor_downloads_never_share_a_folder():
    a, got_a = _fetch_release("obsidian")
    other = service._vendor_download_folder("obsidian")  # a vendor package that happened to share the name
    assert other == paths.cache_dir() / "downloads" / "vendor" / "obsidian" and other != got_a["folder"]
    assert Path(a["path"]).exists()  # making the vendor's folder did not touch the release file


def test_a_file_that_arrives_in_another_size_than_github_declares_is_thrown_away():
    """Found by review: the declared size was checked and then never used, and a file with no published checksum had nothing else to check it by."""
    def short(url, folder, **kw):
        path = Path(folder) / "partial.AppImage"
        path.write_bytes(PROGRAM[:3])
        return path

    with pytest.raises(CygnusError, match="not the size GitHub says"):
        _fetch_release(asset=_asset(sha=None), downloaded=short)
    assert not (paths.cache_dir() / "downloads" / "release" / "obsidian" / "partial.AppImage").exists()


def _age(folder, seconds):
    import os
    import time

    then = time.time() - seconds
    os.utime(folder, (then, then))


def test_an_earlier_release_download_of_the_same_program_is_replaced_and_old_ones_are_cleared():
    first, _ = _fetch_release("obsidian")
    second, _ = _fetch_release("obsidian")
    assert first["path"] == second["path"] and Path(second["path"]).exists()
    base = paths.cache_dir() / "downloads" / "release"
    for key in ("zettlr", "joplin", "tabby"):
        _fetch_release(key)
        for folder in base.iterdir():
            _age(folder, 7200)  # each download is older than an hour by the time the next one starts
    _fetch_release("cemu")
    assert len([p for p in base.iterdir() if p.is_dir()]) <= 3


def test_a_download_from_the_last_hour_is_never_cleared_whatever_the_count():
    """Found by review: a notice for a download that finished after the person left Discover points at its file; the next download must not delete it."""
    base = paths.cache_dir() / "downloads" / "release"
    first, _ = _fetch_release("obsidian")
    for key in ("zettlr", "joplin", "tabby", "cemu"):
        _fetch_release(key)
    assert len([p for p in base.iterdir() if p.is_dir()]) == 5 and Path(first["path"]).exists()


def test_a_busy_download_slot_says_so_and_a_failure_frees_it():
    assert service._FETCH_LOCK.acquire(blocking=False)
    try:
        result, _ = _fetch_release()
        assert result["ok"] is False and "Another download" in result["error"]
    finally:
        service._FETCH_LOCK.release()

    def broken(project, fmt):
        raise CygnusError("GitHub is limiting how often this connection may ask. Try again in a little while.")

    with pytest.raises(CygnusError, match="limiting"):
        service.fetch_release_download("obsidian", "appimage", lambda line: None, newest=broken)
    assert _fetch_release()[0]["ok"] is True  # the slot was freed by the failure


def test_a_download_that_cannot_be_saved_is_said_in_words():
    def full(url, folder, **kw):
        raise OSError(28, "No space left on device")

    with pytest.raises(CygnusError, match="could not be saved: No space left"):
        _fetch_release(downloaded=full)


def test_the_overview_counts_the_makers_downloads_and_the_search_can_ask_for_them(catalogue):
    overview = service.discover_overview()
    assert overview["releases"] == 24 and overview["vendors"] >= 1
    found = service.discover_search("", "", "release", 0)
    assert found["total"] == 24 and all(any(s["kind"] == "release" for s in e["sources"]) for e in found["entries"])
    assert service.discover_search("", "", "bogus", 0)["total"] == overview["total"]  # an unknown kind asks for everything


def test_a_release_source_is_shown_without_an_installed_mark_and_without_failing(catalogue):
    entry = service.discover_app("release:helium")
    [source] = entry["sources"]
    assert source["kind"] == "release" and source["installed"] is False and source["formats"] == ["appimage", "deb", "archive"]
    assert source["page"] == "https://github.com/imputnet/helium-linux/releases" and entry["installed"] is False
    assert entry["name"] == "Helium" and entry["sources"][0]["label"] == "The maker's own download (GitHub)"


def test_a_release_card_says_installed_when_cygnus_already_manages_a_program_of_that_name(catalogue, monkeypatch):
    """Installing a second copy of an installed AppImage adds another row and replaces the first one's menu entry (helium.desktop, for instance, which the
    default-browser choice depends on): the card must not look like a fresh install."""
    monkeypatch.setattr(service, "_installed_marks", lambda: {"flatpak": set(), "pacman": set(), "vendor": set(), "apps": {"helium"}})
    helium = service.discover_app("release:helium")
    assert helium["sources"][0]["installed"] is True and helium["installed"] is True
    assert service.discover_app("release:tabby")["installed"] is False  # another program is not affected
    monkeypatch.setattr(service, "_installed_marks", lambda: {"flatpak": set(), "pacman": set(), "vendor": set()})  # marks from an older cache lack the key
    assert service.discover_app("release:helium")["installed"] is False


def test_the_programs_cygnus_manages_are_marked_by_their_shown_name(monkeypatch):
    from cygnus.core.registry import db as regdb

    service._MARKS.update(at=0.0, value=None)
    from cygnus.core.backends import flatpak as fb

    monkeypatch.setattr(fb, "FlatpakEnv", lambda: (_ for _ in ()).throw(RuntimeError("no libflatpak")))
    monkeypatch.setattr(packages, "installed_versions", lambda **k: {})
    monkeypatch.setattr(regdb, "list_installations", lambda reg: [{"name": " Helium ", "source": {}}, {"name": "Zettlr", "source": {"made_by": "converted", "vendor_package": "zettlr"}},
                                                                   {"name": None, "source": {}}])
    marks = service._installed_marks()
    assert marks["apps"] == {"helium", "zettlr"} and marks["vendor"] == {"zettlr"}


# -- the AUR search ----------------------------------------------------------------------------------------------------------------------

def test_the_aur_is_searched_with_the_words_tidied_and_the_answer_marks_what_is_installed(monkeypatch):
    from cygnus.core.backends import aur

    asked = []
    monkeypatch.setattr(aur, "search", lambda term, by="name", fetch_json=None, strict=False: (asked.append((term, by, strict)), [
        {"Name": "vlc-git", "Version": "1", "Description": "d", "NumVotes": 1, "Popularity": 1.0, "Maintainer": "m", "URL": "", "OutOfDate": None}])[1])
    monkeypatch.setattr(service, "_installed_marks", lambda: {"pacman": {"vlc-git"}})
    out = service.discover_aur("  vlc   player ")
    assert asked == [("vlc player", "name-desc", True)] and out["query"] == "vlc player"
    assert out["total"] == 1 and out["entries"][0]["installed"] is True


@pytest.mark.parametrize("words", ["", " ", "v", " v ", None, 5, ["vlc"]])
def test_the_aur_is_not_asked_for_less_than_two_characters(monkeypatch, words):
    from cygnus.core.backends import aur

    monkeypatch.setattr(aur, "search", lambda *a, **k: pytest.fail("the AUR must not be asked"))
    with pytest.raises(CygnusError, match="at least two characters"):
        service.discover_aur(words)


def test_very_long_words_are_cut_before_they_are_sent(monkeypatch):
    from cygnus.core.backends import aur

    asked = []
    monkeypatch.setattr(aur, "search", lambda term, by="name", fetch_json=None, strict=False: (asked.append(term), [])[1])
    monkeypatch.setattr(service, "_installed_marks", lambda: {"pacman": set()})
    service.discover_aur("x" * 500)
    assert len(asked[0]) == 100


def test_an_aur_answer_that_is_too_big_and_a_network_failure_are_said_in_words(monkeypatch):
    from cygnus.core.backends import aur
    from cygnus.core.util import http

    def too_big(*a, **k):
        raise http.HttpError("response from https://aur.archlinux.org/x exceeds 8388608 bytes")

    monkeypatch.setattr(aur, "search", too_big)
    with pytest.raises(CygnusError, match="So many AUR packages match"):
        service.discover_aur("ab")

    def down(*a, **k):
        raise http.HttpError("network error for https://aur.archlinux.org/x: down")

    monkeypatch.setattr(aur, "search", down)
    with pytest.raises(CygnusError, match="The AUR could not be asked"):
        service.discover_aur("ab")


def test_the_aur_search_uses_a_large_enough_limit_and_the_real_rpc_address(monkeypatch):
    """The answer for a short word is large: the call must allow it, and go to the AUR's RPC over HTTPS."""
    from cygnus.core.util import http

    seen = []
    monkeypatch.setattr(http, "get_json", lambda url, **kw: (seen.append((url, kw)), {"type": "search", "results": []})[1])
    monkeypatch.setattr(service, "_installed_marks", lambda: {"pacman": set()})
    assert service.discover_aur("vlc") == {"total": 0, "entries": [], "query": "vlc"}
    assert seen == [("https://aur.archlinux.org/rpc/v5/search/vlc?by=name-desc", {"limit": service.AUR_ANSWER_LIMIT})]


# -- the online catalogues in the window's lists ----------------------------------------------------------------------------------------------
IMAGE = {"repo": "acme/tool", "name": "Tool", "summary": "Does tool things.", "categories": ["Utility"], "developer": "Acme"}


def _download_catalogue(*records):
    from cygnus.core import online_catalogues as oc

    oc.refresh("appimagehub", fetch=lambda progress: list(records))


def test_the_lists_include_the_downloaded_community_appimages_and_are_built_again_when_the_download_changes(catalogue):
    assert service.discover_overview()["appimages"] == 0
    _download_catalogue(IMAGE)
    overview = service.discover_overview()
    assert overview["appimages"] == 1
    entry = service.discover_app("appimage:acme/tool")
    assert entry["name"] == "Tool" and entry["sources"][0]["kind"] == "appimage" and entry["sources"][0]["installed"] is False
    _download_catalogue(IMAGE, {**IMAGE, "repo": "acme/two", "name": "Two"})
    assert service.discover_overview()["appimages"] == 2


def test_a_switched_off_catalogue_is_not_listed_and_listing_it_again_needs_no_download(catalogue):
    from cygnus.core import online_catalogues as oc

    _download_catalogue(IMAGE)
    assert service.discover_overview()["appimages"] == 1
    oc.set_on("appimagehub", False)
    assert service.discover_overview()["appimages"] == 0
    with pytest.raises(CygnusError, match="no longer in the list"):
        service.discover_app("appimage:acme/tool")
    oc.set_on("appimagehub", True)
    assert service.discover_overview()["appimages"] == 1


def test_the_overview_says_what_the_online_catalogues_hold_and_how_old_they_are(catalogue):
    rows = service.discover_overview()["catalogues"]
    assert [(r["id"], r["on"], r["count"], r["stale"]) for r in rows] == [("appimagehub", True, 0, True), ("debian", True, 0, True), ("fedora", True, 0, True)]
    _download_catalogue(IMAGE)
    row = service.discover_overview()["catalogues"][0]
    assert (row["count"], row["stale"]) == (1, False) and row["fetched"] > 0


def test_a_community_appimage_is_marked_installed_by_the_name_cygnus_manages_it_under(catalogue, monkeypatch):
    _download_catalogue(IMAGE)
    monkeypatch.setattr(service, "_installed_marks", lambda: {"flatpak": set(), "pacman": set(), "vendor": set(), "apps": {"tool"}})
    assert service.discover_app("appimage:acme/tool")["installed"] is True
    monkeypatch.setattr(service, "_installed_marks", lambda: {"flatpak": set(), "pacman": set(), "vendor": set(), "apps": set()})
    assert service.discover_app("appimage:acme/tool")["installed"] is False


def test_the_search_can_ask_for_the_community_appimages_only(catalogue):
    _download_catalogue(IMAGE)
    found = service.discover_search("", "", "appimage", 0)
    assert [e["key"] for e in found["entries"]] == ["appimage:acme/tool"]
    assert service.discover_search("", "", "evil", 0)["total"] == service.discover_search("", "", "", 0)["total"]  # an unknown kind is no filter


def _fetch_image(repo="acme/tool", *, asset=None, downloaded=None, lines=None):
    got = {}

    def download(url, folder, *, expected_sha256, limit, host_ok, progress):
        got.update(url=url, folder=Path(folder), sha=expected_sha256, limit=limit, host_ok=host_ok)
        path = Path(folder) / url.rsplit("/", 1)[-1]
        path.write_bytes(PROGRAM)
        return path

    def newest(project, fmt):
        got.update(project=project, fmt=fmt)
        return asset or _asset("appimage", name="Tool-x86_64.AppImage")

    result = service.fetch_appimage_download(repo, (lines if lines is not None else []).append, newest=newest, download=downloaded or download)
    return result, got


def test_a_community_appimage_is_downloaded_by_its_project_into_a_folder_of_its_own(catalogue):
    from cygnus.core.backends import release_feeds as rf

    _download_catalogue(IMAGE)
    result, got = _fetch_image()
    assert result["ok"] and result["format"] == "appimage" and result["format_label"] == "AppImage" and result["package"] == "acme/tool"
    assert got["fmt"] == "appimage" and got["project"].owner == "acme" and got["project"].repo == "tool" and got["project"].assets["appimage"] == rf.GENERIC_APPIMAGE
    assert got["host_ok"] is rf.github_host and got["limit"] == len(PROGRAM) and got["folder"] == paths.cache_dir() / "downloads" / "appimage" / "acme+tool"
    assert (paths.cache_dir() / "downloads" / "appimage" / "acme+tool").stat().st_mode & 0o777 == 0o700
    assert result["file_url"] == Path(result["path"]).as_uri() and result["late_attention"].startswith("The AppImage finished downloading")


@pytest.mark.parametrize("repo", ["acme/other", "ACME/TOOL", "acme/tool/", "../x", "acme/..", "", None, 5, ["acme/tool"], "acme--tool", "tool"])
def test_only_a_project_that_is_in_the_lists_on_screen_can_be_fetched(catalogue, repo):
    _download_catalogue(IMAGE)
    with pytest.raises(CygnusError, match="not a program Cygnus lists from AppImageHub"):
        _fetch_image(repo)


def test_a_catalogue_that_is_switched_off_cannot_be_used_to_fetch_either(catalogue):
    from cygnus.core import online_catalogues as oc

    _download_catalogue(IMAGE)
    oc.set_on("appimagehub", False)
    with pytest.raises(CygnusError, match="not a program Cygnus lists from AppImageHub"):
        _fetch_image()


def test_a_community_appimage_that_arrives_in_another_size_is_thrown_away_and_a_busy_slot_says_so(catalogue):
    _download_catalogue(IMAGE)

    def short(url, folder, **kw):
        path = Path(folder) / "partial.AppImage"
        path.write_bytes(PROGRAM[:2])
        return path

    with pytest.raises(CygnusError, match="not the size GitHub says"):
        _fetch_image(downloaded=short)
    assert not (paths.cache_dir() / "downloads" / "appimage" / "acme+tool" / "partial.AppImage").exists()
    assert service._FETCH_LOCK.acquire(blocking=False)
    try:
        assert _fetch_image()[0]["ok"] is False
    finally:
        service._FETCH_LOCK.release()
    assert _fetch_image()[0]["ok"] is True


def test_a_community_download_never_shares_a_folder_with_the_makers_downloads(catalogue):
    _download_catalogue(IMAGE)
    _, got = _fetch_image()
    assert got["folder"].parent.name == "appimage" and got["folder"].parent != paths.cache_dir() / "downloads" / "release"


# -- downloading the online catalogues ----------------------------------------------------------------------------------------------------------
@pytest.fixture
def clock(monkeypatch):
    from cygnus.core import online_catalogues as oc

    hub = oc.BY_ID["appimagehub"]  # the refreshing logic is tested on one catalogue: the real list grows
    monkeypatch.setattr(oc, "CATALOGUES", (hub,))
    monkeypatch.setattr(oc, "BY_ID", {hub.id: hub})
    oc.decide(True)  # the person has been asked and said yes (the tests of the question are below)
    monkeypatch.setattr(service, "CATALOGUE_WAIT_SECONDS", 0.05)  # a second request waits for a running download; the tests do not wait for long
    service._CATALOGUE_TRIED.clear()
    return [1000.0]


def _refresh(force=False, clock_value=1000.0, refresh=None, lines=None):
    return service.catalogue_refresh(force, (lines if lines is not None else []).append, refresh=refresh, now=clock_value)


def test_catalogues_that_are_missing_are_downloaded_and_reported_by_name(clock):
    asked = []
    out = _refresh(refresh=lambda cat_id, progress: asked.append(cat_id) or 5)
    assert asked == ["appimagehub"] and out["refreshed"] == ["appimagehub"] and out["errors"] == {} and out["busy"] is False
    assert [r["id"] for r in out["status"]] == ["appimagehub"]


def test_a_recent_catalogue_is_not_downloaded_again_unless_the_person_pressed_the_button(clock):
    _download_catalogue(IMAGE)
    asked = []
    assert _refresh(refresh=lambda c, p: asked.append(c))["refreshed"] == [] and asked == []
    assert _refresh(True, refresh=lambda c, p: asked.append(c))["refreshed"] == ["appimagehub"] and asked == ["appimagehub"]


def test_a_switched_off_catalogue_is_never_downloaded_not_even_when_forced(clock):
    from cygnus.core import online_catalogues as oc

    oc.set_on("appimagehub", False)
    asked = []
    assert _refresh(True, refresh=lambda c, p: asked.append(c))["refreshed"] == [] and asked == []
    assert _refresh(refresh=lambda c, p: asked.append(c))["refreshed"] == [] and asked == []


def test_a_failed_download_is_reported_in_words_and_is_not_tried_again_at_once(clock):
    def down(cat_id, progress):
        raise CygnusError("The connection failed.")

    out = _refresh(refresh=down, clock_value=1000.0)
    assert out["ok"] is True and out["refreshed"] == [] and out["errors"] == {"appimagehub": "AppImageHub: The connection failed."}
    asked = []
    assert _refresh(refresh=lambda c, p: asked.append(c), clock_value=1000.0 + service.CATALOGUE_RETRY_SECONDS - 1)["refreshed"] == [] and asked == []  # the next opening of the page
    assert _refresh(refresh=lambda c, p: asked.append(c), clock_value=1000.0 + service.CATALOGUE_RETRY_SECONDS)["refreshed"] == ["appimagehub"]
    assert _refresh(True, refresh=lambda c, p: asked.append(c), clock_value=1000.0 + 1)["refreshed"] == ["appimagehub"]  # the button always tries


def test_a_download_that_fails_with_something_unexpected_costs_only_that_catalogue(clock):
    def odd(cat_id, progress):
        raise ValueError("secret detail /home/x")

    out = _refresh(refresh=odd)
    assert out["errors"] == {"appimagehub": "AppImageHub: it could not be updated (ValueError)"} and "secret" not in str(out)


def test_two_downloads_at_once_are_one_download(clock):
    assert service._CATALOGUE_LOCK.acquire(blocking=False)
    try:
        out = _refresh(refresh=lambda c, p: pytest.fail("a second download must not start"))
        assert out["busy"] is True and out["refreshed"] == []
    finally:
        service._CATALOGUE_LOCK.release()
    assert _refresh(refresh=lambda c, p: 1)["busy"] is False  # the slot is free again after a normal run


def test_the_lock_is_freed_after_a_failure(clock):
    _refresh(refresh=lambda c, p: (_ for _ in ()).throw(CygnusError("x")))
    assert service._CATALOGUE_LOCK.acquire(blocking=False)
    service._CATALOGUE_LOCK.release()


@pytest.mark.parametrize("force", [None, "yes", 1, 0, [], "False"])
def test_only_a_real_yes_forces_a_download(clock, force):
    _download_catalogue(IMAGE)
    asked = []
    assert _refresh(force, refresh=lambda c, p: asked.append(c))["refreshed"] == [] and asked == []  # a recent catalogue stays as it is


def test_a_catalogue_is_switched_through_the_window_only_with_a_known_id_and_a_real_boolean():
    assert service.catalogue_set_on("appimagehub", False)["status"][0]["on"] is False
    assert service.catalogue_set_on("appimagehub", True)["status"][0]["on"] is True
    for cat_id, on in (("evil", True), ("appimagehub", "no"), ("appimagehub", 0), (None, True), ("appimagehub", None), (["appimagehub"], False)):
        with pytest.raises(CygnusError, match="not one of the online catalogues"):
            service.catalogue_set_on(cat_id, on)
    assert service.catalogue_set_on("appimagehub", True)["status"][0]["on"] is True  # the refusals changed nothing


# -- Debian's programs in the lists and their download ----------------------------------------------------------------------------------------
DEB = {"package": "demo", "id": "demo", "name": "Demo", "summary": "Does demo things.", "categories": ["Office"], "url": "https://demo.example", "version": "1.0-1",
       "filename": "pool/main/d/demo/demo_1.0-1_amd64.deb", "sha256": "ef" * 32, "size": len(PROGRAM), "depends": ["libc6", "demo-data", "firefox"]}


def _download_debian(*records):
    from cygnus.core import online_catalogues as oc

    oc.refresh("debian", fetch=lambda progress: list(records))


@pytest.fixture
def repositories(monkeypatch):
    """What the repositories here have, and when their lists changed: faked, so no test asks pacman."""
    state = {"names": frozenset({"firefox"}), "stamp": 100.0, "asked": 0}

    def names():
        state["asked"] += 1
        return state["names"]

    monkeypatch.setattr(service, "_repository_names", names)
    monkeypatch.setattr(service, "_repository_lists_stamp", lambda: state["stamp"])
    return state


def test_the_lists_include_debian_programs_that_the_repositories_do_not_have_with_what_they_also_need(catalogue, repositories):
    _download_debian(DEB, {**DEB, "package": "firefox", "id": "firefox", "name": "Firefox ESR"})
    overview = service.discover_overview()
    assert overview["debian"] == 1  # the repositories have "firefox": the Arch version is the one shown
    entry = service.discover_app("debian:demo")
    [src] = entry["sources"]
    assert src["kind"] == "debian" and src["needs"] == ["demo-data"] and src["installed"] is False and src["size"] == len(PROGRAM)
    with pytest.raises(CygnusError, match="no longer in the list"):
        service.discover_app("debian:firefox")


def test_the_lists_are_built_again_when_the_repositories_lists_change_and_not_before(catalogue, repositories):
    _download_debian(DEB)
    assert service.discover_overview()["debian"] == 1 and repositories["asked"] == 1
    service.discover_overview()
    assert repositories["asked"] == 1  # nothing changed: not built again
    repositories["names"] = frozenset({"demo"})
    repositories["stamp"] = 200.0  # pacman refreshed its lists and the repositories now have it
    assert service.discover_overview()["debian"] == 0 and repositories["asked"] == 2


def test_the_repositories_are_not_asked_at_all_when_there_are_no_debian_programs(catalogue, monkeypatch):
    monkeypatch.setattr(service, "_repository_names", lambda: pytest.fail("pacman must not be asked"))
    monkeypatch.setattr(service, "_repository_lists_stamp", lambda: pytest.fail("pacman must not be asked"))
    assert service.discover_overview()["debian"] == 0


def test_a_repository_list_that_cannot_be_read_costs_the_comparison_not_the_page(monkeypatch):
    from cygnus.core import packages

    monkeypatch.setattr(packages, "repo_names", lambda **k: (_ for _ in ()).throw(CygnusError("databases unreadable")))
    assert service._repository_names() == frozenset()
    monkeypatch.setattr(packages, "repo_names", lambda **k: (_ for _ in ()).throw(OSError("gone")))
    assert service._repository_names() == frozenset()
    monkeypatch.setattr(packages, "repo_names", lambda **k: frozenset({"bash"}))
    assert service._repository_names() == frozenset({"bash"})


def test_a_debian_program_is_marked_installed_by_its_package_or_by_the_name_cygnus_manages_it_under(catalogue, repositories, monkeypatch):
    _download_debian(DEB)
    marks = {"flatpak": set(), "pacman": set(), "vendor": set(), "apps": set()}
    monkeypatch.setattr(service, "_installed_marks", lambda: marks)
    assert service.discover_app("debian:demo")["installed"] is False
    marks["pacman"] = {"demo"}
    assert service.discover_app("debian:demo")["installed"] is True
    marks["pacman"], marks["apps"] = set(), {"demo"}
    assert service.discover_app("debian:demo")["installed"] is True


def test_the_search_can_ask_for_debian_programs_only(catalogue, repositories):
    _download_debian(DEB)
    assert [e["key"] for e in service.discover_search("", "", "debian", 0)["entries"]] == ["debian:demo"]


def _fetch_debian(package="demo", *, downloaded=None, lines=None):
    got = {}

    def download(url, folder, *, expected_sha256, limit, host_ok, progress):
        got.update(url=url, folder=Path(folder), sha=expected_sha256, limit=limit, host_ok=host_ok)
        path = Path(folder) / url.rsplit("/", 1)[-1]
        path.write_bytes(PROGRAM)
        return path

    return service.fetch_debian_download(package, (lines if lines is not None else []).append, download=downloaded or download), got


def test_a_debian_package_is_downloaded_from_the_debian_mirror_with_the_lists_checksum_and_size_into_a_folder_of_its_own(catalogue, repositories):
    from cygnus.core.backends import debian_index as di

    _download_debian(DEB)
    lines = []
    result, got = _fetch_debian(lines=lines)
    assert result["ok"] and result["checked"] is True and result["version"] == "1.0-1" and result["format"] == "deb" and result["format_label"] == ".deb"
    assert result["package"] == "demo" and result["needs"] == ["demo-data"]
    assert got["url"] == "https://deb.debian.org/debian/pool/main/d/demo/demo_1.0-1_amd64.deb" and got["sha"] == "ef" * 32 and got["limit"] == len(PROGRAM)
    assert got["host_ok"] is di.host_ok and got["folder"] == paths.cache_dir() / "downloads" / "debian" / "demo"
    assert (paths.cache_dir() / "downloads" / "debian" / "demo").stat().st_mode & 0o777 == 0o700
    assert any("Downloading demo_1.0-1_amd64.deb" in l for l in lines)
    assert result["file_url"] == Path(result["path"]).as_uri() and result["late_attention"].startswith("Debian's package finished downloading")


@pytest.mark.parametrize("package", ["other", "firefox", "../demo", "demo/", "DEMO", "", None, 5, ["demo"], "debian:demo"])
def test_only_a_debian_program_that_is_in_the_lists_on_screen_can_be_fetched(catalogue, repositories, package):
    _download_debian(DEB, {**DEB, "package": "firefox", "id": "firefox", "name": "Firefox ESR"})
    with pytest.raises(CygnusError, match="not a program Cygnus lists from Debian"):
        _fetch_debian(package)


def test_a_switched_off_debian_catalogue_cannot_be_used_to_fetch(catalogue, repositories):
    from cygnus.core import online_catalogues as oc

    _download_debian(DEB)
    oc.set_on("debian", False)
    with pytest.raises(CygnusError, match="not a program Cygnus lists from Debian"):
        _fetch_debian()


def test_a_debian_package_of_the_wrong_size_is_thrown_away_naming_debians_list_and_a_busy_slot_says_so(catalogue, repositories):
    _download_debian(DEB)

    def short(url, folder, **kw):
        path = Path(folder) / "partial.deb"
        path.write_bytes(PROGRAM[:2])
        return path

    with pytest.raises(CygnusError, match="not the size Debian's package list says"):
        _fetch_debian(downloaded=short)
    assert not (paths.cache_dir() / "downloads" / "debian" / "demo" / "partial.deb").exists()
    assert service._FETCH_LOCK.acquire(blocking=False)
    try:
        assert _fetch_debian()[0]["ok"] is False
    finally:
        service._FETCH_LOCK.release()
    assert _fetch_debian()[0]["ok"] is True


def test_a_debian_download_that_cannot_be_saved_is_said_in_words_and_frees_the_slot(catalogue, repositories):
    _download_debian(DEB)

    def full(url, folder, **kw):
        raise OSError(28, "No space left on device")

    with pytest.raises(CygnusError, match="could not be saved: No space left"):
        _fetch_debian(downloaded=full)
    assert _fetch_debian()[0]["ok"] is True


def test_a_debian_package_and_a_vendors_package_of_one_name_never_share_a_folder(catalogue, repositories):
    _download_debian(DEB)
    _, got = _fetch_debian()
    assert got["folder"].parent.name == "debian" and got["folder"] != service._vendor_download_folder("demo")


# -- Fedora's programs in the lists and their download ------------------------------------------------------------------------------------------
RPM = {"package": "demo", "id": "demo", "name": "Demo", "summary": "Does demo things.", "categories": ["Office"], "url": "https://demo.example", "release": 44,
       "version": "1.0-1.fc44", "location": "Packages/d/demo-1.0-1.fc44.x86_64.rpm", "sha256": "12" * 32, "size": len(PROGRAM), "depends": ["demo-data", "bash"]}


def _download_fedora(*records):
    from cygnus.core import online_catalogues as oc

    oc.refresh("fedora", fetch=lambda progress: list(records))


def test_the_lists_include_fedora_programs_that_the_repositories_do_not_have_with_what_they_also_need(catalogue, repositories):
    repositories["names"] = frozenset({"bash"})
    _download_fedora(RPM, {**RPM, "package": "bash", "id": "bash", "name": "Bash Terminal"})
    assert service.discover_overview()["fedora"] == 1  # the repositories have "bash": the Arch version is the one shown
    [src] = service.discover_app("fedora:demo")["sources"]
    assert src["kind"] == "fedora" and src["needs"] == ["demo-data"] and src["installed"] is False and src["release"] == 44
    assert [e["key"] for e in service.discover_search("", "", "fedora", 0)["entries"]] == ["fedora:demo"]


def test_the_repositories_are_asked_when_there_are_only_fedora_programs_too(catalogue, repositories):
    _download_fedora(RPM)
    assert service.discover_overview()["fedora"] == 1 and repositories["asked"] == 1


def _fetch_fedora(package="demo", *, downloaded=None):
    got = {}

    def download(url, folder, *, expected_sha256, limit, host_ok, progress):
        got.update(url=url, folder=Path(folder), sha=expected_sha256, limit=limit, host_ok=host_ok)
        path = Path(folder) / url.rsplit("/", 1)[-1]
        path.write_bytes(PROGRAM)
        return path

    return service.fetch_fedora_download(package, lambda line: None, download=downloaded or download), got


def test_a_fedora_package_is_downloaded_from_fedoras_mirror_with_the_lists_checksum_and_size_into_a_folder_of_its_own(catalogue, repositories):
    from cygnus.core.backends import fedora_index as fi

    _download_fedora(RPM)
    result, got = _fetch_fedora()
    assert result["ok"] and result["format"] == "rpm" and result["format_label"] == ".rpm" and result["checked"] is True and result["version"] == "1.0-1.fc44"
    assert got["url"] == "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/os/Packages/d/demo-1.0-1.fc44.x86_64.rpm"
    assert got["sha"] == "12" * 32 and got["limit"] == len(PROGRAM) and got["host_ok"] is fi.host_ok and got["folder"] == paths.cache_dir() / "downloads" / "fedora" / "demo"
    assert result["late_attention"].startswith("Fedora's package finished downloading")


@pytest.mark.parametrize("package", ["other", "bash", "../demo", "DEMO", "", None, 5, ["demo"], "fedora:demo"])
def test_only_a_fedora_program_that_is_in_the_lists_on_screen_can_be_fetched(catalogue, repositories, package):
    repositories["names"] = frozenset({"bash"})
    _download_fedora(RPM, {**RPM, "package": "bash", "id": "bash", "name": "Bash Terminal"})
    with pytest.raises(CygnusError, match="not a program Cygnus lists from Fedora"):
        _fetch_fedora(package)


def test_a_fedora_package_of_the_wrong_size_is_thrown_away_naming_fedoras_list(catalogue, repositories):
    _download_fedora(RPM)

    def short(url, folder, **kw):
        path = Path(folder) / "partial.rpm"
        path.write_bytes(PROGRAM[:2])
        return path

    with pytest.raises(CygnusError, match="not the size Fedora's package list says"):
        _fetch_fedora(downloaded=short)
    assert not (paths.cache_dir() / "downloads" / "fedora" / "demo" / "partial.rpm").exists()


def test_a_program_listed_from_debian_cannot_be_fetched_as_fedora_and_the_other_way_round(catalogue, repositories):
    _download_debian({**DEB, "package": "debonly", "id": "debonly", "name": "Debonly"})
    _download_fedora({**RPM, "package": "rpmonly", "id": "rpmonly", "name": "Rpmonly"})
    with pytest.raises(CygnusError, match="not a program Cygnus lists from Fedora"):
        _fetch_fedora("debonly")
    with pytest.raises(CygnusError, match="not a program Cygnus lists from Debian"):
        _fetch_debian("rpmonly")
    assert _fetch_fedora("rpmonly")[0]["ok"] and _fetch_debian("debonly")[0]["ok"]


def test_the_same_program_in_debian_and_fedora_is_listed_once_from_debian(catalogue, repositories):
    _download_debian(DEB)
    _download_fedora(RPM)  # the same name: "Demo"
    overview = service.discover_overview()
    assert overview["debian"] == 1 and overview["fedora"] == 0


def test_nothing_is_downloaded_by_itself_before_the_person_has_been_asked_and_the_overview_says_so(catalogue, monkeypatch):
    from cygnus.core import online_catalogues as oc

    service._CATALOGUE_TRIED.clear()
    assert service.discover_overview()["catalogues_decided"] is False
    asked = []
    out = service.catalogue_refresh(False, lambda line: None, refresh=lambda c, p: asked.append(c), now=1000.0)
    assert out["refreshed"] == [] and out["errors"] == {} and asked == [] and oc.decided() is False


def test_pressing_the_button_to_download_is_the_answer_yes(catalogue):
    from cygnus.core import online_catalogues as oc

    service._CATALOGUE_TRIED.clear()
    asked = []
    service.catalogue_refresh(True, lambda line: None, refresh=lambda c, p: asked.append(c), now=1000.0)
    assert oc.decided() is True and asked == ["appimagehub", "debian", "fedora"] and service.discover_overview()["catalogues_decided"] is True


def test_the_question_is_answered_through_the_window_with_a_real_yes_or_no(catalogue):
    from cygnus.core import online_catalogues as oc

    assert service.catalogue_decide(False)["decided"] is True and [r["on"] for r in service.catalogue_decide(False)["status"]] == [False, False, False]
    assert service.catalogue_decide(True)["decided"] is True and [r["on"] for r in oc.status()] == [False, False, False]  # already answered for these: a yes later changes nothing
    service.catalogue_set_on("appimagehub", True)
    assert [r["on"] for r in oc.status()] == [True, False, False]
    for bad in ("yes", 1, 0, None, [], "True"):
        with pytest.raises(CygnusError, match="not an answer"):
            service.catalogue_decide(bad)
    assert [r["on"] for r in oc.status()] == [True, False, False]  # the refusals changed nothing


def test_a_community_appimage_whose_repository_is_named_like_an_arch_package_is_not_listed(catalogue, repositories):
    repositories["names"] = frozenset({"anki"})
    _download_catalogue({**IMAGE, "repo": "ankitects/anki", "name": "Anki Desktop"}, IMAGE)
    keys = {e["key"] for e in service.discover_search("", "", "appimage", 0)["entries"]}
    assert keys == {"appimage:acme/tool"}  # the Arch version is the one to use
    repositories["names"], repositories["stamp"] = frozenset(), 2.0  # the repositories lose it: listed again
    assert {e["key"] for e in service.discover_search("", "", "appimage", 0)["entries"]} == {"appimage:acme/tool", "appimage:ankitects/anki"}


def test_the_catalogue_files_are_read_only_when_the_lists_are_built_not_at_every_search(catalogue, repositories, monkeypatch):
    """Reading and re-checking thousands of records at every keystroke made each search cost about 70 ms more."""
    from cygnus.core import online_catalogues as oc

    _download_catalogue(IMAGE)
    _download_debian(DEB)
    service.discover_overview()  # built once
    monkeypatch.setattr(oc, "records", lambda cat_id: pytest.fail("the catalogue files must not be read again while nothing changed"))
    for _ in range(3):
        assert service.discover_search("", "", "debian", 0)["total"] == 1
        assert service.discover_app("appimage:acme/tool")["name"] == "Tool"


def test_a_catalogue_that_arrives_by_name_hint_is_not_called_installed_as_a_fact(catalogue, repositories, monkeypatch):
    _download_catalogue(IMAGE)
    _download_debian(DEB)
    marks = {"flatpak": set(), "pacman": set(), "vendor": set(), "apps": {"tool", "demo"}}
    monkeypatch.setattr(service, "_installed_marks", lambda: marks)
    image, debian = service.discover_app("appimage:acme/tool"), service.discover_app("debian:demo")
    assert image["installed"] is True and image["installed_hint"] is True and image["sources"][0]["hint"] is True  # only a program of that name is installed
    assert debian["installed_hint"] is True
    marks["pacman"] = {"demo"}  # pacman has the very package: that is a fact
    assert service.discover_app("debian:demo")["installed_hint"] is False
    marks["apps"] = set()
    assert service.discover_app("appimage:acme/tool")["installed"] is False and service.discover_app("appimage:acme/tool")["installed_hint"] is False


@pytest.mark.parametrize("answer, message", [({"type": "error", "error": "Too many package results."}, "So many AUR packages match"),
                                             ({"type": "error", "error": "Incorrect by field specified."}, "AUR error: Incorrect by field"),
                                             (["not", "an", "object"], "not in a form Cygnus understands"), (None, "not in a form Cygnus understands")])
def test_an_aur_answer_that_is_an_error_or_not_an_object_is_said_in_words_and_not_as_nothing_found(monkeypatch, answer, message):
    from cygnus.core.util import http

    monkeypatch.setattr(http, "get_json", lambda url, **kw: answer)
    with pytest.raises(CygnusError, match=message):
        service.discover_aur("vlc")


def test_two_projects_whose_names_differ_only_where_the_dashes_fall_never_share_a_folder(catalogue):
    _download_catalogue({**IMAGE, "repo": "a--b/c", "name": "First"}, {**IMAGE, "repo": "a/b--c", "name": "Second"})
    first, got_first = _fetch_image("a--b/c")
    second, got_second = _fetch_image("a/b--c")
    assert got_first["folder"] != got_second["folder"] and Path(first["path"]).exists() and Path(second["path"]).exists()


def test_pacmans_configuration_is_read_once_a_minute_not_at_every_search(monkeypatch):
    import time

    from cygnus.core import packages
    from cygnus.core.backends import pacman as pm

    reads = []
    monkeypatch.setattr(pm, "read_config", lambda: (reads.append(1), "config")[1])
    monkeypatch.setattr(packages, "lists_age", lambda config: {"refreshed": 123.0})
    service._PACMAN_CONFIG.update(at=-1e9, value=None)
    assert [service._repository_lists_stamp() for _ in range(5)] == [123.0] * 5 and len(reads) == 1
    service._PACMAN_CONFIG["at"] = time.monotonic() - service.PACMAN_CONFIG_SECONDS - 1
    service._repository_lists_stamp()
    assert len(reads) == 2  # a minute later it is read again


def test_a_pacman_configuration_that_cannot_be_read_gives_no_stamp_not_an_error(monkeypatch):
    from cygnus.core.backends import pacman as pm

    monkeypatch.setattr(pm, "read_config", lambda: (_ for _ in ()).throw(CygnusError("no pacman.conf")))
    service._PACMAN_CONFIG.update(at=-1e9, value=None)
    assert service._repository_lists_stamp() is None


def test_a_second_request_waits_for_the_download_that_is_running_and_then_downloads_only_what_is_still_missing(clock, monkeypatch):
    import threading

    from cygnus.core import online_catalogues as oc

    monkeypatch.setattr(service, "CATALOGUE_WAIT_SECONDS", 20)
    started, release = threading.Event(), threading.Event()
    asked = []

    def slow(cat_id, progress):
        asked.append(cat_id)
        started.set()
        release.wait(10)
        oc.refresh(cat_id, fetch=lambda p: [{"repo": "a/b", "name": "B", "summary": "", "categories": [], "developer": ""}])

    first = threading.Thread(target=lambda: service.catalogue_refresh(False, lambda l: None, refresh=slow, now=1000.0))
    first.start()
    assert started.wait(10)
    second_result = []
    second = threading.Thread(target=lambda: second_result.append(service.catalogue_refresh(False, lambda l: None, refresh=lambda c, p: asked.append(("second", c)), now=1000.0)))
    second.start()
    second.join(0.3)
    assert second.is_alive()  # it is waiting, not returning "busy"
    release.set()
    first.join(10)
    second.join(10)
    assert asked == ["appimagehub"] and second_result[0]["busy"] is False and second_result[0]["refreshed"] == []  # nothing was missing any more


def test_the_window_asks_without_waiting_and_the_terminal_waits(clock, monkeypatch):
    """The window has a handful of worker threads and must not park one for a quarter of an hour behind a running download: it is told "busy" at once
    and asks again by itself."""
    import time
    from pathlib import Path as P

    assert service._CATALOGUE_LOCK.acquire(blocking=False)
    try:
        monkeypatch.setattr(service, "CATALOGUE_WAIT_SECONDS", 30)
        started = time.monotonic()
        out = service.catalogue_refresh(False, lambda l: None, refresh=lambda c, p: pytest.fail("busy"), now=1000.0, wait=0)
        assert out["busy"] is True and time.monotonic() - started < 1
    finally:
        service._CATALOGUE_LOCK.release()
    source = (P(service.__file__).parent / "bridge.py").read_text()
    assert "service.catalogue_refresh(force, progress, wait=0)" in source  # the window's slot never waits
