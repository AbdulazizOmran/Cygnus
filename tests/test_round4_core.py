"""Regression tests for review round 4: executor / registry / AppImage operations."""

import os

import pytest

from cygnus.core.errors import CygnusError
from cygnus.core.ops import appimage_ops


def _swap_params(tmp_path, old=b"old build", new=b"new build"):
    path, staged, backup = tmp_path / "App.AppImage", tmp_path / "staged", tmp_path / ".App.AppImage.cygnus-previous"
    path.write_bytes(old)
    staged.write_bytes(new)
    return path, staged, backup, {
        "path": str(path), "staged": str(staged), "backup": str(backup),
        "old_sha256": appimage_ops.sha256_file(path), "new_sha256": appimage_ops.sha256_file(staged)}


def test_the_application_file_exists_at_every_moment_of_a_swap(tmp_path, monkeypatch):
    path, staged, backup, params = _swap_params(tmp_path)
    seen = []
    real_replace = os.replace

    def watching_replace(src, dst, *a, **k):
        seen.append((path.exists(), backup.exists()))  # just before the new build takes the path
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(appimage_ops.os, "replace", watching_replace)
    outcome = appimage_ops._swap(params)
    assert seen == [(True, True)]  # the path was never empty, and the previous build was already kept
    assert path.read_bytes() == b"new build" and backup.read_bytes() == b"old build"
    assert outcome.compensation.kind == "appimage.unswap"
    appimage_ops._unswap(params)
    assert path.read_bytes() == b"old build" and not backup.exists()


def test_a_filesystem_without_hard_links_keeps_a_complete_copy_first(tmp_path, monkeypatch):
    path, staged, backup, params = _swap_params(tmp_path)

    def no_links(*a, **k):
        raise OSError("hard links are not supported here")

    monkeypatch.setattr(appimage_ops.os, "link", no_links)
    appimage_ops._swap(params)
    assert path.read_bytes() == b"new build" and backup.read_bytes() == b"old build"
    assert not backup.with_name(backup.name + ".partial").exists()


def test_a_swap_an_older_release_left_between_its_two_renames_is_finished_safely(tmp_path):
    """Older releases renamed path -> backup, then staged -> path; a crash in between left no file at the path."""
    path, staged, backup, params = _swap_params(tmp_path)
    os.rename(path, backup)  # the state the crash left
    assert not path.exists()
    appimage_ops._swap(params)  # "Finish it"
    assert path.read_bytes() == b"new build" and backup.read_bytes() == b"old build"


@pytest.mark.parametrize("state", ["gap", "replaced_not_journaled", "linked_only"])
def test_undoing_an_interrupted_swap_always_leaves_the_previous_build_in_place(tmp_path, state):
    path, staged, backup, params = _swap_params(tmp_path)
    if state == "gap":
        os.rename(path, backup)
    elif state == "replaced_not_journaled":
        os.link(path, backup)
        os.replace(staged, path)
    else:
        os.link(path, backup)
    appimage_ops._swap_abort(params)
    assert path.read_bytes() == b"old build" and not backup.exists()
    appimage_ops._swap_abort(params)  # and it is safe to run again
    assert path.read_bytes() == b"old build"


def test_undoing_a_swap_refuses_to_touch_a_file_someone_changed(tmp_path):
    path, staged, backup, params = _swap_params(tmp_path)
    os.link(path, backup)
    path.unlink()
    path.write_bytes(b"the user's own edit")
    with pytest.raises(CygnusError, match="changed"):
        appimage_ops._swap_abort(params)
    assert path.read_bytes() == b"the user's own edit" and backup.read_bytes() == b"old build"


def test_the_swap_has_an_abort_handler_registered():
    from cygnus.core.registry import open_registry

    ex = appimage_ops.Ops(open_registry(":memory:")).executor()
    assert "appimage.swap.abort" in ex.handlers


