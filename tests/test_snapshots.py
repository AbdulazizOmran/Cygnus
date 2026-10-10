"""What protects you if an update goes wrong, said honestly: judged from the hook files pacman will really use and from snap-pac's own
settings (nothing is run), with "normally" where a hook could still fail, and never claiming more than is true."""

import argparse
import builtins
import re
import threading
import time
from pathlib import Path

import pytest

from cygnus.cli import main as cli
from cygnus.core import news, snapshots
from cygnus.core.backends import pacman as pm
from cygnus.core.privilege import Plan
from cygnus.gui import fixes
from tests.test_qml_text import QML

SHIPPED = "usr/share/libalpm/hooks"
HOOKS = ("05-snap-pac-pre.hook", "zz-snap-pac-post.hook")


def system(tmp_path, *, snap_pac=True, snapper=False, snapper_config=True, timeshift=False, ini=None, configs='SNAPPER_CONFIGS="root"\n'):
    root = tmp_path / "root"
    for d in (SHIPPED, "usr/bin", "etc/pacman.d/hooks", "etc/snapper/configs", "etc/conf.d"):
        (root / d).mkdir(parents=True, exist_ok=True)
    if configs is not None:
        (root / "etc/conf.d/snapper").write_text("## Path: System/Snapper\n" + configs)
    if snap_pac:
        for h in HOOKS:
            (root / SHIPPED / h).write_text("[Trigger]\n")
    if snapper:
        (root / "usr/bin/snapper").write_text("#!/bin/sh\n")
    if snapper_config:
        (root / "etc/snapper/configs/root").write_text('SUBVOLUME="/"\n')
    if timeshift:
        (root / "usr/bin/timeshift").write_text("#!/bin/sh\n")
    if ini is not None:
        (root / "etc/snap-pac.ini").write_text(ini)
    return str(root)


def look(root):
    return snapshots.situation(("/etc/pacman.d/hooks/",), root=root)


def test_snap_pac_in_place_is_said_to_snapshot_normally_and_cygnus_adds_none(tmp_path):
    got = look(system(tmp_path))
    assert got["state"] == "snap-pac" and "normally" in got["text"] and "Cygnus adds none of its own" in got["text"]
    assert "stops before changing anything" not in got["text"]  # snap-pac does not look at snapper's exit status: a failed snapshot does not stop pacman
    assert "goes on without a snapshot" in got["text"]


def test_snap_pac_wins_over_snapper_and_timeshift(tmp_path):
    assert look(system(tmp_path, snapper=True, timeshift=True))["state"] == "snap-pac"


def test_a_hook_switched_off_with_a_link_to_dev_null_is_not_called_active(tmp_path):
    root = system(tmp_path)
    (Path(root) / "etc/pacman.d/hooks/05-snap-pac-pre.hook").symlink_to("/dev/null")
    got = look(root)
    assert got["state"] == "snap-pac-off" and "switched off" in got["text"]


def test_a_hook_replaced_by_the_persons_own_is_named_as_something_cygnus_cannot_judge(tmp_path):
    root = system(tmp_path)
    (Path(root) / "etc/pacman.d/hooks/05-snap-pac-pre.hook").write_text("[Trigger]\n# my own\n")
    got = look(root)
    assert got["state"] == "snap-pac" and "replaced by one of your own" in got["text"]


def test_the_hook_folders_are_searched_in_pacmans_order_and_the_shipped_hook_is_last(tmp_path):
    root = system(tmp_path)
    second = Path(root) / "etc/other-hooks"
    second.mkdir()
    (second / "05-snap-pac-pre.hook").symlink_to("/dev/null")
    got = snapshots.situation(("/etc/pacman.d/hooks/", "/etc/other-hooks/"), root=root)
    assert got["state"] == "snap-pac-off"  # the second folder still comes before the shipped one
    assert snapshots.situation(("/etc/pacman.d/hooks/",), root=root)["state"] == "snap-pac"


def test_a_disabled_post_hook_alone_keeps_the_snapshot_before_but_the_text_says_only_before(tmp_path):
    root = system(tmp_path)
    (Path(root) / "etc/pacman.d/hooks/zz-snap-pac-post.hook").symlink_to("/dev/null")
    got = look(root)
    assert got["state"] == "snap-pac" and "before every package change" in got["text"] and "before and after" not in got["text"]
    assert '"after" hook is switched off' in got["text"]
    assert "before and after every package change" in look(system(tmp_path / "other"))["text"]  # with both hooks, both are claimed


