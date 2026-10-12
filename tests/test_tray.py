"""The update icon: what it counts (stored results only), what it says, switching it on and off in the user's own autostart folder, the controller
that shows and hides it, and the real program started offscreen."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cygnus.core import preferences, tray, updates
from cygnus.core.desktop import entry as desktop_entry
from cygnus.core.errors import CygnusError
from cygnus.gui import tray as gui_tray


@pytest.fixture(autouse=True)
def _no_real_icon(monkeypatch):
    """No test starts the real icon program (it would put a window on the desk of whoever runs the tests): a test that wants to see a start
    gives enable() its own `spawn`, or replaces Popen itself."""
    monkeypatch.setattr(tray, "subprocess", NS(Popen=lambda *a, **k: pytest.fail("a test started a real process"), DEVNULL=subprocess.DEVNULL))


def _statuses(*pairs):
    return [NS(name=name, status=status) for name, status in pairs]


@pytest.fixture
def stored(monkeypatch):
    """The stored results: application statuses (faked) and the system-update file (a real file in the test's cache folder)."""
    state = {"statuses": []}
    monkeypatch.setattr(updates, "last_known", lambda reg: state["statuses"])
    monkeypatch.setattr("cygnus.core.registry.open_registry", lambda path=None, **kw: object())
    monkeypatch.setattr(tray, "PACMAN_LOCAL", Path(os.environ["XDG_CACHE_HOME"]) / "no-pacman-local")  # not this machine's real database

    def system(content):
        path = updates.system_updates_cache()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    state["system"] = system
    return state


# -- what is waiting ------------------------------------------------------------------------------------------------------------------------
def test_only_programs_with_an_update_waiting_are_counted(stored):
    stored["statuses"] = _statuses(("Helium", updates.AVAILABLE), ("WhatPulse", updates.UP_TO_DATE), ("Chrome", updates.UNKNOWN), ("Vim", updates.AVAILABLE))
    result = tray.pending()
    assert result["programs"] == 2 and result["names"] == ["Helium", "Vim"] and result["total"] == 2 and result["system"] == 0


def test_system_updates_come_from_the_stored_result_and_a_kernel_is_noticed(stored):
    stored["system"]({"packages": [{"name": "a"}, {"name": "b"}, {"name": "c"}], "kernel": True, "error": None})
    result = tray.pending()
    assert result["system"] == 3 and result["kernel"] is True and result["total"] == 3


@pytest.mark.parametrize("content", ["not json", "[]", "{}", '{"packages": "many"}', '{"packages": [1], "error": "fakeroot is missing"}', "\x00\x01"])
def test_a_system_result_that_is_damaged_or_failed_counts_for_nothing(stored, content):
    stored["system"](content)
    assert tray.pending()["system"] == 0


def test_a_huge_stored_result_is_not_read(stored, monkeypatch):
    stored["system"]({"packages": [{"name": "a"}], "error": None})
    monkeypatch.setattr(tray, "MAX_CACHE_BYTES", 5)
    assert tray.pending()["system"] == 0


def test_no_stored_result_at_all_is_no_updates(stored):
    assert tray.pending() == {"programs": 0, "system": 0, "kernel": False, "total": 0, "names": []}


def test_a_registry_that_cannot_be_read_leaves_the_system_count_standing(stored, monkeypatch):
    stored["system"]({"packages": [{"name": "a"}], "error": None})
    monkeypatch.setattr(updates, "last_known", lambda reg: (_ for _ in ()).throw(CygnusError("registry locked")))
    assert tray.pending()["total"] == 1


def test_it_never_goes_to_the_network_or_starts_the_helper(stored, monkeypatch):
    import socket

    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("the tray looked at the network"))
    stored["statuses"] = _statuses(("A", updates.AVAILABLE))
    assert tray.pending()["total"] == 1


# -- what it says ----------------------------------------------------------------------------------------------------------------------------
def test_nothing_waiting_means_no_icon():
    shown = tray.describe({"programs": 0, "system": 0, "kernel": False, "total": 0, "names": []})
    assert shown["visible"] is False and "up to date" in shown["tooltip"]


def test_the_tooltip_says_what_waits_in_plain_words():
    one = tray.describe({"programs": 1, "system": 0, "kernel": False, "total": 1, "names": ["Helium"]})
    assert one["visible"] and one["tooltip"] == "1 update waiting: 1 program (Helium). Click to open Cygnus."
    both = tray.describe({"programs": 7, "system": 12, "kernel": True, "total": 19, "names": ["A", "B", "C", "D", "E"]})
    assert "19 updates waiting" in both["tooltip"] and "7 programs (A, B, C, D, E and more)" in both["tooltip"]
    assert "12 system packages, including a new kernel" in both["tooltip"]
    assert tray.describe({"programs": 0, "system": 1, "kernel": False, "total": 1, "names": []})["tooltip"].startswith("1 update waiting: 1 system package.")


