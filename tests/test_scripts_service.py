"""Choosing to run an archive's install scripts, through the functions the screens and the terminal use."""

import pytest

from cygnus.core.errors import CygnusError
from cygnus.core.util import scriptrun
from cygnus.gui import service
from tests import builders as b
from tests.test_archive_service import archive, codes, drive  # noqa: F401 - helpers

RAN = "#!/bin/sh\necho hello > ran.txt\n"


def items(script=RAN, extra=()):
    return [("T", "dir", None, 0o755), ("T/run", "file", b.ELF_STUB, 0o755), ("T/postinst.sh", "file", script.encode(), 0o755), *extra]


def sha_of(plan, name="postinst.sh"):
    return next(i["sha256"] for i in plan["scripts"]["items"] if i["name"] == name)


def test_the_screen_gets_each_script_with_its_checksum_and_when_it_belongs(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    extra = [("T/prerm", "file", b"#!/bin/sh\ntrue\n", 0o755)]
    plan = service.analyse(archive(tmp_path, items(extra=extra), "T.tar.gz"), "HDD")
    got = {i["name"]: i for i in plan["scripts"]["items"]}
    assert got["postinst.sh"]["phase"] == "install" and got["prerm"]["phase"] == "remove"
    assert got["postinst.sh"]["sha256"] == scriptrun.sha256(RAN.encode())
    assert "does not run them unless you tick one" in " ".join(plan["notes"])


def test_nothing_is_run_unless_the_choice_is_made_and_a_user_choice_runs_as_you(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(), "T.tar.gz")
    plan = service.analyse(path, "HDD")
    result = service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None)
    folder = mount / "Applications" / "T"
    assert result["ok"] and result["scripts"] == [] and result["admin_scripts"] == [] and not (folder / "ran.txt").exists()
    service.uninstall(result["installation_id"], True, lambda line: None)
    result = service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None,
                                     run_scripts=[{"name": "postinst.sh", "sha256": sha_of(plan), "as": "user"}])
    [ran] = result["scripts"]
    assert result["ok"] and ran["ran"] and ran["ok"] and (folder / "ran.txt").read_text() == "hello\n" and result["admin_scripts"] == []


def test_a_script_chosen_to_run_as_administrator_is_not_run_by_the_install_and_is_handed_back(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(), "T.tar.gz")
    plan = service.analyse(path, "HDD")
    result = service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None,
                                     run_scripts=[{"name": "postinst.sh", "sha256": sha_of(plan), "as": "admin"}])
    assert result["ok"] and result["scripts"] == [] and not (mount / "Applications" / "T" / "ran.txt").exists()
    assert result["admin_scripts"] == [{"name": "postinst.sh", "sha256": sha_of(plan)}]


@pytest.mark.parametrize("choice", [
    "postinst.sh", [{"name": "postinst.sh"}], [{"name": "postinst.sh", "sha256": "x", "as": "root"}], [{"name": 5, "sha256": "x", "as": "user"}],
    [{"name": "postinst.sh", "sha256": "0" * 64, "as": "user"}], [{"name": "postinst.sh", "sha256": "0" * 64, "as": "admin"}],
    [{"name": "run", "sha256": "0" * 64, "as": "admin"}], [{"name": "x", "sha256": "0" * 64, "as": "user"}] * 9,
])
def test_a_choice_that_is_not_exactly_a_shown_script_stops_everything_before_anything_is_unpacked(tmp_path, monkeypatch, choice):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(), "T.tar.gz")
    plan = service.analyse(path, "HDD")
    with pytest.raises(CygnusError):
        service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None, run_scripts=choice)
    assert not (mount / "Applications").exists()


