"""A Flatpak's permissions shown live in an offscreen window (backend faked): grouped, the worst marked, what you changed said, nothing offered for
a program that is not a Flatpak."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
DRIVE = Path(__file__).with_name("gui_permissions_drive.py")


@pytest.fixture(scope="module")
def drive(tmp_path_factory):
    home = tmp_path_factory.mktemp("permissions-drive")
    env = {**os.environ, **{var: str(home / sub) for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                                                                    ("XDG_STATE_HOME", "state"))}}
    proc = subprocess.run([sys.executable, str(DRIVE)], capture_output=True, text=True, timeout=150, env=env)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert proc.returncode == 0 and lines, (proc.stdout + proc.stderr)[-3000:]
    return json.loads(lines[-1])


def test_a_flatpaks_page_offers_its_permissions_and_asks_once(drive):
    assert drive["snaps"]["before"]["action"] == {"visible": True, "enabled": True}
    assert drive["asked"] == ["inst-flatpak"]


def test_the_dialog_lists_them_by_group_with_the_worst_first_and_marked(drive):
    opened = drive["snaps"]["open"]
    assert opened["dialog_open"] is True and opened["busy"] == ""
    assert opened["summary"] == "2 that can leave the sandbox · 0 broad · 6 in all"
    assert opened["headings"] == ["Internet and other programs", "Windows and sound", "Files", "Programs it can talk to"]
    assert opened["rows"][0] == "• Use the internet and your local network"
    files = [r for r in opened["rows"] if "file" in r.lower() or "folder" in r.lower()]
    assert files[0].startswith("⚠ Every file on your computer (read and write)  ·  can leave the sandbox")
    assert any(r.startswith("⚠ Start programs outside the sandbox (org.freedesktop.Flatpak)  ·  can leave the sandbox") for r in opened["rows"])


def test_what_you_changed_is_said_in_the_rows(drive):
    rows = drive["snaps"]["open"]["rows"]
    assert any("Your Pictures folder (read and write)  ·  changed by you" in r for r in rows)
    assert not any("X11" in r for r in rows)  # your override took X11 away: it is not listed as something it can do


def test_a_program_that_is_not_a_flatpak_has_no_permissions_button(drive):
    assert drive["snaps"]["appimage"]["action"]["visible"] is False and drive["asked"] == ["inst-flatpak"]