# -- switching it on and off ------------------------------------------------------------------------------------------------------------
def test_the_start_up_entry_is_a_proper_hidden_autostart_entry_for_the_command():
    entry = desktop_entry.parse(tray.autostart_text(["/opt/my programs/cygnus-tray", "--50%"]))
    assert entry.get("Type") == "Application" and entry.get("NoDisplay") == "true" and entry.get("Terminal") == "false"
    assert entry.get("X-Cygnus-Managed") == "true" and entry.get("X-KDE-autostart-after") == "panel"
    assert desktop_entry.split_exec(entry.get("Exec")) == ["/opt/my programs/cygnus-tray", "--50%%"]  # a percent sign is doubled, a space is quoted


def test_switching_on_writes_the_entry_remembers_the_choice_and_starts_it_once():
    started = []
    state = tray.enable(spawn=lambda argv, **kw: started.append((argv, kw)))
    assert state["enabled"] is True and tray.autostart_path().is_file() and preferences.load()["tray"] is True
    assert tray.autostart_path().parent == Path(os.environ["XDG_CONFIG_HOME"]) / "autostart"  # only ever the user's own folder
    assert len(started) == 1 and started[0][0] == tray.command() and started[0][1]["start_new_session"] is True


def test_it_can_be_switched_on_without_starting_it_now():
    started = []
    tray.enable(start=False, spawn=lambda *a, **k: started.append(1))
    assert started == [] and tray.status()["enabled"]


def test_an_icon_that_cannot_be_started_now_is_still_set_up_for_the_next_login_and_says_so():
    def broken(*a, **k):
        raise OSError(2, "No such file or directory")

    with pytest.raises(CygnusError, match="will start at your next login"):
        tray.enable(spawn=broken)
    assert tray.status()["enabled"]


