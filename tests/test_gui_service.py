"""GUI service flows against throwaway Flatpak installations, registries and XDG dirs (no host changes).

Repositories are signed with a temporary key in its own GnuPG home; bundles carry an unreachable
RuntimeRepo URL, so every test also proves that installing never fetches the address a bundle declares.
"""

import shutil
from pathlib import Path

import pytest

import builders
from cygnus.core.backends import flatpak as fb
from cygnus.core.errors import CygnusError
from cygnus.core.executor import Executor
from cygnus.core.ops import flatpak_ops
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation
from cygnus.core.desktop import integrate
from cygnus.core.storage import locations
from cygnus.gui import service

RUNTIME = "org.cygnus.TestPlatform/x86_64/1"
OLD_RUNTIME = "org.cygnus.OldPlatform/x86_64/1"
APP_REF = "app/org.cygnus.TestApp/x86_64/stable"
HDD_CAPS = {"exec_allowed": True, "chmod_persists": True, "symlinks": True, "hardlinks": True,
            "atomic_rename": True, "ostree_bare_user_only": True}

flatpak_tools = pytest.mark.needs_tool("ostree", "flatpak", "gpg")


@pytest.fixture(scope="module")
def signed(tmp_path_factory):
    root = tmp_path_factory.mktemp("signed")
    with builders.GpgTestKey(root) as key:
        repo = builders.build_flatpak_repo(root, {RUNTIME: None, OLD_RUNTIME: "superseded by 2"}, key)
        bundles = {}
        for runtime in (RUNTIME, OLD_RUNTIME):
            d = root / runtime.split("/")[0]
            d.mkdir()
            bundles[runtime] = builders.build_flatpak_bundle(d, runtime=runtime)
        yield repo, key, bundles


def _registered_hdd(tmp_path, monkeypatch) -> Path:
    hdd = tmp_path / "hdd"
    hdd.mkdir()
    reg = open_registry()
    reg.add_location(StorageLocation(id="hdd", label="HDD", fs_uuid="X", fs_type="ext4", location_class="user-owned",
                                     canonical_mount=str(hdd), capabilities=HDD_CAPS))
    reg.close()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(loc, True, str(hdd)))
    monkeypatch.setattr(flatpak_ops, "filesystem_uuid", lambda path: "X")  # the fake drive
    return hdd


def _system_like(tmp_path, repo, key, name="vendorrepo"):
    """A non-user installation (stand-in for /var/lib/flatpak) that has the repository configured."""
    root = tmp_path / "system-like"
    builders.add_ostree_remote(root, name, repo, key)
    return _open_system(root)


def _open_system(root: Path):
    return flatpak_ops._open({"path": str(root), "user": False})


def _detect(bundle: Path):
    from cygnus.core.detect import detect_file

    return detect_file(str(bundle))


def _fresh_user():
    flatpak_ops.user_dir().mkdir(parents=True, exist_ok=True)
    return flatpak_ops.installation_for("user")


@flatpak_tools
def test_install_bundle_onto_hdd(signed, tmp_path, monkeypatch):
    repo, key, bundles = signed
    hdd = _registered_hdd(tmp_path, monkeypatch)
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", repo, key)  # already trusted by this user
    lines = []
    result = service.install_flatpak_bundle(str(bundles[RUNTIME]), "HDD", lines.append)
    assert result["ok"], result
    assert result["installation"] == "user"
    assert result["steps"] == ["Install the org.cygnus.TestPlatform 1 runtime", "Install org.cygnus.TestApp"]
    store = hdd / ".cygnus-flatpak-user"
    assert flatpak_ops.relocation_state().relocated_to == store
    assert (store / "app/org.cygnus.TestApp").is_dir()
    assert (store / "runtime/org.cygnus.TestPlatform").is_dir()  # same installation as the app (§2.4)
    inst = flatpak_ops.installation_for("user")
    assert inst.get_installed_ref(1, "org.cygnus.TestPlatform", "x86_64", "1", None).get_origin() == "vendorrepo"
    [row] = regdb.list_installations(open_registry())
    assert row["format"] == "flatpak" and row["location_id"] == "hdd"
    assert row["update_provider"]["type"] == "manual"  # the bundle names no update source
    assert any("Moving Flatpak" in l for l in lines) and lines[-1] == "Installed."


