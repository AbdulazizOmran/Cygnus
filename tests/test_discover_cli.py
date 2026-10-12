"""`cygnus discover …`: reads the application lists and prints; installs nothing, downloads nothing."""

import json

import pytest

from cygnus.cli import main as cli
from cygnus.core import discover
from cygnus.gui import service


@pytest.fixture(autouse=True)
def catalogue(tmp_path, monkeypatch):
    discover._MEMORY.clear()
    service._CATALOGUE.update(signature="", entries=[], sources=[], problems=[], repo_missing=True)
    xml = tmp_path / "flathub.xml"
    xml.write_text('<components origin="o">'
                   '<component><id>org.mozilla.firefox</id><name>Firefox</name><summary>Fast web browser</summary><developer_name>Mozilla</developer_name>'
                   '<url type="homepage">https://www.firefox.com/</url><categories><category>WebBrowser</category></categories>'
                   '<description><p>Browse the web.</p></description></component>'
                   '<component><id>org.example.Calc</id><name>Calculator</name><summary>Add numbers</summary></component></components>')
    extra = tmp_path / "extra.xml"
    extra.write_text('<components origin="o"><component><id>org.mozilla.firefox</id><name>Firefox</name><pkgname>firefox</pkgname></component></components>')
    monkeypatch.setattr(discover, "flatpak_sources", lambda *a, **k: [discover.Source("flatpak", "flathub", xml, tmp_path / "icons", "flathub")])
    monkeypatch.setattr(discover, "repo_sources", lambda *a, **k: [discover.Source("repo", "extra", extra, tmp_path / "icons", "")])
    monkeypatch.setattr(service, "_installed_marks", lambda: {"flatpak": set(), "pacman": {"firefox"}, "vendor": set()})
    monkeypatch.setattr(service, "_repository_names", lambda: frozenset())  # no test asks the real pacman databases
    monkeypatch.setattr(service, "_repository_lists_stamp", lambda: 1.0)


def test_search_lists_the_matches_with_the_ways_to_get_each_and_what_is_installed(capsys):
    assert cli.main(["discover", "search", "web", "browser"]) == 0
    out = capsys.readouterr().out
    assert "org.mozilla.firefox" in out and "Firefox" in out and "[flatpak, repo]" in out and "installed" in out
    assert "Calculator" not in out


def test_an_empty_search_lists_everything_and_a_miss_says_so(capsys):
    assert cli.main(["discover", "search"]) == 0
    assert "Calculator" in capsys.readouterr().out
    assert cli.main(["discover", "search", "zzzzz"]) == 0
    assert "nothing matches" in capsys.readouterr().out


def test_the_filters_narrow_the_list(capsys):
    assert cli.main(["discover", "search", "--source", "repo"]) == 0
    out = capsys.readouterr().out
    assert "Firefox" in out and "Calculator" not in out
    assert cli.main(["discover", "search", "--group", "browsers"]) == 0
    assert "Calculator" not in capsys.readouterr().out
    assert cli.main(["discover", "search", "--group", "nonsense"]) == 2
    assert "unknown kind of program" in capsys.readouterr().err