def test_switching_on_will_not_write_through_a_link_or_over_a_folder(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("precious")
    tray.autostart_path().parent.mkdir(parents=True)
    tray.autostart_path().symlink_to(victim)
    with pytest.raises(CygnusError, match="not a plain file"):
        tray.enable(start=False)
    assert victim.read_text() == "precious"
    tray.autostart_path().unlink()
    tray.autostart_path().mkdir()
    with pytest.raises(CygnusError, match="not a plain file"):
        tray.enable(start=False)


def test_switching_off_removes_the_entry_and_the_choice():
    tray.enable(start=False)
    state = tray.disable()
    assert state == {"enabled": False, "entry": False, "why": ""} and preferences.load()["tray"] is False and not tray.autostart_path().exists()


def test_switching_off_leaves_a_file_of_that_name_that_is_not_cygnus_s_alone(tmp_path):
    tray.autostart_path().parent.mkdir(parents=True)
    tray.autostart_path().write_text("[Desktop Entry]\nType=Application\nName=Something else\nExec=true\n")
    tray.disable()
    assert tray.autostart_path().exists() and "Something else" in tray.autostart_path().read_text()


def test_switching_off_removes_a_link_but_never_what_it_points_at(tmp_path):
    target = tmp_path / "dotfiles-entry"
    target.write_text(tray.autostart_text())
    tray.autostart_path().parent.mkdir(parents=True)
    tray.autostart_path().symlink_to(target)
    tray.disable()
    assert not tray.autostart_path().is_symlink() and target.exists()


def test_the_status_says_when_the_choice_and_the_file_disagree():
    assert tray.status() == {"enabled": False, "entry": False, "why": ""}
    preferences.save({"tray": True})
    assert tray.status()["enabled"] is False and "start-up entry is missing" in tray.status()["why"]
    tray.autostart_path().parent.mkdir(parents=True, exist_ok=True)
    tray.autostart_path().write_text(tray.autostart_text())
    preferences.save({"tray": False})
    assert tray.status()["enabled"] is False and "quits as soon as it starts" in tray.status()["why"]
    preferences.save({"tray": "yes"})  # a hand-edited value that is not exactly true is not "on"
    assert tray.wanted() is False


def test_the_command_is_the_installed_program_or_this_python_running_the_module(monkeypatch):
    monkeypatch.setattr(tray.shutil, "which", lambda name: "/usr/bin/cygnus-tray" if name == "cygnus-tray" else None)
    assert tray.command() == ["/usr/bin/cygnus-tray"]
    monkeypatch.setattr(tray.shutil, "which", lambda name: None)
    root = str(Path(tray.__file__).resolve().parent.parent.parent)
    assert tray.command() == ["env", f"PYTHONPATH={root}", sys.executable, "-m", "cygnus.gui.tray"]  # works from a login session that starts in the home folder
    monkeypatch.setattr(gui_tray.shutil, "which", lambda name: None)
    assert gui_tray.gui_command() == [sys.executable, "-m", "cygnus.gui.app"] and gui_tray.check_command() == [sys.executable, "-m", "cygnus", "watch"]


# -- the controller ----------------------------------------------------------------------------------------------------------------------------
class FakeIcon:
    def __init__(self):
        self.calls = []

    def setToolTip(self, text):
        self.calls.append(("tip", text))

    def setVisible(self, visible):
        self.calls.append(("visible", visible))


def test_the_icon_appears_while_something_waits_and_goes_when_nothing_does():
    icon, quit_calls = FakeIcon(), []
    waiting = {"state": {"programs": 1, "system": 0, "kernel": False, "total": 1, "names": ["A"]}}
    controller = gui_tray.Controller(icon, lambda: quit_calls.append(1), state=lambda: waiting["state"], wanted=lambda: True)
    controller.refresh()
    assert icon.calls[-1] == ("visible", True) and "1 update waiting" in icon.calls[0][1]
    waiting["state"] = {"programs": 0, "system": 0, "kernel": False, "total": 0, "names": []}
    controller.refresh()
    assert icon.calls[-1] == ("visible", False) and quit_calls == []


def test_an_unchanged_state_does_not_touch_the_icon_again():
    icon = FakeIcon()
    controller = gui_tray.Controller(icon, lambda: None, state=lambda: {"programs": 1, "system": 0, "kernel": False, "total": 1, "names": ["A"]}, wanted=lambda: True)
    controller.refresh()
    controller.refresh()
    controller.refresh()
    assert len(icon.calls) == 2  # the tooltip and the visibility, once


def test_it_quits_when_it_is_switched_off_and_does_nothing_else():
    icon, quit_calls = FakeIcon(), []
    controller = gui_tray.Controller(icon, lambda: quit_calls.append(1), state=lambda: pytest.fail("looked at the results"), wanted=lambda: False)
    controller.refresh()
    assert quit_calls == [1] and icon.calls == []


def test_a_minute_when_the_results_cannot_be_read_keeps_what_was_shown():
    icon = FakeIcon()
    answers = [{"programs": 1, "system": 0, "kernel": False, "total": 1, "names": ["A"]}, RuntimeError("locked")]

    def state():
        value = answers.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    controller = gui_tray.Controller(icon, lambda: None, state=state, wanted=lambda: True)
    controller.refresh()
    controller.refresh()
    assert icon.calls[-1] == ("visible", True)


# -- the real program ----------------------------------------------------------------------------------------------------------------------------
pytest.importorskip("PySide6")


def _run(code, tmp_path, *, tray_on):
    env = {**os.environ, **{var: str(tmp_path / sub) for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                                                                       ("XDG_STATE_HOME", "state"))}, "QT_QPA_PLATFORM": "offscreen", "XDG_RUNTIME_DIR": str(tmp_path)}
    if tray_on:
        (tmp_path / "config" / "cygnus").mkdir(parents=True)
        (tmp_path / "config" / "cygnus" / "preferences.json").write_text('{"tray": true}')
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env, cwd=str(Path(__file__).resolve().parent.parent))


def test_the_program_does_nothing_when_it_is_switched_off(tmp_path):
    result = _run("from cygnus.gui import tray; raise SystemExit(tray.main())", tmp_path, tray_on=False)
    assert result.returncode == 0 and "switched off" in result.stdout


def test_the_program_starts_its_window_pieces_and_leaves_politely_when_there_is_no_tray(tmp_path):
    code = "from cygnus.gui import tray; tray.WAIT_FOR_TRAY_SECONDS = 0; raise SystemExit(tray.main())"
    result = _run(code, tmp_path, tray_on=True)
    assert result.returncode == 0 and "no system tray" in result.stderr, result.stderr[-1500:]


# -- the terminal command ------------------------------------------------------------------------------------------------------------------
def test_the_terminal_command_switches_it_on_and_off_and_says_what_it_would_show(stored, monkeypatch, capsys):
    from cygnus.cli import main as cli

    started = []
    monkeypatch.setattr(tray.subprocess, "Popen", lambda argv, **kw: started.append(argv))  # no test ever starts the real icon
    stored["statuses"] = _statuses(("Helium", updates.AVAILABLE))
    assert cli.main(["tray", "status"]) == 0
    out = capsys.readouterr().out
    assert "update icon: off" in out and "1 update waiting: 1 program (Helium)" in out and started == []
    assert cli.main(["tray", "on"]) == 0
    assert "the update icon is on (it starts at every login)" in capsys.readouterr().out and len(started) == 1 and tray.status()["enabled"]
    assert cli.main(["tray", "off"]) == 0
    assert "the update icon is off" in capsys.readouterr().out and not tray.autostart_path().exists() and len(started) == 1
    assert cli.main(["tray", "status"]) == 0
    assert "update icon: off" in capsys.readouterr().out


