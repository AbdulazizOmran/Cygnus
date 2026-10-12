"""Discover driven live in an offscreen window (backend faked): the list fills in pages, typing waits before it searches, the filters ask for
the right thing, and each way to get a program hands over to the page that already does that job, installing nothing itself."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
DRIVE = Path(__file__).with_name("gui_discover_drive.py")


@pytest.fixture(scope="module")
def drive(tmp_path_factory):
    home = tmp_path_factory.mktemp("discover-drive")  # the window reads its registry and settings from here, never from the real ones
    env = {**os.environ, **{var: str(home / sub) for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"),
                                                                    ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"))}}
    proc = subprocess.run([sys.executable, str(DRIVE)], capture_output=True, text=True, timeout=200, env=env)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert proc.returncode == 0 and lines, (proc.stdout + proc.stderr)[-3000:]
    return json.loads(lines[-1])


def test_the_list_fills_with_the_first_page_and_says_how_many_there_are(drive):
    start = drive["snaps"]["start"]
    assert start["title"] == "Discover" and start["values"][:3] == [40, 95, 3]
    assert drive["searches"][0] == ["", "", "", 0]


def test_the_count_reads_as_plain_english_with_no_translation_loaded(drive):
    """Qt's qsTr("%n program(s)") only fills in the number without a translation, which showed "93 program(s)": the sentence is chosen by hand."""
    assert drive["snaps"]["start"]["count_text"] == ["95 programs"]
    assert drive["snaps"]["typed"]["count_text"] == ["1 program"]
    assert all("(s)" not in t for snap in drive["snaps"].values() for t in snap["count_text"])


def test_the_note_about_missing_repository_data_is_shown_when_it_is_missing(drive):
    assert drive["snaps"]["start"]["values"][3] is True and drive["snaps"]["start"]["visible"]["repoMissing"] == [True]


def test_typing_waits_and_then_asks_once(drive):
    assert [s for s in drive["searches"] if s[0] == "fire"] == [["fire", "", "", 0]]  # one question for the whole word, not one per letter
    assert not [s for s in drive["searches"] if s[0] in ("f", "fi", "fir")]
    assert drive["snaps"]["typed"]["values"] == [1]  # and its answer is what is shown


def test_show_more_goes_on_with_the_list_on_screen_not_with_a_query_still_waiting(drive):
    searches = drive["searches"]
    assert ["", "", "", 80] in searches  # the next page of the list that was shown
    assert ["zzz", "", "", 80] not in searches and ["zzz", "", "", 0] in searches  # the new query got its own first page, later
    assert searches.index(["", "", "", 80]) < searches.index(["zzz", "", "", 0])


def test_choosing_a_group_filters_and_choosing_it_again_clears_it(drive):
    assert drive["snaps"]["group"]["values"] == ["browsers", 4]
    assert drive["snaps"]["group-cleared"]["values"] == ["", 40]
    assert ["", "browsers", "", 0] in drive["searches"]


def test_the_source_filter_asks_for_vendor_packages_only(drive):
    assert ["", "", "vendor", 0] in drive["searches"] and drive["snaps"]["vendor-only"]["values"] == [2]


def test_show_more_adds_the_next_page_after_the_ones_already_there(drive):
    assert drive["snaps"]["more"]["values"] == [80, 95]
    assert ["", "", "", 40] in drive["searches"]


def test_a_program_shows_every_way_to_get_it(drive):
    firefox = drive["snaps"]["firefox"]
    assert firefox["values"] == ["Firefox", 2] and firefox["title"] == "Firefox"
    visible = firefox["visible"]  # one card per way: the Flatpak card has the Install button, the repository card its own
    assert visible["websiteButton"] == [True] and visible["flatpakButton"] == [True, False] and visible["repoButton"] == [False, True]
    assert visible["debButton"] == [False, False] and visible["rpmButton"] == [False, False]
    assert all(visible[name] == [] for name in ("releaseButton_appimage", "releaseButton_deb", "releaseButton_archive"))  # no maker's download card at all
    chrome = drive["snaps"]["chrome"]["visible"]  # Chrome: the Flatpak, and the vendor's package in both formats
    assert chrome["flatpakButton"] == [True, False] and chrome["debButton"] == [False, True] and chrome["rpmButton"] == [False, True]
    assert chrome["repoButton"] == [False, False] and chrome["websiteButton"] == [False]  # no website known: no button


