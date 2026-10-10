"""Cygnus says when a newer Cygnus exists and where to get it, and nothing more: no download, no install."""

import argparse
import json

import pytest

from cygnus.cli import main as cli
from cygnus.core import self_update, watch
from cygnus.core.errors import CygnusError
from cygnus.core.util import http
from cygnus.gui import service

PAGE = "https://github.com/AbdulazizOmran/Cygnus/releases/tag/"


_DEFAULT = object()


def rel(tag, *, pre=True, draft=False, url=_DEFAULT):
    return {"tag_name": tag, "prerelease": pre, "draft": draft, "html_url": PAGE + tag if url is _DEFAULT else url}


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setattr(self_update, "__version__", "0.1.1")


def test_the_highest_numbered_release_is_found_whatever_order_github_lists_them_in():
    found = self_update.newest([rel("v0.1.0"), rel("v0.1.10"), rel("v0.1.9"), rel("v0.1.2", pre=False)])
    assert found == {"version": "0.1.10", "url": PAGE + "v0.1.10", "prerelease": True}  # 10 is more than 9 (numbers, not text)
    assert self_update.newest([rel("v1.0"), rel("0.9.9")])["version"] == "1.0"
    assert self_update.newest([rel("v0.2.0", pre=False)])["prerelease"] is False


@pytest.mark.parametrize("releases", [[], None, "x", {}, [None, 5, "v1.0"], [rel("nightly")], [rel("v1")], [rel("v1.2.3.4.5")], [rel("v2.0-rc1")],
                                      [rel("v9.9.9", draft=True)], [{"html_url": PAGE}], [{"tag_name": 5}], [{"tag_name": ["v1.0"]}]])
def test_tags_that_are_not_plain_versions_and_drafts_and_nonsense_are_ignored(releases):
    assert self_update.newest(releases) is None


@pytest.mark.parametrize("tag", ["v\u0660.\u0661.\u0662", "v1.\uff12", "v\u0663.0", "v1.0\n", " v1.0", "v1.0 ", "v1..0", "v.1.0", "V1.0"])
def test_only_ascii_digits_make_a_version(tag):
    assert self_update.newest([rel(tag)]) is None


def test_a_draft_or_odd_tag_never_hides_a_real_release():
    found = self_update.newest([rel("v9.9.9", draft=True), rel("nightly"), rel("v0.1.2"), rel("v3")])
    assert found["version"] == "0.1.2"


@pytest.mark.parametrize("url", ["https://evil.example/Cygnus/releases/x", "http://github.com/AbdulazizOmran/Cygnus/releases/x", "javascript:alert(1)",
                                 PAGE + "v1 .0", PAGE + "v1\n.0", "x" * 300, "", 5, None])
def test_an_address_that_is_not_this_projects_release_page_is_never_passed_on(url):
    found = self_update.newest([rel("v0.2.0", url=url)])
    assert found["url"] == self_update.PAGE_PREFIX  # the releases page itself, whatever the answer said


def test_a_good_check_says_newer_remembers_the_answer_and_asks_only_for_the_list_of_releases(tmp_path):
    asked = []

    def fetch(url, **kw):
        asked.append((url, kw))
        return [rel("v0.1.0"), rel("v0.1.2")]

    out = self_update.check(fetch=fetch)
    assert out["ok"] and out["newer"] and (out["current"], out["latest"], out["url"]) == ("0.1.1", "0.1.2", PAGE + "v0.1.2")
    [(url, kw)] = asked
    assert url == "https://api.github.com/repos/AbdulazizOmran/Cygnus/releases?per_page=15"  # the list, not "latest" (which skips pre-releases)
    assert kw["headers"] == {"Accept": "application/vnd.github+json"} and kw["limit"] <= 1024 * 1024
    assert kw["host_ok"]("api.github.com") and not kw["host_ok"]("evil.example") and not kw["host_ok"]("github.com")  # redirects stay on GitHub's API
    assert json.loads(self_update.cache_path().read_text())["latest"]["version"] == "0.1.2"
    assert self_update.last_known()["newer"] is True


def test_an_equal_or_older_release_is_not_newer():
    assert self_update.check(fetch=lambda url, **kw: [rel("v0.1.1")])["newer"] is False
    assert self_update.check(fetch=lambda url, **kw: [rel("v0.1.0")])["newer"] is False
    assert self_update.check(fetch=lambda url, **kw: [rel("v0.1.1.1")])["newer"] is True