def test_json_output_is_the_same_thing_the_window_gets(capsys):
    assert cli.main(["discover", "search", "firefox", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["total"] >= 1 and result["entries"][0]["key"] == "org.mozilla.firefox" and result["entries"][0]["installed"] is True


def _fake_pages(monkeypatch, total):
    asked = []

    def fake(query="", group="", kind="", offset=0):
        asked.append(offset)
        shown = [{"key": f"k{i}", "name": f"N{i}", "sources": [{"kind": "flatpak"}], "installed": False} for i in range(offset, min(total, offset + 40))]
        return {"total": total, "offset": offset, "entries": shown, "groups": []}
    monkeypatch.setattr(service, "discover_search", fake)
    return asked


def test_the_next_page_hint_names_the_right_page_and_stops_at_the_last(capsys, monkeypatch):
    asked = _fake_pages(monkeypatch, 90)
    assert cli.main(["discover", "search", "--page", "2"]) == 0
    assert "90 in all; --page 3 shows the next ones" in capsys.readouterr().out and asked == [40]
    assert cli.main(["discover", "search", "--page", "3"]) == 0
    assert "--page" not in capsys.readouterr().out and asked[-1] == 80  # the last page has no hint


@pytest.mark.parametrize("page", ["0", "-3"])
def test_a_page_below_one_is_the_first_page(capsys, monkeypatch, page):
    asked = _fake_pages(monkeypatch, 90)
    assert cli.main(["discover", "search", "--page", page]) == 0
    assert asked == [0] and "--page 2 shows the next ones" in capsys.readouterr().out


def test_a_page_past_the_end_says_how_many_pages_there_are(capsys, monkeypatch):
    _fake_pages(monkeypatch, 90)
    assert cli.main(["discover", "search", "--page", "10"]) == 0
    out = capsys.readouterr().out
    assert "there are 90 matches on 3 pages; --page 3 is the last" in out and "nothing matches" not in out


def test_paging_says_how_to_see_the_next_page(capsys, monkeypatch):
    assert cli.main(["discover", "search", "--page", "1"]) == 0
    out = capsys.readouterr().out
    assert "in all" not in out  # two programs fit on one page: nothing to page through
    monkeypatch.setattr(service, "discover_search", lambda *a: {"total": 90, "offset": 0, "entries": [
        {"key": "k", "name": "N", "sources": [{"kind": "flatpak"}], "installed": False}], "groups": []})
    assert cli.main(["discover", "search"]) == 0
    assert "90 in all; --page 2 shows the next ones" in capsys.readouterr().out


def test_info_shows_each_way_with_the_command_that_does_it(capsys):
    assert cli.main(["discover", "info", "org.mozilla.firefox"]) == 0
    out = capsys.readouterr().out
    assert "Firefox  (Mozilla)" in out and "website: https://www.firefox.com/" in out and "Browse the web." in out
    assert "cygnus install flathub:org.mozilla.firefox" in out and "cygnus packages install firefox  [installed]" in out


def test_info_for_something_not_in_the_list_is_explained(capsys):
    assert cli.main(["discover", "info", "org.nothing.Here"]) == 2
    assert "no longer in the list" in capsys.readouterr().err


def test_the_search_can_ask_for_the_makers_own_downloads(capsys):
    assert cli.main(["discover", "search", "--source", "release"]) == 0
    out = capsys.readouterr().out
    assert "release:helium" in out and "[release]" in out and "Calculator" not in out and "Firefox" not in out


def test_info_for_a_makers_download_names_the_kinds_and_the_page_and_downloads_nothing(capsys, monkeypatch):
    from cygnus.core.util import http

    monkeypatch.setattr(http, "get", lambda *a, **k: pytest.fail("the terminal command must not ask the network"))
    monkeypatch.setattr(http, "download", lambda *a, **k: pytest.fail("the terminal command must not download"))
    assert cli.main(["discover", "info", "release:helium"]) == 0
    out = capsys.readouterr().out
    assert "The maker's own download (AppImage / .deb / archive)" in out and "https://github.com/imputnet/helium-linux/releases" in out
    assert "cygnus install FILE" in out and "[installed]" not in out
    assert cli.main(["discover", "info", "release:helium", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"][0]["kind"] == "release"


# -- the online catalogues ----------------------------------------------------------------------------------------------------------------
IMAGE = {"repo": "acme/tool", "name": "Tool", "summary": "Does tool things.", "categories": ["Utility"], "developer": "Acme"}


@pytest.fixture(autouse=True)
def only_the_hub(monkeypatch):
    from cygnus.core import online_catalogues as oc

    hub = oc.BY_ID["appimagehub"]  # these commands are tested on one catalogue: the real list grows
    monkeypatch.setattr(oc, "CATALOGUES", (hub,))
    monkeypatch.setattr(oc, "BY_ID", {hub.id: hub})


def _download(*records):
    from cygnus.core import online_catalogues as oc

    oc.refresh("appimagehub", fetch=lambda progress: list(records or [IMAGE]))


def test_the_search_can_ask_for_the_community_appimages_and_info_says_nobody_checked_them(capsys, monkeypatch):
    from cygnus.core.util import http

    _download()
    monkeypatch.setattr(http, "get", lambda *a, **k: pytest.fail("the terminal command must not ask the network"))
    assert cli.main(["discover", "search", "--source", "appimage"]) == 0
    out = capsys.readouterr().out
    assert "appimage:acme/tool" in out and "[appimage]" in out and "Firefox" not in out
    assert cli.main(["discover", "info", "appimage:acme/tool"]) == 0
    out = capsys.readouterr().out
    assert "A community listing (AppImageHub), not checked by Cygnus" in out and "https://github.com/acme/tool/releases" in out and "cygnus install FILE" in out


def test_the_catalogues_are_listed_with_their_state_and_age(capsys):
    assert cli.main(["discover", "catalogues"]) == 0
    out = capsys.readouterr().out
    assert "appimagehub" in out and "off" not in out.split("appimagehub")[1][:12] and "not downloaded yet" in out and "about 2 MB" in out
    _download()
    assert cli.main(["discover", "catalogues"]) == 0
    assert "1 programs  downloaded today" in capsys.readouterr().out
    assert cli.main(["discover", "catalogues", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["id"] == "appimagehub" and row["count"] == 1 and row["on"] is True


def test_a_catalogue_is_switched_off_and_on_from_the_terminal(capsys):
    assert cli.main(["discover", "catalogue", "appimagehub", "off"]) == 0
    assert "appimagehub is now off" in capsys.readouterr().out
    assert cli.main(["discover", "catalogues"]) == 0
    assert " off " in capsys.readouterr().out
    assert cli.main(["discover", "catalogue", "appimagehub", "on"]) == 0
    assert "now on" in capsys.readouterr().out
    assert cli.main(["discover", "catalogue", "evil", "on"]) == 2
    assert "not one of the online catalogues" in capsys.readouterr().err


def test_update_downloads_what_is_missing_and_says_so_and_a_recent_one_is_left_alone(capsys, monkeypatch):
    from cygnus.core import online_catalogues as oc

    service._CATALOGUE_TRIED.clear()
    asked = []
    real = oc.refresh
    monkeypatch.setattr(oc, "refresh", lambda cat_id, progress=None, **k: (asked.append(cat_id), real(cat_id, fetch=lambda p: [IMAGE]))[1])
    assert cli.main(["discover", "update"]) == 0
    out = capsys.readouterr().out
    assert asked == ["appimagehub"] and "appimagehub: 1 programs" in out
    assert cli.main(["discover", "update"]) == 0
    assert asked == ["appimagehub"] and "are recent" in capsys.readouterr().out
    assert cli.main(["discover", "update", "--all"]) == 0
    assert asked == ["appimagehub", "appimagehub"]


def test_a_failed_update_says_why_and_exits_with_an_error(capsys, monkeypatch):
    from cygnus.core import online_catalogues as oc
    from cygnus.core.errors import CygnusError

    service._CATALOGUE_TRIED.clear()
    monkeypatch.setattr(oc, "refresh", lambda *a, **k: (_ for _ in ()).throw(CygnusError("The connection failed.")))
    assert cli.main(["discover", "update"]) == 1
    assert "AppImageHub: The connection failed." in capsys.readouterr().err


def test_info_for_a_debian_program_says_it_may_not_work_and_names_what_debian_also_needs(capsys, monkeypatch):
    from cygnus.core import online_catalogues as oc
    from cygnus.core.backends import debian_index
    from cygnus.core.util import http

    both = {"appimagehub": oc.BY_ID["appimagehub"], "debian": oc.Catalogue("debian", "Debian", "Programs from Debian.", "1 MB", debian_index)}
    monkeypatch.setattr(oc, "CATALOGUES", tuple(both.values()))
    monkeypatch.setattr(oc, "BY_ID", both)
    monkeypatch.setattr(service, "_repository_names", lambda: frozenset())
    monkeypatch.setattr(service, "_repository_lists_stamp", lambda: 1.0)
    monkeypatch.setattr(http, "get", lambda *a, **k: pytest.fail("the terminal command must not ask the network"))
    deb = {"package": "demo", "id": "demo", "name": "Demo", "summary": "Does demos.", "categories": ["Office"], "url": "", "version": "1.0-1",
           "filename": "pool/main/d/demo/demo_1.0-1_amd64.deb", "sha256": "ab" * 32, "size": 10, "depends": ["libc6", "demo-data"]}
    oc.refresh("debian", fetch=lambda progress: [deb])
    assert cli.main(["discover", "search", "--source", "debian"]) == 0
    assert "debian:demo" in capsys.readouterr().out
    assert cli.main(["discover", "info", "debian:demo"]) == 0
    out = capsys.readouterr().out
    assert "Debian's own package, converted by Cygnus (it may not work: it was built for Debian; Debian also lists as needed: demo-data)" in out and "cygnus install FILE" in out


def test_the_listing_says_nothing_is_downloaded_until_you_say_so_and_update_is_the_answer_yes(capsys, monkeypatch):
    from cygnus.core import online_catalogues as oc

    assert cli.main(["discover", "catalogues"]) == 0
    assert "Nothing is downloaded until you say so" in capsys.readouterr().out
    service._CATALOGUE_TRIED.clear()
    real = oc.refresh
    monkeypatch.setattr(oc, "refresh", lambda cat_id, progress=None, **k: real(cat_id, fetch=lambda p: [IMAGE]))
    assert oc.decided() is False and cli.main(["discover", "update"]) == 0
    assert oc.decided() is True and "appimagehub: 1 programs" in capsys.readouterr().out
    assert cli.main(["discover", "catalogues"]) == 0
    assert "Nothing is downloaded until you say so" not in capsys.readouterr().out
