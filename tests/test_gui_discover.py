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
    assert start["title"] == "Discover" and start["values"][:3] == [40, 100, 3]
    assert drive["searches"][0] == ["", "", "", 0]


def test_the_count_reads_as_plain_english_with_no_translation_loaded(drive):
    """Qt's qsTr("%n program(s)") only fills in the number without a translation, which showed "93 program(s)": the sentence is chosen by hand."""
    assert drive["snaps"]["start"]["count_text"] == ["100 programs"]
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
    assert drive["snaps"]["more"]["values"] == [80, 100]
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


def test_the_aur_is_asked_only_when_the_button_is_pressed_and_for_at_least_two_letters(drive):
    assert drive["snaps"]["aur-start"]["visible"]["aurSearchButton"] == [False] and drive["snaps"]["aur-one-letter"]["visible"]["aurSearchButton"] == [False]
    button = drive["snaps"]["aur-button"]
    assert button["visible"]["aurSearchButton"] == [True] and button["texts"]["aurSearchButton"] == ["Also search the AUR for “vlc”"]
    assert drive["aur"][0] == "vlc"  # nothing was sent to the AUR before the press (typing 'v' and 'vlc' only searched the local lists)
    assert [q for q in drive["aur"] if q in ("v",)] == []


def test_the_aurs_answer_is_listed_with_a_warning_and_each_package_goes_to_the_install_page(drive):
    results = drive["snaps"]["aur-results"]
    assert results["values"] == [31, 2] and results["visible"]["aurWarning"] == [True]
    assert results["texts"]["aurHeading"] == ["From the AUR: the 2 most popular of 31 packages"]
    assert results["visible"]["aurInstallButton"] == [True, True] and results["visible"]["aurWebsiteButton"] == [True, False]  # no address: no button
    assert results["texts"]["aurInstallButton"] == ["Review and install…", "Look at it again…"]  # the second is installed already
    handover = drive["snaps"]["aur-handover"]
    assert handover["title"] == "Install" and handover["values"] == ["aur:vlc-git"]


def test_other_words_drop_the_answer_for_the_old_ones_and_an_error_is_said_in_words(drive):
    assert drive["snaps"]["aur-cleared"]["values"] == [True]
    error = drive["snaps"]["aur-error"]
    assert error["values"] == ["The AUR could not be asked: no network", True] and error["visible"]["aurError"] == [True]


def test_a_community_appimage_is_listed_with_its_own_button_and_the_warning_that_nobody_checked_it(drive):
    card = drive["snaps"]["appimage-card"]
    assert card["values"] == ["Tool", 1] and card["visible"]["appimageButton"] == [True]
    assert card["texts"]["appimageButton"] == ["Download the AppImage and review…"]
    assert card["visible"]["releasePageButton"] == [True] and card["texts"]["releasePageButton"] == ["Open the release page"]  # the way out when Cygnus cannot choose a file
    assert all(card["visible"][name] == [] for name in ("releaseButton_appimage", "releaseButton_deb", "releaseButton_archive"))  # the maker's own cards are not shown for it
    assert card["visible"]["releaseInstalledNote"] == [False]


def test_a_community_appimage_is_fetched_by_its_project_and_handed_over_saying_nobody_checked_it(drive):
    assert drive["appimages"] == ["owner/Tool"]
    handover = drive["snaps"]["appimage-handover"]
    assert handover["title"] == "Install" and handover["values"][0] == "file:///nonexistent/tool.AppImage"
    notice = handover["values"][1]
    assert "the GitHub release page of owner/Tool (version v1.2)" in notice and "community list that Cygnus has not checked" in notice
    assert "could not check it against a checksum, because GitHub has none for this file" in notice and "nothing is installed until you confirm" in notice


def test_a_community_appimage_whose_name_cygnus_already_manages_warns_before_a_second_copy(drive):
    seen = drive["snaps"]["appimage-seen"]
    assert seen["values"] == ["Seen"] and seen["visible"]["releaseInstalledNote"] == [True] and seen["visible"]["appimageButton"] == [True]


def test_the_source_filter_asks_for_the_community_appimages_only(drive):
    assert ["", "", "appimage", 0] in drive["searches"] and drive["snaps"]["appimage-only"]["values"] == [2]


def test_nothing_is_downloaded_before_the_person_answers_the_one_question_and_yes_downloads_them(drive):
    asked = drive["snaps"]["question"]
    assert asked["values"] == [False, False] and asked["visible"]["cataloguesQuestion"] == [True]  # shown, and nothing was fetched while it was
    assert asked["refreshes"] == 0  # not asked for before the answer
    assert asked["visible"]["catalogueSwitch"] == [] and asked["visible"]["catalogueUpdateButton"] == [False]  # and nothing to switch or press until it is answered
    assert drive["decisions"][0] is True
    assert drive["snaps"]["start"]["visible"]["cataloguesQuestion"] == [False]  # answered: the question is gone
    assert drive["snaps"]["start"]["refreshes"] == 1 and drive["refreshes"][0] is False  # and the yes itself started the download


def test_not_now_switches_every_catalogue_off_and_the_question_is_not_asked_again(drive):
    again = drive["snaps"]["question-again"]
    assert again["values"] == [False] and again["visible"]["cataloguesQuestion"] == [True]
    no = drive["snaps"]["question-no"]
    assert no["values"] == [True, False] and no["visible"]["cataloguesQuestion"] == [False] and no["checked"] == [False]
    assert drive["decisions"] == [True, False]


