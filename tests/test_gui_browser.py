"""The default-browser choice driven live in an offscreen window (backend faked): the page offers it only for a browser, says what will
happen, goes through with the choice, and then offers to put the earlier one back. A browser that cannot be chosen says why."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
DRIVE = Path(__file__).with_name("gui_browser_drive.py")


@pytest.fixture(scope="module")
def drive(tmp_path_factory):
    home = tmp_path_factory.mktemp("browser-drive")  # the window reads its registry and settings from here, never from the real ones
    env = {**os.environ, **{var: str(home / sub) for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"),
                                                                    ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"))}}
    proc = subprocess.run([sys.executable, str(DRIVE)], capture_output=True, text=True, timeout=150, env=env)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert proc.returncode == 0 and lines, (proc.stdout + proc.stderr)[-3000:]
    return json.loads(lines[-1])


def test_a_browser_is_offered_the_choice_and_says_what_will_happen(drive):
    start, asking = drive["snaps"]["start"], drive["snaps"]["asking"]
    assert start["action"]["visible"] and start["action"]["text"] == "Make default browser…" and start["action"]["enabled"]
    assert asking["dialog"]["open"] and asking["dialog"]["title"] == "Make Helium your default browser?"
    assert "instead of Firefox" in asking["dialog"]["subtitle"] and "remembered" in asking["dialog"]["subtitle"]
    assert "drive that can be unplugged" in asking["dialog"]["subtitle"]  # a note reaches the person before they choose
    assert asking["button"]["text"] == "Make default" and asking["button"]["enabled"]


def test_choosing_goes_through_once_and_the_page_then_offers_to_put_it_back(drive):
    assert drive["calls"][:2] == [["enable", "helium.desktop"], ["restore"]]
    chosen = drive["snaps"]["chosen"]
    assert chosen["is_default"] is True and chosen["action"]["text"] == "Put back the earlier browser…"
    assert chosen["message"]["visible"] and "now your default browser" in chosen["message"]["text"]
    assert chosen["busy"] == ""
    putting = drive["snaps"]["putting-back"]
    assert putting["dialog"]["title"] == "Put back what was set before?" and putting["button"]["text"] == "Put back"


def test_putting_back_returns_to_the_first_state(drive):
    done = drive["snaps"]["put-back"]
    assert done["is_default"] is False and done["action"]["text"] == "Make default browser…"
    assert "back" in done["message"]["text"] and len(drive["calls"]) == 2  # nothing else was run


def test_a_browser_that_cannot_be_chosen_says_why_and_the_button_stays_off(drive):
    blocked = drive["snaps"]["blocked"]
    assert blocked["dialog"]["open"] and "This cannot be done yet" in blocked["dialog"]["subtitle"]
    assert "https links" in blocked["dialog"]["subtitle"]
    assert blocked["button"]["enabled"] is False


def test_a_browser_that_is_first_only_by_accident_is_told_so_and_a_program_that_is_no_browser_shows_nothing(drive):
    assert drive["snaps"]["by-accident"]["action"]["visible"]
    assert drive["snaps"]["by-accident"]["status_line"] == "info"
    assert drive["snaps"]["not-a-browser"]["action"]["visible"] is False
    assert drive["snaps"]["not-a-browser"]["status_line"] == "none"
