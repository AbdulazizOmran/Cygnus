"""Install scripts that come in an archive: never run unless the person ticked them, and then exactly the bytes that were shown.
Every script here is a harmless one that writes inside the test's own folder; no vendor script is ever run."""

import os
import time
from pathlib import Path

import pytest

from cygnus.core.detect import detect_file
from cygnus.core.errors import CygnusError
from cygnus.core.ops import archive_ops as ops
from cygnus.core.ops import scripts
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation
from cygnus.core.util import scriptrun
from tests import builders as b

HDD = StorageLocation(id="hdd", label="HDD", fs_uuid="ABCD", fs_type="ext4", location_class="user-owned")
ROOT_SCRIPT = "#!/bin/sh\necho hello > ran.txt\necho \"name=$0\" >> ran.txt\npwd >> ran.txt\n"


@pytest.fixture
def env(tmp_path):
    reg = open_registry(":memory:")
    reg.add_location(StorageLocation(id="hdd", label="HDD", fs_uuid="ABCD", fs_type="ext4", location_class="user-owned", canonical_mount=str(tmp_path)))
    (tmp_path / "downloads").mkdir()
    return reg, tmp_path


def entries(script=ROOT_SCRIPT, name="postinst.sh", extra=()):
    data = script if isinstance(script, bytes) else script.encode()
    return [("T", "dir", None, 0o755), ("T/run", "file", b.ELF_STUB, 0o755), (f"T/{name}", "file", data, 0o755), *extra]


def candidate(tmp, items=None, archive="T.tar.gz"):
    return detect_file(b.build_tarball(tmp / "downloads" / archive, items or entries()))


def plan(tmp, cand, run=None):
    return ops.plan_install(cand, app_key=ops.app_key_for(cand), location=HDD, apps_dir=tmp / "Applications", main=cand.metadata["main"], run_scripts=run)


def chosen(cand, *names):
    return [{"name": n, "sha256": cand.metadata["script_sha256"][n]} for n in names]


def test_the_scripts_of_an_archive_are_listed_with_the_checksum_of_their_exact_bytes(env):
    _, tmp = env
    raw = "#!/bin/sh\n# caf\u00e9 \u2713\necho ok\n".encode()  # not plain ASCII: the checksum is of the bytes, not of some decoded copy
    cand = candidate(tmp, entries(raw))
    assert cand.metadata["script_sha256"] == {"postinst.sh": scriptrun.sha256(raw)}


def test_a_script_that_was_not_ticked_is_never_run_and_is_still_there_for_later(env):
    reg, tmp = env
    cand = candidate(tmp)
    report = ops.executor(reg).run("install", plan(tmp, cand))
    folder = tmp / "Applications" / "T"
    assert report.state == "succeeded" and (folder / "postinst.sh").is_file() and not (folder / "ran.txt").exists()
    assert not any(r.get("ran") for r in report.results.values() if isinstance(r, dict) and "script" in r)
    [inst] = regdb.list_installations(reg)
    assert inst["source"]["scripts"] == [{"name": "postinst.sh", "sha256": cand.metadata["script_sha256"]["postinst.sh"], "phase": "install"}]


def test_a_ticked_script_runs_after_the_installation_as_you_in_the_programs_folder_with_its_own_name(env):
    reg, tmp = env
    cand = candidate(tmp)
    steps = plan(tmp, cand, chosen(cand, "postinst.sh"))
    assert [s.kind for s in steps][-2:] == ["registry.record", "archive.run_script"]  # last: after the program is recorded
    report = ops.executor(reg).run("install", steps)
    folder = tmp / "Applications" / "T"
    [result] = [r for r in report.results.values() if r.get("script")]
    assert report.state == "succeeded" and result["ran"] and result["ok"] and result["exit"] == 0 and result["as"] == "user"
    lines = (folder / "ran.txt").read_text().splitlines()
    assert lines[0] == "hello" and lines[1] == f"name={folder / 'postinst.sh'}" and Path(lines[2]).resolve() == folder.resolve()