@flatpak_tools
def test_runtime_repository_is_copied_with_its_signing_key(signed, tmp_path):
    repo, key, bundles = signed
    system = _system_like(tmp_path, repo, key)
    user = _fresh_user()
    env = fb.FlatpakEnv([system], target=user)
    [issue] = [i for i in fb.analyse_bundle(_detect(bundles[RUNTIME]), env)
               if i.code == "FP_RUNTIME_MISSING"]
    assert "add it to your personal Flatpak installation" in issue.explanation
    assert issue.resolutions[0].privilege.value == "user"

    steps = flatpak_ops.plan_bundle_install(user, bundles[RUNTIME], APP_REF, RUNTIME, env)
    assert [s.kind for s in steps] == ["flatpak.mirror_remote", "flatpak.install_ref", "flatpak.install_bundle"]
    report = Executor(open_registry(":memory:"), dict(flatpak_ops.HANDLERS)).run("install", steps)
    assert report.state == "succeeded", report.error
    user = flatpak_ops.installation_for("user")
    remote = user.get_remote_by_name("vendorrepo", None)
    assert remote.get_gpg_verify() and remote.get_url() == f"file://{repo}"
    assert (flatpak_ops.user_dir() / "repo/vendorrepo.trustedkeys.gpg").read_bytes() == \
        (tmp_path / "system-like/repo/vendorrepo.trustedkeys.gpg").read_bytes()
    assert user.get_installed_ref(0, "org.cygnus.TestApp", "x86_64", "stable", None) is not None


@flatpak_tools
def test_unsigned_repository_is_never_copied(signed, tmp_path):
    repo, _, bundles = signed
    system = _system_like(tmp_path, repo, None)
    user = _fresh_user()
    env = fb.FlatpakEnv([system], target=user)
    steps = flatpak_ops.plan_bundle_install(user, bundles[RUNTIME], APP_REF, RUNTIME, env)
    report = Executor(open_registry(":memory:"), dict(flatpak_ops.HANDLERS)).run("install", steps)
    assert report.state == "rolled_back" and "not signed" in report.error
    user = flatpak_ops.installation_for("user")
    assert [r.get_name() for r in user.list_remotes(None)] == []
    assert list(user.list_installed_refs(None)) == []


@flatpak_tools
def test_failed_bundle_rolls_back_runtime_and_copied_repository(signed, tmp_path):
    repo, key, _ = signed
    system = _system_like(tmp_path, repo, key)
    user = _fresh_user()
    broken = tmp_path / "broken.flatpak"
    broken.write_bytes(b"flatpak\x00\x01\x00\x89\xe5 truncated")
    steps = flatpak_ops.plan_bundle_install(user, broken, APP_REF, RUNTIME, fb.FlatpakEnv([system], target=user))
    report = Executor(open_registry(":memory:"), dict(flatpak_ops.HANDLERS)).run("install", steps)
    assert report.state == "rolled_back", report
    assert report.failed_step == 2 and report.compensated == [1, 0]
    user = flatpak_ops.installation_for("user")
    assert list(user.list_installed_refs(None)) == []
    assert [r.get_name() for r in user.list_remotes(None)] == []


@flatpak_tools
def test_obsolete_runtime_needs_explicit_approval(signed, tmp_path):
    repo, key, bundles = signed
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", repo, key)
    user = flatpak_ops.installation_for("user")
    env = fb.FlatpakEnv([], target=user)
    with pytest.raises(CygnusError, match="no longer receives security updates"):
        flatpak_ops.plan_bundle_install(user, bundles[OLD_RUNTIME], APP_REF, OLD_RUNTIME, env)
    steps = flatpak_ops.plan_bundle_install(user, bundles[OLD_RUNTIME], APP_REF, OLD_RUNTIME, env,
                                            allow_obsolete_runtime=True)
    assert [s.kind for s in steps] == ["flatpak.install_ref", "flatpak.install_bundle"]


