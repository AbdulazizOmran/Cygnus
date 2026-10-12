"""The Settings switch for the update icon, driven live in an offscreen window (tray backend faked)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
DRIVE = Path(__file__).with_name("gui_tray_drive.py")


@pytest.fixture(scope="module")
def drive(tmp_path_factory):
    home = tmp_path_factory.mktemp("tray-drive")
    env = {**os.environ, **{var: str(home / sub) for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                                                                    ("XDG_STATE_HOME", "state"))}}
    proc = subprocess.run([sys.executable, str(DRIVE)], capture_output=True, text=True, timeout=150, env=env)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert proc.returncode == 0 and lines, (proc.stdout + proc.stderr)[-3000:]
    return json.loads(lines[-1])


def test_the_switch_starts_off_when_it_is_off(drive):
    assert drive["snaps"]["start"]["checked"] is False and drive["snaps"]["start"]["enabled"] is True


def test_switching_on_calls_the_backend_once_and_stays_on(drive):
    assert drive["calls"][0] == "enable" and drive["snaps"]["on"]["checked"] is True and drive["snaps"]["on"]["world"] is True
    assert drive["snaps"]["on"]["note"] in ("", None)


def test_a_failure_puts_the_switch_back_to_what_is_really_so_and_says_why(drive):
    failed = drive["snaps"]["failed"]
    assert drive["calls"] == ["enable", "disable"]
    assert failed["checked"] is True and failed["world"] is True  # switching off failed: it is still on, and the switch says so
    assert "disk full" in failed["note"] and failed["enabled"] is True
