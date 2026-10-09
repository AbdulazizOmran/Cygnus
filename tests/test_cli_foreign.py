"""`cygnus install FILE.deb`: the two options that need a deliberate choice are never taken on the person's behalf."""

import argparse

import pytest

from cygnus.cli import main as cli
from cygnus.gui import service

OPTIONAL = [{"package": "qt5-base", "libraries": ["libQt5Core.so.5"], "files": ["libqt5_shim.so"]}]
REVIEW = {"name": "chrome", "version": "1", "verdict": "needs review", "issues": [], "strategy": "review", "depends": [],
          "optional": OPTIONAL, "installable_after_review": True, "sha256": "ab" * 32,
          "scripts": {"unreadable": ["[postinst] command substitution"], "texts": {},
                      "mentions": [{"command": "groupadd", "what": "creates a group", "scripts": ["postinst"],
                                    "important": True}]}}


@pytest.fixture
def calls(monkeypatch):
    seen = {"convert": [], "commits": []}

    class Client:
        def plan_packages(self, **kw):
            return "plan"

    monkeypatch.setattr("cygnus.core.privilege.HelperClient", Client)
    monkeypatch.setattr(cli, "_helper_commit", lambda client, plan, yes: seen["commits"].append(plan) or True)
    monkeypatch.setattr(service, "file_sha256", lambda p: "cd" * 32)
    monkeypatch.setattr(service, "record_package_install", lambda *a, **k: None)

    def convert(path, progress, sha, optional=(), accept_unread_scripts=False):
        seen["convert"].append((list(optional), accept_unread_scripts))
        return {"package": "/x.pkg.tar.zst", "name": "chrome", "version": "1", "notes": [], "vendor_package": "chrome",
                "vendor_format": "deb"}

    monkeypatch.setattr(service, "convert_foreign", convert)
    return seen


def _run(monkeypatch, plan, **flags):
    monkeypatch.setattr(service, "analyse", lambda *a, **k: plan)
    options = {"with_optional": False, "accept_scripts": False, **flags}
    return cli._install_foreign(argparse.Namespace(file="/tmp/x.deb", registry=None, yes=True, **options))


def test_scripts_cygnus_could_not_read_are_never_accepted_by_yes_alone(monkeypatch, calls, capsys):
    assert _run(monkeypatch, REVIEW) == 1
    out = capsys.readouterr().out
    assert calls["convert"] == [] and "--accept-scripts" in out and "groupadd" in out  # shown, and nothing converted


def test_with_the_flag_the_scripts_are_accepted_and_the_conversion_is_told(monkeypatch, calls):
    assert _run(monkeypatch, REVIEW, accept_scripts=True) == 0
    assert calls["convert"] == [([], True)]  # the optional library was not asked for


def test_optional_libraries_are_only_installed_when_asked_for(monkeypatch, calls):
    plan = {**REVIEW, "strategy": "convert", "installable_after_review": False, "scripts": None}
    assert _run(monkeypatch, plan) == 0 and calls["convert"] == [([], False)]
    assert _run(monkeypatch, plan, with_optional=True) == 0 and calls["convert"][-1] == (["qt5-base"], False)


def test_a_package_that_may_not_be_gone_past_is_not_converted_even_with_the_flag(monkeypatch, calls):
    plan = {**REVIEW, "installable_after_review": False, "scripts": None}
    assert _run(monkeypatch, plan, accept_scripts=True) == 1 and calls["convert"] == []


class _TimedOut:
    def __init__(self, state, ledger_down=False):
        self.state, self.ledger_down = state, ledger_down

    def commit(self, plan, on_progress=None):
        from cygnus.core.privilege import HelperTimeout

        raise HelperTimeout("op-9")

    def operation_state(self, op_id, timeout_ms=None):
        if self.ledger_down:
            raise RuntimeError("no bus")
        return self.state


PLAN = type("P", (), {"message": "Install thing"})()


def test_a_helper_that_stops_answering_is_judged_by_its_ledger_not_assumed_done(capsys):
    assert cli._helper_commit(_TimedOut("succeeded"), PLAN, True) is True  # it did finish: the caller records it
    assert cli._helper_commit(_TimedOut("failed"), PLAN, True) is False
    assert "reported that the change failed" in capsys.readouterr().out
    for client in (_TimedOut("running"), _TimedOut(None), _TimedOut("failed", ledger_down=True)):
        with pytest.raises(cli.CygnusError, match="lost contact with the administrator helper.*list of installed software was NOT updated.*check what was really changed"):
            cli._helper_commit(client, PLAN, True)  # unknown: nothing is recorded; the person is told to check
    with pytest.raises(cli.CygnusError, match="stopped before it finished"):
        cli._helper_commit(_TimedOut("interrupted"), PLAN, True)


def test_progress_lines_that_only_move_a_bar_are_not_printed(capsys):
    from cygnus.core.progress import Progress

    cli._say(Progress("Installing org.x.App… 37%", 0.37, log=False))
    cli._say(Progress("Installing org.x.App…", 0.0))
    cli._say("plain line")
    cli._say("indented", "  ")
    assert capsys.readouterr().out.splitlines() == ["Installing org.x.App…", "plain line", "  indented"]