@flatpak_tools
def test_runtime_installed_elsewhere_still_goes_into_the_apps_installation(signed, tmp_path):
    """A runtime present only in another installation is not reused (it could be pruned there)."""
    repo, key, _ = signed
    other = tmp_path / "other-user"
    builders.add_ostree_remote(other, "vendorrepo", repo, key)
    other_inst = flatpak_ops.installation_for("user", other)
    assert flatpak_ops.run_transaction(other_inst, installs=[("vendorrepo", f"runtime/{RUNTIME}")]).ok
    other_sys = _open_system(other)  # same directory, seen as a non-user installation
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", repo, key)
    user = flatpak_ops.installation_for("user")
    status = fb.FlatpakEnv([other_sys], target=user).runtime_status(RUNTIME)
    assert status.installed_in and not status.installed
    assert status.install_source == ("user", "vendorrepo")


# -- AppImage adoption --------------------------------------------------------------------------------------
@pytest.mark.needs_tool("mksquashfs", "unsquashfs")
def test_adopt_appimage_makes_it_managed(tmp_path):
    app = builders.build_appimage(tmp_path)
    assert not service.is_managed(str(app))
    lines = []
    result = service.adopt_appimage(str(app), lines.append)
    assert result["ok"], result
    assert service.is_managed(str(app))
    assert (integrate.xdg_data_home() / "applications/hello.desktop").exists()
    assert lines and lines[0].startswith("[1/")
    [row] = service.list_apps()
    assert row["origin"] == "adopted" and row["path"] == str(app)
    assert service.uninstall(row["installation_id"], False, lines.append)["ok"]
    assert app.exists() and not service.is_managed(str(app))


def test_adopt_refuses_other_formats(tmp_path):
    pkg = builders.build_pkg(tmp_path)
    with pytest.raises(CygnusError, match="not an AppImage"):
        service.adopt_appimage(str(pkg), lambda _: None)


@flatpak_tools
def test_uninstall_removes_app_and_the_runtime_cygnus_added(signed, tmp_path, monkeypatch):
    repo, key, bundles = signed
    hdd = _registered_hdd(tmp_path, monkeypatch)
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", repo, key)
    assert service.install_flatpak_bundle(str(bundles[RUNTIME]), "HDD", lambda _: None)["ok"]
    [row] = service.list_apps()
    lines = []
    result = service.uninstall(row["installation_id"], False, lines.append)
    assert result["ok"], result
    inst = flatpak_ops.installation_for("user")
    assert list(inst.list_installed_refs(None)) == []
    assert service.list_apps() == []
    assert any("runtimes nothing uses" in l for l in lines)
    assert flatpak_ops.relocation_state().relocated_to == hdd / ".cygnus-flatpak-user"  # storage stays put


@flatpak_tools
def test_uninstall_keeps_a_runtime_the_user_installed(signed, tmp_path, monkeypatch):
    repo, key, bundles = signed
    _registered_hdd(tmp_path, monkeypatch)
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", repo, key)
    user = flatpak_ops.installation_for("user")
    assert flatpak_ops.run_transaction(user, installs=[("vendorrepo", f"runtime/{RUNTIME}")]).ok  # by the user
    result = service.install_flatpak_bundle(str(bundles[RUNTIME]), "HDD", lambda _: None)
    assert result["steps"] == ["Install org.cygnus.TestApp"]
    [row] = service.list_apps()
    assert service.uninstall(row["installation_id"], False, lambda _: None)["ok"]
    refs = [r.format_ref() for r in flatpak_ops.installation_for("user").list_installed_refs(None)]
    assert refs == [f"runtime/{RUNTIME}"]


