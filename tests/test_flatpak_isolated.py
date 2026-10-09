"""Isolated Flatpak integration tests: a throwaway user installation and a local test repository.

Nothing here touches the real system or user installations (no system helper is involved:
user installations at a custom path are written by this process only).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cygnus.core.executor import Executor
from cygnus.core.ops import flatpak_ops
from cygnus.core.registry import open_registry

pytestmark = pytest.mark.needs_tool("ostree", "flatpak")


def _commit(repo: Path, ref: str, tree: Path, metadata: str) -> None:
    subprocess.run(["ostree", "commit", f"--repo={repo}", f"--branch={ref}", f"--tree=dir={tree}",
                    "--subject=test", f"--add-metadata-string=xa.metadata={metadata}"],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def test_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("fprepo")
    repo = root / "repo"
    subprocess.run(["ostree", "init", "--mode=archive-z2", f"--repo={repo}"], check=True)
    # runtime
    rt = root / "runtime"
    (rt / "files/bin").mkdir(parents=True)
    (rt / "files/bin/sh-stub").write_text("#!/bin/sh\n")
    rt_meta = ("[Runtime]\nname=org.cygnus.TestPlatform\nruntime=org.cygnus.TestPlatform/x86_64/1\n"
               "sdk=org.cygnus.TestPlatform/x86_64/1\n")
    (rt / "metadata").write_text(rt_meta)
    _commit(repo, "runtime/org.cygnus.TestPlatform/x86_64/1", rt, rt_meta)
    # app using it, and an app using a runtime that does not exist
    for app_id, runtime in (("org.cygnus.TestApp", "org.cygnus.TestPlatform/x86_64/1"),
                            ("org.cygnus.Orphan", "org.cygnus.MissingPlatform/x86_64/9")):
        app = root / app_id
        (app / "files/bin").mkdir(parents=True)
        exe = app / "files/bin/testapp"
        exe.write_text("#!/bin/sh\necho hi\n")
        exe.chmod(0o755)
        meta = f"[Application]\nname={app_id}\nruntime={runtime}\nsdk={runtime}\ncommand=testapp\n"
        (app / "metadata").write_text(meta)
        _commit(repo, f"app/{app_id}/x86_64/stable", app, meta)
    subprocess.run(["flatpak", "build-update-repo", str(repo)], check=True, capture_output=True,
                   env={**os.environ, "FLATPAK_USER_DIR": str(root / "unused")})
    return repo


def make_installation(base: Path, repo: Path):
    import gi

    gi.require_version("Flatpak", "1.0")
    from gi.repository import Flatpak

    base.mkdir(parents=True, exist_ok=True)
    inst = flatpak_ops.installation_for("user", base)
    remote = Flatpak.Remote.new("cygnus-test")
    remote.set_url(f"file://{repo}")
    remote.set_gpg_verify(False)
    inst.add_remote(remote, True, None)
    return inst


def test_dry_run_plans_runtime_and_app(tmp_path, test_repo):
    inst = make_installation(tmp_path / "user", test_repo)
    res = flatpak_ops.run_transaction(inst, installs=[("cygnus-test", "app/org.cygnus.TestApp/x86_64/stable")],
                                      dry_run=True)
    assert res.ok, res.error
    refs = {op["ref"] for op in res.operations}
    assert refs == {"app/org.cygnus.TestApp/x86_64/stable", "runtime/org.cygnus.TestPlatform/x86_64/1"}
    assert not (tmp_path / "user/app/org.cygnus.TestApp").exists()  # nothing deployed


def test_missing_runtime_is_classified(tmp_path, test_repo):
    inst = make_installation(tmp_path / "user", test_repo)
    res = flatpak_ops.run_transaction(inst, installs=[("cygnus-test", "app/org.cygnus.Orphan/x86_64/stable")],
                                      dry_run=True)
    assert not res.ok
    assert res.error_code == "runtime-not-found" and res.issues[0].code == "FP_RUNTIME_UNAVAILABLE"
    assert "MissingPlatform" in res.error


def test_install_into_relocated_user_installation_and_undo(tmp_path, test_repo):
    base = tmp_path / "user"
    target = tmp_path / "hdd/.cygnus-flatpak-user"
    target.parent.mkdir(exist_ok=True)  # the drive's location folder
    inst = make_installation(base, test_repo)  # repo/ now exists with the remote config
    ex = Executor(open_registry(":memory:"), dict(flatpak_ops.HANDLERS))
    steps = flatpak_ops.plan_relocation(target, base)
    assert [s.params["names"] for s in steps] == [["repo", "app", "runtime", ".removed"]]
    assert ex.run("relocate", steps).state == "succeeded"
    state = flatpak_ops.relocation_state(base)
    assert state.relocated_to == target and all(v.startswith("symlink:") for v in state.entries.values())

    inst = flatpak_ops.installation_for("user", base)  # re-open through the symlinks
    res = flatpak_ops.run_transaction(inst, installs=[("cygnus-test", "app/org.cygnus.TestApp/x86_64/stable")])
    assert res.ok, res.error
    assert (target / "app/org.cygnus.TestApp").is_dir()
    assert (target / "runtime/org.cygnus.TestPlatform").is_dir()  # runtime lives next to the app
    assert inst.get_installed_ref(0, "org.cygnus.TestApp", "x86_64", "stable", None) is not None

    # Undo the relocation (as a rollback would): everything comes back to the base directory.
    flatpak_ops.HANDLERS["flatpak.unrelocate"](steps[0].params)
    assert all(v == "dir" for k, v in flatpak_ops.relocation_state(base).entries.items() if k != ".removed")
    inst = flatpak_ops.installation_for("user", base)
    assert inst.get_installed_ref(0, "org.cygnus.TestApp", "x86_64", "stable", None) is not None


def test_relocation_refuses_second_location(tmp_path, test_repo):
    base = tmp_path / "user"
    make_installation(base, test_repo)
    ex = Executor(open_registry(":memory:"), dict(flatpak_ops.HANDLERS))
    ex.run("relocate", flatpak_ops.plan_relocation(tmp_path / "a", base))
    from cygnus.core.errors import CygnusError

    with pytest.raises(CygnusError, match="already stored"):
        flatpak_ops.plan_relocation(tmp_path / "b", base)



def test_relocation_keeps_hardlinks_and_checks_space(tmp_path, test_repo, monkeypatch):
    base = tmp_path / "user"
    inst = make_installation(base, test_repo)
    assert flatpak_ops.run_transaction(inst, installs=[("cygnus-test", "app/org.cygnus.TestApp/x86_64/stable")]).ok
    deployed = next(p for p in (base / "app").rglob("testapp") if p.is_file())
    assert deployed.stat().st_nlink >= 2  # shared with an object in repo/

    target = tmp_path / "hdd/.cygnus-flatpak-user"
    target.parent.mkdir(exist_ok=True)  # the drive's location folder
    [step] = flatpak_ops.plan_relocation(target, base)
    real_statvfs = os.statvfs
    monkeypatch.setattr(os, "statvfs", lambda p: type("S", (), {"f_bavail": 1, "f_frsize": 4096})())
    with pytest.raises(Exception, match="needs .* MiB free"):
        flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert not any(p.is_symlink() for p in base.iterdir())  # nothing changed
    monkeypatch.setattr(os, "statvfs", real_statvfs)

    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    moved = next(p for p in (target / "app").rglob("testapp") if p.is_file())
    assert moved.stat().st_nlink >= 2  # still one copy shared by repo/ and app/


def test_failed_copy_leaves_nothing_behind_and_can_be_retried(tmp_path, test_repo, monkeypatch):
    base = tmp_path / "user"
    make_installation(base, test_repo)
    target = tmp_path / "hdd/.cygnus-flatpak-user"
    target.parent.mkdir(exist_ok=True)  # the drive's location folder
    [step] = flatpak_ops.plan_relocation(target, base)

    def broken(sources, dest):
        (dest / "repo").mkdir()
        (dest / "repo" / "partial").write_text("x")
        raise flatpak_ops.CygnusError("copying failed: No space left on device")

    with pytest.MonkeyPatch.context() as mp:  # scoped: never undo conftest's isolation
        mp.setattr(flatpak_ops, "_copy_together", broken)
        with pytest.raises(flatpak_ops.CygnusError, match="No space left"):
            flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert (base / "repo").is_dir() and not (base / "repo").is_symlink()
    assert not (target / "repo").exists()
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)  # a retry works
    assert (base / "repo").is_symlink()


def _interrupted_relocation(tmp_path, test_repo, stage):
    """A relocation the process died in, at `stage` ("copying" or "switching")."""
    from cygnus.core.registry import open_registry as _open

    base = tmp_path / "user"
    make_installation(base, test_repo)
    target = tmp_path / "hdd/.cygnus-flatpak-user"
    target.parent.mkdir(exist_ok=True)  # the drive's location folder
    [step] = flatpak_ops.plan_relocation(target, base)
    reg = _open(":memory:")
    ex = Executor(reg, dict(flatpak_ops.HANDLERS))

    def dies(params):
        target.mkdir(parents=True, exist_ok=True)
        if stage == "copying":
            (target / flatpak_ops.PARTIAL / "repo").mkdir(parents=True)
            (target / flatpak_ops.PARTIAL / "repo" / "half").write_text("x")
        else:  # everything copied (with its fingerprint), repo switched to its backup, link not created yet
            flatpak_ops._mark(target, "repo", flatpak_ops.tree_fingerprint(base / "repo"))
            shutil.copytree(base / "repo", target / "repo", symlinks=True)
            os.rename(base / "repo", base / ".repo.cygnus-before-move")
        raise KeyboardInterrupt  # power loss

    ex.register("flatpak.relocate", dies)
    with pytest.raises(KeyboardInterrupt):
        ex.run("relocate", [step])
    ex.register("flatpak.relocate", flatpak_ops.HANDLERS["flatpak.relocate"])
    [op] = ex.incomplete()
    return ex, op["id"], base, target


@pytest.mark.parametrize("stage", ["copying", "switching"])
def test_an_interrupted_relocation_can_be_finished(tmp_path, test_repo, stage):
    ex, op, base, target = _interrupted_relocation(tmp_path, test_repo, stage)
    assert ex.resume(op).state == "succeeded"
    assert flatpak_ops.relocation_state(base).relocated_to == target
    assert not (target / flatpak_ops.PARTIAL).exists() and not (base / ".repo.cygnus-before-move").exists()
    assert flatpak_ops.installation_for("user", base).list_remotes(None)  # the repository works


@pytest.mark.parametrize("stage", ["copying", "switching"])
def test_an_interrupted_relocation_can_be_undone(tmp_path, test_repo, stage):
    ex, op, base, target = _interrupted_relocation(tmp_path, test_repo, stage)
    assert ex.roll_back(op).state == "rolled_back"
    assert (base / "repo").is_dir() and not (base / "repo").is_symlink()
    assert not (target / "repo").exists() and not (target / flatpak_ops.PARTIAL).exists()
    assert flatpak_ops.installation_for("user", base).list_remotes(None)


@pytest.mark.parametrize("crash_after", ["copy", "unlink"])
def test_an_interrupted_move_back_can_be_finished(tmp_path, test_repo, crash_after):
    """Moving Flatpak's storage back from the drive is interrupted; running it again completes it."""
    base = tmp_path / "user"
    make_installation(base, test_repo)
    target = tmp_path / "hdd/.cygnus-flatpak-user"
    target.parent.mkdir(exist_ok=True)  # the drive's location folder
    [step] = flatpak_ops.plan_relocation(target, base)
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    remotes_before = [r.get_name() for r in flatpak_ops.installation_for("user", base).list_remotes(None)]
    # the state a power loss leaves: everything copied back into the restore folder…
    restore = base / ".cygnus-restore"
    restore.mkdir()
    for n in ("repo", "app", "runtime"):
        shutil.copytree(target / n, restore / n, symlinks=True)
    if crash_after == "unlink":  # …and the repo link already removed
        os.unlink(base / "repo")
    flatpak_ops.HANDLERS["flatpak.unrelocate"](step.params)
    assert all(v in ("dir", "absent") for v in flatpak_ops.relocation_state(base).entries.values())
    assert (base / "repo").is_dir() and not restore.exists()
    assert not (target / "repo").exists()
    assert [r.get_name() for r in flatpak_ops.installation_for("user", base).list_remotes(None)] == remotes_before


