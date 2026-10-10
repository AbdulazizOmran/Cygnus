"""Running an install script as administrator through the helper: a plan bound to the exact bytes that were shown, one command that
is checked against the plan, authorisation asked every time. Nothing here runs a script: the runner is faked."""

import json
import os
import pwd
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from cygnus.core.privilege import HelperError
from cygnus.core.util import scriptrun
from cygnus.helper import actions
from cygnus.helper.actions import PlanRefused

REPO = Path(__file__).resolve().parent.parent
BODY = b"#!/bin/sh\necho hello > /tmp/not-run\n"
SHA = scriptrun.sha256(BODY)
ME = pwd.getpwuid(os.getuid())


def make(tmp_path, *, name="postinst.sh", data=BODY, sha=SHA, folder=None):
    folder = folder if folder is not None else str(tmp_path)
    return actions.plan_script(name, data, sha, folder, caller_uid=os.getuid(), caller_sender=":1.1")


def test_a_script_plan_says_what_will_happen_and_builds_one_command(tmp_path):
    plan = make(tmp_path)
    assert plan.kind == "script" and plan.action_id.endswith(".scripts.run") and "postinst.sh" in plan.message and SHA[:8] in plan.message
    assert "as administrator" in plan.message and "cannot undo" in plan.message
    argv = actions.script_argv(plan)
    assert argv[:5] == ["systemd-run", "--quiet", "--wait", "--pipe", "--collect"] and re.fullmatch(r"--unit=cygnus-script-[0-9a-f]{16}", argv[5])
    assert f"--working-directory={tmp_path}" in argv and "RuntimeMaxSec=600" in argv
    end = argv[argv.index("--") + 1:]
    assert end == ["/usr/bin/sh", "-c", BODY.decode(), os.path.join(str(tmp_path), "postinst.sh")]
    assert f"--setenv=SUDO_USER={ME.pw_name}" in argv and f"--setenv=SUDO_UID={ME.pw_uid}" in argv and "--setenv=HOME=/root" in argv
    assert not any(a.startswith("--setenv=") and a.split("=")[1] not in {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "SUDO_USER", "SUDO_UID", "SUDO_GID"} for a in argv)
    actions.check_script_command(argv, plan)  # the command it builds is the command it allows


def test_the_ledger_and_the_log_keep_what_ran_but_not_the_text(tmp_path):
    plan = make(tmp_path)
    [shown] = plan.commands
    assert BODY.decode() not in " ".join(shown) and SHA in " ".join(shown)
    assert plan.summary["sha256"] == SHA and plan.summary["bytes"] == len(BODY) and plan.summary["folder"] == str(tmp_path)


@pytest.mark.parametrize("what", [
    {"sha": "0" * 64}, {"sha": ""}, {"sha": "A" * 64}, {"sha": None}, {"data": b""}, {"data": b"#!/bin/sh\n\0echo\n"},
    {"data": b"#!/usr/bin/ruby\nputs 1\n"}, {"data": b"#!/tmp/evil\n"}, {"data": b"#!\n"}, {"name": "a\nb"}, {"name": ""}, {"name": "x" * 201},
    {"name": "bad\x1b[2J"}, {"name": None}, {"folder": "relative/path"}, {"folder": "/a/../b"}, {"folder": "/no/such/folder/anywhere"},
    {"folder": "/etc/hostname"},
])
def test_a_plan_is_refused_unless_everything_about_the_script_is_right(tmp_path, what):
    args = {"name": "postinst.sh", "data": BODY, "sha": SHA, "folder": str(tmp_path), **what}
    if "data" in what and "sha" not in what:
        args["sha"] = scriptrun.sha256(args["data"]) if isinstance(args["data"], bytes) else SHA  # the checksum matches: it is the script that is wrong
    with pytest.raises(PlanRefused):
        actions.plan_script(args["name"], args["data"], args["sha"], args["folder"], caller_uid=os.getuid(), caller_sender=":1.1")


def test_a_script_too_big_for_a_command_line_is_refused(tmp_path):
    big = b"#!/bin/sh\n" + b"#" * scriptrun.MAX_SCRIPT_BYTES
    with pytest.raises(PlanRefused, match="bigger"):
        actions.plan_script("big.sh", big, scriptrun.sha256(big), str(tmp_path), caller_uid=os.getuid(), caller_sender=":1.1")