def test_the_scripts_of_an_installed_program_can_be_listed_and_one_can_be_run_later(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(), "T.tar.gz")
    plan = service.analyse(path, "HDD")
    result = service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None)
    iid, folder = result["installation_id"], mount / "Applications" / "T"
    listing = service.archive_scripts(iid)
    [one] = listing["scripts"]
    assert listing["ours"] and one["name"] == "postinst.sh" and one["state"] == "ok" and one["text"] == RAN and one["phase"] == "install"
    assert listing["report"]["texts"] == {"postinst.sh": RAN} and not (folder / "ran.txt").exists()
    with pytest.raises(CygnusError):
        service.run_archive_script(iid, "postinst.sh", "0" * 64)  # not the checksum that was recorded
    assert not (folder / "ran.txt").exists()
    out = service.run_archive_script(iid, "postinst.sh", one["sha256"])
    assert out["ok"] and out["result"]["ran"] and (folder / "ran.txt").read_text() == "hello\n"
    (folder / "postinst.sh").write_text("#!/bin/sh\necho changed > ran.txt\n")
    assert service.archive_scripts(iid)["scripts"][0]["state"] == "changed" and service.archive_scripts(iid)["scripts"][0]["text"] is None
    again = service.run_archive_script(iid, "postinst.sh", one["sha256"])
    assert not again["ok"] and "not the one that was shown" in again["result"]["error"] and (folder / "ran.txt").read_text() == "hello\n"
    (folder / "postinst.sh").unlink()
    assert service.archive_scripts(iid)["scripts"][0]["state"] == "missing"


def test_the_scripts_of_a_folder_that_is_not_ours_are_not_listed_as_available(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(), "T.tar.gz")
    plan = service.analyse(path, "HDD")
    iid = service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None)["installation_id"]
    (mount / "Applications" / "T" / ".cygnus-installation").write_text('{"installation": "someone-else"}')
    listing = service.archive_scripts(iid)
    assert not listing["ours"] and listing["scripts"][0]["state"] == "missing" and listing["scripts"][0]["text"] is None


def test_a_removal_script_chosen_at_uninstall_runs_first_and_a_wrong_one_stops_the_removal(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    marker = tmp_path / "prerm-ran"
    extra = [("T/prerm", "file", f"#!/bin/sh\necho done > {marker}\n".encode(), 0o755)]
    path = archive(tmp_path, items(extra=extra), "T.tar.gz")
    plan = service.analyse(path, "HDD")
    iid = service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None)["installation_id"]
    with pytest.raises(CygnusError):
        service.uninstall(iid, True, lambda line: None, run_scripts=[{"name": "prerm", "sha256": "0" * 64, "as": "user"}])
    assert (mount / "Applications" / "T").is_dir() and not marker.exists()  # nothing was removed, nothing was run
    out = service.uninstall(iid, True, lambda line: None, run_scripts=[{"name": "prerm", "sha256": sha_of(plan, "prerm"), "as": "user"}])
    assert out["ok"] and out["scripts"][0]["ran"] and marker.read_text() == "done\n" and not (mount / "Applications" / "T").exists()


# -- as administrator: what the screen hands the helper ---------------------------------------------------------------------------------
class FakeClient:
    """Stands in for the helper: records what it was asked and answers like a plan."""

    def __init__(self):
        self.asked, self.committed = [], []

    def plan_script(self, name, data, sha256, folder):
        from cygnus.core.privilege import Plan

        self.asked.append((name, data, sha256, folder))
        return Plan("plan-1", {"script": name}, f"Run the install script {name} as administrator")

    def commit(self, plan, on_progress=lambda line: None):
        self.committed.append(plan.plan_id)
        on_progress("done")
        return True, "ok"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    from cygnus.gui import fixes

    mount = drive(tmp_path, monkeypatch)
    fake = FakeClient()
    monkeypatch.setattr(fixes, "HelperClient", lambda *a, **k: fake)
    path = archive(tmp_path, items(), "T.tar.gz")
    plan = service.analyse(path, "HDD")
    result = service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None)
    return fixes, fake, result["installation_id"], mount / "Applications" / "T", sha_of(plan)


def test_the_helper_is_handed_the_exact_bytes_and_checksum_and_commits_only_after_the_confirmation(installed):
    fixes, fake, iid, folder, sha = installed
    out = fixes.plan_script_admin(iid, "postinst.sh", sha)
    assert fake.asked == [("postinst.sh", RAN.encode(), sha, str(folder))] and fake.committed == []  # planned, not run
    assert out["kind"] == "helper" and out["title"] == "Run postinst.sh as administrator" and "as administrator" in out["message"]
    assert "Cygnus cannot undo it" in out["security_note"]
    assert fixes.commit(out["token"], lambda line: None) == {"ok": True} and fake.committed == ["plan-1"]
    with pytest.raises(CygnusError, match="expired"):
        fixes.commit(out["token"], lambda line: None)  # a confirmation is used once