# -- concurrent writes, stale copies and absent drives (review round 3) ---------------------------------
def _relocatable(tmp_path, test_repo):
    base = tmp_path / "user"
    make_installation(base, test_repo)
    (base / "app").mkdir(exist_ok=True)
    (base / "app" / "old.bin").write_text("old")
    target = tmp_path / "hdd/.cygnus-flatpak-user"
    target.parent.mkdir(exist_ok=True)
    [step] = flatpak_ops.plan_relocation(target, base)
    return base, target, step


def test_a_write_during_the_copy_cancels_the_move(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    real = flatpak_ops._copy_together

    def copy_while_flatpak_writes(sources, dest):
        real(sources, dest)
        (base / "app" / "new.bin").write_text("installed meanwhile")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flatpak_ops, "_copy_together", copy_while_flatpak_writes)
        with pytest.raises(flatpak_ops.CygnusError, match="changed while Cygnus was copying"):
            flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert (base / "app").is_dir() and not (base / "app").is_symlink()
    assert (base / "app" / "new.bin").read_text() == "installed meanwhile"
    assert not (target / "app").exists() and not any(p.name.endswith("cygnus-before-move") for p in base.iterdir())
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)  # nothing writing now: it works
    assert (base / "app" / "new.bin").read_text() == "installed meanwhile"