def test_a_script_that_fails_leaves_the_installation_in_place_and_says_how_it_ended(env):
    reg, tmp = env
    cand = candidate(tmp, entries("#!/bin/sh\necho about to fail\nexit 3\n"))
    report = ops.executor(reg).run("install", plan(tmp, cand, chosen(cand, "postinst.sh")))
    [result] = [r for r in report.results.values() if r.get("script")]
    assert report.state == "succeeded" and (tmp / "Applications" / "T" / "run").is_file() and len(regdb.list_installations(reg)) == 1
    assert (result["ran"], result["ok"], result["exit"]) == (True, False, 3) and "exit status 3" in result["error"] and "about to fail" in result["output"]


def test_a_script_that_was_not_the_one_shown_is_refused_before_anything_is_planned(env):
    _, tmp = env
    cand = candidate(tmp)
    with pytest.raises(CygnusError, match="not the one that was shown"):
        plan(tmp, cand, [{"name": "postinst.sh", "sha256": "0" * 64}])
    with pytest.raises(CygnusError, match="not a script of this program"):
        plan(tmp, cand, [{"name": "run", "sha256": scriptrun.sha256(b.ELF_STUB)}])  # a program is not a script
    with pytest.raises(CygnusError, match="not a script of this program"):
        plan(tmp, cand, [{"name": "../etc/passwd", "sha256": "0" * 64}])
    with pytest.raises(CygnusError, match="not a script of this program"):
        plan(tmp, cand, [{"sha256": "0" * 64}])
    with pytest.raises(CygnusError, match="not a script of this program"):
        plan(tmp, cand, ["postinst.sh"])  # type: ignore[list-item]


def test_a_removal_script_is_not_offered_at_install_nor_an_install_script_at_removal(env):
    reg, tmp = env
    cand = candidate(tmp, entries(extra=[("T/prerm", "file", b"#!/bin/sh\ntrue\n", 0o755)]))
    with pytest.raises(CygnusError, match="for removing the program"):
        plan(tmp, cand, chosen(cand, "prerm"))
    ops.executor(reg).run("install", plan(tmp, cand))
    [inst] = regdb.list_installations(reg)
    with pytest.raises(CygnusError, match="for installing the program"):
        ops.plan_uninstall(reg, inst["id"], remove_payload=False, run_scripts=chosen(cand, "postinst.sh"))
    assert scripts.phase_of("prerm") == scripts.phase_of("T/postrm.sh") == scripts.phase_of("uninstall") == "remove"
    assert scripts.phase_of("postinst.sh") == scripts.phase_of("setup") == scripts.phase_of("install.sh") == "install"


def test_a_script_changed_after_it_was_shown_is_not_run(env):
    reg, tmp = env
    cand = candidate(tmp)
    ops.executor(reg).run("install", plan(tmp, cand))
    folder = tmp / "Applications" / "T"
    sha = cand.metadata["script_sha256"]["postinst.sh"]
    (folder / "postinst.sh").write_text("#!/bin/sh\necho evil > ran.txt\n")
    out = scripts.claim_and_run(root=folder, rel="postinst.sh", sha=sha, run_id=scripts.new_run_id())
    assert out["ran"] is False and "not the one that was shown" in out["error"] and not (folder / "ran.txt").exists()


@pytest.mark.parametrize("how", ["file is a link", "folder is a link", "not a file", "path climbs"])
def test_a_script_is_only_read_by_a_path_with_no_link_in_it(env, how):
    _, tmp = env
    folder = tmp / "prog"
    (folder / "d").mkdir(parents=True)
    other = tmp / "other"
    other.mkdir()
    body = b"#!/bin/sh\necho x > ran.txt\n"
    (other / "s.sh").write_bytes(body)
    rel = {"file is a link": "s.sh", "folder is a link": "ln/s.sh", "not a file": "d", "path climbs": "../other/s.sh"}[how]
    if how == "file is a link":
        (folder / "s.sh").symlink_to(other / "s.sh")
    if how == "folder is a link":
        (folder / "ln").symlink_to(other)
    out = scripts.claim_and_run(root=folder, rel=rel, sha=scriptrun.sha256(body), run_id=scripts.new_run_id())
    assert out["ran"] is False and out["error"] and not (folder / "ran.txt").exists() and not (other / "ran.txt").exists()


