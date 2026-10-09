"""The helper never removes protected system packages, including kernels, firmware and HoldPkg."""

import pytest

from cygnus.core.backends import pacman as pm
from cygnus.helper import actions

CONFIG = pm.PacmanConfig(arch=("x86_64",), dbpath="/var/lib/pacman", cachedirs=(), gpgdir="", repos=("core",),
                         servers={}, hold=("my-held-pkg",))


class _Ledger:
    def owns_package(self, name):
        return True


def _plan(analysis=None, **req):
    def analyse(targets, **kw):
        return analysis or pm.PacmanAnalysis(targets=targets)

    return actions.plan_packages(req, caller_uid=1000, caller_sender=":1.1", ledger=_Ledger(), analyse=analyse,
                                 config=CONFIG, sync_fresh=lambda: "/nonexistent")


@pytest.mark.parametrize("name", ["glibc", "linux-cachyos", "linux-cachyos-lts", "linux-firmware-intel",
                                  "my-held-pkg"])
def test_explicit_removal_of_protected_packages_is_refused(name):
    with pytest.raises(actions.PlanRefused, match="protected"):
        _plan(remove=[name])


def test_ordinary_removal_is_planned():
    plan = _plan(remove=["ncdu"])
    assert plan.commands == [["pacman", "-R", "--noconfirm", "--", "ncdu"]]


def test_conflict_removal_of_a_protected_package_is_refused():
    analysis = pm.PacmanAnalysis(targets=["evil-sudo"], to_add=[{"name": "evil-sudo", "version": "1"}],
                                 to_remove=[{"name": "sudo", "version": "1.9"}])
    with pytest.raises(actions.PlanRefused, match="protected system packages: sudo"):
        _plan(analysis, install_repo=["evil-sudo"])


def test_repository_replacement_during_upgrade_is_shown_not_refused():
    analysis = pm.PacmanAnalysis(targets=[], sysupgrade=True,
                                 to_add=[{"name": "systemd-ng", "version": "2", "replaces_installed": "systemd"}],
                                 to_remove=[{"name": "systemd", "version": "1", "replaced_by": "systemd-ng"}])
    plan = _plan(analysis, sysupgrade=True)
    assert plan.summary["remove"] == [{"name": "systemd", "version": "1",
                                       "reason": "replaced by systemd-ng (repository decision)"}]
    assert plan.action_id.endswith("packages.remove")  # strictest tier, details every time


def test_password_message_counts_every_removed_package():
    msg = actions._package_message({"install": [], "local": [], "remove": [{"name": f"pkg{i}"} for i in range(50)]},
                                   sysupgrade=False)
    assert msg == "Remove " + ", ".join(f"pkg{i}" for i in range(8)) + " and 42 more."
    short = actions._package_message({"install": [], "local": [], "remove": [{"name": "a"}, {"name": "b"}]},
                                     sysupgrade=False)
    assert short == "Remove a, b."


def test_staging_copies_exactly_the_size_that_was_checked(tmp_path, monkeypatch):
    import hashlib
    import os

    pkg = tmp_path / "x.pkg.tar.zst"
    pkg.write_bytes(b"A" * 4096)
    digest = hashlib.sha256(b"A" * 4096).hexdigest()
    real_fstat = os.fstat
    with open(pkg, "rb") as fh:
        # the size measured for the staging budget was 0; the data appeared afterwards
        monkeypatch.setattr(actions.os, "fstat", lambda fd: os.stat_result((*real_fstat(fd)[:6], 0, *real_fstat(fd)[7:])))
        with pytest.raises(actions.PlanRefused, match="changed while it was being read"):
            actions.stage_local_package(fh.fileno(), digest, tmp_path / "staging")
        monkeypatch.setattr(actions.os, "fstat", real_fstat)
        path, got = actions.stage_local_package(fh.fileno(), digest, tmp_path / "staging")
    assert got == digest and path.stat().st_size == 4096
    assert len(list((tmp_path / "staging").iterdir())) == 1  # the refused copy left nothing behind


def test_staging_refuses_a_file_that_grew_after_it_was_charged_to_the_budget(tmp_path):
    import hashlib

    pkg = tmp_path / "x.pkg.tar.zst"
    pkg.write_bytes(b"A" * 4096)
    digest = hashlib.sha256(b"A" * 4096).hexdigest()
    with open(pkg, "rb") as fh:
        # the budget was charged for 10 bytes; by the time of the copy the file is 4096
        with pytest.raises(actions.PlanRefused, match="changed while it was being read"):
            actions.stage_local_package(fh.fileno(), digest, tmp_path / "staging", expected_size=10)
        assert not (tmp_path / "staging").exists() or not list((tmp_path / "staging").iterdir())
        path, got = actions.stage_local_package(fh.fileno(), digest, tmp_path / "staging", expected_size=4096)
    assert got == digest and path.stat().st_size == 4096


def test_a_process_merely_named_pacman_does_not_count(tmp_path):
    import os

    fake = tmp_path / "proc/4242"
    fake.mkdir(parents=True)
    (fake / "comm").write_text("pacman\n")
    os.symlink(str(tmp_path / "pacman"), fake / "exe")  # a user's copy, running as that user
    assert actions.package_managers_running(str(tmp_path / "proc")) == []


def test_plans_are_refused_when_pacmans_configuration_cannot_be_read(monkeypatch):
    def broken():
        raise pm.CygnusError("pacman-conf timed out")

    monkeypatch.setattr(pm, "read_config", broken)
    with pytest.raises(actions.PlanRefused, match="configuration could not be read"):
        actions.plan_packages({"remove": ["some-app"]}, caller_uid=1000, caller_sender=":1.1", ledger=_Ledger(),
                              analyse=lambda t, **kw: pm.PacmanAnalysis(targets=t), config=None,
                              sync_fresh=lambda: "/nonexistent")


@pytest.mark.parametrize("name", ["sof-firmware", "intel-ucode", "amd-ucode", "alsa-firmware", "linux-firmware-amdgpu"])
def test_firmware_and_microcode_are_protected(name):
    assert pm.is_protected(name)


@pytest.mark.parametrize("name", ["firefox", "ucode-tools", "linux-cachyos-headers"])
def test_ordinary_packages_are_not(name):
    assert not pm.is_protected(name)


def test_unit_names_cannot_look_like_options():
    assert not actions.UNIT_NAME.match("-x.service") and actions.UNIT_NAME.match("cups.service")


def test_a_plain_install_may_not_replace_a_protected_package():
    analysis = pm.PacmanAnalysis(targets=["sudo-rs"])
    analysis.to_add = [{"name": "sudo-rs", "version": "1-1", "repo": "extra", "installed_version": None}]
    analysis.to_remove = [{"name": "sudo", "version": "1.9-1", "replaced_by": "sudo-rs"}]
    with pytest.raises(actions.PlanRefused, match="protected"):
        _plan(analysis, install_repo=["sudo-rs"])