def test_a_folder_that_is_a_link_in_any_spelling_or_not_the_askers_is_refused(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to("/usr/share")  # a link to a folder that belongs to root
    for spelling in (str(link), str(link) + "/", str(link) + "/.", str(link) + "//", str(tmp_path / "real" / ".." / "real"), str(real) + "/", str(real) + "/."):
        with pytest.raises(PlanRefused):
            make(tmp_path, folder=spelling)
    for system_folder in ("/tmp", "/usr/share", "/", "/etc"):  # root's folders (and /tmp, which anyone may write into): not the asker's
        with pytest.raises(PlanRefused, match="not yours|plain absolute path"):
            make(tmp_path, folder=system_folder)
    assert make(tmp_path, folder=str(real)).summary["folder"] == str(real)  # the plain spelling of a folder of the asker's is fine
    real_fstat = os.fstat

    def fstat(fd):
        st = real_fstat(fd)
        if os.path.realpath(f"/proc/self/fd/{fd}") == str(real):
            fields = list(tuple(st)[:10])
            fields[4] = 4242  # st_uid: somebody else
            return os.stat_result(fields)
        return st

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "fstat", fstat)
        with pytest.raises(PlanRefused, match="not yours"):
            make(tmp_path, folder=str(real))


def test_the_folder_must_still_be_the_planned_one_when_the_script_runs(tmp_path):
    folder = tmp_path / "prog"
    folder.mkdir()
    plan = make(tmp_path, folder=str(folder))
    ran = []
    ok, detail = actions.execute(plan, run=lambda argv, sink: ran.append(argv) or 0, ledger=FakeLedger(), op_id="a")
    assert ok and len(ran) == 1  # untouched: it runs
    plan = make(tmp_path, folder=str(folder))
    folder.rename(tmp_path / "moved")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "prog").symlink_to(tmp_path / "elsewhere")  # between the approval and the run the folder became a link
    ran.clear()
    ok, detail = actions.execute(plan, run=lambda argv, sink: ran.append(argv) or 0, ledger=FakeLedger(), op_id="b")
    assert not ok and ran == [] and "not the one that was planned" in detail or "not there" in detail
    (tmp_path / "prog").unlink()
    (tmp_path / "prog").mkdir()  # a different folder at the same place: also refused
    ok, detail = actions.execute(plan, run=lambda argv, sink: ran.append(argv) or 0, ledger=FakeLedger(), op_id="c")
    assert not ok and ran == [] and "not the one that was planned" in detail


def test_any_change_to_the_command_after_planning_is_refused(tmp_path):
    plan = make(tmp_path)
    good = actions.script_argv(plan)
    other = actions.script_argv(make(tmp_path))  # another plan: another unit name
    cases = {
        "another text": [*good[:-2], "#!/bin/sh\nrm -rf /\n", good[-1]],
        "another interpreter": [*good[:-4], "/usr/bin/bash", *good[-3:]],
        "an extra property": [*good[:6], "-p", "ProtectSystem=no", *good[6:]],
        "another folder": [a if not a.startswith("--working-directory=") else "--working-directory=/" for a in good],
        "another environment": [*good[:-5], "--setenv=LD_PRELOAD=/tmp/x.so", *good[-5:]],
        "another unit": other,
        "no separator": [a for a in good if a != "--"],
        "another program": ["sh", *good[1:]],
        "empty": [],
    }
    for label, argv in cases.items():
        with pytest.raises(PlanRefused):
            actions.check_script_command(argv, plan)
        assert label
    with pytest.raises(PlanRefused):
        actions.check_script_command(good, actions.HelperPlan(id="x" * 32, kind="group", caller_uid=0, caller_sender="", action_id="", message="",
                                                              summary={}, commands=[], expires=0.0))  # only a script plan may run a script


def test_the_script_is_checked_again_when_it_runs_so_a_changed_plan_runs_nothing(tmp_path):
    plan = make(tmp_path)
    plan.script = b"#!/bin/sh\nrm -rf /\n"  # the plan's own copy is changed after the person agreed
    ran = []
    ok, detail = actions.execute(plan, run=lambda argv, sink: ran.append(argv) or 0, ledger=FakeLedger(), op_id="op")
    assert not ok and ran == [] and "no longer matches" in detail


class FakeLedger:
    def __init__(self):
        self.began, self.finished = [], []

    def begin(self, op_id, kind, uid, summary, argv):
        self.began.append((op_id, kind, uid, summary, argv))

    def finish(self, op_id, state, log):
        self.finished.append((op_id, state, log))


