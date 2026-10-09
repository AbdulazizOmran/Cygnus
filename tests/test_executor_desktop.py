import os
from pathlib import Path

import pytest

import builders
from cygnus.core.desktop import integrate
from cygnus.core.errors import CygnusError
from cygnus.core.executor import Executor, Step, StepOutcome
from cygnus.core.registry import open_registry


# -- executor ---------------------------------------------------------------------------------------
def make_exec(log, fail_at=None):
    reg = open_registry(":memory:")
    ex = Executor(reg)

    def do(params):
        if params["n"] == fail_at:
            raise RuntimeError("boom")
        log.append(("do", params["n"]))
        return StepOutcome(result={"n": params["n"]}, compensation=Step(kind="undo", params={"n": params["n"]}))

    def undo(params):
        log.append(("undo", params["n"]))
        return StepOutcome()

    ex.register("do", do)
    ex.register("undo", undo)
    return ex


def steps(n):
    return [Step(kind="do", params={"n": i}) for i in range(n)]


def test_success_is_journaled():
    log = []
    ex = make_exec(log)
    rep = ex.run("test", steps(3))
    assert rep.state == "succeeded" and rep.completed == [0, 1, 2]
    rows = ex.registry.conn.execute("SELECT state FROM operation_step ORDER BY seq").fetchall()
    assert [r[0] for r in rows] == ["done"] * 3 and not ex.incomplete()


def test_failure_rolls_back_in_reverse():
    log = []
    ex = make_exec(log, fail_at=2)
    rep = ex.run("test", steps(4))
    assert rep.state == "rolled_back" and rep.failed_step == 2
    assert log == [("do", 0), ("do", 1), ("undo", 1), ("undo", 0)]


def test_failure_without_rollback_needs_attention_then_resume():
    log = []
    ex = make_exec(log, fail_at=1)
    rep = ex.run("test", steps(3), rollback_on_failure=False)
    assert rep.state == "needs_attention" and ex.incomplete()[0]["id"] == rep.op_id
    ex.handlers["do"] = make_exec(log).handlers["do"]  # the cause was fixed
    again = ex.resume(rep.op_id)
    assert again.state == "succeeded" and ("do", 2) in log


def test_crash_leaves_running_operation_detectable():
    log = []
    ex = make_exec(log)

    def crash(params):
        raise KeyboardInterrupt  # simulates the process dying mid-step

    ex.register("crash", crash)
    with pytest.raises(KeyboardInterrupt):
        ex.run("test", [Step(kind="do", params={"n": 0}), Step(kind="crash")])
    [op] = ex.incomplete()
    assert op["state"] == "running"
    report = ex.roll_back(op["id"])
    # the finished step is undone; the one the process died in may have left work that nothing can undo
    assert report.state == "needs_attention" and ("undo", 0) in log
    assert "was interrupted" in report.compensation_errors[0]


def test_an_interrupted_step_with_an_abort_handler_is_undone_completely():
    log = []
    ex = make_exec(log)
    ex.register("crash", lambda params: (_ for _ in ()).throw(KeyboardInterrupt))
    ex.register("crash.abort", lambda params: log.append(("abort", params.get("n"))) or StepOutcome(result={}))
    with pytest.raises(KeyboardInterrupt):
        ex.run("test", [Step(kind="do", params={"n": 0}), Step(kind="crash", params={"n": 1})])
    [op] = ex.incomplete()
    assert ex.roll_back(op["id"]).state == "rolled_back" and ("abort", 1) in log and ("undo", 0) in log


def test_operation_still_owned_by_another_process_is_left_alone():
    from cygnus.core.executor import OperationBusy, owning

    log = []
    ex = make_exec(log)
    ex.register("crash", lambda params: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):  # a refresh-only step: nothing to undo if interrupted
        ex.run("test", [Step(kind="do", params={"n": 0}), Step(kind="crash", params={"rerun_after_rollback": True})])
    [op] = ex.incomplete()
    assert [s["state"] for s in op["steps"]] == ["done", "running"]
    with owning(op["id"]):  # e.g. the CLI is still running it while the GUI starts
        assert ex.incomplete() == []
        with pytest.raises(OperationBusy):
            ex.roll_back(op["id"])
        with pytest.raises(OperationBusy):
            ex.resume(op["id"])
    assert ex.roll_back(op["id"]).state == "rolled_back"
    with pytest.raises(CygnusError, match="not interrupted"):
        ex.resume(op["id"])  # already settled: never run twice


def test_unknown_action_rejected_before_anything_runs():
    ex = make_exec([])
    with pytest.raises(CygnusError):
        ex.run("test", [Step(kind="do", params={"n": 0}), Step(kind="nope")])
    assert not ex.registry.conn.execute("SELECT COUNT(*) FROM operation").fetchone()[0]


