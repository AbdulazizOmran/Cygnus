"""The Updates dialog driven live in an offscreen window (backend faked): news that needs a manual step holds the Update button until
the box is ticked, nothing is ticked for you, and news that needs nothing does not hold it."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
DRIVE = Path(__file__).with_name("gui_updates_drive.py")


@pytest.fixture(scope="module")
def drive():
    proc = subprocess.run([sys.executable, str(DRIVE)], capture_output=True, text=True, timeout=150)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert proc.returncode == 0 and lines, (proc.stdout + proc.stderr)[-3000:]
    return json.loads(lines[-1])


def test_the_update_button_is_disabled_until_the_box_is_ticked(drive):
    assert drive["states"][:2] == [False, True]  # before the tick, then after it
    assert drive["commits"][0] == "tok-manual" and drive["commits"].count("tok-manual") == 1  # once, and only after the tick


def test_news_that_needs_nothing_does_not_hold_the_update(drive):
    assert drive["states"][2] is True and drive["commits"] == ["tok-manual", "tok-plain"]


def test_the_box_starts_clear_for_every_plan(drive):
    assert drive["shown"] == [False, False]