def test_nothing_reaches_the_helper_for_a_script_that_is_not_the_recorded_one_or_has_changed_or_is_in_the_wrong_folder(installed):
    fixes, fake, iid, folder, sha = installed
    with pytest.raises(CygnusError):
        fixes.plan_script_admin(iid, "postinst.sh", "0" * 64)
    with pytest.raises(CygnusError):
        fixes.plan_script_admin(iid, "run", scriptrun.sha256(b.ELF_STUB))
    with pytest.raises(CygnusError):
        fixes.plan_script_admin(iid, "../../etc/passwd", sha)
    (folder / "postinst.sh").write_text("#!/bin/sh\necho changed\n")
    with pytest.raises(CygnusError, match="not the one that was shown"):
        fixes.plan_script_admin(iid, "postinst.sh", sha)
    (folder / "postinst.sh").write_text(RAN)
    (folder / ".cygnus-installation").write_text('{"installation": "someone-else"}')
    with pytest.raises(CygnusError, match="not the one Cygnus made"):
        fixes.plan_script_admin(iid, "postinst.sh", sha)
    with pytest.raises(CygnusError):
        fixes.plan_script_admin("no-such-installation", "postinst.sh", sha)
    assert fake.asked == []


# -- the terminal ------------------------------------------------------------------------------------------------------------------------
def cli_args(path, **kw):
    from tests.test_archive_service import args

    return args(path, **kw)


def test_the_terminal_shows_the_scripts_in_full_and_runs_them_as_you_only_when_asked(tmp_path, monkeypatch, capsys):
    from cygnus.cli import main as cli

    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(), "T.tar.gz")
    monkeypatch.setattr(service, "archive_libraries", lambda *a, **k: {"glibc": None, "packages": [], "unresolved": []})
    assert cli.cmd_install(cli_args(path)) == 0  # -y alone never runs a script
    out = capsys.readouterr().out
    assert not (mount / "Applications" / "T" / "ran.txt").exists() and "done" in out
    from cygnus.core.registry import db as regdb
    from cygnus.core.registry import open_registry

    service.uninstall(regdb.list_installations(open_registry())[0]["id"], True, lambda line: None)
    assert cli.cmd_install(cli_args(path, run_scripts=True)) == 0
    out = capsys.readouterr().out
    assert "--- postinst.sh (it would run as you) ---" in out and "echo hello > ran.txt" in out and "ran postinst.sh: ok" in out
    assert (mount / "Applications" / "T" / "ran.txt").read_text() == "hello\n"


def test_the_terminal_asks_before_running_and_a_no_runs_nothing_and_installs_nothing(tmp_path, monkeypatch, capsys):
    from cygnus.cli import main as cli

    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(), "T.tar.gz")
    answers = iter(["y", "n"])  # unpack it: yes; run the scripts: no
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.cmd_install(cli_args(path, run_scripts=True, yes=False)) == 1
    assert "cancelled" in capsys.readouterr().out and not (mount / "Applications" / "T").exists()


def test_the_terminal_hands_an_administrator_script_to_the_helper_after_installing(tmp_path, monkeypatch, capsys):
    from cygnus.cli import main as cli
    from cygnus.core import privilege

    mount = drive(tmp_path, monkeypatch)
    fake = FakeClient()
    monkeypatch.setattr(privilege, "HelperClient", lambda *a, **k: fake)
    path = archive(tmp_path, items(), "T.tar.gz")
    monkeypatch.setattr(service, "archive_libraries", lambda *a, **k: {"glibc": None, "packages": [], "unresolved": []})
    assert cli.cmd_install(cli_args(path, run_scripts=True, scripts_as_admin=True)) == 0
    out = capsys.readouterr().out
    assert "it would run as administrator" in out and "as administrator" in out and not (mount / "Applications" / "T" / "ran.txt").exists()
    assert fake.asked == [("postinst.sh", RAN.encode(), scriptrun.sha256(RAN.encode()), str(mount / "Applications" / "T"))] and fake.committed == ["plan-1"]