def test_a_script_runs_once_and_an_interrupted_attempt_is_not_repeated_by_a_resume(env):
    _, tmp = env
    folder = tmp / "prog"
    folder.mkdir()
    body = b"#!/bin/sh\necho again >> count.txt\n"
    (folder / "s.sh").write_bytes(body)
    run_id = scripts.new_run_id()
    first = scripts.claim_and_run(root=folder, rel="s.sh", sha=scriptrun.sha256(body), run_id=run_id)
    second = scripts.claim_and_run(root=folder, rel="s.sh", sha=scriptrun.sha256(body), run_id=run_id)  # what a resume would do
    assert first["ran"] and not second["ran"] and "second time" in second["error"]
    assert (folder / "count.txt").read_text() == "again\n"
    for bad in ("", "x", "../../etc/passwd", "0" * 8):
        assert scripts.claim_and_run(root=folder, rel="s.sh", sha=scriptrun.sha256(body), run_id=bad)["ran"] is False


def test_a_script_does_not_see_this_programs_environment(env, monkeypatch):
    _, tmp = env
    monkeypatch.setenv("CYGNUS_TEST_SECRET", "hunter2")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    folder = tmp / "prog"
    folder.mkdir()
    body = b"#!/bin/sh\nenv > env.txt\n"
    (folder / "s.sh").write_bytes(body)
    assert scripts.claim_and_run(root=folder, rel="s.sh", sha=scriptrun.sha256(body), run_id=scripts.new_run_id())["ok"]
    text = (folder / "env.txt").read_text()
    assert "hunter2" not in text and "ghp_secret" not in text and "HOME=" in text and "PATH=/usr/local/sbin:/usr/local/bin:/usr/bin" in text


def test_a_script_that_runs_too_long_is_stopped_and_one_that_prints_too_much_is_cut(env):
    _, tmp = env
    folder = tmp / "prog"
    folder.mkdir()
    slow = b"#!/bin/sh\nsleep 30\n"
    (folder / "slow.sh").write_bytes(slow)
    started = time.monotonic()
    out = scripts.claim_and_run(root=folder, rel="slow.sh", sha=scriptrun.sha256(slow), run_id=scripts.new_run_id(), timeout=1)
    assert out["timed_out"] and not out["ok"] and "stopped" in out["error"] and time.monotonic() - started < 10
    loud = b"#!/bin/sh\nyes 0123456789 | head -c 3000000\nprintf '\\033[2Jdone\\n'\n"
    (folder / "loud.sh").write_bytes(loud)
    out = scripts.claim_and_run(root=folder, rel="loud.sh", sha=scriptrun.sha256(loud), run_id=scripts.new_run_id())
    assert out["ok"] and out["truncated"] and len(out["output"]) <= scriptrun.MAX_OUTPUT + 8
    assert "\x1b" not in out["output"] and out["output"].rstrip().endswith("done")  # the end is kept, and cannot draw over the screen


@pytest.mark.parametrize("first_line, runs", [("#!/bin/sh", True), ("#!/usr/bin/env bash", True), ("#!/bin/bash", True), ("", True),
                                              ("#!/usr/bin/python3", True), ("#!/usr/bin/ruby", False), ("#!/tmp/evil", False),
                                              ("#!/usr/bin/env -S ruby", False), ("#!", False)])
def test_only_scripts_for_interpreters_cygnus_knows_are_run(env, first_line, runs):
    _, tmp = env
    folder = tmp / "prog"
    folder.mkdir()
    body = (first_line + "\n" if first_line else "") + ("print('x')\n" if "python" in first_line else "echo x\n")
    (folder / "s").write_bytes(body.encode())
    out = scripts.claim_and_run(root=folder, rel="s", sha=scriptrun.sha256(body.encode()), run_id=scripts.new_run_id())
    refusal = ("run it yourself", "names no program", "does not follow")
    assert out["ran"] is runs and (out["ok"] if runs else any(w in out["error"] for w in refusal)), out


@pytest.mark.parametrize("data", [b"", b"#!/bin/sh\n\0echo x\n", b"#!/bin/sh\n" + b"#" * (scriptrun.MAX_SCRIPT_BYTES + 1)])
def test_an_empty_binary_or_oversized_script_is_not_run(env, data):
    _, tmp = env
    folder = tmp / "prog"
    folder.mkdir()
    (folder / "s").write_bytes(data)
    out = scripts.claim_and_run(root=folder, rel="s", sha=scriptrun.sha256(data), run_id=scripts.new_run_id())
    assert out["ran"] is False and out["error"]