def test_a_status_with_nothing_waiting_says_the_icon_would_not_be_shown(stored, capsys):
    from cygnus.cli import main as cli

    assert cli.main(["tray", "status"]) == 0
    assert "(and not be shown)" in capsys.readouterr().out


# -- round 17 findings ------------------------------------------------------------------------------------------------------------------------
def _checked(hours_ago):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_system_count_is_dropped_once_packages_were_installed_or_upgraded_after_the_check(stored, monkeypatch, tmp_path):
    local = tmp_path / "pacman-local"
    local.mkdir()
    monkeypatch.setattr(tray, "PACMAN_LOCAL", local)
    stored["system"]({"packages": [{"name": "a"}, {"name": "b"}], "kernel": False, "error": None, "checked_at": _checked(5)})
    old = __import__("time").time() - 10 * 3600
    os.utime(local, (old, old))  # pacman last changed before the check: the list is current
    assert tray.pending()["system"] == 2
    new = __import__("time").time() - 3600
    os.utime(local, (new, new))  # an upgrade ran after the check: the list is out of date, so nothing is claimed
    assert tray.pending()["system"] == 0


@pytest.mark.parametrize("checked_at", [None, "yesterday", 5, "2026-13-45T99:99:99Z", ""])
def test_a_check_time_that_cannot_be_read_leaves_the_count_standing(stored, monkeypatch, tmp_path, checked_at):
    local = tmp_path / "pacman-local"
    local.mkdir()
    monkeypatch.setattr(tray, "PACMAN_LOCAL", local)
    stored["system"]({"packages": [{"name": "a"}], "error": None, "checked_at": checked_at})
    assert tray.pending()["system"] == 1


def test_a_database_that_is_locked_or_newer_does_not_take_the_system_count_down_with_it(stored, monkeypatch):
    import sqlite3

    stored["system"]({"packages": [{"name": "a"}], "error": None})
    monkeypatch.setattr(updates, "last_known", lambda reg: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    assert tray.pending() == {"programs": 0, "system": 1, "kernel": False, "total": 1, "names": []}


def test_the_poll_opens_the_registry_without_migrating_it(monkeypatch):
    seen = {}
    monkeypatch.setattr("cygnus.core.registry.open_registry", lambda path=None, **kw: seen.update(kw) or object())
    monkeypatch.setattr(updates, "last_known", lambda reg: [])
    tray.pending()
    assert seen == {"migrate": False}


def test_switching_off_leaves_a_link_to_a_file_that_is_not_cygnus_s_alone(tmp_path):
    target = tmp_path / "their-entry.desktop"
    target.write_text("[Desktop Entry]\nType=Application\nName=Theirs\nExec=true\n")
    tray.autostart_path().parent.mkdir(parents=True)
    tray.autostart_path().symlink_to(target)
    tray.disable()
    assert tray.autostart_path().is_symlink() and target.exists()


def test_the_icon_takes_one_lock_and_a_second_copy_leaves_quietly(tmp_path):
    from PySide6.QtCore import QLockFile

    held = QLockFile(str(tmp_path / "cygnus-tray.lock"))
    assert held.tryLock(0)
    try:
        result = _run("from cygnus.gui import tray; tray.WAIT_FOR_TRAY_SECONDS = 0; raise SystemExit(tray.main())", tmp_path, tray_on=True)
    finally:
        held.unlock()
    assert result.returncode == 0 and "no system tray" not in result.stderr  # it did not even look for a tray: another copy is running


def test_without_a_runtime_folder_the_lock_goes_to_the_persons_own_state_folder_never_to_tmp(tmp_path):
    (tmp_path / "config" / "cygnus").mkdir(parents=True)
    (tmp_path / "config" / "cygnus" / "preferences.json").write_text('{"tray": true}')
    env = {**os.environ, **{var: str(tmp_path / sub) for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                                                                       ("XDG_STATE_HOME", "state"))}, "QT_QPA_PLATFORM": "offscreen", "XDG_RUNTIME_DIR": str(tmp_path / "missing")}
    code = "from cygnus.gui import tray; tray.WAIT_FOR_TRAY_SECONDS = 0; raise SystemExit(tray.main())"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env, cwd=str(Path(__file__).resolve().parent.parent))
    assert result.returncode == 0 and "no system tray" in result.stderr
    assert (tmp_path / "state" / "cygnus").is_dir() and not list(Path("/tmp").glob("cygnus-tray*.lock"))  # the person's own folder was used (the lock goes when it exits), not /tmp


def test_it_never_starts_the_helper(stored, monkeypatch):
    from cygnus.core import privilege

    monkeypatch.setattr(privilege, "HelperClient", lambda *a, **k: pytest.fail("the tray started the root helper"))
    stored["statuses"] = _statuses(("A", updates.AVAILABLE))
    assert tray.pending()["total"] == 1
