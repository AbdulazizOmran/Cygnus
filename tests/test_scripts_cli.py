"""The terminal and the service functions when a script fails, is declined, or is asked for in a way that makes no sense. Only harmless
scripts that write inside the test's own folder are run."""

import argparse

import pytest

from cygnus.cli import main as cli
from cygnus.core import privilege
from cygnus.core.errors import CygnusError
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.util import scriptrun
from cygnus.gui import service
from tests import builders as b
from tests.test_archive_service import archive, drive
from tests.test_scripts_service import FakeClient, items

FAILS = "#!/bin/sh\necho about to fail\nexit 3\n"


def install_args(path, **kw):
    from tests.test_archive_service import args

    return args(path, **kw)


def uninstall_args(app="T", **kw):
    base = dict(app=app, delete_file=True, remove_autostart=False, yes=True, registry=None, run_scripts=False, scripts_as_admin=False)
    return argparse.Namespace(**{**base, **kw})


@pytest.fixture
def quiet_libraries(monkeypatch):
    monkeypatch.setattr(service, "archive_libraries", lambda *a, **k: {"glibc": None, "packages": [], "unresolved": []})


def test_the_exit_status_says_when_a_script_you_asked_for_did_not_finish_and_the_install_stays(tmp_path, monkeypatch, capsys, quiet_libraries):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(FAILS), "T.tar.gz")
    assert cli.cmd_install(install_args(path, run_scripts=True)) == 2
    out = capsys.readouterr().out
    assert "postinst.sh: did not finish: it ended with exit status 3" in out and "about to fail" in out and "done" in out
    assert (mount / "Applications" / "T" / "run").is_file() and len(regdb.list_installations(open_registry())) == 1  # the install is not undone


def test_a_declined_or_failed_administrator_script_gives_status_2_and_a_neutral_message(tmp_path, monkeypatch, capsys, quiet_libraries):
    mount = drive(tmp_path, monkeypatch)

    class Declined(FakeClient):
        def commit(self, plan, on_progress=lambda line: None):
            return False, "Authorization was denied."

    monkeypatch.setattr(privilege, "HelperClient", lambda *a, **k: Declined())
    path = archive(tmp_path, items(), "T.tar.gz")
    assert cli.cmd_install(install_args(path, run_scripts=True, scripts_as_admin=True)) == 2
    assert "did not run or did not finish" in capsys.readouterr().out and (mount / "Applications" / "T" / "run").is_file()


def test_a_script_that_worked_gives_status_0(tmp_path, monkeypatch, quiet_libraries):
    drive(tmp_path, monkeypatch)
    assert cli.cmd_install(install_args(archive(tmp_path, items(), "T.tar.gz"), run_scripts=True)) == 0


def test_a_removal_script_that_fails_is_reported_and_the_program_is_still_removed(tmp_path, monkeypatch, capsys, quiet_libraries):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(extra=[("T/prerm", "file", FAILS.encode(), 0o755)]), "T.tar.gz")
    assert cli.cmd_install(install_args(path)) == 0
    capsys.readouterr()
    assert cli.cmd_uninstall(uninstall_args(run_scripts=True)) == 2
    out = capsys.readouterr().out
    assert "prerm: did not finish: it ended with exit status 3" in out and "done" in out and not (mount / "Applications" / "T").exists()


def test_an_administrator_removal_script_runs_only_after_the_removal_was_confirmed(tmp_path, monkeypatch, capsys, quiet_libraries):
    mount = drive(tmp_path, monkeypatch)
    fake = FakeClient()
    monkeypatch.setattr(privilege, "HelperClient", lambda *a, **k: fake)
    path = archive(tmp_path, items(extra=[("T/prerm", "file", b"#!/bin/sh\ntrue\n", 0o755)]), "T.tar.gz")
    assert cli.cmd_install(install_args(path)) == 0
    answers = iter(["y", "n"])  # run the removal script: yes; apply these changes: no
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.cmd_uninstall(uninstall_args(yes=False, run_scripts=True, scripts_as_admin=True)) == 1
    assert fake.committed == [] and (mount / "Applications" / "T").is_dir()  # declined at the last question: the helper was never asked
    answers = iter(["y", "y", "y"])  # script: yes; apply: yes; the helper's own "continue (your password will be asked)": yes
    assert cli.cmd_uninstall(uninstall_args(yes=False, run_scripts=True, scripts_as_admin=True)) == 0
    assert fake.committed == ["plan-1"] and not (mount / "Applications" / "T").exists()
    assert fake.asked and fake.asked[0][0] == "prerm"  # a removal script, planned as one


