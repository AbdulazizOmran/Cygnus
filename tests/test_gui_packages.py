"""The Packages screens, driven live in an offscreen window with the backend faked: typing waits, a late search answer is dropped, and
Install / Remove reach the helper only through the confirmation window (and, for a package Cygnus did not install, a ticked box)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
DRIVE = Path(__file__).with_name("gui_packages_drive.py")


@pytest.fixture(scope="module")
def drive():
    proc = subprocess.run([sys.executable, str(DRIVE)], capture_output=True, text=True, timeout=150)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert proc.returncode == 0 and lines, (proc.stdout + proc.stderr)[-3000:]
    return json.loads(lines[-1])


def test_typing_quickly_searches_only_the_last_text(drive):
    assert drive["searches"][:1] == ["fire"]  # "fi" and "fir" were never searched
    assert json.loads(drive["after_typing"]) == ["fire-result"]


def test_a_search_answered_late_does_not_replace_the_newer_one(drive):
    assert drive["searches"][1:3] == ["slow", "fast"]  # both were asked ...
    assert json.loads(drive["after_late_answer"]) == ["fast-result"]  # ... and the slow one, answered last, was dropped


def test_install_reaches_the_helper_only_through_the_confirmation_window(drive):
    assert ["install", ["sl"]] in drive["plans"]
    assert "tok-Install" in drive["commits"]


def test_a_package_cygnus_did_not_install_is_planned_with_the_confirmation_only_after_the_tick(drive):
    removes = [p for p in drive["plans"] if p[0] == "remove" and p[1] == ["cowsay"]]
    assert removes == [["remove", ["cowsay"], False], ["remove", ["cowsay"], True]]
    warn = [enabled for name, enabled in drive["enabled"] if name == "warnAction"]
    assert warn == [False, True]  # the button is disabled until the box is ticked, and pressing it then plans again
    assert drive["commits"][:2] == ["tok-Install", "tok-Remove"]


def test_older_versions_are_listed_for_an_installed_package_and_going_back_goes_through_the_confirmation(drive):
    assert json.loads(drive["older_shown"]) == ["0.9-1", "0.8-1"]
    assert ["downgrade", "cowsay", "0.9-1"] in drive["plans"]
    assert "tok-Downgrade-0.9-1" in drive["commits"]


def test_a_package_that_is_not_installed_has_no_older_versions_looked_for(drive):
    assert set(drive["older_asked"]) == {"cowsay"} and json.loads(drive["older_shown_sl"]) == []  # (asked again after each change to it)


def test_leaving_the_package_page_while_its_change_is_being_made_still_refreshes_the_list(drive):
    assert drive["title_after"] == "Packages" and json.loads(drive["list_before"]) == ["cowsay", "other"]
    assert "tok-Downgrade-0.1-slow" in drive["commits"]  # the commit finished although its page was gone
    assert json.loads(drive["list_after"]) == ["other"]  # and the list below was told


def test_everything_no_longer_needed_can_be_ticked_asked_about_and_removed_in_one_plan(drive):
    assert json.loads(drive["orphans_shown"]) == ["orph1", "orph2"]  # the one that is still needed is not on the list
    assert json.loads(drive["picked_before"]) == [] and json.loads(drive["picked_all"]) == ["orph1", "orph2"]  # nothing is ticked until the person ticks
    batch = [p for p in drive["plans"] if p[0] == "remove" and "orph1" in p[1]]
    assert batch == [["remove", ["orph1", "orph2"], False], ["remove", ["orph1", "orph2"], True]]  # asked about first, then planned with the confirmation
    assert [e for n, e in drive["enabled"] if n == "listWarnAction"] == [False, True]  # the question's button is disabled until its box is ticked
    assert "tok-Remove-orphans" in drive["commits"]
    assert json.loads(drive["picked_after"]) == [] and "orph1" not in json.loads(drive["orphans_after"])  # the ticks are gone and so are the packages


def test_the_cache_dialog_previews_each_setting_and_cleans_through_the_confirmation(drive):
    assert drive["cache_first"] == 4 and drive["cache_then"] == 10  # the preview followed the setting (keep 2, then 0)
    assert drive["previews"][:1] == [[2, False]] and [0, False] in drive["previews"]
    assert ["cache", 0, False] in drive["plans"] and drive["commits"][-1] == "tok-Cache"


def test_a_blank_line_of_progress_from_pacman_does_not_make_the_page_look_idle_mid_commit(drive):
    assert drive["busy_mid"] == "removing packages…"  # the blank lines that followed it were ignored: a second change cannot start now