# -- what must be recorded after a commit is done by the backend, not by a page --------------------------------
class _CommitClient:
    def __init__(self, ok=True):
        self.ok, self.committed = ok, []

    def plan_packages(self, **kw):
        from cygnus.core.privilege import Plan
        return Plan("p1", {}, "plan")

    def commit(self, plan, on_progress=None):
        self.committed.append(plan.plan_id)
        return (True, "") if self.ok else (False, "pacman failed")


def test_uninstalling_packages_forgets_the_installation_when_the_commit_succeeds(monkeypatch, tmp_path):
    from cygnus.gui import fixes

    forgotten = []
    monkeypatch.setattr(fixes, "HelperClient", lambda: _CommitClient())
    monkeypatch.setattr(service, "forget_installation", forgotten.append)
    plan = fixes.plan_remove_packages(["pfetch"], forget="inst-1")
    assert fixes.commit(plan["token"], lambda line: None) == {"ok": True}
    assert forgotten == ["inst-1"]
    assert plan["token"] not in fixes._ON_SUCCESS


def test_a_failed_commit_records_nothing(monkeypatch):
    from cygnus.gui import fixes

    forgotten = []
    monkeypatch.setattr(fixes, "HelperClient", lambda: _CommitClient(ok=False))
    monkeypatch.setattr(service, "forget_installation", forgotten.append)
    plan = fixes.plan_remove_packages(["pfetch"], forget="inst-1")
    assert fixes.commit(plan["token"], lambda line: None) == {"ok": False, "detail": "pacman failed"}
    assert forgotten == [] and plan["token"] not in fixes._ON_SUCCESS


def test_built_packages_are_recorded_by_the_backend_after_the_commit(monkeypatch, tmp_path):
    from cygnus.gui import fixes

    calls = []
    pkg = tmp_path / "x.pkg.tar.zst"
    pkg.write_bytes(b"x")
    monkeypatch.setattr(fixes, "HelperClient", lambda: _CommitClient())
    monkeypatch.setattr(service, "record_aur_install", lambda *a, **k: calls.append(("aur", a, k)))
    monkeypatch.setattr(service, "record_package_install", lambda *a, **k: calls.append(("pkg", a, k)))
    dep = {"kind": "aur", "pkgbase": "lib", "name": "lib", "commit": "c1", "version": "1-1", "names": ["lib", "lib-docs"],
           "dependency_of": "app"}
    top = {"kind": "aur", "pkgbase": "app", "name": "app", "commit": "c2", "version": "2-1"}
    conv = {"kind": "converted", "name": "tool", "version": "3", "source": "/home/u/tool.deb"}
    for record in (dep, top, conv):
        plan = fixes.plan_built_packages([str(pkg)], origin="converted" if record is conv else "aur", record=record)
        assert fixes.commit(plan["token"], lambda line: None)["ok"]
    assert calls == [
        ("aur", ("lib", "lib", "c1", "1-1"), {"packages": ["lib", "lib-docs"], "dependency_of": "app"}),
        ("aur", ("app", "app", "c2", "2-1"), {}),
        ("pkg", ("tool", "3"), {"origin": "converted", "source": {"file": "/home/u/tool.deb"}})]


def test_a_recording_failure_after_a_successful_install_is_reported_honestly(monkeypatch, tmp_path):
    from cygnus.gui import fixes

    def boom(*a, **k):
        raise CygnusError("registry is locked")

    pkg = tmp_path / "x.pkg.tar.zst"
    pkg.write_bytes(b"x")
    client = _CommitClient()
    monkeypatch.setattr(fixes, "HelperClient", lambda: client)
    monkeypatch.setattr(service, "record_aur_install", boom)
    plan = fixes.plan_built_packages([str(pkg)], record={"kind": "aur", "pkgbase": "a", "name": "a", "commit": "c",
                                                         "version": "1"})
    out = fixes.commit(plan["token"], lambda line: None)
    assert client.committed and out["ok"] is False
    assert "change was made" in out["detail"] and "registry is locked" in out["detail"]