def test_the_terminal_runs_removal_scripts_before_removing_when_asked(tmp_path, monkeypatch, capsys):
    import argparse

    from cygnus.cli import main as cli

    mount = drive(tmp_path, monkeypatch)
    marker = tmp_path / "prerm-ran"
    path = archive(tmp_path, items(extra=[("T/prerm", "file", f"#!/bin/sh\necho done > {marker}\n".encode(), 0o755)]), "T.tar.gz")
    monkeypatch.setattr(service, "archive_libraries", lambda *a, **k: {"glibc": None, "packages": [], "unresolved": []})
    assert cli.cmd_install(cli_args(path)) == 0
    plain = argparse.Namespace(app="T", delete_file=False, remove_autostart=False, yes=True, registry=None, run_scripts=False, scripts_as_admin=False)
    ns = argparse.Namespace(**{**vars(plain), "delete_file": True, "run_scripts": True})
    assert cli.cmd_uninstall(ns) == 0
    out = capsys.readouterr().out
    assert "--- prerm (it would run as you) ---" in out and marker.read_text() == "done\n" and not (mount / "Applications" / "T").exists()


# -- the text shown is the whole of what runs --------------------------------------------------------------------------------------------
def test_a_long_script_is_shown_whole_and_what_cannot_be_shown_whole_is_not_offered(tmp_path, monkeypatch):
    from cygnus.core.backends import portable

    drive(tmp_path, monkeypatch)
    long_script = "#!/bin/sh\n" + "# padding\n" * 6300 + "echo END-OF-SCRIPT\n"  # about 63,000 bytes: more than the 60,000 characters shown elsewhere
    assert 60_000 < len(long_script) <= scriptrun.MAX_SCRIPT_BYTES
    plan = service.analyse(archive(tmp_path, items(long_script), "T.tar.gz"), "HDD")
    assert plan["scripts"]["texts"]["postinst.sh"] == long_script and plan["scripts"]["texts"]["postinst.sh"].rstrip().endswith("END-OF-SCRIPT")
    assert [i["name"] for i in plan["scripts"]["items"]] == ["postinst.sh"] and plan["scripts"]["items"][0]["sha256"] == scriptrun.sha256(long_script.encode())
    real = portable.scripts_report
    monkeypatch.setattr(portable, "scripts_report", lambda scripts, **kw: real(scripts, cap=100))  # whatever cut the text, the script is not offered
    cut = service.analyse(archive(tmp_path, items(long_script), "T2.tar.gz"), "HDD")
    assert cut["scripts"]["items"] == [] and len(cut["scripts"]["texts"]["postinst.sh"]) == 100


def test_a_script_that_cannot_be_offered_is_listed_with_why_whatever_the_reason(tmp_path, monkeypatch):
    from cygnus.core.detect import archive as ad

    drive(tmp_path, monkeypatch)
    huge = "#!/bin/sh\n" + "#" * (scriptrun.MAX_SCRIPT_BYTES + 1)
    plan = service.analyse(archive(tmp_path, items(huge), "H.tar.gz"), "HDD")
    assert plan["scripts"]["items"] == [] and plan["scripts"]["not_runnable"] == [
        {"name": "postinst.sh", "reason": f"it is larger than {ad.MAX_SCRIPT_BYTES // 1024} KiB, so it cannot be shown to you whole"}]
    binary = service.analyse(archive(tmp_path, items(extra=[("T/setup", "file", b"\x7fELF\0\0\0 not text", 0o755)]), "B.tar.gz"), "HDD")
    assert [i["name"] for i in binary["scripts"]["items"]] == ["postinst.sh"]
    assert binary["scripts"]["not_runnable"] and binary["scripts"]["not_runnable"][0]["name"] == "setup" and "zero byte" in binary["scripts"]["not_runnable"][0]["reason"]


