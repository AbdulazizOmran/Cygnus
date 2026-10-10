"""Where the Arch news is used: attached to the system upgrade plan without ever holding it up, shown by `cygnus upgrade` with an
acknowledgement that --yes does not give, and in the Updates dialog behind a box that starts clear. (Static rules over the QML here;
test_gui_updates.py drives the real dialog.)"""

import argparse
import builtins
import re
import threading
import time

import pytest

from cygnus.cli import main as cli
from cygnus.core import news
from cygnus.core.privilege import Plan
from cygnus.gui import fixes
from tests.test_qml_text import QML, _function


def _code(text: str) -> str:
    return "\n".join(re.sub(r"\s//(?![/:]).*$", "", line) if not line.lstrip().startswith("//") else "" for line in text.splitlines())


UPDATES = _code((QML / "UpdatesPage.qml").read_text())

OK_MANUAL = {"ok": True, "items": [{"title": "foo requires manual intervention", "link": "https://archlinux.org/news/foo/", "published": 1.0,
                                    "summary": "Do this:\n• first", "manual": True}], "total": 1, "since": 0.0, "needs_ack": True, "checked_at": 1.0}
OK_PLAIN = {"ok": True, "items": [{"title": "New ISO", "link": "", "published": 1.0, "summary": "A new ISO.", "manual": False}], "total": 1,
            "since": 0.0, "needs_ack": False, "checked_at": 1.0}
NONE = {"ok": True, "items": [], "total": 0, "since": 0.0, "needs_ack": False, "checked_at": 1.0}
FAILED = {"ok": False, "error": "no route to host"}


# -- the plan -------------------------------------------------------------------------------------------------------------------------------

class Client:
    def plan_packages(self, **kw):
        return Plan("p1", {"upgrade": [{"name": "foo", "from": "1", "version": "2"}], "install": [], "remove": [], "download_bytes": 10}, "Upgrade")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(fixes, "HelperClient", Client)
    yield
    fixes._PENDING.clear()


def test_the_upgrade_plan_carries_the_news(client, monkeypatch):
    monkeypatch.setattr(news, "check", lambda: OK_MANUAL)
    out = fixes.plan_system_upgrade()
    assert out["news"] == OK_MANUAL and out["upgrades"] == [{"name": "foo", "from": "1", "to": "2"}] and out["token"] in fixes._PENDING


def test_a_feed_that_fails_is_part_of_the_plan_not_a_failure_of_it(client, monkeypatch):
    monkeypatch.setattr(news, "check", lambda: FAILED)
    assert fixes.plan_system_upgrade()["news"] == FAILED