@pytest.mark.parametrize("record", [{"kind": "aur", "pkgbase": "a"}, {"kind": "nope"}, {"kind": "converted"}])
def test_a_malformed_record_is_refused_before_anything_is_planned(monkeypatch, record):
    from cygnus.gui import fixes

    monkeypatch.setattr(fixes, "HelperClient", lambda: pytest.fail("must not plan with a bad record"))
    with pytest.raises(CygnusError):
        fixes.plan_built_packages(["/nonexistent"], record=record)


def test_a_fifo_given_as_a_package_file_is_refused_instead_of_hanging(tmp_path):
    import os
    import threading

    from cygnus.core.privilege import HelperClient  # noqa: F401 - imported for its open_regular use below

    fifo = tmp_path / "x.pkg.tar.zst"
    os.mkfifo(fifo)
    result = []

    def attempt():
        try:
            service.file_sha256(str(fifo))
        except CygnusError as exc:
            result.append(str(exc))

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "reading a FIFO must not block"
    assert result and "not a regular file" in result[0]
    from cygnus.core.util.fs import open_regular
    with pytest.raises(CygnusError, match="not a regular file"):
        open_regular(fifo, follow_symlinks=False)
    real = tmp_path / "real"
    real.write_bytes(b"x")
    os.close(open_regular(real))
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(CygnusError, match="is a link to another file"):
        open_regular(tmp_path / "link", follow_symlinks=False)


@pytest.mark.needs_tool("makepkg", "fakeroot", "bsdtar", "zstd")
def test_a_converted_file_must_be_the_one_that_was_analysed(tmp_path, monkeypatch):
    import builders
    from cygnus.core.backends import pacman as pm

    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    monkeypatch.setattr(pm, "read_config", lambda: cfg)
    monkeypatch.setattr(pm, "run_worker", lambda c, req: {"packages": {}, "satisfiers": {}})
    monkeypatch.setattr("cygnus.core.backends.aur.info", lambda names, **kw: {})
    monkeypatch.setattr("cygnus.core.backends.sources._flathub_search", lambda q: [])
    deb = builders.build_deb(tmp_path, depends="", files={"usr/bin/hello": b"#!/bin/sh\n"})
    plan = service.analyse(str(deb), None)
    assert plan["kind"] == "foreign" and len(plan["sha256"]) == 64 and plan["sha256"] == service.file_sha256(str(deb))
    deb.write_bytes(deb.read_bytes() + b"\0")  # replaced after the analysis the user confirmed
    with pytest.raises(CygnusError, match="changed after it was analysed"):
        service.convert_foreign(str(deb), lambda line: None, plan["sha256"])


@pytest.mark.needs_tool("makepkg", "fakeroot", "bsdtar", "zstd")
def test_a_file_replaced_while_it_is_being_analysed_is_refused_not_shown_with_the_new_checksum(tmp_path, monkeypatch):
    import builders
    from cygnus.core.backends import pacman as pm

    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    monkeypatch.setattr(pm, "read_config", lambda: cfg)
    monkeypatch.setattr(pm, "run_worker", lambda c, req: {"packages": {}, "satisfiers": {}})
    monkeypatch.setattr("cygnus.core.backends.aur.info", lambda names, **kw: {})
    monkeypatch.setattr("cygnus.core.backends.sources._flathub_search", lambda q: [])
    deb = builders.build_deb(tmp_path, depends="", files={"usr/bin/hello": b"#!/bin/sh\n"})
    real = service._foreign_verdict

    def replaced_midway(cand, progress):
        verdict = real(cand, progress)
        deb.write_bytes(deb.read_bytes() + b"\0")  # someone swaps the file while the analysis runs
        return verdict

    monkeypatch.setattr(service, "_foreign_verdict", replaced_midway)
    with pytest.raises(CygnusError, match="changed while Cygnus was checking it"):
        service.analyse(str(deb), None)