def test_a_failed_check_says_so_and_keeps_what_was_known():
    self_update.check(fetch=lambda url, **kw: [rel("v0.2.0")])

    def down(url, **kw):
        raise http.HttpError("HTTP 403 for https://api.github.com/x", 403)

    out = self_update.check(fetch=down)
    assert out["ok"] is False and "403" in out["error"] and "newer" not in out
    assert self_update.last_known()["latest"] == "0.2.0"  # offline for a moment: the notice does not vanish
    assert self_update.check(fetch=lambda url, **kw: {"message": "rate limited"})["ok"] is False
    assert self_update.check(fetch=lambda url, **kw: [])["ok"] is False
    assert self_update.last_known()["latest"] == "0.2.0"


def test_after_the_update_the_notice_is_gone_without_asking_again(monkeypatch):
    self_update.check(fetch=lambda url, **kw: [rel("v0.2.0")])
    assert self_update.last_known()["newer"] is True
    monkeypatch.setattr(self_update, "__version__", "0.2.0")
    assert self_update.last_known()["newer"] is False and self_update.last_known()["current"] == "0.2.0"


@pytest.mark.parametrize("content", ["not json", "[]", '{"latest": 5}', '{"latest": {"version": "nightly"}}', '{"latest": {"version": ["0.2"]}}', "", "\0" * 50,
                                     '{"latest": {"version": "0.2.0", "url": "https://evil.example/x"}}'])
def test_a_damaged_or_tampered_cache_is_ignored_or_made_safe(content):
    path = self_update.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    got = self_update.last_known()
    assert got is None or got["url"] == self_update.PAGE_PREFIX


def test_nothing_is_known_before_the_first_check():
    assert self_update.last_known() is None


# -- the notification: once for each version -----------------------------------------------------------------------------------------------
def result(latest, newer=True):
    return {"ok": True, "current": "0.1.1", "latest": latest, "url": PAGE + "v" + latest, "newer": newer, "prerelease": True, "checked_at": "now"}


def test_a_new_version_is_announced_once_and_a_still_newer_one_again():
    state: dict = {}
    shown = []
    for latest, newer in (("0.1.2", True), ("0.1.2", True), ("0.1.2", True), ("0.1.3", True), ("0.1.3", False)):
        notices = watch.collect(app_updates=[], health=[], interrupted=[], cygnus=result(latest, newer))
        show, state = watch.select(notices, None, state)
        shown += [n.key for n in show]
    assert shown == ["cygnus:0.1.2", "cygnus:0.1.3"]


def test_there_is_no_notice_for_a_failed_or_missing_check():
    assert watch.collect(app_updates=[], health=[], interrupted=[], cygnus={"ok": False, "error": "x"}) == []
    assert watch.collect(app_updates=[], health=[], interrupted=[], cygnus=None) == []
    assert watch.collect(app_updates=[], health=[], interrupted=[]) == []
    [n] = watch.collect(app_updates=[], health=[], interrupted=[], cygnus=result("0.1.2"))
    assert "0.1.2" in n.body and "0.1.1" in n.body and n.title == "A new version of Cygnus is available"


def test_the_background_check_asks_and_hands_the_answer_to_the_notification(monkeypatch):
    from cygnus.core import updates

    monkeypatch.setattr(self_update, "check", lambda **kw: result("0.1.9"))
    monkeypatch.setattr(service, "interrupted_operations", lambda registry_path=None: [])
    monkeypatch.setattr(updates, "check_system_updates", lambda progress=None: (_ for _ in ()).throw(CygnusError("offline")))
    gathered = service.watch_gather(lambda line: None)
    assert gathered["cygnus"]["latest"] == "0.1.9"
    shown = []
    watch.run(gather=lambda: gathered, notify=shown.append)
    assert [n.key for n in shown] == ["cygnus:0.1.9"]


# -- the screens and the terminal ---------------------------------------------------------------------------------------------------------
def test_checking_for_updates_includes_cygnus_and_a_failure_there_does_not_break_the_rest(monkeypatch):
    from cygnus.core import updates

    monkeypatch.setattr(updates, "check_system_updates", lambda progress=None: (_ for _ in ()).throw(CygnusError("offline")))
    asked, lines = [], []
    real = self_update.check
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(self_update, "check", lambda **kw: asked.append(1) or {"ok": False, "error": "offline"})
        assert service.check_updates(lines.append) == [] and asked == [1]
    assert any("could not be looked for: offline" in l for l in lines)
    assert service.cygnus_update() is None  # nothing known yet
    real(fetch=lambda url, **kw: [rel("v0.3.0")])
    assert service.cygnus_update()["latest"] == "0.3.0" and service.cygnus_update()["newer"] is True


def test_the_terminal_says_where_to_get_it_in_words_and_in_json(capsys, monkeypatch):
    self_update.check(fetch=lambda url, **kw: [rel("v0.4.0")])
    ns = argparse.Namespace(check=False, json=False, registry=None)
    assert cli.cmd_updates(ns) == 1
    out = capsys.readouterr().out
    assert "Cygnus 0.4.0 is available (you have 0.1.1)" in out and PAGE + "v0.4.0" in out and "does not update itself" in out
    assert cli.cmd_updates(argparse.Namespace(check=False, json=True, registry=None)) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["cygnus"]["latest"] == "0.4.0" and data["cygnus"]["newer"] is True