def test_execute_runs_exactly_the_one_command_and_reports_its_exit_status(tmp_path):
    plan = make(tmp_path)
    seen, ledger = [], FakeLedger()

    def run(argv, sink):
        seen.append(argv)
        sink("hello from the script")
        return 3

    ok, detail = actions.execute(plan, run=run, ledger=ledger, op_id="op1")
    assert not ok and seen == [actions.script_argv(plan)] and "hello from the script" in detail and "[exit status 3]" in detail
    [(_, kind, uid, summary, argv)] = ledger.began
    assert kind == "script" and uid == os.getuid() and BODY.decode() not in json.dumps(argv)  # the ledger keeps the checksum, not the text
    assert ledger.finished[0][1] == "failed"
    ok, _ = actions.execute(make(tmp_path), run=lambda argv, sink: 0, ledger=FakeLedger(), op_id="op2")
    assert ok


def test_what_a_script_prints_cannot_fill_the_helpers_memory(tmp_path):
    plan, ledger = make(tmp_path), FakeLedger()

    def run(argv, sink):
        for i in range(5000):
            sink(f"line {i}")
        return 0

    ok, detail = actions.execute(plan, run=run, ledger=ledger, op_id="op")
    log = ledger.finished[0][2].splitlines()
    assert ok and len(log) <= actions.MAX_SCRIPT_LOG_LINES + 1 and "were dropped" in log[0] and log[-1] == "line 4999" and len(detail.splitlines()) <= 50


def test_the_polkit_action_always_asks_for_an_administrators_password_and_is_never_remembered():
    root = ET.parse(REPO / "data/polkit/io.github.omranabdulaziz.Cygnus.policy").getroot()
    actions_by_id = {a.get("id"): a for a in root.findall("action")}
    mine = actions_by_id["io.github.omranabdulaziz.Cygnus.scripts.run"]
    defaults = {d.tag: d.text for d in mine.find("defaults")}
    assert defaults == {"allow_any": "no", "allow_inactive": "no", "allow_active": "auth_admin"}  # not auth_admin_keep: nothing is cached
    assert "administrator" in mine.find("message").text
    for name in ("packages.install-local", "services.manage", "recovery.manage", "permissions.manage"):
        assert f"io.github.omranabdulaziz.Cygnus.{name}" in actions_by_id


@pytest.mark.parametrize("remote", ["Unknown method PlanScript", "UnknownMethod", "No such method 'PlanScript'"])
def test_a_helper_from_before_this_action_existed_says_what_to_do_instead_of_a_bus_error(monkeypatch, remote):
    from cygnus.core.privilege import HelperClient

    client = HelperClient.__new__(HelperClient)
    client.GLib = __import__("gi.repository.GLib", fromlist=["GLib"])

    def old_helper(method, params, reply, **kw):
        raise HelperError(remote)

    client._call = old_helper
    with pytest.raises(HelperError, match="older version of Cygnus.*systemctl restart cygnus-helper"):
        client.plan_script("postinst.sh", BODY, SHA, "/tmp")
    client._call = lambda *a, **k: (_ for _ in ()).throw(HelperError("the script is not the one that was shown"))  # another refusal is passed on as it is
    with pytest.raises(HelperError, match="not the one that was shown"):
        client.plan_script("postinst.sh", BODY, SHA, "/tmp")


def test_systemd_run_is_not_in_the_general_allow_list_so_no_other_plan_can_reach_it():
    for argv in (["systemd-run", "--scope", "--", "/bin/rm", "-rf", "/tmp/x"], ["systemd-run", "--", "/bin/sh", "-c", "true"]):
        with pytest.raises(PlanRefused, match="command not allowed"):
            actions.check_command(argv, [])
    plan = actions.HelperPlan(id="p" * 32, kind="packages", caller_uid=os.getuid(), caller_sender=":1", action_id="x", message="m", summary={},
                              commands=[["systemd-run", "--", "/bin/true"]], expires=0.0)
    ran = []
    ok, detail = actions.execute(plan, run=lambda argv, sink: ran.append(argv) or 0, ledger=FakeLedger(), op_id="o")
    assert not ok and ran == [] and "command not allowed" in detail