# -- registry: an update is all or nothing ---------------------------------------------------------------
def _registry_with_two_installations():
    from cygnus.core.registry import db as regdb
    from cygnus.core.registry import open_registry

    reg = open_registry(":memory:")
    for app, iid in (("org.a.A", "inst-a"), ("org.b.B", "inst-b")):
        regdb.add_application(reg, app_id=app, display_name=app, vendor=None, trust_level=None)
        regdb.add_installation(reg, app_id=app, fmt="appimage", source={}, version="1", location_id=None,
                               origin="adopted", update_provider=None, installation_id=iid)
    regdb.add_artifact(reg, installation_id="inst-a", kind="desktop", locator="/menu/shared.desktop")
    regdb.add_artifact(reg, installation_id="inst-b", kind="file", locator="/apps/B.AppImage", sha256="aa")
    regdb.add_artifact(reg, installation_id="inst-b", kind="shim", locator="/shims/b")
    return reg, regdb


def test_an_update_that_conflicts_on_an_artifact_changes_nothing():
    reg, regdb = _registry_with_two_installations()
    before = regdb.artifacts_of(reg, "inst-b")
    params = {"installation_id": "inst-b", "version": "2", "source": {"sha256": "bb"},
              "artifacts": [{"kind": "shim", "locator": "/shims/b"},
                            {"kind": "desktop", "locator": "/menu/shared.desktop"},  # owned by inst-a
                            {"kind": "file", "locator": "/apps/B.AppImage", "sha256": "bb"}],
              "history": {"to": "2"}}
    with pytest.raises(CygnusError, match="already belongs to another installation"):
        appimage_ops._update_record(reg, params)
    assert regdb.artifacts_of(reg, "inst-b") == before  # nothing was lost, the payload is still recorded
    [row] = [r for r in regdb.list_installations(reg) if r["id"] == "inst-b"]
    assert row["version"] == "1"
    assert reg.conn.execute("SELECT COUNT(*) FROM history WHERE installation_id='inst-b'").fetchone()[0] == 0


def test_an_update_replaces_the_artifacts_and_records_history_together():
    reg, regdb = _registry_with_two_installations()
    params = {"installation_id": "inst-b", "version": "2", "source": {"sha256": "bb"},
              "artifacts": [{"kind": "file", "locator": "/apps/B2.AppImage", "sha256": "bb"}], "history": {"to": "2"}}
    outcome = appimage_ops._update_record(reg, params)
    assert [a["locator"] for a in regdb.artifacts_of(reg, "inst-b")] == ["/apps/B2.AppImage"]
    assert reg.conn.execute("SELECT COUNT(*) FROM history WHERE installation_id='inst-b'").fetchone()[0] == 1
    appimage_ops._update_record(reg, outcome.compensation.params)  # the compensation puts the old record back
    assert sorted(a["locator"] for a in regdb.artifacts_of(reg, "inst-b")) == ["/apps/B.AppImage", "/shims/b"]


def test_a_failed_commit_does_not_leave_the_registry_stuck_in_a_transaction():
    import sqlite3

    reg, regdb = _registry_with_two_installations()

    class FailingCommit:
        """A connection whose COMMIT fails once (disk full, I/O error)."""
        def __init__(self, conn):
            self._conn, self.failed = conn, False

        def execute(self, sql, *a):
            if sql == "COMMIT" and not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    real = reg.conn
    reg.conn = FailingCommit(real)
    with pytest.raises(sqlite3.OperationalError):
        regdb.add_history(reg, "inst-a", "x")
    reg.conn = real
    assert not real.in_transaction
    regdb.add_history(reg, "inst-a", "y")  # later callers still work


# -- an expired curated manifest keeps its signature pin --------------------------------------------------
def _helium(expired):
    from datetime import UTC, datetime

    from cygnus.core.manifest import catalog

    raw = (catalog.resources.files("cygnus.data") / "manifests/net.imput.helium.json").read_bytes()
    now = datetime(2099, 1, 1, tzinfo=UTC) if expired else None
    return catalog.load(raw, origin="bundled", now=now)