@pytest.mark.needs_tool("makepkg", "fakeroot", "bsdtar", "zstd")
def test_a_file_swapped_between_being_recognised_and_being_summed_is_refused(tmp_path, monkeypatch):
    import builders
    from cygnus.core.backends import pacman as pm

    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    monkeypatch.setattr(pm, "read_config", lambda: cfg)
    monkeypatch.setattr(pm, "run_worker", lambda c, req: {"packages": {}, "satisfiers": {}})
    monkeypatch.setattr("cygnus.core.backends.aur.info", lambda names, **kw: {})
    monkeypatch.setattr("cygnus.core.backends.sources._flathub_search", lambda q: [])
    deb = builders.build_deb(tmp_path, depends="", files={"usr/bin/hello": b"#!/bin/sh\n"})
    real = service.detect_file

    def swapped_after_detecting(path):
        cand = real(path)
        other = tmp_path / "other.deb"
        other.write_bytes(deb.read_bytes() + b"\0")  # a different file moved into place right after the first was read
        other.replace(deb)
        return cand

    monkeypatch.setattr(service, "detect_file", swapped_after_detecting)
    with pytest.raises(CygnusError, match="changed while Cygnus was opening it"):
        service.analyse(str(deb), None)


def test_a_file_that_grows_while_it_is_copied_is_stopped_at_the_limit():
    import hashlib
    import io

    class Endless:
        def read(self, n):
            return b"y" * n  # it was small when its size was looked at, but it never stops coming

    out = io.BytesIO()
    with pytest.raises(CygnusError, match="too large to convert"):
        service._copy_capped(Endless(), out, hashlib.sha256(), 3 << 20)
    assert len(out.getvalue()) <= 3 << 20
    ok = io.BytesIO()
    service._copy_capped(io.BytesIO(b"abc"), ok, hashlib.sha256(), 3)
    assert ok.getvalue() == b"abc"  # exactly the limit is allowed


# -- review round 4: AppImage install on a machine with no storage set up ----------------------------------
@pytest.mark.needs_tool("mksquashfs", "unsquashfs")
def test_an_appimage_cannot_be_installed_until_a_storage_location_exists(tmp_path):
    app = builders.build_appimage(tmp_path)
    plan = service.analyse(str(app), "SSD")  # the page's placeholder label on a fresh machine
    assert not plan["installable"] and plan["blocked"]
    [issue] = [i for i in plan["issues"] if i["code"] == "STORAGE_NOT_SET_UP"]
    assert "Storage" in issue["explanation"]
    with pytest.raises(CygnusError, match="add one on the Storage page"):
        service.install_appimage(str(app), "SSD", lambda line: None)


@pytest.mark.needs_tool("mksquashfs", "unsquashfs")
def test_with_a_registered_location_the_analysis_names_the_target_the_install_uses(tmp_path, monkeypatch):
    app = builders.build_appimage(tmp_path / "src") if (tmp_path / "src").mkdir() is None else None
    _registered_hdd(tmp_path, monkeypatch)
    plan = service.analyse(str(app), "HDD")
    assert plan["installable"] and plan["target"] == "HDD"
    assert not any(i["code"] == "STORAGE_NOT_SET_UP" for i in plan["issues"])
    assert service.install_appimage(str(app), plan["target"], lambda line: None)["ok"]


def test_a_local_package_installed_from_the_gui_is_recorded_so_it_can_be_removed_later(monkeypatch, tmp_path):
    from cygnus.core.privilege import Plan
    from cygnus.gui import fixes

    pkg = tmp_path / "tool-1.2-1-x86_64.pkg.tar.zst"
    pkg.write_bytes(b"x")

    class Client:
        def plan_packages(self, **kw):
            return Plan("p1", {"local": [{"name": "tool", "version": "1.2-1"}]}, "Install tool 1.2-1")

        def commit(self, plan, on_progress=None):
            return True, ""

    monkeypatch.setattr(fixes, "HelperClient", Client)
    plan = fixes.plan_local_package(str(pkg))
    assert fixes.commit(plan["token"], lambda line: None) == {"ok": True}
    [row] = [r for r in regdb.list_installations(open_registry()) if r["name"] == "tool"]
    assert row["format"] == "pacman" and row["source"]["made_by"] == "local" and row["source"]["file"] == str(pkg)
    assert service.package_names(row["id"]) == ["tool"]


