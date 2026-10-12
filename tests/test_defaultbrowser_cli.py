"""`cygnus default-browser …`: reads change nothing, `set` asks first and reads the result back, `undo` puts back what was there, and a
browser that cannot be chosen is refused with the reason. Real xdg tools, in the test's own isolated folders."""

import builtins
import json
import os
import shutil
from pathlib import Path

import pytest

from cygnus.cli import main as cli
from cygnus.core import defaultbrowser as db
from cygnus.core import preferences
from cygnus.core.desktop import integrate

real = pytest.mark.needs_tool("xdg-settings", "xdg-mime", "gio")
ALL = ";".join(db.MIME_TYPES) + ";"
PROGRAM = shutil.which("true") or "/usr/bin/true"


@pytest.fixture(autouse=True)
def _session(tmp_path, monkeypatch):
    for var in ("XDG_CURRENT_DESKTOP", "KDE_FULL_SESSION", "KDE_SESSION_VERSION", "DESKTOP_SESSION", "XDG_SESSION_DESKTOP",
                "GNOME_DESKTOP_SESSION_ID", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "no-system-data"))


@pytest.fixture
def apps():
    folder = integrate.xdg_data_home() / "applications"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _browser(apps: Path, name: str, title: str, claims: str = ALL) -> None:
    (apps / name).write_text(f"[Desktop Entry]\nType=Application\nName={title}\nExec={PROGRAM} %U\nCategories=Network;WebBrowser;\nMimeType={claims}\n")


def _answers(monkeypatch, *replies):
    queue = list(replies)
    asked = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": (asked.append(prompt), queue.pop(0))[1])
    return asked


def test_the_browsers_are_listed_by_name_and_file(apps, capsys):
    _browser(apps, "helium.desktop", "Helium")
    _browser(apps, "firefox.desktop", "Firefox")
    assert cli.main(["default-browser", "list"]) == 0
    out = capsys.readouterr().out
    assert "Firefox  (firefox.desktop)" in out and "Helium  (helium.desktop)" in out
    assert cli.main(["default-browser", "list", "--json"]) == 0
    assert {b["desktop_id"] for b in json.loads(capsys.readouterr().out)} == {"firefox.desktop", "helium.desktop"}


@real
def test_status_tells_chosen_from_first_by_accident_and_changes_nothing(apps, capsys):
    _browser(apps, "helium.desktop", "Helium")
    assert cli.main(["default-browser", "status"]) == 0
    assert "chosen on purpose: no" in capsys.readouterr().out
    assert not (Path(os.environ["XDG_CONFIG_HOME"]) / "mimeapps.list").exists()


@real
def test_set_asks_first_and_no_changes_nothing(apps, monkeypatch, capsys):
    _browser(apps, "helium.desktop", "Helium")
    asked = _answers(monkeypatch, "")
    assert cli.main(["default-browser", "set", "Helium"]) == 1
    assert "cancelled" in capsys.readouterr().out and asked
    assert "browser_backup" not in preferences.load()


@real
def test_set_by_the_shown_name_the_file_name_or_the_bare_name_and_then_undo(apps, capsys):
    _browser(apps, "helium.desktop", "Helium")
    _browser(apps, "firefox.desktop", "Firefox")
    for spec in ("Helium", "helium", "helium.desktop", "HELIUM"):
        assert cli.main(["default-browser", "set", spec, "-y"]) == 0, spec
    out = capsys.readouterr().out
    assert out.count("done: links open with Helium") == 1 and out.count("already so") == 3
    assert cli.main(["default-browser", "status"]) == 0
    assert "chosen on purpose: yes" in capsys.readouterr().out
    assert cli.main(["default-browser", "undo", "-y"]) == 0
    assert "browser_backup" not in preferences.load()
    assert "helium" not in (Path(os.environ["XDG_CONFIG_HOME"]) / "mimeapps.list").read_text()


@real
def test_a_browser_that_cannot_be_chosen_is_refused_with_the_reason(apps, capsys):
    _browser(apps, "half.desktop", "Half", claims="text/html;")
    assert cli.main(["default-browser", "set", "half.desktop", "-y"]) == 1
    err = capsys.readouterr().err
    assert "cannot be the default browser" in err and "http links" in err and "https links" in err


def test_a_name_nobody_has_and_an_ambiguous_one_are_explained(apps, capsys):
    _browser(apps, "a.desktop", "Same")
    _browser(apps, "b.desktop", "Same")
    assert cli.main(["default-browser", "set", "Nothing", "-y"]) == 2
    assert "no web browser called Nothing" in capsys.readouterr().err
    assert cli.main(["default-browser", "set", "same", "-y"]) == 2
    assert "more than one web browser is called same" in capsys.readouterr().err


def test_undo_without_a_memory_says_there_is_nothing_to_put_back(apps, capsys):
    if shutil.which("xdg-settings") is None:
        pytest.skip("requires xdg-settings")
    assert cli.main(["default-browser", "undo", "-y"]) == 2
    assert "nothing to put back" in capsys.readouterr().err