def test_the_password_dialog_says_what_the_script_is_and_that_nothing_proves_it_was_shown(tmp_path):
    body = b"#!/bin/sh -e\necho hello\n"
    plan = actions.plan_script("postinst.sh", body, scriptrun.sha256(body), str(tmp_path), caller_uid=os.getuid(), caller_sender=":1")
    for part in (f"{len(body)} bytes", "/usr/bin/sh -e", "its first line is '#!/bin/sh -e'", scriptrun.sha256(body), "cannot check that anything was shown",
                 "only go on if you just asked Cygnus", "Cygnus cannot undo"):
        assert part in plan.message, part
    assert plan.summary["first_line"] == "#!/bin/sh -e" and "-e" in actions.script_argv(plan)


def test_a_first_line_that_asks_for_an_option_is_followed_and_the_ledger_text_placeholder_replaces_the_right_argument(tmp_path):
    body = b"#!/bin/sh -e\nfalse\necho not-reached\n"
    plan = actions.plan_script("p.sh", body, scriptrun.sha256(body), str(tmp_path), caller_uid=os.getuid(), caller_sender=":1")
    real = actions.script_argv(plan)
    end = real[real.index("--") + 1:]
    assert end[:3] == ["/usr/bin/sh", "-e", "-c"] and end[3] == body.decode()
    [shown] = plan.commands
    assert shown[shown.index("--") + 1:][:3] == ["/usr/bin/sh", "-e", "-c"] and body.decode() not in shown and "<the script with checksum" in " ".join(shown)


# -- the real runner (a harmless command stands in for systemd-run) ------------------------------------------------------------------------
def feeder(chunks):
    """A reader like a pipe's: each call gives the next piece, then b"" for the end."""
    pieces = iter([*chunks, b""])
    return lambda size: next(pieces, b"")


def test_what_a_program_prints_is_passed_on_line_by_line_and_cut_not_collected():
    chunks = [b"first\nsec", b"ond\n", b"x" * 5000, b"yyy\nlast-without-newline"]
    lines = []
    total = actions.forward_output(feeder(chunks), lines.append, limit=10**6, line_limit=100)
    assert total == sum(map(len, chunks)) and lines[0] == "first" and lines[1] == "second"
    assert lines[2] == "x" * 100 and lines[3] == "last-without-newline" and len(lines) == 4  # the long line was cut and its rest dropped
    lines.clear()
    actions.forward_output(feeder([b"a\n"] * 10), lines.append, limit=7, line_limit=100)
    assert lines == ["a", "a", "a", "[the rest of the output was dropped: more than 7 bytes]"]  # past the limit nothing is passed on, only counted


def test_a_line_that_never_ends_cannot_grow_memory_however_much_is_written():
    import tracemalloc

    count = [0]
    piece = b"z" * 65536

    def endless(size):
        count[0] += 1
        return b"" if count[0] > 3200 else piece  # 200 MB with no newline at all

    lines = []
    tracemalloc.start()
    actions.forward_output(endless, lines.append, limit=10**12, line_limit=2000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 2 * 1024 * 1024 and len(lines) == 1 and len(lines[0]) == 2000


def test_the_real_script_runner_passes_output_on_returns_the_status_and_does_not_hold_memory(monkeypatch):
    from cygnus.helper import service

    lines = []
    rc = service.script_runner(["/usr/bin/sh", "-c", "echo one; head -c 3000000 /dev/zero | tr '\\0' x; echo; echo after; exit 5"], lines.append)
    assert rc == 5 and lines[0] == "one" and any("dropped" in line for line in lines) and max(map(len, lines)) <= 2000 + 80


def test_the_real_script_runner_stops_waiting_for_a_command_that_hangs(monkeypatch):
    import time

    from cygnus.helper import service

    monkeypatch.setattr(actions, "SCRIPT_RUN_SECONDS", -119.8)  # the guard timer fires after a fifth of a second
    started = time.monotonic()
    rc = service.script_runner(["/usr/bin/sh", "-c", "sleep 30"], lambda line: None)
    assert rc != 0 and time.monotonic() - started < 10


def test_a_link_to_a_folder_of_the_askers_own_is_refused_too_not_only_one_to_a_folder_of_root(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "mine"
    link.symlink_to(real)  # the target is the asker's own folder: only the rule against links stops it
    with pytest.raises(PlanRefused, match="not there, or is not a plain folder"):
        make(tmp_path, folder=str(link))
    assert make(tmp_path, folder=str(real)).summary["folder"] == str(real)