def test_only_scripts_directly_in_the_programs_folder_are_install_scripts_and_none_is_crowded_out_or_dropped_silently(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    nested = [(f"T/d{i}/update", "file", b"#!/bin/sh\ntrue\n", 0o755) for i in range(12)] + [("T/tools/setup", "file", b"#!/bin/sh\ntrue\n", 0o755)]
    plan = service.analyse(archive(tmp_path, items(extra=nested), "N.tar.gz"), "HDD")
    assert [i["name"] for i in plan["scripts"]["items"]] == ["postinst.sh"] and plan["scripts"]["not_runnable"] == []  # parts of the program, not install scripts
    many = [(f"T/{n}", "file", b"#!/bin/sh\ntrue\n", 0o755) for n in ("install", "install.sh", "setup", "setup.sh", "update", "update.sh", "prerm", "postrm", "uninstall", "preinst")]
    plan = service.analyse(archive(tmp_path, items(extra=many), "M.tar.gz"), "HDD")
    offered = [i["name"] for i in plan["scripts"]["items"]]
    assert len(offered) == 8
    left = [n["name"] for n in plan["scripts"]["not_runnable"]]
    assert len(left) == 3 and all("more than 8 install scripts" in n["reason"] for n in plan["scripts"]["not_runnable"]) and set(left).isdisjoint(offered)


def test_a_script_whose_name_has_control_characters_is_never_offered_and_never_reaches_a_terminal(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    # (the pattern that recognises install scripts lets a trailing newline through: "setup\n" is such a name; a folder name is not part of it)
    plan = service.analyse(archive(tmp_path, items(extra=[("T/setup\n", "file", b"#!/bin/sh\ntrue\n", 0o755),
                                                          ("T/\x1b[2Jdir/setup", "file", b"#!/bin/sh\ntrue\n", 0o755)]), "E.tar.gz"), "HDD")
    assert [i["name"] for i in plan["scripts"]["items"]] == ["postinst.sh"]
    reasons = plan["scripts"]["not_runnable"]
    assert len(reasons) == 1 and "\n" not in reasons[0]["name"] and "cannot be shown" in reasons[0]["reason"]  # the one in a folder is not a script at all


# -- as administrator: what the screen hands the helper ---------------------------------------------------------------------------------
class FakeClient:
    """Stands in for the helper: records what it was asked and answers like a plan."""

    def __init__(self):
        self.asked, self.committed = [], []

    def plan_script(self, name, data, sha256, folder):
        from cygnus.core.privilege import Plan

        self.asked.append((name, data, sha256, folder))
        return Plan("plan-1", {"script": name}, f"Run the install script {name} as administrator")

    def commit(self, plan, on_progress=lambda line: None):
        self.committed.append(plan.plan_id)
        on_progress("done")
        return True, "ok"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    from cygnus.gui import fixes

    mount = drive(tmp_path, monkeypatch)
    fake = FakeClient()
    monkeypatch.setattr(fixes, "HelperClient", lambda *a, **k: fake)
    path = archive(tmp_path, items(), "T.tar.gz")
    plan = service.analyse(path, "HDD")
    result = service.install_archive(path, "HDD", plan["sha256"], "run", lambda line: None)
    return fixes, fake, result["installation_id"], mount / "Applications" / "T", sha_of(plan)


def test_the_helper_is_handed_the_exact_bytes_and_checksum_and_commits_only_after_the_confirmation(installed):
    fixes, fake, iid, folder, sha = installed
    out = fixes.plan_script_admin(iid, "postinst.sh", sha)
    assert fake.asked == [("postinst.sh", RAN.encode(), sha, str(folder))] and fake.committed == []  # planned, not run
    assert out["kind"] == "helper" and out["title"] == "Run postinst.sh as administrator" and "as administrator" in out["message"]
    assert "Cygnus cannot undo it" in out["security_note"]
    assert fixes.commit(out["token"], lambda line: None) == {"ok": True} and fake.committed == ["plan-1"]
    with pytest.raises(CygnusError, match="expired"):
        fixes.commit(out["token"], lambda line: None)  # a confirmation is used once


def test_nothing_reaches_the_helper_for_a_script_that_is_not_the_recorded_one_or_has_changed_or_is_in_the_wrong_folder(installed):
    fixes, fake, iid, folder, sha = installed
    with pytest.raises(CygnusError):
        fixes.plan_script_admin(iid, "postinst.sh", "0" * 64)
    with pytest.raises(CygnusError):
        fixes.plan_script_admin(iid, "run", scriptrun.sha256(b.ELF_STUB))
    with pytest.raises(CygnusError):
        fixes.plan_script_admin(iid, "../../etc/passwd", sha)
    (folder / "postinst.sh").write_text("#!/bin/sh\necho changed\n")
    with pytest.raises(CygnusError, match="not the one that was shown"):
        fixes.plan_script_admin(iid, "postinst.sh", sha)
    (folder / "postinst.sh").write_text(RAN)
    (folder / ".cygnus-installation").write_text('{"installation": "someone-else"}')
    with pytest.raises(CygnusError, match="not the one Cygnus made"):
        fixes.plan_script_admin(iid, "postinst.sh", sha)
    with pytest.raises(CygnusError):
        fixes.plan_script_admin("no-such-installation", "postinst.sh", sha)
    assert fake.asked == []


# -- the terminal ------------------------------------------------------------------------------------------------------------------------
def cli_args(path, **kw):
    from tests.test_archive_service import args

    return args(path, **kw)


def test_the_terminal_shows_the_scripts_in_full_and_runs_them_as_you_only_when_asked(tmp_path, monkeypatch, capsys):
    from cygnus.cli import main as cli

    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(), "T.tar.gz")
    monkeypatch.setattr(service, "archive_libraries", lambda *a, **k: {"glibc": None, "packages": [], "unresolved": []})
    assert cli.cmd_install(cli_args(path)) == 0  # -y alone never runs a script
    out = capsys.readouterr().out
    assert not (mount / "Applications" / "T" / "ran.txt").exists() and "done" in out
    from cygnus.core.registry import db as regdb
    from cygnus.core.registry import open_registry

    service.uninstall(regdb.list_installations(open_registry())[0]["id"], True, lambda line: None)
    assert cli.cmd_install(cli_args(path, run_scripts=True)) == 0
    out = capsys.readouterr().out
    assert "--- postinst.sh (it would run as you) ---" in out and "echo hello > ran.txt" in out and "ran postinst.sh: ok" in out
    assert (mount / "Applications" / "T" / "ran.txt").read_text() == "hello\n"


def test_the_terminal_asks_before_running_and_a_no_runs_nothing_and_installs_nothing(tmp_path, monkeypatch, capsys):
    from cygnus.cli import main as cli

    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path, items(), "T.tar.gz")
    answers = iter(["y", "n"])  # unpack it: yes; run the scripts: no
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.cmd_install(cli_args(path, run_scripts=True, yes=False)) == 1
    assert "cancelled" in capsys.readouterr().out and not (mount / "Applications" / "T").exists()


