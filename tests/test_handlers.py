"""Becoming the default installer: Flathub's Install button, Flatpak/deb/rpm/AppImage files. Run with the REAL xdg-mime in the
test's own isolated config folder, so what happens is what happens on a machine, and nothing real is touched."""

import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cygnus.core import handlers, preferences
from cygnus.core.errors import CygnusError

real = pytest.mark.needs_tool("xdg-mime", "gio")


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Cygnus's menu entry (and a few others) exist in the test's own application folder; the machine's real application
    folders are out of sight, so what is installed here (Shelly, Discover...) cannot leak in."""
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "no-system-data"))
    apps = Path(os.environ["XDG_DATA_HOME"]) / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    claims = ";".join(handlers.MIME_TYPES) + ";application/vnd.flatpak.repo;"  # xdg-mime ignores a default that does not claim the type
    for name in (handlers.DESKTOP_ID, "org.example.Previous.desktop", "org.example.Other.desktop", "org.example.A.desktop",
                 "org.example.B.desktop"):
        (apps / name).write_text(f"[Desktop Entry]\nType=Application\nName={name}\nExec=true %U\nMimeType={claims}\n")
    return Path(os.environ["XDG_CONFIG_HOME"]) / "mimeapps.list"


def _seed(config: Path, **defaults):
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[Default Applications]\n" + "".join(f"{m}={d}\n" for m, d in defaults.items()))


@real
def test_enabling_makes_cygnus_the_default_for_everything_it_installs_and_remembers_what_was_there(installed):
    _seed(installed, **{"application/vnd.flatpak.ref": "org.example.Previous.desktop"})
    state = handlers.enable()
    assert state["enabled"] and all(v == handlers.DESKTOP_ID for v in state["current"].values())
    assert set(state["current"]) == set(handlers.MIME_TYPES)
    assert "application/vnd.flatpak.repo" not in installed.read_text()  # repository files stay with whoever explains them
    assert preferences.load()["handler_backup"]["application/vnd.flatpak.ref"] == "org.example.Previous.desktop"


@real
def test_disabling_puts_back_what_each_type_was_opened_by(installed):
    _seed(installed, **{"application/vnd.flatpak.ref": "org.example.Previous.desktop"})
    handlers.enable()
    state = handlers.disable()
    assert not state["enabled"]
    assert state["current"]["application/vnd.flatpak.ref"] == "org.example.Previous.desktop"
    text = installed.read_text()
    assert handlers.DESKTOP_ID not in text  # no leftover line pointing at Cygnus
    assert "handler_backup" not in preferences.load()


@real
def test_enabling_twice_keeps_the_first_memory_and_does_not_remember_cygnus_itself(installed):
    _seed(installed, **{"application/vnd.flatpak.ref": "org.example.Previous.desktop"})
    handlers.enable()
    handlers.enable()
    assert preferences.load()["handler_backup"]["application/vnd.flatpak.ref"] == "org.example.Previous.desktop"
    assert handlers.disable()["current"]["application/vnd.flatpak.ref"] == "org.example.Previous.desktop"


@real
def test_a_type_someone_else_has_changed_since_is_left_as_they_set_it(installed):
    handlers.enable()
    config = installed.read_text().replace(f"x-scheme-handler/appstream={handlers.DESKTOP_ID}",
                                           "x-scheme-handler/appstream=org.example.Other.desktop")
    installed.write_text(config)
    assert "org.example.Other.desktop" in config
    state = handlers.disable()
    assert state["current"]["x-scheme-handler/appstream"] == "org.example.Other.desktop"


def test_without_the_menu_entry_it_cannot_be_chosen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "no-system-data"))  # not even the machine's own installation
    state = handlers.status()
    assert state["available"] is False and "menu entry" in state["why"]
    with pytest.raises(CygnusError, match="menu entry"):
        handlers.enable()


def test_a_type_that_will_not_change_is_reported_by_name(installed, monkeypatch):
    def run(argv, **kw):  # a program that keeps answering "Shelly" whatever is set
        out = "Default application for \u201cx\u201d: shelly.desktop\nRegistered applications:\n" if argv[0] == "gio" else ""
        return NS(stdout=out, stderr="", returncode=0)

    monkeypatch.setattr(handlers.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(CygnusError, match="could not be set.*x-scheme-handler/appstream.*menu entry does not claim"):
        handlers.enable(run)
    assert "handler_backup" in preferences.load()  # remembered before anything was changed


def test_clearing_one_type_keeps_other_programs_in_its_list(installed):
    _seed(installed, **{"application/vnd.flatpak.ref": f"org.example.A.desktop;{handlers.DESKTOP_ID};"})
    installed.write_text(installed.read_text() + "\n[Added Associations]\napplication/vnd.flatpak.ref=org.example.B.desktop;\n")
    handlers._forget_default("application/vnd.flatpak.ref")
    text = installed.read_text()
    assert "application/vnd.flatpak.ref=org.example.A.desktop;" in text and handlers.DESKTOP_ID not in text
    assert "[Added Associations]" in text and "org.example.B.desktop" in text


def test_a_hand_spaced_line_is_cleared_too(installed):
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(f"[Default Applications]\napplication/vnd.flatpak.ref = {handlers.DESKTOP_ID};\n")
    handlers._forget_default("application/vnd.flatpak.ref")
    assert handlers.DESKTOP_ID not in installed.read_text()


def test_a_stray_non_utf8_byte_in_a_comment_does_not_stop_clearing_and_is_kept(installed):
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_bytes(b"# caf\xe9\n[Default Applications]\napplication/vnd.flatpak.ref=" + handlers.DESKTOP_ID.encode() + b";\n")
    handlers._forget_default("application/vnd.flatpak.ref")
    data = installed.read_bytes()
    assert b"# caf\xe9\n" in data and handlers.DESKTOP_ID.encode() not in data


def test_a_mimeapps_file_kept_as_a_link_stays_a_link(installed, tmp_path):
    target = tmp_path / "dotfiles" / "mimeapps.list"
    target.parent.mkdir()
    target.write_text(f"[Default Applications]\napplication/vnd.flatpak.ref={handlers.DESKTOP_ID};\n")
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.symlink_to(target)
    handlers._forget_default("application/vnd.flatpak.ref")
    assert installed.is_symlink() and handlers.DESKTOP_ID not in target.read_text()


def test_a_file_that_does_not_list_the_type_is_not_rewritten(installed):
    _seed(installed, **{"application/vnd.flatpak.ref": "org.example.A.desktop;"})
    before = installed.stat().st_ino
    handlers._forget_default("x-scheme-handler/appstream")
    assert installed.stat().st_ino == before  # an atomic write would have replaced the file


def test_turning_it_off_without_the_tools_says_which_are_missing(installed, monkeypatch):
    monkeypatch.setattr(handlers.shutil, "which", lambda name: None)
    with pytest.raises(CygnusError, match="xdg-mime and gio .*not installed"):
        handlers.disable()