def test_an_expired_curated_manifest_keeps_the_vendor_signature_pin(tmp_path):
    fresh, stale = _helium(False), _helium(True)
    assert stale.trust_level == "unverified" and not stale.can_drive_actions  # expiry still stops actions
    assert appimage_ops.pinned_fingerprint(stale) == appimage_ops.pinned_fingerprint(fresh) is not None
    unsigned = tmp_path / "x.AppImage"
    unsigned.write_bytes(b"not an appimage")
    for manifest in (fresh, stale):  # refused the same way, whether or not the manifest has expired
        with pytest.raises(CygnusError):
            appimage_ops.require_pinned_signature(unsigned, manifest)


def test_a_manifest_that_is_not_authentic_pins_nothing():
    from cygnus.core.manifest import catalog

    raw = (catalog.resources.files("cygnus.data") / "manifests/net.imput.helium.json").read_bytes()
    imported = catalog.load(raw, origin="import")  # nobody vouches for it
    assert not imported.authentic and appimage_ops.pinned_fingerprint(imported) is None


def test_planning_an_unsigned_download_of_a_pinned_app_is_blocked_even_when_the_manifest_expired():
    from cygnus.core import planner
    from cygnus.core.models import Candidate, PackageFormat
    from cygnus.core.registry.db import StorageLocation

    hdd = StorageLocation(id="h", label="HDD", fs_uuid="U", fs_type="ext4", location_class="user-owned",
                          capabilities={"supports_appimages": True})
    ssd = StorageLocation(id="s", label="SSD", fs_uuid=None, fs_type="", location_class="system")
    cand = Candidate(format=PackageFormat.APPIMAGE, source="/x.AppImage", name="Helium", metadata={"file_size": 1})
    for expired in (False, True):
        plan = planner.plan_appimage(cand, hdd, ssd, manifest=_helium(expired), signature_status="unsigned",
                                     health_probe=lambda c: False)
        assert plan.blocked and any(i.code == "APPIMAGE_SIGNATURE_INVALID" for i in plan.issues), expired


def test_adopting_a_file_in_place_requires_the_vendor_signature_whoever_asks(tmp_path, monkeypatch):
    """The GUI's 'already in a managed folder' branch used to skip the check that install and repair make."""
    from cygnus.core.backends import appimage as ab
    from cygnus.core.models import Candidate, PackageFormat

    app = tmp_path / "Helium.AppImage"
    app.write_bytes(b"\x7fELF" + b"\0" * 64)
    cand = Candidate(format=PackageFormat.APPIMAGE, source=str(app), name="Helium", metadata={"file_size": 68})
    monkeypatch.setattr(ab, "verify_signature", lambda path, fpr: ab.SignatureResult(status="wrong-key", detail=""))
    with pytest.raises(CygnusError, match="not signed by"):
        appimage_ops.plan_adopt(cand, app_key="net.imput.helium", location=None, manifest=_helium(False))
    monkeypatch.setattr(ab, "verify_signature", lambda path, fpr: ab.SignatureResult(status="verified", detail=""))
    steps = appimage_ops.plan_adopt(cand, app_key="net.imput.helium", location=None, manifest=_helium(False))
    record = next(s for s in steps if s.kind == "registry.record")
    assert record.params["source"]["sha256"] == appimage_ops.sha256_file(app)
    # an unknown application is adopted as before, with nothing to verify
    assert appimage_ops.plan_adopt(cand, app_key="x.y.z", location=None, manifest=None)