def test_a_flatpak_is_handed_to_the_install_page_by_its_remote_and_id(drive):
    assert drive["snaps"]["flatpak-handover"]["values"] == ["flathub:org.mozilla.firefox"] and drive["snaps"]["flatpak-handover"]["title"] == "Install"


def test_a_repository_package_opens_its_page_in_packages(drive):
    assert drive["snaps"]["repo-handover"]["values"] == ["firefox"] and drive["snaps"]["repo-handover"]["title"] == "firefox"


def test_a_vendors_deb_is_fetched_then_handed_over_with_the_checksum_said_in_words(drive):
    assert drive["fetches"][0] == ["google-chrome-stable", "deb"]
    assert drive["snaps"]["deb-handover"]["title"] == "Install"
    values = drive["snaps"]["deb-handover"]["values"]
    assert values[0] == "file:///nonexistent/google-chrome-stable.deb"
    assert "checked it against the checksum the vendor publishes" in values[1] and "nothing is installed until you confirm" in values[1]


def test_a_vendors_rpm_says_plainly_that_nothing_vouches_for_it_but_the_connection(drive):
    vendor = [f for f in drive["fetches"] if f[0] != "release"]
    assert vendor == [["google-chrome-stable", "deb"], ["google-chrome-stable", "rpm"]]  # each button fetched once, nothing else was
    values = drive["snaps"]["rpm-handover"]["values"]
    assert values[0] == "file:///nonexistent/google-chrome-stable.rpm" and "publishes none" in values[1]


def test_the_source_filter_asks_for_the_makers_own_downloads_only(drive):
    assert ["", "", "release", 0] in drive["searches"] and drive["snaps"]["release-only"]["values"] == [2]


def test_a_program_with_a_makers_download_shows_one_button_for_each_kind_of_file_it_has(drive):
    card = drive["snaps"]["release-card"]
    assert card["values"] == ["Obsidian", 2]
    assert card["visible"]["flatpakButton"] == [True, False]  # the Flatpak card keeps its own button
    assert card["visible"]["releaseButton_appimage"] == [True] and card["visible"]["releaseButton_deb"] == [True] and card["visible"]["releaseButton_archive"] == [True]
    assert card["visible"]["releaseButton_rpm"] == [] and card["visible"]["releaseButton_pkg"] == []  # nothing is offered that the project does not have
    assert card["visible"]["releasePageButton"] == [False, True]  # only the release card shows it: the way out when Cygnus cannot choose a file
    assert card["texts"]["releasePageButton"][1] == "Open the release page"
    assert card["texts"]["releaseButton_appimage"] == ["Download the AppImage and review…"]
    assert card["texts"]["releaseButton_deb"] == ["Download the .deb and review…"] and card["texts"]["releaseButton_archive"] == ["Download the archive and review…"]


def test_a_makers_download_is_fetched_then_handed_over_to_the_install_page_and_says_how_it_was_checked(drive):
    assert ["release", "obsidian", "deb"] in drive["fetches"] and ["release", "obsidian", "appimage"] in drive["fetches"]
    deb = drive["snaps"]["release-deb-handover"]
    assert deb["title"] == "Install" and deb["values"][0] == "file:///nonexistent/obsidian.deb"
    assert "the .deb from the maker's GitHub release page for Obsidian (version v9.9)" in deb["values"][1]
    assert "checked it against the checksum GitHub publishes for it" in deb["values"][1] and "nothing is installed until you confirm" in deb["values"][1]
    appimage = drive["snaps"]["release-appimage-handover"]["values"]
    assert appimage[0] == "file:///nonexistent/obsidian.appimage" and "the AppImage from the maker's GitHub release page" in appimage[1]
    assert "could not check it against a checksum, because GitHub has none for this file" in appimage[1]


def test_a_program_that_is_not_on_flathub_shows_the_makers_download_alone(drive):
    assert drive["snaps"]["release-alone"]["values"] == ["Helium", 1]
    assert drive["snaps"]["release-alone"]["visible"]["releaseButton_archive"] == [True]
    assert drive["snaps"]["release-alone"]["visible"]["releaseInstalledNote"] == [True]  # a Helium Cygnus already manages: the card warns before a second copy
    assert "replace the first one's menu entry" in drive["snaps"]["release-alone"]["texts"]["releaseInstalledNote"][0]
    assert drive["snaps"]["release-card"]["visible"]["releaseInstalledNote"] == [False, False]  # no warning where nothing of that name is installed
    assert drive["fetches"] == [["google-chrome-stable", "deb"], ["google-chrome-stable", "rpm"], ["release", "obsidian", "deb"], ["release", "obsidian", "appimage"]]