def test_bytes_that_are_not_text_are_never_offered_or_run_because_they_cannot_be_shown_as_they_run(env):
    reg, tmp = env
    raw = b"#!/bin/sh\n# caf\xe9 \xff\nprintf 'ok\\n' > out.txt\n"
    cand = candidate(tmp, entries(raw))
    assert "postinst.sh" not in cand.metadata["script_sha256"] and "not valid text" in cand.metadata["script_problems"]["postinst.sh"]
    with pytest.raises(CygnusError, match="not a script of this program"):
        plan(tmp, cand, [{"name": "postinst.sh", "sha256": scriptrun.sha256(raw)}])  # nor can it be asked for by its checksum
    folder = tmp / "prog"
    folder.mkdir()
    (folder / "s.sh").write_bytes(raw)
    out = scripts.claim_and_run(root=folder, rel="s.sh", sha=scriptrun.sha256(raw), run_id=scripts.new_run_id())
    assert out["ran"] is False and "not valid text" in out["error"] and not (folder / "out.txt").exists()
    assert scriptrun.script_text("caf\u00e9 \u2713\n".encode()).encode() == "caf\u00e9 \u2713\n".encode()


@pytest.mark.parametrize("text", ["-x", "-ex", "-x\necho hi > ran.txt\n", "-c 'id'\n", "--\necho x\n"])
def test_a_script_that_starts_with_a_dash_is_never_run_because_a_shell_would_read_it_as_an_option(env, text):
    # A text that is itself a valid option (`-x`) makes `sh -c -x NAME` run the file called NAME (the one on disk) instead of the text; any
    # other text that starts with a dash is an "invalid option" error. Either way what was shown would not be what runs.
    _, tmp = env
    folder = tmp / "prog"
    folder.mkdir()
    (folder / "s").write_bytes(text.encode())
    out = scripts.claim_and_run(root=folder, rel="s", sha=scriptrun.sha256(text.encode()), run_id=scripts.new_run_id())
    assert out["ran"] is False and "dash" in out["error"] and not (folder / "ran.txt").exists()
    cand = candidate(tmp, entries(text))
    assert cand.metadata["script_sha256"] == {} and "dash" in cand.metadata["script_problems"]["postinst.sh"]


def test_a_script_for_a_program_cygnus_does_not_run_scripts_with_is_listed_but_never_offered(env):
    _, tmp = env
    cand = candidate(tmp, entries("#!/usr/bin/ruby\nputs 1\n"))
    assert cand.metadata["script_sha256"] == {} and "run it yourself" in cand.metadata["script_problems"]["postinst.sh"]
    assert "postinst.sh" in cand.metadata["scripts"]  # still shown to the person


def test_a_folder_that_is_not_the_one_cygnus_made_has_nothing_run_in_it(env):
    reg, tmp = env
    cand = candidate(tmp)
    ops.executor(reg).run("install", plan(tmp, cand))
    folder = tmp / "Applications" / "T"
    steps = ops.script_steps(folder, "someone-elses-installation", chosen(cand, "postinst.sh"))
    out = ops._run_script(steps[0].params).result
    assert out["ran"] is False and "not the one Cygnus made" in out["error"] and not (folder / "ran.txt").exists()
    (folder / ".cygnus-installation").unlink()
    inst = regdb.list_installations(reg)[0]["id"]
    out = ops._run_script(ops.script_steps(folder, inst, chosen(cand, "postinst.sh"))[0].params).result
    assert out["ran"] is False and not (folder / "ran.txt").exists()
    out = ops._run_script(ops.script_steps(tmp / "gone", inst, chosen(cand, "postinst.sh"))[0].params).result
    assert out["ran"] is False and "not there" in out["error"]


