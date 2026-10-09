"""Background check: each thing is reported once; problems that come back are reported again."""

from cygnus.core import watch


def _update(iid, name, version):
    return {"installation_id": iid, "name": name, "status": "update-available", "available": version, "facts": {}}


def _broken(app="org.whatpulse.WhatPulse", feature="Keyboard tracking"):
    return {"app_id": app, "name": "WhatPulse", "installs": [
        {"where": "/mnt/data/wp.AppImage", "overall": "missing_component",
         "features": [{"name": feature, "status": "missing_component"}, {"name": "Network", "status": "ok"}]}]}


def _run(data, now=1_000_000.0):
    shown = []
    notices = watch.collect(app_updates=data.get("app_updates", []), health=data.get("health", []),
                            interrupted=data.get("interrupted", []))
    show, state = watch.select(notices, data.get("system"), watch.load_state(), now=now)
    watch.save_state(state)
    shown += show
    return shown


def test_each_thing_is_reported_once():
    data = {"app_updates": [_update("a", "Helium", "0.19.0")], "health": [_broken()],
            "interrupted": [{"id": "op1", "title": "Installing Helium"}]}
    first = _run(data)
    assert sorted(n.title for n in first) == ["Installing Helium did not finish", "Update available",
                                              "WhatPulse: something stopped working"]
    assert "Keyboard tracking" in next(n for n in first if n.key.startswith("health")).body
    assert _run(data) == []  # nothing new


def test_a_problem_that_comes_back_is_reported_again():
    _run({"health": [_broken()]})
    assert _run({"health": []}) == []  # fixed
    assert [n.title for n in _run({"health": [_broken()]})] == ["WhatPulse: something stopped working"]


def test_a_newer_version_is_a_new_notice():
    _run({"app_updates": [_update("a", "Helium", "0.19.0")]})
    assert [n.body for n in _run({"app_updates": [_update("a", "Helium", "0.20.0")]})] == \
        ["Helium 0.20.0 — open Cygnus to update."]


def test_several_updates_make_one_notification():
    shown = _run({"app_updates": [_update("a", "Helium", "0.19.0"), _update("b", "Foo", "2.0")]})
    assert [n.title for n in shown] == ["2 application updates available"]


def test_system_updates_at_most_once_a_day():
    system = {"packages": [{"name": f"p{i}"} for i in range(12)], "kernel": True, "error": None}
    [n] = _run({"system": system}, now=1_000_000)
    assert n.title == "12 system updates available" and "kernel" in n.body
    more = {**system, "packages": system["packages"] + [{"name": "q"}]}
    assert _run({"system": more}, now=1_000_000 + 3600) == []  # changed, but within a day
    assert [n.title for n in _run({"system": more}, now=1_000_000 + 25 * 3600)] == ["13 system updates available"]
    assert _run({"system": {**system, "error": "offline"}}, now=1_000_000 + 50 * 3600) == []  # errors stay quiet


def test_cli_watch_runs_the_checks_and_notifies(monkeypatch, capsys):
    from cygnus.cli.main import main
    from cygnus.gui import service

    sent = []
    monkeypatch.setattr(service, "watch_gather", lambda progress, registry_path=None: {"app_updates": [_update("a", "Helium", "1.0")]})
    monkeypatch.setattr(watch, "send", lambda n: sent.append(n))
    assert main(["watch"]) == 0
    assert [n.title for n in sent] == ["Update available"]
    assert main(["watch"]) == 0 and "nothing new" in capsys.readouterr().out


def test_a_flatpak_update_without_a_version_is_reported_once():
    def flatpak_update(checked_at):
        return {"installation_id": "fp1", "name": "Krita", "status": "update-available", "available": None,
                "checked_at": checked_at, "facts": {"origin": "flathub", "commit": "abc123"}}

    assert len(_run({"app_updates": [flatpak_update("2026-10-07T08:00")]})) == 1
    assert _run({"app_updates": [flatpak_update("2026-10-07T14:00")]}) == []  # a later check: same update


def test_a_failing_notification_does_not_make_the_others_repeat():
    data = {"health": [_broken()], "interrupted": [{"id": "op1", "title": "Installing Helium"}]}
    calls = []

    def flaky(notice):
        calls.append(notice.title)
        if notice.key.startswith("op:"):
            raise TimeoutError("notify-send hung")

    shown = watch.run(gather=lambda: data, notify=flaky)
    assert [n.title for n in shown] == ["WhatPulse: something stopped working"] and len(calls) == 2
    # what was shown is not repeated, but the notice that could not be shown is tried again
    again = []
    retried = watch.run(gather=lambda: data, notify=lambda n: again.append(n.key))
    assert [n.key for n in retried] == ["op:op1"] and again == ["op:op1"]
    assert watch.run(gather=lambda: data, notify=lambda n: again.append(n.key)) == []  # now it is remembered


def test_notification_bodies_are_not_markup(monkeypatch):
    sent = []
    monkeypatch.setattr(watch.proc, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(watch.proc, "run", lambda argv, timeout: sent.append(argv))
    watch.send(watch.Notice(key="k", title="t", body='<a href="x">Evil & Co</a>'))
    assert sent[0][-1] == '&lt;a href="x"&gt;Evil &amp; Co&lt;/a&gt;'


def test_a_notify_send_that_exits_with_an_error_counts_as_not_shown(monkeypatch):
    results = iter([1, 0])
    monkeypatch.setattr(watch.proc, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(watch.proc, "run", lambda argv, timeout: watch.proc.Result(tuple(argv), next(results), "", "no daemon"))
    data = {"health": [_broken()]}
    assert watch.run(gather=lambda: data) == []  # the first run could not show it (the service was not up yet)
    assert [n.title for n in watch.run(gather=lambda: data)] == ["WhatPulse: something stopped working"]
    assert watch.run(gather=lambda: data) == []  # shown once: now it is remembered


def test_a_summary_that_could_not_be_shown_keeps_every_update_it_stands_for_unreported():
    data = {"app_updates": [_update("a", "Helium", "0.19.0"), _update("b", "Foo", "2.0")]}

    def down(notice):
        raise RuntimeError("no notification service")

    assert watch.run(gather=lambda: data, notify=down) == []
    shown = watch.run(gather=lambda: data, notify=lambda n: None)
    assert [n.title for n in shown] == ["2 application updates available"]


def test_a_system_update_notice_that_could_not_be_shown_is_mentioned_again():
    system = {"packages": [{"name": "x"}] * 3, "kernel": False}

    def down(notice):
        raise RuntimeError("no notification service")

    assert watch.run(gather=lambda: {"system": system}, notify=down) == []
    assert [n.title for n in watch.run(gather=lambda: {"system": system}, notify=lambda n: None)] == \
        ["3 system updates available"]


def test_notification_titles_are_not_markup_either(monkeypatch):
    sent = []
    monkeypatch.setattr(watch.proc, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(watch.proc, "run", lambda argv, timeout: sent.append(argv))
    watch.send(watch.Notice(key="k", title="<b>Evil</b>: something stopped working", body="x"))
    assert sent[0][-2] == "&lt;b&gt;Evil&lt;/b&gt;: something stopped working"