def test_the_terminal_hands_an_administrator_script_to_the_helper_after_installing(tmp_path, monkeypatch, capsys):
    from cygnus.cli import main as cli
    from cygnus.core import privilege

    mount = drive(tmp_path, monkeypatch)
    fake = FakeClient()
    monkeypatch.setattr(privilege, "HelperClient", lambda *a, **k: fake)
    path = archive(tmp_path, items(), "T.tar.gz")
    monkeypatch.setattr(service, "archive_libraries", lambda *a, **k: {"glibc": None, "packages": [], "unresolved": []})
    assert cli.cmd_install(cli_args(path, run_scripts=True, scripts_as_admin=True)) == 0
    out = capsys.readouterr().out
    assert "it would run as administrator" in out and "as administrator" in out and not (mount / "Applications" / "T" / "ran.txt").exists()
    assert fake.asked == [("postinst.sh", RAN.encode(), scriptrun.sha256(RAN.encode()), str(mount / "Applications" / "T"))] and fake.committed == ["plan-1"]


def test_the_terminal_runs_removal_scripts_before_removing_when_asked(tmp_path, monkeypatch, capsys):
    import argparse

    from cygnus.cli import main as cli

    mount = drive(tmp_path, monkeypatch)
    marker = tmp_path / "prerm-ran"
    path = archive(tmp_path, items(extra=[("T/prerm", "file", f"#!/bin/sh\necho done > {marker}\n".encode(), 0o755)]), "T.tar.gz")
    monkeypatch.setattr(service, "archive_libraries", lambda *a, **k: {"glibc": None, "packages": [], "unresolved": []})
    assert cli.cmd_install(cli_args(path)) == 0
    plain = argparse.Namespace(app="T", delete_file=False, remove_autostart=False, yes=True, registry=None, run_scripts=False, scripts_as_admin=False)
    ns = argparse.Namespace(**{**vars(plain), "delete_file": True, "run_scripts": True})
    assert cli.cmd_uninstall(ns) == 0
    out = capsys.readouterr().out
    assert "--- prerm (it would run as you) ---" in out and marker.read_text() == "done\n" and not (mount / "Applications" / "T").exists()