# -- desktop integration -------------------------------------------------------------------------------
def plan_for(tmp_path, **kw):
    args = dict(app_key="org.example.Hello", display_name="Hello", appimage_path=str(tmp_path / "Hello.AppImage"),
                fs_uuid="ABCD", location_label="HDD", upstream_desktop={"Name": "Hello", "Exec": "hello %U",
                                                                         "Categories": "Utility;", "Actions": "new;"},
                upstream_actions={"new": {"Name": "New", "Exec": "hello --new"}},
                upstream_desktop_id="hello.desktop", icon=builders.PNG_1PX, icon_kind="png")
    args.update(kw)
    return integrate.plan_appimage_integration(**args)


def test_integration_plan_files(tmp_path):
    plan = plan_for(tmp_path)
    kinds = {f.kind: f for f in plan.files}
    assert set(kinds) == {"launcher_shim", "desktop_entry", "icon"}
    de = kinds["desktop_entry"].data.decode()
    assert "Exec=" + str(integrate.shim_dir() / "org.example.Hello") + " %U" in de
    assert "[Desktop Action new]" in de and "X-Cygnus-Managed=true" in de
    assert integrate.validate_desktop_file(kinds["desktop_entry"].data) == []
    assert kinds["icon"].path.parts[-3] == "1x1"
    assert str(integrate.shim_dir()).startswith(os.environ["XDG_DATA_HOME"].rsplit("/", 1)[0])


def test_shim_quotes_hostile_values(tmp_path):
    shim = integrate.launcher_shim("x", "Evil'; rm -rf ~; '", "/mnt/a b/'quoted'.AppImage", "U", "HDD")
    import subprocess

    # Parse-check with sh -n: must be syntactically valid and not execute anything.
    assert subprocess.run(["sh", "-n", "-c", shim]).returncode == 0
    assert "rm -rf" in shim and "'\\''" in shim


def test_desktop_id_collision_uses_cygnus_id(tmp_path, monkeypatch):
    sysapps = tmp_path / "sys/applications"
    sysapps.mkdir(parents=True)
    (sysapps / "hello.desktop").write_text("[Desktop Entry]\nName=System hello\n")
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "sys"))
    plan = plan_for(tmp_path)
    assert plan.desktop_id == "cygnus-org.example.Hello.desktop" and plan.shadowed_system_entry


def test_unsafe_app_key_rejected(tmp_path):
    with pytest.raises(CygnusError):
        plan_for(tmp_path, app_key="../../evil")


def test_write_remove_restore_owned(tmp_path):
    target = integrate.xdg_data_home() / "applications" / "t.desktop"
    out = integrate.write_owned({"path": str(target), "data_hex": b"one".hex()})
    assert target.read_bytes() == b"one" and out.compensation.kind == "fs.remove_owned"
    out2 = integrate.write_owned({"path": str(target), "data_hex": b"two".hex()})
    assert out2.compensation.kind == "fs.restore_owned"
    integrate.restore_owned(out2.compensation.params)
    assert target.read_bytes() == b"one"
    target.write_bytes(b"user edited")
    with pytest.raises(CygnusError, match="changed"):
        integrate.remove_owned(out.compensation.params)
    assert target.exists()


def test_owned_paths_are_confined(tmp_path):
    for bad in ("/etc/passwd", str(Path.home() / ".bashrc"), str(integrate.xdg_data_home() / "applications/../x")):
        with pytest.raises(CygnusError):
            integrate.write_owned({"path": bad, "data_hex": "00"})


def test_symlinked_target_refused(tmp_path):
    apps = integrate.xdg_data_home() / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "link.desktop").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(CygnusError, match="symbolic link"):
        integrate.write_owned({"path": str(apps / "link.desktop"), "data_hex": "00"})


def test_autostart_rewrite(tmp_path):
    auto = integrate.xdg_config_home() / "autostart"
    auto.mkdir(parents=True)
    f = auto / "hello.desktop"
    f.write_text("[Desktop Entry]\nType=Application\nExec=/mnt/data/Hello.AppImage --minimized\n")
    data = integrate.rewrite_autostart(f, integrate.shim_dir() / "org.example.Hello")
    assert b"launch/org.example.Hello --minimized" in data