def test_a_feed_that_is_slow_does_not_hold_the_plan_up(client, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(news, "check", lambda: release.wait(30) and OK_PLAIN)
    monkeypatch.setattr(fixes, "NEWS_WAIT_S", 0.3)
    started = time.monotonic()
    out = fixes.plan_system_upgrade()
    release.set()
    assert time.monotonic() - started < 5 and out["news"]["ok"] is False and "in time" in out["news"]["error"]


def test_a_plan_that_fails_does_not_wait_for_the_feed(monkeypatch):
    release = threading.Event()

    class Failing:
        def plan_packages(self, **kw):
            raise fixes.CygnusError("could not plan")

    monkeypatch.setattr(fixes, "HelperClient", Failing)
    monkeypatch.setattr(news, "check", lambda: release.wait(30))
    started = time.monotonic()
    with pytest.raises(fixes.CygnusError, match="could not plan"):
        fixes.plan_system_upgrade()
    release.set()
    assert time.monotonic() - started < 5


# -- the terminal -----------------------------------------------------------------------------------------------------------------------------

def args(**kw):
    return argparse.Namespace(**{"yes": False, "ack_news": False, **kw})


def test_news_that_needs_a_manual_step_is_shown_and_asked_about(monkeypatch, capsys):
    monkeypatch.setattr(news, "check", lambda: OK_MANUAL)
    asked = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": asked.append(prompt) or "y")
    assert cli._news_acknowledged(args()) is True
    out = capsys.readouterr().out
    assert "[MANUAL STEP] foo requires manual intervention" in out and "https://archlinux.org/news/foo/" in out and "• first" in out
    assert len(asked) == 1 and "[MANUAL STEP]" in asked[0]


@pytest.mark.parametrize("answer", ["n", "", "yes please maybe", "no"])
def test_anything_but_yes_cancels(monkeypatch, capsys, answer):
    monkeypatch.setattr(news, "check", lambda: OK_MANUAL)
    monkeypatch.setattr(builtins, "input", lambda prompt="": answer)
    assert cli._news_acknowledged(args()) is False and "cancelled" in capsys.readouterr().out


def test_yes_does_not_acknowledge_the_news_and_neither_does_the_end_of_input(monkeypatch, capsys):
    monkeypatch.setattr(news, "check", lambda: OK_MANUAL)

    def end_of_input(prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", end_of_input)
    assert cli._news_acknowledged(args(yes=True)) is False  # --yes answers the password question only


def test_the_flag_acknowledges_in_advance_and_nothing_is_asked(monkeypatch, capsys):
    monkeypatch.setattr(news, "check", lambda: OK_MANUAL)
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("nothing is asked when the flag is given"))
    assert cli._news_acknowledged(args(ack_news=True)) is True
    assert "[MANUAL STEP]" in capsys.readouterr().out  # still shown


def test_news_that_needs_nothing_is_shown_and_not_asked_about(monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("not asked"))
    monkeypatch.setattr(news, "check", lambda: OK_PLAIN)
    assert cli._news_acknowledged(args()) is True and "New ISO" in capsys.readouterr().out
    monkeypatch.setattr(news, "check", lambda: NONE)
    assert cli._news_acknowledged(args()) is True and "No Arch news since your last upgrade" in capsys.readouterr().out


def test_a_feed_that_cannot_be_read_does_not_stop_the_upgrade(monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("not asked"))
    monkeypatch.setattr(news, "check", lambda: FAILED)
    assert cli._news_acknowledged(args()) is True
    assert "could not be checked (no route to host)" in capsys.readouterr().out


def test_more_items_than_shown_say_so(monkeypatch, capsys):
    monkeypatch.setattr(news, "check", lambda: {**OK_PLAIN, "total": 14})
    cli._news_acknowledged(args())
    assert "and 13 more" in capsys.readouterr().out


def test_an_upgrade_does_not_reach_the_helper_until_the_news_is_acknowledged(monkeypatch):
    commits = []

    class UpClient:
        def plan_packages(self, **kw):
            return Plan("p1", {"upgrade": [{"name": "foo", "from": "1", "version": "2"}], "install": [], "remove": []}, "Upgrade")

    monkeypatch.setattr("cygnus.core.privilege.HelperClient", UpClient)
    monkeypatch.setattr(cli, "_helper_commit", lambda client, plan, yes: commits.append(plan.plan_id) or True)
    monkeypatch.setattr(news, "check", lambda: OK_MANUAL)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "n")
    assert cli.main(["upgrade", "--yes"]) == 1 and commits == []
    assert cli.main(["upgrade", "--yes", "--ack-news"]) == 0 and commits == ["p1"]


# -- the Updates dialog ----------------------------------------------------------------------------------------------------------------------------

def test_the_news_box_starts_clear_for_every_plan_and_is_never_ticked_for_you():
    body = _function(UPDATES, "    function planUpgrade()")
    assert body.index("newsRead = false;") < body.index("upgradeDialog.open()")
    assert "property bool newsRead: false" in UPDATES
    assert UPDATES.count("newsRead = true") == 0  # only the person's own click (onToggled) changes it
    assert "onToggled: page.newsRead = checked" in UPDATES


def test_the_update_button_waits_for_the_box_when_the_news_needs_a_manual_step():
    action = UPDATES[UPDATES.index('objectName: "updateAction"'):][:700]
    assert "(!page.newsNeedsAck() || page.newsRead)" in action
    run = _function(UPDATES, "    function runUpgrade()")
    assert "(newsNeedsAck() && !newsRead)" in run[:run.index("return;")]  # the other way in is guarded too
    needs = _function(UPDATES, "    function newsNeedsAck()")
    assert "news.ok" in needs and "news.needs_ack" in needs


def test_a_failed_news_check_is_said_and_does_not_block():
    needs = _function(UPDATES, "    function newsNeedsAck()")
    assert "news.ok &&" in needs  # no feed, no box: the update is not held for a web page
    assert "Arch news could not be checked" in UPDATES


def test_each_news_link_is_opened_only_by_a_click_and_is_text_otherwise():
    assert UPDATES.count("Qt.openUrlExternally(modelData.link)") == 1
    assert "onClicked: Qt.openUrlExternally(modelData.link)" in UPDATES