# -- the text shown is the whole of what runs --------------------------------------------------------------------------------------------
def test_a_long_script_is_shown_whole_and_what_cannot_be_shown_whole_is_not_offered(tmp_path, monkeypatch):
    from cygnus.core.backends import portable

    drive(tmp_path, monkeypatch)
    long_script = "#!/bin/sh\n" + "# padding\n" * 6300 + "echo END-OF-SCRIPT\n"  # about 63,000 bytes: more than the 60,000 characters shown elsewhere
    assert 60_000 < len(long_script) <= scriptrun.MAX_SCRIPT_BYTES
    plan = service.analyse(archive(tmp_path, items(long_script), "T.tar.gz"), "HDD")
    assert plan["scripts"]["texts"]["postinst.sh"] == long_script and plan["scripts"]["texts"]["postinst.sh"].rstrip().endswith("END-OF-SCRIPT")
    assert [i["name"] for i in plan["scripts"]["items"]] == ["postinst.sh"] and plan["scripts"]["items"][0]["sha256"] == scriptrun.sha256(long_script.encode())
    real = portable.scripts_report
    monkeypatch.setattr(portable, "scripts_report", lambda scripts, **kw: real(scripts, cap=100))  # whatever cut the text, the script is not offered
    cut = service.analyse(archive(tmp_path, items(long_script), "T2.tar.gz"), "HDD")
    assert cut["scripts"]["items"] == [] and len(cut["scripts"]["texts"]["postinst.sh"]) == 100


def test_a_script_too_big_to_pass_to_a_program_is_not_listed_at_all_and_one_that_is_not_text_is_listed_with_why(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    huge = "#!/bin/sh\n" + "#" * (scriptrun.MAX_SCRIPT_BYTES + 1)
    plan = service.analyse(archive(tmp_path, items(huge), "H.tar.gz"), "HDD")
    assert plan["scripts"] is None or plan["scripts"]["items"] == []
    binary = service.analyse(archive(tmp_path, items(extra=[("T/setup", "file", b"\x7fELF\0\0\0 not text", 0o755)]), "B.tar.gz"), "HDD")
    assert [i["name"] for i in binary["scripts"]["items"]] == ["postinst.sh"]
    assert binary["scripts"]["not_runnable"] and binary["scripts"]["not_runnable"][0]["name"] == "setup" and "zero byte" in binary["scripts"]["not_runnable"][0]["reason"]


def test_files_that_only_look_like_install_scripts_never_crowd_out_the_real_one_in_an_archive_with_no_single_folder(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    decoys = [(f"d{i}/update", "file", b"#!/bin/sh\ntrue\n", 0o755) for i in range(1, 11)]  # ten of them first, each in a folder of its own
    entries = [("run", "file", b.ELF_STUB, 0o755), *decoys, ("postinst.sh", "file", RAN.encode(), 0o755)]
    plan = service.analyse(archive(tmp_path, entries, "R.tar.gz"), "HDD")
    assert [i["name"] for i in plan["scripts"]["items"]] == ["postinst.sh"] and plan["scripts"]["not_runnable"] == []