@pytest.mark.needs_tool("mksquashfs", "unsquashfs")
def test_an_appimage_is_adopted_into_the_deepest_location_that_holds_it(tmp_path, monkeypatch):
    outer = tmp_path / "hdd"
    inner = outer / "apps"
    inner.mkdir(parents=True)
    reg = open_registry()
    for lid, label, path in (("outer", "Outer", outer), ("inner", "Inner", inner)):
        reg.add_location(StorageLocation(id=lid, label=label, fs_uuid=lid, fs_type="ext4", location_class="user-owned",
                                         canonical_mount=str(path), capabilities=HDD_CAPS))
    reg.close()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(
        loc, True, str(outer if loc.id == "outer" else inner)))
    app = inner / "Hello-x86_64.AppImage"
    shutil.copy2(builders.build_appimage(tmp_path), app)
    assert service.adopt_appimage(str(app), lambda line: None)["ok"]
    [row] = regdb.list_installations(open_registry())
    assert row["location_id"] == "inner"


def test_a_flatpak_that_installed_but_could_not_be_recorded_is_reported_as_such(monkeypatch):
    from cygnus.core.errors import RegistryError

    class Locked:
        def transaction(self):
            raise RegistryError("database is locked")

    with pytest.raises(RegistryError):
        service._record_flatpak(Locked(), app_id="org.a.A", name="A", trust="unverified", source={}, version="1",
                                location_id=None, provider="manual", locator="user:app/org.a.A/x86_64/stable")
    message = service._installed_but_unrecorded("A", RegistryError("database is locked"))
    assert "A was installed" in message and "could not record it" in message and "database is locked" in message


def test_the_three_registry_rows_of_a_flatpak_are_written_together_or_not_at_all():
    reg = open_registry(":memory:")
    iid = service._record_flatpak(reg, app_id="org.a.A", name="A", trust="unverified", source={"ref": "r"},
                                  version="1", location_id=None, provider="manual", locator="user:r")
    assert [a["locator"] for a in regdb.artifacts_of(reg, iid)] == ["user:r"]
    count = lambda: reg.conn.execute("SELECT COUNT(*) FROM application").fetchone()[0]  # noqa: E731
    before = count()
    with pytest.raises(Exception):  # a duplicate locator for another installation fails the artifact insert
        service._record_flatpak(reg, app_id="org.b.B", name="B", trust="unverified", source={"ref": "r"},
                                version="1", location_id=None, provider="manual", locator="user:r")
    assert count() == before  # the application row of the failed attempt was rolled back too


def _managed_appimage_on_an_unplugged_drive(monkeypatch):
    reg = open_registry()
    reg.add_location(StorageLocation(id="hdd", label="HDD", fs_uuid="X", fs_type="ntfs3", location_class="user-owned",
                                     canonical_mount="/mnt/data", capabilities=HDD_CAPS))
    regdb.add_application(reg, app_id="org.whatpulse.WhatPulse", display_name="WhatPulse")
    regdb.add_installation(reg, app_id="org.whatpulse.WhatPulse", fmt="appimage",
                           source={"path": "/mnt/data/apps/WhatPulse.AppImage"}, version="5.1", location_id="hdd",
                           origin="adopted", update_provider=None)
    reg.close()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(
        loc, False, None, "the drive is not connected"))
    # discovery also looks at running programs: a WhatPulse running on the machine the tests run on must not count
    from cygnus.core import inventory
    monkeypatch.setattr(inventory, "find_installs", lambda *a, **k: [])


def test_an_application_on_an_unplugged_drive_is_offline_not_missing(monkeypatch):
    _managed_appimage_on_an_unplugged_drive(monkeypatch)
    report = service.check_app("WhatPulse")
    [install] = report["installs"]
    assert install["overall"] == "offline" and install["symbol"] == "⏏"
    assert install["where"] == "/mnt/data/apps/WhatPulse.AppImage" and install["version"] == "5.1"
    assert all(f["status"] == "offline" for f in install["features"])