def test_a_removal_script_runs_first_when_ticked_and_not_otherwise(env):
    reg, tmp = env
    marker = tmp / "prerm-ran"
    items = entries(extra=[("T/prerm", "file", f"#!/bin/sh\necho done > {marker}\n".encode(), 0o755)])
    for ticked in (False, True):
        cand = candidate(tmp, items, archive=f"T{ticked}.tar.gz")
        ops.executor(reg).run("install", plan(tmp, cand))
        [inst] = regdb.list_installations(reg)
        steps = ops.plan_uninstall(reg, inst["id"], remove_payload=True, run_scripts=chosen(cand, "prerm") if ticked else None)
        assert (steps[0].kind == "archive.run_script") is ticked  # before the folder and everything else goes
        report = ops.executor(reg).run("uninstall", steps)
        assert report.state == "succeeded" and not (tmp / "Applications" / "T").exists() and regdb.list_installations(reg) == []
        assert marker.exists() is ticked
        marker.unlink(missing_ok=True)


def test_the_report_of_the_operation_tells_which_step_gave_which_answer(env):
    reg, tmp = env
    cand = candidate(tmp)
    steps = plan(tmp, cand, chosen(cand, "postinst.sh"))
    report = ops.executor(reg).run("install", steps)
    index = [i for i, s in enumerate(steps) if s.kind == "archive.run_script"]
    assert len(index) == 1 and report.results[index[0]]["script"] == "postinst.sh" and report.results[index[0]]["ok"]
    journaled = reg.conn.execute("SELECT result FROM operation_step WHERE op_id=? AND seq=?", (report.op_id, index[0])).fetchone()[0]
    assert "postinst.sh" in journaled


def test_removal_scripts_are_only_for_programs_from_archives(env):
    from cygnus.core.ops import plan_uninstall

    reg, tmp = env
    cand = candidate(tmp, entries(extra=[("T/prerm", "file", b"#!/bin/sh\ntrue\n", 0o755)]))
    ops.executor(reg).run("install", plan(tmp, cand))
    [inst] = regdb.list_installations(reg)
    plan_uninstall(reg, inst["id"], run_scripts=chosen(cand, "prerm"))  # an archive program: fine
    with reg.transaction() as c:
        c.execute("UPDATE installation SET format='appimage' WHERE id=?", (inst["id"],))
    with pytest.raises(CygnusError, match="only programs installed from an archive"):
        plan_uninstall(reg, inst["id"], run_scripts=chosen(cand, "prerm"))


# -- round 13: the runner under awkward scripts -------------------------------------------------------------------------------------------
def test_a_program_the_script_leaves_running_never_holds_the_run_up_past_its_time_limit(env):
    _, tmp = env
    started = time.monotonic()
    res = scriptrun.run_bounded(["/usr/bin/sh", "-c", "setsid sleep 6 & sleep 100"], env={"PATH": "/usr/bin"}, cwd=str(tmp), timeout=1.5)
    assert res["timed_out"] and time.monotonic() - started < 4  # (the child in a session of its own is not the script's: it is left alone)
    started = time.monotonic()
    res = scriptrun.run_bounded(["/usr/bin/sh", "-c", "sleep 6 & echo started; exit 4"], env={"PATH": "/usr/bin"}, cwd=str(tmp), timeout=30)
    assert res["exit"] == 4 and "started" in res["output"] and not res["timed_out"] and time.monotonic() - started < 4
    # a script that starts a program on purpose and ends is allowed to: the program is not stopped, and not killed by a broken pipe
    marker = tmp / "alive.txt"
    scriptrun.run_bounded(["/usr/bin/sh", "-c", f"( sleep 1.2; echo still-here; echo ok > {marker} ) & exit 0"], env={"PATH": "/usr/bin"}, cwd=str(tmp), timeout=30)
    for _ in range(40):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.read_text() == "ok\n"


def test_a_script_that_is_killed_says_which_signal_and_an_unexpected_failure_is_an_answer_not_an_exception(env, monkeypatch):
    _, tmp = env
    folder = tmp / "prog"
    folder.mkdir()
    body = b"#!/bin/sh\nkill -9 $$\n"
    (folder / "k.sh").write_bytes(body)
    out = scripts.claim_and_run(root=folder, rel="k.sh", sha=scriptrun.sha256(body), run_id=scripts.new_run_id())
    assert out["ran"] and not out["ok"] and out["error"] == "it was stopped by signal 9"
    ok = b"#!/bin/sh\ntrue\n"
    (folder / "o.sh").write_bytes(ok)

    def boom(*a, **k):
        raise RuntimeError("cannot start new thread")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scripts, "run_as_user", boom)
        out = scripts.claim_and_run(root=folder, rel="o.sh", sha=scriptrun.sha256(ok), run_id=scripts.new_run_id())
    assert out["ran"] is False and "could not be run (RuntimeError)" in out["error"]  # an exception here would have rolled the install back