def test_a_failed_administrator_removal_script_asks_whether_to_remove_anyway_and_ends_with_status_2(tmp_path, monkeypatch, capsys, quiet_libraries):
    mount = drive(tmp_path, monkeypatch)

    class Failing(FakeClient):
        def commit(self, plan, on_progress=lambda line: None):
            return False, "[exit status 1]"

    monkeypatch.setattr(privilege, "HelperClient", lambda *a, **k: Failing())
    path = archive(tmp_path, items(extra=[("T/prerm", "file", b"#!/bin/sh\ntrue\n", 0o755)]), "T.tar.gz")
    assert cli.cmd_install(install_args(path)) == 0
    answers = iter(["y", "y", "y", "n"])  # script yes, apply yes, helper yes; "remove it anyway?": no
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.cmd_uninstall(uninstall_args(yes=False, run_scripts=True, scripts_as_admin=True)) == 1
    assert (mount / "Applications" / "T").is_dir()
    assert cli.cmd_uninstall(uninstall_args(yes=True, run_scripts=True, scripts_as_admin=True)) == 2  # -y: goes on, and says it did not finish
    assert not (mount / "Applications" / "T").exists()


def test_nothing_from_a_names_or_a_scripts_output_reaches_the_terminal_as_a_control_sequence(capsys):
    cli._show_scripts([{"name": "evil\x1b[2J\x1b[H"}], {"evil\x1b[2J\x1b[H": "echo \x1b[31mred\x1b[0m\n"}, "you")
    cli._report_scripts([{"script": "a\x1b]0;x\x07b", "ok": False, "ran": True, "error": "bad \x1b[2J", "output": "out \x1b[1;1H\n"}])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "evil?[2J?[H" in out


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_as_administrator_without_run_scripts_is_refused_not_silently_ignored(tmp_path, monkeypatch, command):
    drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(), "T.tar.gz")
    with pytest.raises(CygnusError, match="together with --run-scripts"):
        if command == "install":
            cli.cmd_install(install_args(path, scripts_as_admin=True))
        else:
            cli.cmd_uninstall(uninstall_args(scripts_as_admin=True))


def test_run_scripts_is_refused_for_files_that_have_no_scripts_cygnus_runs(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    (tmp_path / "downloads").mkdir(exist_ok=True)
    appimage = b.build_appimage(tmp_path / "downloads")
    with pytest.raises(CygnusError, match="no install scripts"):
        cli.cmd_install(install_args("flathub:org.kde.kcalc", run_scripts=True))
    with pytest.raises(CygnusError, match="no install scripts"):
        cli.cmd_install(install_args(str(appimage), run_scripts=True))


# -- the service functions -----------------------------------------------------------------------------------------------------------
def installed(tmp_path, monkeypatch, entries, name="T.tar.gz"):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, entries, name)
    plan = service.analyse(path, "HDD")
    result = service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None)
    return mount, plan, result


def sha(plan, name):
    return next(i["sha256"] for i in plan["scripts"]["items"] if i["name"] == name)


def test_a_failed_script_puts_a_warning_in_the_answer_for_the_page_or_for_the_window_that_replaces_it(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(FAILS), "T.tar.gz")
    plan = service.analyse(path, "HDD")
    result = service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None,
                                     run_scripts=[{"name": "postinst.sh", "sha256": sha(plan, "postinst.sh"), "as": "user"}])
    assert result["ok"] and "installed, but the script postinst.sh did not finish: it ended with exit status 3" in result["attention"] and "late_attention" not in result
    clean = service.analyse(archive(tmp_path, items(), "U.tar.gz"), "HDD")
    assert "attention" not in service.uninstall(result["installation_id"], True, lambda line: None)  # (nothing was ticked)
    ok = service.install_archive(archive(tmp_path, items(), "U.tar.gz"), "HDD", clean["sha256"], "run", lambda line: None,
                                 run_scripts=[{"name": "postinst.sh", "sha256": sha(clean, "postinst.sh"), "as": "admin"}])
    assert "attention" not in ok and "were not run, because the page was left" in ok["late_attention"] and ok["admin_scripts"]