def test_the_terminal_is_quiet_when_there_is_nothing_newer_and_says_when_it_is_up_to_date(capsys):
    self_update.check(fetch=lambda url, **kw: [rel("v0.1.1")])
    assert cli.cmd_updates(argparse.Namespace(check=False, json=False, registry=None)) == 0
    assert "Cygnus 0.1.1: up to date" in capsys.readouterr().out


def test_nothing_is_ever_downloaded_or_installed_by_the_notice(monkeypatch):
    monkeypatch.setattr(http, "download", lambda *a, **k: pytest.fail("a download"))
    monkeypatch.setattr("cygnus.core.privilege.HelperClient", lambda: pytest.fail("the helper"))
    self_update.check(fetch=lambda url, **kw: [rel("v0.5.0")])
    watch.collect(app_updates=[], health=[], interrupted=[], cygnus=self_update.last_known())
    self_update.last_known()


# -- what the second review found ---------------------------------------------------------------------------------------------------------
def test_a_check_that_fails_one_day_does_not_make_the_notice_come_again_the_next(monkeypatch):
    from cygnus.core import updates

    monkeypatch.setattr(service, "interrupted_operations", lambda registry_path=None: [])
    monkeypatch.setattr(updates, "check_system_updates", lambda progress=None: (_ for _ in ()).throw(CygnusError("offline")))
    answers = iter([[rel("v0.2.0")], "down", [rel("v0.2.0")], [rel("v0.3.0")], "down", "down", [rel("v0.3.0")]])
    shown = []

    def fetch(url, **kw):
        answer = next(answers)
        if answer == "down":
            raise http.HttpError("HTTP 503 for https://api.github.com/x", 503)
        return answer

    real = self_update.check
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(self_update, "check", lambda **kw: real(fetch=fetch))
        for _ in range(7):
            watch.run(gather=lambda: service.watch_gather(lambda line: None), notify=lambda n: shown.append(n.key))
    assert shown == ["cygnus:0.2.0", "cygnus:0.3.0"]  # each version once, however the days between went


def test_an_answer_that_nests_far_too_deep_is_just_a_failed_check():
    deep = "[" * 200_000 + "]" * 200_000
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(http, "get", lambda url, **kw: deep.encode())
        out = self_update.check()
    assert out["ok"] is False and "invalid JSON" in out["error"]

    def overflow(url, **kw):
        raise RecursionError("maximum recursion depth exceeded")

    out = self_update.check(fetch=overflow)  # whatever else goes wrong in reading it, it is an answer that says so
    assert out["ok"] is False and "RecursionError" in out["error"] and self_update.last_known()["last_error"] == out["error"]


def test_the_page_can_tell_that_the_last_try_failed_and_when_nothing_was_ever_learned(monkeypatch):
    assert self_update.last_known() is None
    self_update.check(fetch=lambda url, **kw: (_ for _ in ()).throw(http.HttpError("HTTP 500 for https://api.github.com/x", 500)))
    only_error = self_update.last_known()
    assert only_error["ok"] is False and only_error["newer"] is False and "500" in only_error["last_error"]  # no good answer yet, but a reason
    self_update.check(fetch=lambda url, **kw: [rel("v0.2.0")])
    good = self_update.last_known()
    assert good["newer"] is True and good["last_error"] is None
    self_update.check(fetch=lambda url, **kw: (_ for _ in ()).throw(http.HttpError("HTTP 500 for https://api.github.com/x", 500)))
    again = self_update.last_known()
    assert again["newer"] is True and again["latest"] == "0.2.0" and "500" in again["last_error"]  # the notice stays, and the failure is said too


def test_the_watch_answer_falls_back_to_what_was_known_and_says_nothing_new_when_nothing_was(monkeypatch):
    from cygnus.core import updates

    monkeypatch.setattr(service, "interrupted_operations", lambda registry_path=None: [])
    monkeypatch.setattr(updates, "check_system_updates", lambda progress=None: (_ for _ in ()).throw(CygnusError("offline")))
    down = lambda **kw: {"ok": False, "error": "offline"}  # noqa: E731
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(self_update, "check", down)
        assert service.watch_gather(lambda line: None)["cygnus"] == {"ok": False, "error": "offline"}  # never known: no notice
    self_update.check(fetch=lambda url, **kw: [rel("v0.2.0")])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(self_update, "check", down)
        gathered = service.watch_gather(lambda line: None)["cygnus"]
    assert gathered["ok"] and gathered["newer"] and gathered["latest"] == "0.2.0"