# -- uninstall keeps what you edited, and what that points at ---------------------------------------------
def _installed_with_edits(tmp_path, monkeypatch, edit_entry):
    """An installation with a menu entry and a launcher shim written through the real handlers."""
    from cygnus.core.desktop import integrate
    from cygnus.core.registry import db as regdb
    from cygnus.core.registry import open_registry

    monkeypatch.setattr(integrate, "xdg_data_home", lambda: tmp_path / "data")
    monkeypatch.setattr(integrate, "xdg_config_home", lambda: tmp_path / "config")
    monkeypatch.setattr(integrate, "shim_dir", lambda: tmp_path / "bin")
    entry = tmp_path / "data/applications/hello.desktop"
    shim = tmp_path / "bin/hello-launch"
    reg = open_registry(":memory:")
    regdb.add_application(reg, app_id="org.a.Hello", display_name="Hello", vendor=None, trust_level=None)
    iid = regdb.add_installation(reg, app_id="org.a.Hello", fmt="appimage", source={"path": str(tmp_path / "x")},
                                 version="1", location_id=None, origin="installed", update_provider=None)
    body = f"[Desktop Entry]\nName=Hello\nExec={shim}\n".encode()
    for path, data in ((entry, body), (shim, b"#!/bin/sh\nexec /x\n")):
        integrate.write_owned({"path": str(path), "data_hex": data.hex(), "mode": 0o755})
    regdb.add_artifact(reg, installation_id=iid, kind="desktop_entry", locator=str(entry),
                       sha256=integrate.sha256_bytes(body))
    regdb.add_artifact(reg, installation_id=iid, kind="launcher_shim", locator=str(shim),
                       sha256=integrate.sha256_bytes(b"#!/bin/sh\nexec /x\n"))
    if edit_entry:
        entry.write_text(entry.read_text() + "X-My-Edit=1\n")
    return reg, iid, entry, shim


def test_uninstall_keeps_a_menu_entry_you_edited_and_the_launcher_it_uses(tmp_path, monkeypatch):
    from cygnus.core.ops import executor_for, plan_uninstall

    reg, iid, entry, shim = _installed_with_edits(tmp_path, monkeypatch, edit_entry=True)
    report = executor_for(reg).run("uninstall", plan_uninstall(reg, iid, remove_payload=False))
    assert report.state == "succeeded" and report.kept == [str(entry)]
    assert "X-My-Edit=1" in entry.read_text() and shim.exists()  # the kept entry still works
    assert reg.conn.execute("SELECT COUNT(*) FROM installation").fetchone()[0] == 0


def test_uninstall_removes_unedited_files_as_before(tmp_path, monkeypatch):
    from cygnus.core.ops import executor_for, plan_uninstall

    reg, iid, entry, shim = _installed_with_edits(tmp_path, monkeypatch, edit_entry=False)
    report = executor_for(reg).run("uninstall", plan_uninstall(reg, iid, remove_payload=False))
    assert report.state == "succeeded" and report.kept == []
    assert not entry.exists() and not shim.exists()


def test_a_rollback_still_refuses_to_remove_a_file_that_changed(tmp_path, monkeypatch):
    from cygnus.core.desktop import integrate

    reg, iid, entry, shim = _installed_with_edits(tmp_path, monkeypatch, edit_entry=True)
    with pytest.raises(CygnusError, match="was changed after Cygnus wrote it"):
        integrate.remove_owned({"path": str(entry), "expect_sha256": integrate.sha256_bytes(b"something else")})


def test_a_rolled_back_forget_gets_its_history_and_update_checks_back():
    from cygnus.core.registry import db as regdb
    from cygnus.core.registry import open_registry

    reg = open_registry(":memory:")
    regdb.add_application(reg, app_id="org.a.A", display_name="A", vendor=None, trust_level=None)
    iid = regdb.add_installation(reg, app_id="org.a.A", fmt="appimage", source={"path": "/x"}, version="1",
                                 location_id=None, origin="installed", update_provider=None)
    regdb.add_artifact(reg, installation_id=iid, kind="file", locator="/x", sha256="aa")
    regdb.add_history(reg, iid, "installed", {"version": "1"})
    regdb.add_history(reg, iid, "updated", {"version": "2"})
    with reg.transaction() as c:
        c.execute("INSERT INTO update_check(installation_id, provider, current, available, checked_at, notes) "
                  "VALUES (?,?,?,?,?,?)", (iid, "zsync", "1", "2", "2026-10-08T00:00:00Z", "n"))
    ops = appimage_ops.Ops(reg)
    outcome = ops._forget({"installation_id": iid})
    assert reg.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0  # cascaded away
    ops._restore(outcome.compensation.params)
    assert reg.conn.execute("SELECT COUNT(*) FROM history WHERE installation_id=?", (iid,)).fetchone()[0] == 2
    assert reg.conn.execute("SELECT available FROM update_check WHERE installation_id=?", (iid,)).fetchone() == ("2",)
    assert [a["locator"] for a in regdb.artifacts_of(reg, iid)] == ["/x"]
    assert "history" in ops._cascading_tables() and "update_check" in ops._cascading_tables()


