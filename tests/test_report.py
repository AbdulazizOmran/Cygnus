"""`cygnus report`: something to paste into a bug report that leaves out what is private and changes nothing."""

import pytest

from cygnus.core import report
from cygnus.gui import service


def test_the_home_folder_and_the_user_name_are_replaced_wherever_they_appear(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/alice")))
    monkeypatch.setenv("USER", "alice")
    text = "path /home/alice/.cache/x, owner alice, by alice.\nalice-laptop and malice stay: " + "/home/alice"
    out = report.redact(text)
    assert "/home/alice" not in out and "~/.cache/x" in out and "owner <user>," in out and "by <user>." in out
    assert "alice-laptop" in out and "malice" in out  # only the whole word is replaced
    assert report.redact("nothing to hide") == "nothing to hide"


def test_the_report_names_the_parts_and_lists_counts_never_applications():
    service.record_package_install("secret-app-name", "1.0", origin="converted", source={"file": "/home/someone/Downloads/secret.deb"})
    out = report.build()
    for heading in ("# Cygnus report", "## System", "## Tools", "## Python modules", "## Installation", "## Your setup", "## Helper log"):
        assert heading in out
    assert "1 pacman" in out and "converted from a .deb/.rpm: 1" in out
    assert "secret-app-name" not in out and "secret.deb" not in out and "Downloads" not in out


def test_the_report_never_starts_the_root_helper(monkeypatch):
    from cygnus.core import privilege

    monkeypatch.setattr(privilege, "HelperClient", lambda *a, **k: pytest.fail("a report must not talk to the helper"))
    assert "## Your setup" in report.build()


def test_one_thing_that_cannot_be_read_does_not_spoil_the_report(monkeypatch):
    import cygnus.core.registry as registry

    monkeypatch.setattr(registry, "open_registry", lambda path=None: (_ for _ in ()).throw(OSError("disk on fire")))
    out = report.build()
    assert "could not read Cygnus's own data: OSError: disk on fire" in out and "## Helper log" in out


def test_the_command_prints_the_report(capsys):
    from cygnus.cli import main as cli

    assert cli.main(["report"]) == 0
    assert capsys.readouterr().out.startswith("# Cygnus report")