def test_a_lock_on_a_removed_file_does_not_count_as_ownership(monkeypatch):
    """Process B opened the lock file, the owner removed it, then B locked the orphaned inode."""
    import os

    from cygnus.core import executor as ex_mod

    path = ex_mod._lock_path("op-race")
    path.write_text("")
    orphan = os.open(path, os.O_RDWR)
    os.unlink(path)
    real_open, calls = os.open, []

    def first_open_gets_the_orphan(p, flags, mode=0o777):
        calls.append(p)
        return os.dup(orphan) if len(calls) == 1 else real_open(p, flags, mode)

    monkeypatch.setattr(ex_mod.os, "open", first_open_gets_the_orphan)
    with ex_mod.owning("op-race"):
        assert path.exists() and ex_mod._flock_held(path)  # retried on the real file
    assert len(calls) == 2
    os.close(orphan)


def test_liveness_check_never_takes_the_lock():
    from cygnus.core import executor as ex_mod

    with ex_mod.owning("op-live"):
        assert ex_mod.is_live("op-live")
        assert ex_mod.is_live("op-live")  # probing twice does not disturb the owner
    assert not ex_mod.is_live("op-live")


def test_shim_names_cannot_start_new_lines(tmp_path):
    import subprocess

    marker = tmp_path / "INJECTED"
    evil = f"HDD\ntouch {marker} #\r\x00\x1b"
    shim = integrate.launcher_shim("x", f"App\ntouch {marker}", "true", None, evil)
    assert len(shim.splitlines()) == 9 and "\r" not in shim and "\x00" not in shim and "\x1b" not in shim
    script = tmp_path / "shim"
    script.write_text(shim)
    assert subprocess.run(["sh", str(script)]).returncode == 0
    assert not marker.exists()


def test_undo_restores_the_launchers_permissions(tmp_path):
    import stat as st_

    shim = integrate.shim_dir() / "perm-test"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("#!/bin/sh\nexec old\n")
    shim.chmod(0o755)
    out = integrate.write_owned({"path": str(shim), "data_hex": b"#!/bin/sh\nexec new\n".hex(), "mode": 0o755})
    integrate.restore_owned(out.compensation.params)
    assert shim.read_text() == "#!/bin/sh\nexec old\n" and st_.S_IMODE(shim.stat().st_mode) == 0o755


def test_ownership_is_read_from_the_entry_not_from_any_text(tmp_path):
    mine = tmp_path / "a.desktop"
    mine.write_text("[Desktop Entry]\nName=A\nX-Cygnus-Managed=true\n")
    users = tmp_path / "b.desktop"
    users.write_text("[Desktop Entry]\nName=B\nComment=copied from X-Cygnus-Managed=true\n# X-Cygnus-Managed=true\n")
    assert integrate._is_ours(mine) and not integrate._is_ours(users)


@pytest.mark.parametrize("program", ["/home/me/My Apps/shim", "/home/me/50%/shim", "/home/me/a\"b/shim"])
def test_exec_lines_follow_the_desktop_entry_rules(program):
    from cygnus.core.desktop import entry

    value = integrate._rewrite_exec('/opt/x/app --name="a b" %U', Path(program))
    assert entry.split_exec(value)[0] == program.replace("%", "%%") and entry.split_exec(value)[1:] == ["--name=a b", "%U"]
    de = entry.DesktopEntry()
    de.set("Exec", value)
    assert entry.parse(de.serialize()).get("Exec") == value  # stored escaped, read back the same


def test_aborting_an_interrupted_write_removes_only_what_it_created(tmp_path):
    entry_path = integrate.xdg_data_home() / "applications/abort-test.desktop"
    data = b"[Desktop Entry]\nName=X\n"
    params = {"path": str(entry_path), "data_hex": data.hex(), "existed": False}
    integrate.write_owned(params)  # the write landed, then the process died
    integrate.write_owned_abort(params)
    assert not entry_path.exists()
    entry_path.write_bytes(b"someone else's")  # a different file there now: never touched
    integrate.write_owned_abort(params)
    assert entry_path.read_bytes() == b"someone else's"
    integrate.write_owned({**params, "existed": True})
    with pytest.raises(CygnusError, match="could not be restored"):
        integrate.write_owned_abort({**params, "existed": True})


def test_aborting_an_interrupted_copy(tmp_path):
    import hashlib

    from cygnus.core.ops import appimage_ops

    src = tmp_path / "a.AppImage"
    src.write_bytes(b"payload")
    dest = tmp_path / "dest/a.AppImage"
    params = {"src": str(src), "dest": str(dest), "expect_sha256": hashlib.sha256(b"payload").hexdigest(),
              "existed": False}
    appimage_ops._copy(params)
    (dest.parent / ".a.AppImage.cygnus-123.part").write_bytes(b"half")
    appimage_ops._copy_abort(params)
    assert not dest.exists() and not list(dest.parent.iterdir()) and src.exists()