def test_two_managed_apps_that_ship_the_same_desktop_file_name_do_not_overwrite_each_other(tmp_path, monkeypatch):
    from cygnus.core.desktop import integrate

    monkeypatch.setattr(integrate, "xdg_data_home", lambda: tmp_path / "data")
    monkeypatch.setattr(integrate, "xdg_config_home", lambda: tmp_path / "config")
    monkeypatch.setattr(integrate, "shim_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(integrate, "system_desktop_dirs", lambda: [])

    def plan(key):
        return integrate.plan_appimage_integration(
            app_key=key, display_name=key, appimage_path=f"/apps/{key}.AppImage", fs_uuid=None,
            location_label="SSD", upstream_desktop={"Name": key, "Exec": "app %U"}, upstream_desktop_id="hello.desktop",
            icon=None, icon_kind=None)

    first = plan("org.example.One")
    assert first.desktop_id == "hello.desktop"
    entry = next(f for f in first.files if f.kind == "desktop_entry")
    entry.path.parent.mkdir(parents=True)
    entry.path.write_bytes(entry.data)
    second = plan("org.example.Two")  # a different Cygnus-managed app with the same desktop-file name
    assert second.desktop_id == "cygnus-org.example.Two.desktop"
    assert integrate._is_ours(entry.path) and integrate._is_ours(entry.path, "org.example.One")
    assert not integrate._is_ours(entry.path, "org.example.Two")
    again = plan("org.example.One")  # the first app updating itself keeps its own entry
    assert again.desktop_id == "hello.desktop"


# -- resuming and aborting interrupted steps --------------------------------------------------------------
def test_a_copy_that_landed_before_it_was_journaled_is_still_removed_when_the_operation_is_undone(tmp_path):
    src, dest = tmp_path / "src.AppImage", tmp_path / "apps/App.AppImage"
    src.write_bytes(b"app bytes")
    digest = appimage_ops.sha256_file(src)
    params = {"src": str(src), "dest": str(dest), "expect_sha256": digest, "existed": False}
    appimage_ops._copy(params)  # the copy lands...
    outcome = appimage_ops._copy(params)  # ...the process dies; "Finish it" runs the step again
    assert outcome.result["already_present"] and outcome.compensation.kind == "appimage.remove"
    appimage_ops._remove(outcome.compensation.params)
    assert not dest.exists()
    # a file that was already there beforehand is never claimed
    dest.parent.mkdir(exist_ok=True)
    dest.write_bytes(b"app bytes")
    assert appimage_ops._copy({**params, "existed": True}).compensation is None


def test_a_desktop_write_that_landed_before_it_was_journaled_is_removed_on_undo(tmp_path, monkeypatch):
    from cygnus.core.desktop import integrate

    monkeypatch.setattr(integrate, "xdg_data_home", lambda: tmp_path / "data")
    path = tmp_path / "data/applications/x.desktop"
    params = {"path": str(path), "data_hex": b"[Desktop Entry]\n".hex(), "mode": 0o644, "existed": False}
    integrate.write_owned(params)
    outcome = integrate.write_owned(params)  # run again after the crash
    assert outcome.compensation.kind == "fs.remove_owned" and outcome.result["replaced"] is False
    integrate.remove_owned(outcome.compensation.params)
    assert not path.exists()
    # a file that genuinely existed is restored, as before
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"old")
    again = integrate.write_owned({**params, "existed": True})
    assert again.compensation.kind == "fs.restore_owned" and again.result["replaced"] is True


