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