def test_a_resumed_move_never_uses_a_copy_of_an_older_state(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    target.mkdir()
    flatpak_ops._mark(target, "app", flatpak_ops.tree_fingerprint(base / "app"))
    shutil.copytree(base / "app", target / "app", symlinks=True)  # an interrupted run's complete copy…
    (base / "app" / "new.bin").write_text("installed after the crash")  # …then Flatpak installed something
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert (base / "app").is_symlink() and (base / "app" / "new.bin").read_text() == "installed after the crash"


def test_a_folder_cygnus_did_not_copy_is_left_alone(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    (target / "app").mkdir(parents=True)
    (target / "app" / "someone-elses.txt").write_text("keep me")
    with pytest.raises(flatpak_ops.CygnusError, match="did not create it"):
        flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert (target / "app" / "someone-elses.txt").read_text() == "keep me"
    flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    assert (target / "app" / "someone-elses.txt").read_text() == "keep me"  # undo never deletes it either


def test_a_write_during_the_move_back_is_not_lost(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    real = flatpak_ops._copy_together

    def copy_while_flatpak_writes(sources, dest):
        real(sources, dest)
        (base / "app" / "new.bin").write_text("written through the link")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flatpak_ops, "_copy_together", copy_while_flatpak_writes)
        with pytest.raises(flatpak_ops.CygnusError, match="changed while Cygnus was copying"):
            flatpak_ops.HANDLERS["flatpak.unrelocate"](step.params)
    assert (base / "app").is_symlink() and (target / "app" / "new.bin").exists()  # still linked, nothing lost
    flatpak_ops.HANDLERS["flatpak.unrelocate"](step.params)
    assert not (base / "app").is_symlink() and (base / "app" / "new.bin").read_text() == "written through the link"
    assert not (target / "app").exists()


def test_a_resumed_move_back_copies_again_if_the_drive_changed(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    restore = base / ".cygnus-restore"
    restore.mkdir()
    for n in ("repo", "app", "runtime", ".removed"):
        flatpak_ops._mark(restore, n, flatpak_ops.tree_fingerprint(target / n))
        shutil.copytree(target / n, restore / n, symlinks=True)  # the crashed run's complete copies
    (target / "app" / "new.bin").write_text("written through the link after the crash")
    flatpak_ops.HANDLERS["flatpak.unrelocate"](step.params)
    assert (base / "app" / "new.bin").read_text() == "written through the link after the crash"


def test_moving_back_without_the_drive_changes_nothing(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    os.rename(target.parent, tmp_path / "unplugged")  # the drive is gone
    with pytest.raises(flatpak_ops.CygnusError, match="is not available"):
        flatpak_ops.HANDLERS["flatpak.unrelocate"](step.params)
    assert all((base / n).is_symlink() for n in ("repo", "app", "runtime", ".removed"))
    os.rename(tmp_path / "unplugged", target.parent)
    (target / "app").rename(tmp_path / "lost-app")  # the drive is back but one folder is not
    with pytest.raises(flatpak_ops.CygnusError, match="is missing"):
        flatpak_ops.HANDLERS["flatpak.unrelocate"](step.params)
    assert all((base / n).is_symlink() for n in ("repo", "app", "runtime", ".removed"))


def test_a_folder_flatpak_recreated_during_an_interrupted_move_back_is_not_overwritten(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    restore = base / ".cygnus-restore"
    restore.mkdir()
    flatpak_ops._mark(restore, "app", flatpak_ops.tree_fingerprint(target / "app"))
    shutil.copytree(target / "app", restore / "app", symlinks=True)
    os.unlink(base / "app")  # crashed right after removing the link…
    (base / "app").mkdir()
    (base / "app" / "fresh.bin").write_text("flatpak made a new one")  # …and Flatpak recreated the folder
    with pytest.raises(flatpak_ops.CygnusError, match="created again"):
        flatpak_ops.HANDLERS["flatpak.unrelocate"](step.params)
    assert (base / "app" / "fresh.bin").exists() and (target / "app" / "old.bin").exists()


def test_relocation_refuses_a_drive_that_is_not_mounted(tmp_path, test_repo, monkeypatch):
    base = tmp_path / "user"
    make_installation(base, test_repo)
    target = tmp_path / "mnt-data/.cygnus-flatpak-user"
    target.parent.mkdir()  # the empty mountpoint folder on the system drive
    [step] = flatpak_ops.plan_relocation(target, base, fs_uuid="DRIVE-UUID")
    monkeypatch.setattr(flatpak_ops, "filesystem_uuid", lambda path: "SYSTEM-UUID")
    with pytest.raises(flatpak_ops.CygnusError, match="not on the chosen drive"):
        flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert not target.exists() and not any(p.is_symlink() for p in base.iterdir())


# -- round 4: nothing is deleted once a drive copy is live or a folder reappeared ----------------------
def _relocated_but_not_cleaned_up(tmp_path, test_repo, monkeypatch):
    """The state a crash leaves just before the backups are deleted: links in place, backups and marks kept."""
    base, target, step = _relocatable(tmp_path, test_repo)
    real_rename = os.rename

    def power_loss_before_cleanup(src, dst, *a, **k):
        if str(dst).endswith("cygnus-trash"):
            raise KeyboardInterrupt
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(flatpak_ops.os, "rename", power_loss_before_cleanup)
    with pytest.raises(KeyboardInterrupt):
        flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    monkeypatch.setattr(flatpak_ops.os, "rename", real_rename)
    assert (base / "repo").is_symlink() and (base / ".repo.cygnus-before-move").is_dir()
    return base, target, step


def test_undoing_a_switched_relocation_never_discards_what_was_written_through_the_link(tmp_path, test_repo,
                                                                                         monkeypatch):
    base, target, step = _relocated_but_not_cleaned_up(tmp_path, test_repo, monkeypatch)
    (base / "repo" / "installed-later.txt").write_text("days of normal Flatpak use")  # goes to the drive
    with pytest.raises(flatpak_ops.CygnusError, match="kept both copies"):
        flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    assert (target / "repo" / "installed-later.txt").read_text() == "days of normal Flatpak use"
    assert (base / "repo").is_symlink() and (base / ".repo.cygnus-before-move").is_dir()  # both copies stay
    assert (base / "app").is_dir() and not (base / "app").is_symlink()  # untouched folders are put back
    assert (base / "app" / "old.bin").read_text() == "old" and not (target / "app").exists()
    with pytest.raises(flatpak_ops.CygnusError, match="kept both copies"):  # and doing it again changes nothing
        flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    assert (target / "repo" / "installed-later.txt").exists()


def test_undoing_a_switched_relocation_that_was_not_written_to_puts_everything_back(tmp_path, test_repo,
                                                                                     monkeypatch):
    base, target, step = _relocated_but_not_cleaned_up(tmp_path, test_repo, monkeypatch)
    flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    for n in ("repo", "app"):  # the folders that had content before the move
        assert (base / n).is_dir() and not (base / n).is_symlink() and not (target / n).exists()
    assert (base / "app" / "old.bin").read_text() == "old"
    # the folders that did not exist before are not left behind as links to empty drive folders either
    assert flatpak_ops.relocation_state(base).relocated_to is None
    assert not (base / "runtime").is_symlink() and not (base / ".removed").is_symlink()


def test_a_late_write_into_the_old_folder_is_kept_and_reported_not_deleted(tmp_path, test_repo, monkeypatch):
    """A handle opened before the move writes into the old folder right after its link was made."""
    base, target, step = _relocatable(tmp_path, test_repo)
    real_symlink = os.symlink

    def symlink_then_late_write(src, dst, *a, **k):
        real_symlink(src, dst, *a, **k)
        if Path(dst).name == "app":
            (base / ".app.cygnus-before-move" / "late.txt").write_text("written through an old handle")

    monkeypatch.setattr(flatpak_ops.os, "symlink", symlink_then_late_write)
    outcome = flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert outcome.result["kept"] is True  # the move itself worked; the old folder is the only place with the late file
    assert (base / "app").is_symlink()
    assert (base / ".app.cygnus-before-move" / "late.txt").read_text() == "written through an old handle"
    assert not (base / ".repo.cygnus-before-move").exists()  # the verified ones are gone
    assert (target / "app" / "old.bin").read_text() == "old"
    # undoing it afterwards cannot be sure which copy is complete: both stay
    with pytest.raises(flatpak_ops.CygnusError, match="kept both copies"):
        flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    assert (base / ".app.cygnus-before-move" / "late.txt").exists() and (target / "app" / "old.bin").exists()


def test_a_drive_folder_that_is_a_link_is_refused(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    elsewhere = tmp_path / "ssd-folder"
    elsewhere.mkdir()
    target.symlink_to(elsewhere)
    with pytest.raises(flatpak_ops.CygnusError, match="is a link"):
        flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert not any(elsewhere.iterdir()) and not (base / "repo").is_symlink()


def test_a_foreign_drive_folder_is_not_adopted_when_the_local_one_is_absent(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    shutil.rmtree(base / "app")
    (target / "app").mkdir(parents=True)
    (target / "app" / "someone-elses.txt").write_text("keep me")
    with pytest.raises(flatpak_ops.CygnusError, match="did not create it"):
        flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert (target / "app" / "someone-elses.txt").read_text() == "keep me" and not (base / "app").is_symlink()


def _moving_back(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    restore = base / ".cygnus-restore"
    restore.mkdir()
    for n in ("repo", "app", "runtime"):
        shutil.copytree(target / n, restore / n, symlinks=True)
        flatpak_ops._mark(restore, n, flatpak_ops.tree_fingerprint(target / n))
    return base, target, step, restore


def test_moving_back_deletes_nothing_when_flatpak_recreates_the_folder_in_the_window(tmp_path, test_repo, monkeypatch):
    base, target, step, restore = _moving_back(tmp_path, test_repo)
    real_unlink = os.unlink

    def unlink_then_flatpak_recreates(path, *a, **k):
        real_unlink(path, *a, **k)
        if Path(path) == base / "repo":
            (base / "repo").mkdir()
            (base / "repo" / "new-object").write_text("Flatpak wrote this meanwhile")

    monkeypatch.setattr(flatpak_ops.os, "unlink", unlink_then_flatpak_recreates)
    with pytest.raises(flatpak_ops.CygnusError, match="created again"):
        flatpak_ops.HANDLERS["flatpak.unrelocate"](step.params)
    assert (target / "repo").is_dir() and any((target / "repo").iterdir())  # the complete copy is still there
    assert (restore / "repo").is_dir()  # and so is the restore copy
    assert (base / "repo" / "new-object").exists()


def test_moving_back_reports_failure_when_something_links_the_folder_to_the_drive_again(tmp_path, test_repo,
                                                                                     monkeypatch):
    base, target, step, restore = _moving_back(tmp_path, test_repo)
    real_unlink = os.unlink

    def unlink_then_relinked(path, *a, **k):
        real_unlink(path, *a, **k)
        if Path(path) == base / "repo":
            os.symlink(target / "repo", base / "repo")  # another operation's self-heal

    monkeypatch.setattr(flatpak_ops.os, "unlink", unlink_then_relinked)
    with pytest.raises(flatpak_ops.CygnusError, match="linked to the drive again"):
        flatpak_ops.HANDLERS["flatpak.unrelocate"](step.params)
    assert (target / "repo").is_dir() and (restore / "repo").is_dir()


# -- round 5: the drive, partial deletes, stray links, recreated folders, old states --------------------
def test_with_the_drive_unplugged_an_undo_keeps_everything_and_does_not_revert_silently(tmp_path, test_repo,
                                                                                         monkeypatch):
    base, target, step = _relocated_but_not_cleaned_up(tmp_path, test_repo, monkeypatch)
    hidden = tmp_path / "hdd-unplugged"
    (tmp_path / "hdd").rename(hidden)  # the drive is gone
    with pytest.raises(flatpak_ops.CygnusError, match="drive is not connected"):
        flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    assert (base / "repo").is_symlink() and (base / "app").is_symlink()  # still linked, not reverted to stale backups
    assert (base / ".app.cygnus-before-move").is_dir() and (base / ".repo.cygnus-before-move").is_dir()
    with pytest.raises(flatpak_ops.CygnusError):  # finishing it needs the drive, too
        flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    (tmp_path / "hdd-unplugged").rename(tmp_path / "hdd")  # plugged in again, nothing written meanwhile
    flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)  # now an undo is safe
    assert (base / "app" / "old.bin").read_text() == "old" and not (base / "app").is_symlink()


def test_a_backup_that_was_half_deleted_by_a_power_cut_is_never_put_back_over_the_complete_copy(tmp_path, test_repo,
                                                                                                 monkeypatch):
    base, target, step = _relocated_but_not_cleaned_up(tmp_path, test_repo, monkeypatch)
    (base / ".app.cygnus-before-move" / "old.bin").unlink()  # the cleanup got this far before the power went
    with pytest.raises(flatpak_ops.CygnusError, match="kept both copies"):
        flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    assert (target / "app" / "old.bin").read_text() == "old"  # the complete drive copy is untouched
    assert (base / "app").is_symlink()
    outcome = flatpak_ops.HANDLERS["flatpak.relocate"](step.params)  # finishing instead: still loses nothing
    assert outcome.result["kept"] is True and (target / "app" / "old.bin").read_text() == "old"
    assert str(base / ".app.cygnus-before-move") in outcome.result["left_over"]


def test_a_failure_part_way_leaves_no_links_to_folders_that_did_not_exist(tmp_path, test_repo, monkeypatch):
    base, target, step = _relocatable(tmp_path, test_repo)
    real_switch, calls = flatpak_ops._switch, []

    def fail_on_the_third(b, r, n):
        calls.append(n)
        if len(calls) == 3:
            raise flatpak_ops.CygnusError("something went wrong")
        return real_switch(b, r, n)

    monkeypatch.setattr(flatpak_ops, "_switch", fail_on_the_third)
    with pytest.raises(flatpak_ops.CygnusError, match="something went wrong"):
        flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    state = flatpak_ops.relocation_state(base)
    assert state.relocated_to is None and all(not v.startswith("symlink:") for v in state.entries.values())
    assert flatpak_ops.ensure_relocation_links(base, dry_run=True) == []  # Flatpak operations are not blocked
    assert (base / "app" / "old.bin").read_text() == "old"


def test_a_folder_created_again_in_the_instant_before_its_link_is_kept_with_both_copies(tmp_path, test_repo,
                                                                                         monkeypatch):
    base, target, step = _relocatable(tmp_path, test_repo)
    real_symlink = os.symlink

    def recreated_first(src, dst, *a, **k):
        if Path(dst).name == "app":
            Path(dst).mkdir()  # something (a Flatpak command, a software centre) made it again
            (Path(dst) / "new.txt").write_text("new")
        return real_symlink(src, dst, *a, **k)

    monkeypatch.setattr(flatpak_ops.os, "symlink", recreated_first)
    with pytest.raises(flatpak_ops.CygnusError, match="kept both copies"):
        flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert (target / "app" / "old.bin").exists()  # the complete drive copy was NOT deleted
    assert (base / ".app.cygnus-before-move" / "old.bin").exists() and (base / "app" / "new.txt").exists()


def test_an_older_state_without_a_drive_baseline_is_never_undone_by_guessing(tmp_path, test_repo, monkeypatch):
    base, target, step = _relocated_but_not_cleaned_up(tmp_path, test_repo, monkeypatch)
    for n in ("repo", "app"):
        (target / flatpak_ops.COPIED / f"{n}.drive").unlink()  # as left by a release without the baseline
    (base / "app" / "installed-later.bin").write_text("written through the live link")
    with pytest.raises(flatpak_ops.CygnusError, match="kept both copies"):
        flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    assert (target / "app" / "installed-later.bin").exists()
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)  # resuming completes without inventing a baseline
    assert (target / "app" / "installed-later.bin").exists()


def test_what_a_crash_left_of_a_backup_being_deleted_is_cleaned_up_on_the_next_run(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    (base / ".app.cygnus-trash").mkdir()
    (base / ".app.cygnus-trash" / "half-deleted").write_text("x")
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    assert not (base / ".app.cygnus-trash").exists() and (target / "app" / "old.bin").read_text() == "old"


def test_every_verified_backup_is_checked_before_any_is_deleted(tmp_path, test_repo, monkeypatch):
    """Deleting one backup changes the hardlinks it shares with another: the checks must all come first."""
    base, target, step = _relocatable(tmp_path, test_repo)
    order = []
    real_fingerprint, real_rmtree = flatpak_ops.tree_fingerprint, shutil.rmtree

    def fingerprint(path):
        order.append(("check", Path(path).name))
        return real_fingerprint(path)

    def rmtree(path, *a, **k):
        order.append(("delete", Path(path).name))
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(flatpak_ops, "tree_fingerprint", fingerprint)
    monkeypatch.setattr(flatpak_ops.shutil, "rmtree", rmtree)
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    kinds = [k for k, name in order if name.endswith(("cygnus-before-move", "cygnus-trash"))]
    kinds = kinds[kinds.index("check"):]  # (the clean-up of leftover trash folders at the start is not a backup)
    assert "delete" in kinds and kinds == sorted(kinds, key=lambda k: k == "delete")  # all checks, then all deletes


def test_an_older_per_folder_move_that_stopped_between_rename_and_link_is_put_back(tmp_path, test_repo):
    base = tmp_path / "user"
    make_installation(base, test_repo)
    target = tmp_path / "hdd/.cygnus-flatpak-user/repo"
    params = {"base": str(base), "name": "repo", "target": str(target)}
    shutil.copytree(base / "repo", target, symlinks=True)  # what the interrupted run had copied
    os.rename(base / "repo", base / ".repo.cygnus-before-move")  # ...and renamed, then the power went
    assert not (base / "repo").exists()
    assert "flatpak.relocate_dir.abort" in flatpak_ops.HANDLERS
    flatpak_ops.HANDLERS["flatpak.relocate_dir.abort"](params)
    assert (base / "repo").is_dir() and not (base / "repo").is_symlink() and not target.exists()
    assert flatpak_ops.installation_for("user", base).list_remotes(None)  # the repository works again


def test_an_older_move_that_is_already_linked_is_never_guessed_at(tmp_path, test_repo):
    base = tmp_path / "user"
    make_installation(base, test_repo)
    target = tmp_path / "hdd/.cygnus-flatpak-user/repo"
    params = {"base": str(base), "name": "repo", "target": str(target)}
    shutil.copytree(base / "repo", target, symlinks=True)
    os.rename(base / "repo", base / ".repo.cygnus-before-move")
    os.symlink(target, base / "repo")
    with pytest.raises(flatpak_ops.CygnusError, match="cannot be sure which has everything"):
        flatpak_ops.HANDLERS["flatpak.relocate_dir.abort"](params)
    assert target.is_dir() and (base / ".repo.cygnus-before-move").is_dir() and (base / "repo").is_symlink()


def test_undoing_a_move_that_already_finished_says_so_instead_of_reporting_a_rollback(tmp_path, test_repo):
    base, target, step = _relocatable(tmp_path, test_repo)
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)  # completed, cleaned up, but (say) never journaled
    with pytest.raises(flatpak_ops.CygnusError, match='already moved completely.*Move back'):
        flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    assert (base / "app").is_symlink() and (target / "app" / "old.bin").read_text() == "old"  # nothing was touched


def test_an_undo_and_a_move_back_leave_no_empty_folder_behind_on_the_drive(tmp_path, test_repo, monkeypatch):
    base, target, step = _relocated_but_not_cleaned_up(tmp_path, test_repo, monkeypatch)
    flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    assert not target.exists()  # the folder this step made on the drive is gone again
    flatpak_ops.HANDLERS["flatpak.relocate"](step.params)
    flatpak_ops.HANDLERS["flatpak.unrelocate"](step.params)
    assert not target.exists() and (base / "app").is_dir() and not (base / "app").is_symlink()
    keep = tmp_path / "hdd/.cygnus-flatpak-user"
    keep.mkdir(parents=True, exist_ok=True)
    (keep / "someone-elses.txt").write_text("not ours")
    flatpak_ops.HANDLERS["flatpak.relocate.abort"](step.params)
    assert (keep / "someone-elses.txt").exists()  # a folder with anything else in it is never removed


def test_a_flatpak_install_reports_real_progress_inside_its_step(tmp_path, test_repo):
    from types import SimpleNamespace

    from cygnus.core import progress as pr

    inst = make_installation(tmp_path / "user", test_repo)
    seen = []
    pr.steps(seen.append)(1, 4, SimpleNamespace(description="Install it", kind="flatpak.install"))
    seen.clear()
    res = flatpak_ops.run_transaction(inst, installs=[("cygnus-test", "app/org.cygnus.TestApp/x86_64/stable")])
    pr.clear()
    assert res.ok, res.error
    fractions = [s.fraction for s in seen]
    assert all(0.25 <= f <= 0.5 + 1e-9 for f in fractions)  # step 2 of 4: between a quarter and a half
    assert fractions == sorted(fractions) and all(not s.log for s in seen)  # moves the bar, never floods the log
    assert all(("Installing" in str(s)) for s in seen)
    # both operations (the runtime, then the app) were reported, each in its own share of the step
    assert {round(f, 3) for f in fractions} >= {0.25, 0.375} and any("TestApp" in str(s) for s in seen)