def test_the_abort_of_a_write_that_was_refused_does_not_wedge_the_operation(tmp_path, monkeypatch):
    from cygnus.core.desktop import integrate

    monkeypatch.setattr(integrate, "xdg_data_home", lambda: tmp_path / "data")
    apps = tmp_path / "data/applications"
    apps.mkdir(parents=True)
    (tmp_path / "elsewhere").write_text("x")
    (apps / "x.desktop").symlink_to(tmp_path / "elsewhere")  # e.g. managed by a dotfile tool
    params = {"path": str(apps / "x.desktop"), "data_hex": b"data".hex(), "existed": True}
    with pytest.raises(CygnusError, match="symbolic link"):
        integrate.write_owned(params)  # the step refuses before writing...
    assert integrate.write_owned_abort(params).result == {"untouched": str(apps / "x.desktop")}  # ...so its abort is fine


def test_the_abort_of_a_write_removes_the_temporary_file_of_an_interrupted_atomic_write(tmp_path, monkeypatch):
    from cygnus.core.desktop import integrate

    monkeypatch.setattr(integrate, "xdg_data_home", lambda: tmp_path / "data")
    apps = tmp_path / "data/applications"
    apps.mkdir(parents=True)
    (apps / ".x.desktop.tmp-4242").write_bytes(b"half")
    (apps / ".other.desktop.tmp-4242").write_bytes(b"not ours to touch")
    integrate.write_owned_abort({"path": str(apps / "x.desktop"), "data_hex": b"data".hex(), "existed": False})
    assert not (apps / ".x.desktop.tmp-4242").exists() and (apps / ".other.desktop.tmp-4242").exists()


@pytest.mark.parametrize("lower, higher", [
    ("1.0b2", "1.0"), ("1.0a1", "1.0b1"), ("1.0b2", "1.0rc1"), ("1.0rc1", "1.0"), ("1.0b1", "1.0b2"),
    ("2.0a3", "2.0"), ("1.0.0b2", "1.0.1"), ("1.0dev", "1.0a1"), ("1.0beta2", "1.0"), ("1.0-rc1", "1.0"),
])
def test_pre_releases_of_every_spelling_sort_before_the_release(lower, higher):
    from cygnus.core.backends.appimage import version_key

    assert version_key(lower) < version_key(higher)
    assert not version_key(higher) <= version_key(lower)


def test_an_ordinary_version_is_not_mistaken_for_a_pre_release():
    from cygnus.core.backends.appimage import version_key

    assert version_key("1.2.3") < version_key("1.2.10") and version_key("0.18.3.1") < version_key("0.19.0")
    assert version_key("1.0") == version_key("1.0.0") and version_key("v2.5.0") == version_key("2.5.0")


def test_the_executor_says_why_something_was_left_in_place(tmp_path, monkeypatch):
    from cygnus.core.executor import Step, StepOutcome

    reg, iid, entry, shim = _installed_with_edits(tmp_path, monkeypatch, edit_entry=True)
    from cygnus.core.ops import executor_for, plan_uninstall

    report = executor_for(reg).run("uninstall", plan_uninstall(reg, iid, remove_payload=False))
    assert "you changed it" in report.kept_reasons[str(entry)]
    ex = executor_for(reg)
    ex.register("x.step", lambda params: StepOutcome(result={"kept": True, "left_over": ["/a", "/b"], "reason": "why"}))
    report = ex.run("x", [Step(kind="x.step", params={})])
    assert report.kept == ["/a", "/b"] and report.kept_reasons == {"/a": "why", "/b": "why"}