def test_a_folder_that_is_a_fifo_or_a_script_that_is_a_fifo_does_not_hang_the_reader(env):
    import os
    import subprocess

    _, tmp = env
    folder = tmp / "prog"
    folder.mkdir()
    os.mkfifo(folder / "pipe.sh")
    started = time.monotonic()
    out = scripts.claim_and_run(root=folder, rel="pipe.sh", sha="0" * 64, run_id=scripts.new_run_id())
    assert out["ran"] is False and "not a plain file" in out["error"] and time.monotonic() - started < 5
    os.mkfifo(folder / "fifo-dir")
    out = scripts.claim_and_run(root=folder, rel="fifo-dir/x.sh", sha="0" * 64, run_id=scripts.new_run_id())
    assert out["ran"] is False and time.monotonic() - started < 10


def test_the_environment_has_a_home_even_when_there_is_none_to_find(monkeypatch):
    from pathlib import Path

    def no_home(cls):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    assert scripts.user_env()["HOME"] == "/"


def test_the_first_line_options_are_followed_for_a_script_run_as_you(env):
    _, tmp = env
    folder = tmp / "prog"
    folder.mkdir()
    body = b"#!/bin/sh -e\nfalse\necho not-reached > reached.txt\n"
    (folder / "e.sh").write_bytes(body)
    out = scripts.claim_and_run(root=folder, rel="e.sh", sha=scriptrun.sha256(body), run_id=scripts.new_run_id())
    assert out["ran"] and out["exit"] == 1 and not (folder / "reached.txt").exists()  # as the first line says: stop at the first failure
    assert scriptrun.command_for("#!/usr/bin/env python3\nprint(1)\n", "n")[:3] == ["/usr/bin/python3", "-c", "#!/usr/bin/env python3\nprint(1)\n"]
    assert scriptrun.command_for("#!/usr/bin/python3 -u\nprint(1)\n", "n")[:3] == ["/usr/bin/python3", "-u", "-c"]
    assert scriptrun.command_for("#!/usr/bin/perl -w\n1;\n", "n")[:3] == ["/usr/bin/perl", "-w", "-e"]


@pytest.mark.parametrize("first_line", ["#!/bin/sh -c", "#!/bin/sh -s", "#!/bin/sh -l", "#!/bin/sh -n", "#!/bin/sh -i", "#!/bin/sh -ec", "#!/bin/sh -eX", "#!/bin/sh -",
                                        "#!/bin/sh --posix", "#!/usr/bin/bash -p", "#!/usr/bin/python3 -m", "#!/usr/bin/python3 -c", "#!/usr/bin/python3 -V",
                                        "#!/usr/bin/perl -i", "#!/usr/bin/perl -n", "#!/usr/bin/perl -e"])
def test_a_single_option_a_first_line_asks_for_that_cygnus_does_not_pass_on_refuses_the_script(first_line):
    problem = scriptrun.runnable_problem((first_line + "\ntrue\n").encode())
    assert problem and "does not follow" in problem, first_line


@pytest.mark.parametrize("first_line, options", [("#!/bin/sh -e", ("-e",)), ("#!/bin/sh -ex", ("-ex",)), ("#!/usr/bin/bash -euf", ("-euf",)), ("#!/usr/bin/python3 -u", ("-u",)),
                                                 ("#!/usr/bin/python3 -OB", ("-OB",)), ("#!/usr/bin/perl -w", ("-w",)), ("#!/bin/sh", ())])
def test_the_options_a_first_line_asks_for_that_are_safe_are_passed_on_exactly(first_line, options):
    assert scriptrun.interpreter_for(first_line + "\ntrue\n").options == options and scriptrun.runnable_problem((first_line + "\ntrue\n").encode()) is None