@pytest.mark.parametrize("ini, state", [
    ("", "snap-pac"), ("[root]\nsnapshot = True\n", "snap-pac"), ("[root]\nsnapshot = False\n", "snap-pac-off"),
    ("[root]\nsnapshot = no\n", "snap-pac-off"), ("[root]\nsnapshot = 0\n", "snap-pac-off"), ("[root]\nsnapshot = OFF\n", "snap-pac-off"),
    ("[DEFAULT]\nsnapshot = False\n", "snap-pac"),  # snap-pac sets root's value itself: a [DEFAULT] section never reaches root
    ("[DEFAULT]\nsnapshot = False\n[root]\nsnapshot = True\n", "snap-pac"), ("[DEFAULT]\nsnapshot = True\n[root]\nsnapshot = False\n", "snap-pac-off"),
    ("[home]\nsnapshot = False\n", "snap-pac"),  # another configuration: not the root one
    ("[root]\ndesc_limit = 40\n", "snap-pac"), ("this is not an ini file\n", "snap-pac-unclear"),
    ("\ufeff[root]\nsnapshot = no\n", "snap-pac-unclear"),  # a file configparser cannot read (snap-pac reads it the same way): not "protected"
])
def test_snap_pacs_own_settings_decide_whether_the_root_configuration_is_snapshotted(tmp_path, ini, state):
    assert look(system(tmp_path, ini=ini))["state"] == state


def test_a_file_of_your_own_named_like_a_snap_pac_hook_does_not_make_snap_pac_installed(tmp_path):
    root = system(tmp_path, snap_pac=False)
    (Path(root) / "etc/pacman.d/hooks/05-snap-pac-pre.hook").write_text("[Trigger]\n# a leftover of mine\n")
    assert look(root)["state"] == "none"
    root = system(tmp_path / "again", snap_pac=False, snapper=True)
    (Path(root) / "etc/pacman.d/hooks/05-snap-pac-pre.hook").write_text("[Trigger]\n")
    assert look(root)["state"] == "snapper"


@pytest.mark.parametrize("configs, state", [
    ('SNAPPER_CONFIGS="root"\n', "snap-pac"), ('SNAPPER_CONFIGS="root home"\n', "snap-pac"), ('SNAPPER_CONFIGS="home"\n', "snap-pac-off"),
    ('SNAPPER_CONFIGS=""\n', "snap-pac-off"), (None, "snap-pac-unclear"),
    ("# no such line\n", "snap-pac-unclear"), ('export SNAPPER_CONFIGS="root"\n', "snap-pac-unclear"), (' SNAPPER_CONFIGS="root"\n', "snap-pac-unclear"),
])  # (snap-pac reads the line that STARTS with SNAPPER_CONFIGS; with none it fails, and a failing pre hook stops pacman)
def test_snap_pac_snapshots_the_configurations_snapper_lists_so_root_must_be_among_them(tmp_path, configs, state):
    got = look(system(tmp_path, configs=configs))
    assert got["state"] == state
    if state == "snap-pac-off":
        assert "do not include" in got["text"] and "root" in got["text"]
    if state == "snap-pac-unclear":
        assert "SNAPPER_CONFIGS" in got["text"] and "may refuse" in got["text"]


def test_snap_pac_that_is_not_there_is_not_claimed(tmp_path):
    got = look(system(tmp_path, snap_pac=False, snapper=True))
    assert got["state"] == "snapper" and "nothing takes a snapshot" in got["text"] and "snapper create" in got["text"]
    assert "normally takes" not in got["text"]


def test_snapper_without_a_configuration_says_so(tmp_path):
    root = system(tmp_path, snap_pac=False, snapper=True, snapper_config=False)
    assert "no configuration" in look(root)["text"]


def test_timeshift_is_named_and_not_run(tmp_path):
    got = look(system(tmp_path, snap_pac=False, timeshift=True))
    assert got["state"] == "timeshift" and "does not run it" in got["text"]


def test_no_tool_at_all_is_said(tmp_path):
    got = look(system(tmp_path, snap_pac=False))
    assert got["state"] == "none" and "nothing to roll back" in got["text"]


def test_looking_changes_nothing(tmp_path):
    root = system(tmp_path, snapper=True)
    before = sorted(str(p) for p in Path(root).rglob("*"))
    look(root)
    assert sorted(str(p) for p in Path(root).rglob("*")) == before


def test_the_partial_upgrade_explanation_makes_no_claim_about_snapshots_at_all():
    issue = pm._partial_upgrade_issue([{"name": "a", "installed_version": "1", "version": "2"}], [])
    for text in (issue.title, issue.explanation, *(r.explanation for r in issue.resolutions), *(r.title for r in issue.resolutions)):
        assert "snapshot" not in text.lower() and "snapper" not in text.lower()  # what protects the person is said by cygnus.core.snapshots