def test_the_command_line_reports_it_the_same_way(monkeypatch, capsys):
    from cygnus.cli.main import main

    _managed_appimage_on_an_unplugged_drive(monkeypatch)
    assert main(["check", "WhatPulse"]) == 0
    out = capsys.readouterr().out
    assert "/mnt/data/apps/WhatPulse.AppImage" in out and "offline" in out and "not installed" not in out


def test_an_old_folder_kept_by_a_relocation_is_reported_to_the_caller(monkeypatch, tmp_path):
    from pathlib import Path

    from cygnus.core.executor import Step, StepOutcome
    from cygnus.core.ops import flatpak_ops

    _registered_hdd(tmp_path, monkeypatch)
    old = "/home/u/.local/share/flatpak/.app.cygnus-before-move"
    monkeypatch.setattr(flatpak_ops, "plan_relocation", lambda *a, **k: [Step(kind="flatpak.relocate", params={})])
    monkeypatch.setitem(flatpak_ops.HANDLERS, "flatpak.relocate", lambda params: StepOutcome(result={
        "moved": ["app"], "kept": True, "path": old, "left_over": [old],
        "reason": "changes made while Flatpak's storage was being moved are in it"}))
    lines, attention = [], []
    service._prepare_flatpak_installation(open_registry(), "HDD", lines.append, attention)
    [note] = attention
    assert "was moved" in note and old in note and "changes made while Flatpak's storage was being moved" in note
    assert "did not delete it" in note and any(old in line for line in lines)
    attention2 = []  # and nothing is said when nothing was kept
    monkeypatch.setitem(flatpak_ops.HANDLERS, "flatpak.relocate", lambda params: StepOutcome(result={"moved": ["app"]}))
    service._prepare_flatpak_installation(open_registry(), "HDD", lambda line: None, attention2)
    assert attention2 == []


@pytest.mark.needs_tool("makepkg", "fakeroot", "bsdtar", "zstd")
def test_a_whole_conversion_through_the_service_works_from_a_private_copy_and_cleans_up(tmp_path, monkeypatch):
    from cygnus.core import paths
    from cygnus.core.backends import pacman as pm

    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    monkeypatch.setattr(pm, "read_config", lambda: cfg)
    monkeypatch.setattr(pm, "run_worker", lambda c, req: {"packages": {}, "satisfiers": {}})
    monkeypatch.setattr("cygnus.core.backends.aur.info", lambda names, **kw: {})
    monkeypatch.setattr("cygnus.core.backends.sources._flathub_search", lambda q: [])
    deb = builders.build_deb(tmp_path / "in" if (tmp_path / "in").mkdir() is None else tmp_path, depends="",
                             files={"usr/bin/hello": b"#!/bin/sh\necho hi\n"})
    plan = service.analyse(str(deb), None)
    assert plan["strategy"] == "convert"
    lines = []
    result = service.convert_foreign(str(deb), lines.append, plan["sha256"])
    assert Path(result["package"]).is_file() and result["package"].endswith(".pkg.tar.zst")
    assert result["source"] == str(deb) and result["name"] == "hello" and result["notes"] is not None
    assert "Checking the package again…" in lines
    assert not list((paths.cache_dir() / "convert" / "incoming").iterdir())  # the private copy is gone
    assert deb.exists()  # and the original was only read
    import subprocess
    listing = subprocess.run(["bsdtar", "-tf", result["package"]], capture_output=True, text=True).stdout
    assert "usr/bin/hello" in listing


def test_a_file_that_is_too_large_is_refused_before_it_is_copied(tmp_path, monkeypatch):
    from cygnus.core import paths
    from cygnus.core.backends import foreign

    big = tmp_path / "big.deb"
    big.write_bytes(b"x" * 100)
    monkeypatch.setattr(foreign, "MAX_EXTRACT_BYTES", 10)
    with pytest.raises(CygnusError, match="too large to convert"):
        service.convert_foreign(str(big), lambda line: None)
    assert not list((paths.cache_dir() / "convert" / "incoming").iterdir())