def test_a_removal_script_for_administrator_cannot_be_asked_of_the_function_that_removes_as_you(tmp_path, monkeypatch):
    mount, plan, result = installed(tmp_path, monkeypatch, items(extra=[("T/prerm", "file", b"#!/bin/sh\ntrue\n", 0o755)]))
    with pytest.raises(CygnusError, match="administrator is run through the helper"):
        service.uninstall(result["installation_id"], True, lambda line: None, run_scripts=[{"name": "prerm", "sha256": sha(plan, "prerm"), "as": "admin"}])
    assert (mount / "Applications" / "T").is_dir()  # nothing was removed on a half-understood request


def test_a_failed_removal_script_puts_a_warning_in_the_answer(tmp_path, monkeypatch):
    mount, plan, result = installed(tmp_path, monkeypatch, items(extra=[("T/prerm", "file", FAILS.encode(), 0o755)]))
    out = service.uninstall(result["installation_id"], True, lambda line: None, run_scripts=[{"name": "prerm", "sha256": sha(plan, "prerm"), "as": "user"}])
    assert out["ok"] and "removed, but the script prerm did not finish" in out["attention"] and not (mount / "Applications" / "T").exists()


def test_the_scripts_dialog_and_the_run_function_keep_to_install_scripts_unless_the_removal_flow_asks(tmp_path, monkeypatch):
    mount, plan, result = installed(tmp_path, monkeypatch, items(extra=[("T/prerm", "file", b"#!/bin/sh\necho ran > prerm-ran.txt\n", 0o755)]))
    iid, folder = result["installation_id"], mount / "Applications" / "T"
    with pytest.raises(CygnusError, match="for removing the program"):
        service.run_archive_script(iid, "prerm", sha(plan, "prerm"))  # as the screens call it: install scripts only
    assert not (folder / "prerm-ran.txt").exists()
    fake = FakeClient()
    with pytest.raises(CygnusError, match="for removing the program"):
        service.prepare_admin_script(iid, "prerm", sha(plan, "prerm"), fake)
    assert fake.asked == []
    assert service.run_archive_script(iid, "prerm", sha(plan, "prerm"), phase="remove")["ok"] and (folder / "prerm-ran.txt").exists()
    service.prepare_admin_script(iid, "prerm", sha(plan, "prerm"), fake, phase="remove")
    assert fake.asked and fake.asked[0][0] == "prerm"


@pytest.mark.parametrize("path", ["", "relative/path", ".", None])
def test_a_record_without_a_usable_folder_is_refused_instead_of_meaning_where_cygnus_happens_to_be(tmp_path, monkeypatch, path):
    mount, plan, result = installed(tmp_path, monkeypatch, items())
    reg = open_registry()
    import json

    row = next(r for r in regdb.list_installations(reg) if r["id"] == result["installation_id"])
    source = {**row["source"], "path": path}
    with reg.transaction() as c:
        c.execute("UPDATE installation SET source=? WHERE id=?", (json.dumps(source), result["installation_id"]))
    for call in (lambda: service.archive_scripts(result["installation_id"]), lambda: service.run_archive_script(result["installation_id"], "postinst.sh", sha(plan, "postinst.sh"))):
        with pytest.raises(CygnusError, match="does not say where its folder is"):
            call()


def test_the_bridge_reads_the_choice_inside_the_job_so_bad_json_is_an_answer_not_an_exception():
    import ast
    from pathlib import Path

    source = Path(service.__file__).with_name("bridge.py").read_text()
    tree = ast.parse(source)
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in ("installArchive", "uninstallArchive")):
        outside = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "loads"]
        inside = [c for lam in ast.walk(fn) if isinstance(lam, ast.Lambda) for c in ast.walk(lam) if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "loads"]
        assert outside and len(outside) == len(inside), fn.name  # every json.loads of the choice sits inside the lambda that is the job


def test_the_scripts_text_in_messages_is_plain(tmp_path):
    assert service._plain_name("a\x1b[2Jb\n") == "a?[2Jb?" and scriptrun.printable(b"ok") == "ok"