# -- where it is shown -----------------------------------------------------------------------------------------------------------------

class Client:
    def plan_packages(self, **kw):
        return Plan("p1", {"upgrade": [{"name": "foo", "from": "1", "version": "2"}], "install": [], "remove": []}, "Upgrade")


def test_the_upgrade_plan_carries_the_snapshot_note(monkeypatch):
    monkeypatch.setattr(fixes, "HelperClient", Client)
    monkeypatch.setattr(news, "check", lambda: {"ok": True, "items": [], "total": 0, "since": None, "needs_ack": False, "checked_at": 1.0})
    monkeypatch.setattr(pm, "read_config", lambda: pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=()))
    monkeypatch.setattr(snapshots, "situation", lambda hookdirs: {"state": "none", "text": "No snapshot tool was found"})
    out = fixes.plan_system_upgrade()
    assert out["snapshot"] == {"state": "none", "text": "No snapshot tool was found"}
    fixes._PENDING.clear()


def test_a_note_that_cannot_be_made_never_stops_the_plan(monkeypatch):
    monkeypatch.setattr(pm, "read_config", lambda: (_ for _ in ()).throw(RuntimeError("no pacman")))
    got = fixes._snapshot_situation()
    assert got["state"] == "unknown" and "no pacman" in got["text"]


def test_the_terminal_prints_the_note_before_the_news_and_the_question(monkeypatch, capsys):
    order = []
    monkeypatch.setattr("cygnus.core.privilege.HelperClient", Client)
    monkeypatch.setattr(pm, "read_config", lambda: pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=()))
    monkeypatch.setattr(snapshots, "situation", lambda hookdirs: order.append("snapshot") or {"state": "none", "text": "No snapshot tool was found"})
    monkeypatch.setattr(news, "check_within", lambda seconds, **kw: order.append("news") or {"ok": True, "items": [], "total": 0, "since": 0, "needs_ack": False})
    monkeypatch.setattr(cli, "_helper_commit", lambda c, p, y: order.append("question") or True)
    assert cli.main(["upgrade", "--yes"]) == 0
    assert order == ["snapshot", "news", "question"] and "Snapshot: No snapshot tool was found" in capsys.readouterr().out


def test_the_dialog_shows_the_snapshot_note():
    text = (QML / "UpdatesPage.qml").read_text()
    assert 'text: page.upgradePlan && page.upgradePlan.snapshot ? "Snapshot: " + page.upgradePlan.snapshot.text : ""' in text


# -- every way to start a full upgrade shows the news -------------------------------------------------------------------------------------

def test_only_two_places_plan_a_full_upgrade_and_both_really_show_the_news_and_the_snapshot_note():
    """The acknowledgement only means something if every full upgrade passes through it. (The "upgrade the whole system, then install"
    resolution that a partial-upgrade refusal offers is data that nothing acts on; if something ever does, it must be added here.)"""
    import inspect

    root = Path(__file__).resolve().parent.parent / "cygnus"
    users = sorted(str(p.relative_to(root)) for p in root.rglob("*.py")
                   if "helper" not in p.parts and re.search(r"plan_packages\(\s*sysupgrade=True", p.read_text()))
    assert users == ["cli/main.py", "gui/fixes.py"]
    gui = inspect.getsource(fixes.plan_system_upgrade)  # the function itself, not a word somewhere in the file
    assert "news.start()" in gui and 'out["news"] = waiting(' in gui and 'out["snapshot"] = _snapshot_situation()' in gui
    cmd = inspect.getsource(cli.cmd_upgrade)
    assert cmd.index("_snapshot_note()") < cmd.index("_news_acknowledged(args)") < cmd.index("_helper_commit(")
    acting = [str(p.relative_to(root)) for p in root.rglob("*.py") if "sysupgrade_with_targets" in p.read_text()]
    assert acting == ["core/backends/pacman.py"]  # only where the resolution is described


# -- the news is never waited for for long in the terminal ----------------------------------------------------------------------------------

def test_the_terminal_does_not_wait_for_a_slow_feed(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(news, "check", lambda **kw: release.wait(30) and {"ok": True})
    started = time.monotonic()
    got = news.check_within(0.3)
    release.set()
    assert time.monotonic() - started < 5 and got["ok"] is False and "in time" in got["error"]
    assert cli.NEWS_SECONDS <= 30


def test_a_feed_that_answers_in_time_is_returned_as_it_is(monkeypatch):
    monkeypatch.setattr(news, "check", lambda **kw: {"ok": True, "items": [], "total": 0, "since": 0, "needs_ack": False, "checked_at": 1.0})
    assert news.check_within(5)["ok"] is True