def test_the_online_catalogues_are_downloaded_once_when_discover_opens_and_again_only_when_asked(drive):
    first = drive["snaps"]["start"]
    assert first["visible"]["catalogueSwitch"] == [True] and first["checked"] == [True]
    assert drive["refreshes"][0] is False  # the page's own look: only what is missing or old is downloaded
    assert drive["snaps"]["catalogues-now"]["values"] == [2796, False]  # the lists were read again once the download finished
    assert "2796 programs, downloaded today" in drive["snaps"]["catalogues-now"]["texts"]["catalogueState"][0]
    assert drive["refreshes"][1] is True and drive["snaps"]["catalogues-forced"]["values"] == [2796]  # the button downloads even what is recent


def test_a_catalogue_can_be_switched_off_and_on_and_switching_on_downloads_nothing_that_is_recent(drive):
    assert drive["switches"] == [["appimagehub", False], ["appimagehub", True], ["appimagehub", True]]  # the last is the one that could not be saved (below)
    assert drive["snaps"]["catalogues-off"]["values"] == [False] and drive["snaps"]["catalogues-off"]["checked"] == [False]
    assert "Switched off" in drive["snaps"]["catalogues-off"]["texts"]["catalogueState"][0]
    assert drive["snaps"]["catalogues-on"]["values"] == [True] and drive["snaps"]["catalogues-on"]["checked"] == [True]
    assert drive["refreshes"][2:] == [False]  # asked after switching on, and answered with nothing to download: no third forced download


def test_a_debian_program_says_it_may_not_work_and_names_what_debian_says_it_also_needs(drive):
    needy = drive["snaps"]["debian-needy"]
    assert needy["values"] == ["AbiWord", 1] and needy["visible"]["debianButton"] == [True] and needy["visible"]["foreignWarning"] == [True]
    assert needy["texts"]["debianButton"] == ["Download the .deb and review…"]
    warning = needy["texts"]["foreignWarning"][0]
    assert warning.startswith("This may not work: it was built for Debian.") and "abiword-common, fonts-urw-base35" in warning
    assert "Cygnus does not fetch them" in warning
    assert needy["visible"]["releaseButton_appimage"] == [] and needy["visible"]["releaseButton_deb"] == [] and needy["visible"]["appimageButton"] == [False]  # no other kind's buttons


def test_a_debian_program_that_needs_only_libraries_says_so_and_still_says_it_may_not_work(drive):
    warning = drive["snaps"]["debian-plain"]["texts"]["foreignWarning"][0]
    assert warning.startswith("This may not work: it was built for Debian.") and "Besides libraries" in warning and "abiword-common" not in warning


def test_a_debian_program_is_fetched_by_package_and_handed_over_saying_its_signature_is_not_checked(drive):
    assert drive["debians"] == ["abiword"]
    handover = drive["snaps"]["debian-handover"]
    assert handover["title"] == "Install" and handover["values"][0] == "file:///nonexistent/abiword.deb"
    notice = handover["values"][1]
    assert "Debian's package abiword (version 1.0-1)" in notice and "checksum in Debian's package list" in notice and "Debian's own signature is not checked" in notice
    assert "may fail or leave something out" in notice and "Nothing is installed until you confirm" in notice


def test_the_source_filter_asks_for_debian_packages_only(drive):
    assert ["", "", "debian", 0] in drive["searches"] and drive["snaps"]["debian-only"]["values"] == [2]


def test_a_fedora_program_says_it_may_not_work_names_what_fedora_also_needs_and_is_fetched_by_package(drive):
    card = drive["snaps"]["fedora-card"]
    assert card["values"] == ["Tweaks", 1] and card["visible"]["fedoraButton"] == [True] and card["visible"]["debianButton"] == [False]
    assert card["texts"]["fedoraButton"] == ["Download the .rpm and review…"]
    warning = card["texts"]["foreignWarning"][0]
    assert warning.startswith("This may not work: it was built for Fedora.") and "gnome-tweaks-data, python3-gobject-base" in warning and "Cygnus does not fetch them" in warning
    assert drive["fedoras"] == ["gnome-tweaks"]
    handover = drive["snaps"]["fedora-handover"]
    assert handover["title"] == "Install" and handover["values"][0] == "file:///nonexistent/gnome-tweaks.rpm"
    notice = handover["values"][1]
    assert "Fedora's package gnome-tweaks (version 2.0-1.fc44)" in notice and "checksum in Fedora's package list" in notice and "Fedora's own signature is not checked" in notice
    assert "may fail or leave something out" in notice and "Nothing is installed until you confirm" in notice


def test_the_source_filter_asks_for_fedora_packages_only(drive):
    assert ["", "", "fedora", 0] in drive["searches"] and drive["snaps"]["fedora-only"]["values"] == [1]


def test_a_program_that_only_another_list_has_wears_a_warning_badge_beside_its_name(drive):
    appimages = drive["snaps"]["appimage-only"]
    assert appimages["texts"]["listBadge"] == ["Not checked", "Not checked"] and appimages["visible"]["listBadge"] == [True, True]
    assert drive["snaps"]["debian-only"]["texts"]["listBadge"] == ["May not work", "May not work"]
    assert drive["snaps"]["fedora-only"]["texts"]["listBadge"] == ["May not work"]
    assert drive["snaps"]["release-only"]["visible"]["listBadge"] == [False, False]  # the makers' own downloads and the rest carry no badge


def test_an_installed_mark_that_only_comes_from_the_same_name_says_so(drive):
    seen = drive["snaps"]["appimage-only"]  # two community entries; the second has the name of a program Cygnus manages
    assert seen["visible"]["listInstalled"] == [False, True] and seen["texts"]["listInstalled"][1] == "Same name installed"


def test_a_switch_that_could_not_be_saved_shows_what_is_saved_and_says_so(drive):
    failed = drive["snaps"]["switch-failed"]
    assert failed["values"] == ["The setting could not be saved.", False] and failed["checked"] == [False]  # clicked to on, shown as saved: still off
    assert failed["visible"]["refreshError"] == [True]
