"""Installing Flatpak apps from a repository by id, and moving them between installations.

The "system" installation is replaced by a throwaway one: writes to the real system installation
go through Flatpak's root helper, which ignores test environment variables.
"""

import os
import subprocess
from pathlib import Path

import pytest

import builders
from cygnus.core.errors import CygnusError
from cygnus.core.ops import flatpak_ops
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation
from cygnus.core.storage import locations
from cygnus.gui import service

pytestmark = pytest.mark.needs_tool("ostree", "flatpak", "gpg")
RUNTIME = "org.cygnus.TestPlatform/x86_64/1"
APP = "org.cygnus.RemoteApp"
CAPS = {"exec_allowed": True, "chmod_persists": True, "symlinks": True, "hardlinks": True, "atomic_rename": True,
        "ostree_bare_user_only": True}


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("refrepo")
    with builders.GpgTestKey(root) as key:
        yield builders.build_flatpak_repo(root, {RUNTIME: None}, key, apps={APP: RUNTIME}), key


@pytest.fixture
def hdd(tmp_path, monkeypatch):
    path = tmp_path / "hdd"
    path.mkdir()
    reg = open_registry()
    reg.add_location(StorageLocation(id="hdd", label="HDD", fs_uuid="X", fs_type="ext4", location_class="user-owned",
                                     canonical_mount=str(path), capabilities=CAPS))
    reg.close()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(loc, True, str(path)))
    monkeypatch.setattr(flatpak_ops, "filesystem_uuid", lambda path: "X")  # the fake drive
    return path


@pytest.fixture
def fake_system(tmp_path, monkeypatch):
    root = tmp_path / "fake-system"
    root.mkdir()
    real = flatpak_ops.installation_for

    def installation_for(kind, path=None):
        return real("user", root) if kind == "system" else real(kind, path)

    monkeypatch.setattr(flatpak_ops, "installation_for", installation_for)
    return root


def _refs(inst):
    return sorted(r.format_ref() for r in inst.list_installed_refs(None))


def test_install_by_id_onto_hdd(repo, hdd):
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", *repo)
    plan = service.analyse_flatpak_ref(f"vendorrepo:{APP}", "HDD")
    assert plan["installable"] and plan["ref"] == f"app/{APP}/x86_64/stable"
    assert any(i["code"] == "FP_RUNTIME_MISSING" for i in plan["issues"])
    lines = []
    result = service.install_flatpak_ref(f"vendorrepo:{APP}", "HDD", lines.append)
    assert result["ok"], (result, lines)
    assert result["steps"] == ["Install the org.cygnus.TestPlatform 1 runtime", f"Install {APP}"]
    store = hdd / ".cygnus-flatpak-user"
    assert (store / f"app/{APP}").is_dir() and (store / "runtime/org.cygnus.TestPlatform").is_dir()
    [row] = regdb.list_installations(open_registry())
    assert row["source"] == {"ref": f"app/{APP}/x86_64/stable", "installation": "user", "remote": "vendorrepo"}
    assert row["update_provider"]["type"] == "flatpak-remote"


def test_install_copies_a_system_wide_repository_with_its_key(repo, hdd):
    system_dir = Path(os.environ["FLATPAK_SYSTEM_DIR"])
    builders.add_ostree_remote(system_dir, "sysrepo", *repo)
    try:
        result = service.install_flatpak_ref(f"sysrepo:{APP}", "HDD", lambda _: None)
        assert result["ok"], result
        assert result["steps"][0] == "Add sysrepo to your personal Flatpak installation"
        remote = flatpak_ops.installation_for("user").get_remote_by_name("sysrepo", None)
        assert remote.get_gpg_verify()
    finally:
        subprocess.run(["ostree", "remote", "delete", f"--repo={system_dir / 'repo'}", "sysrepo"], check=True)


def test_unknown_app_and_bad_ids_are_refused(repo, hdd):
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", *repo)
    with pytest.raises(CygnusError, match="does not offer org.cygnus.Nope"):
        service.install_flatpak_ref("vendorrepo:org.cygnus.Nope", "HDD", lambda _: None)
    with pytest.raises(CygnusError, match="no Flatpak repository named"):
        service.install_flatpak_ref("nosuchremote:org.cygnus.Nope", "HDD", lambda _: None)
    for bad in ["org", "flathub:../../etc", "flathub:org.x.Y//$(id)", "a b:org.x.Y"]:
        with pytest.raises(CygnusError, match="not a Flatpak application id"):
            service.parse_flatpak_spec(bad)
    assert service.parse_flatpak_spec("org.x.Y") == ("flathub", "org.x.Y", None)
    assert service.parse_flatpak_spec("fh:org.x.Y//25.08") == ("fh", "org.x.Y", "25.08")


def test_move_from_hdd_to_system_and_back(repo, hdd, fake_system):
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", *repo)
    assert service.install_flatpak_ref(f"vendorrepo:{APP}", "HDD", lambda _: None)["ok"]
    [row] = service.list_apps()
    lines = []
    result = service.move_flatpak(row["installation_id"], "SSD", lines.append)
    assert result["ok"], (result, lines)
    system = flatpak_ops.installation_for("system")
    assert _refs(system) == [f"app/{APP}/x86_64/stable", f"runtime/{RUNTIME}"]
    assert _refs(flatpak_ops.installation_for("user")) == []  # old copy and the runtime Cygnus added are gone
    [moved] = regdb.list_installations(open_registry())
    assert moved["source"]["installation"] == "system"

    result = service.move_flatpak(row["installation_id"], "HDD", lines.append)
    assert result["ok"], result
    assert _refs(flatpak_ops.installation_for("user")) == [f"app/{APP}/x86_64/stable", f"runtime/{RUNTIME}"]
    assert f"app/{APP}/x86_64/stable" not in _refs(flatpak_ops.installation_for("system"))
    with pytest.raises(CygnusError, match="already lives in that installation"):
        service.move_flatpak(row["installation_id"], "HDD", lines.append)


def test_cli_install_and_move(repo, hdd, fake_system, capsys):
    from cygnus.cli.main import main

    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", *repo)
    assert main(["install", "--yes", f"vendorrepo:{APP}", "--to", "HDD"]) == 0
    out = capsys.readouterr().out
    assert "org.cygnus.TestPlatform 1, installed next to the application" in out and out.rstrip().endswith("done")
    assert main(["move", "--yes", APP, "--to", "SSD"]) == 0
    assert f"app/{APP}/x86_64/stable" in _refs(flatpak_ops.installation_for("system"))


def test_uninstall_also_removes_extensions_that_came_with_the_runtime(tmp_path, hdd):
    with builders.GpgTestKey(tmp_path / "k") as key:
        ext = "org.cygnus.TestPlatform.Ext"
        repo_path = builders.build_flatpak_repo(tmp_path / "r", {RUNTIME: None, f"{ext}/x86_64/1": None}, key,
                                                apps={APP: RUNTIME}, extensions={RUNTIME: ext})
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", repo_path, key)
    assert service.install_flatpak_ref(f"vendorrepo:{APP}", "HDD", lambda _: None)["ok"]
    user = flatpak_ops.installation_for("user")
    assert f"runtime/{ext}/x86_64/1" in _refs(user)  # came along as a related ref
    [row] = service.list_apps()
    assert service.uninstall(row["installation_id"], False, lambda _: None)["ok"]
    assert _refs(flatpak_ops.installation_for("user")) == []


@pytest.fixture
def other_filesystem(tmp_path, monkeypatch):
    """An "HDD" on a different filesystem than the user installation (/dev/shm vs /tmp), so a
    rename between them fails exactly like SSD↔HDD does."""
    import shutil
    import uuid

    if os.stat("/dev/shm").st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("needs /dev/shm on a separate filesystem")
    path = Path("/dev/shm") / f"cygnus-test-{uuid.uuid4().hex}"
    path.mkdir()
    reg = open_registry()
    reg.add_location(StorageLocation(id="hdd", label="HDD", fs_uuid="X", fs_type="tmpfs", location_class="user-owned",
                                     canonical_mount=str(path), capabilities=CAPS))
    reg.close()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(loc, True, str(path)))
    monkeypatch.setattr(flatpak_ops, "filesystem_uuid", lambda path: "X")  # the fake drive
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_uninstall_and_update_work_across_filesystems(repo, other_filesystem):
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", *repo)
    assert service.install_flatpak_ref(f"vendorrepo:{APP}", "HDD", lambda _: None)["ok"]
    assert (flatpak_ops.user_dir() / ".removed").is_symlink()
    [row] = service.list_apps()
    result = service.uninstall(row["installation_id"], False, lambda _: None)
    assert result["ok"], result
    assert _refs(flatpak_ops.installation_for("user")) == []


def test_links_removed_by_flatpak_repair_are_restored(repo, other_filesystem):
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", *repo)
    assert service.install_flatpak_ref(f"vendorrepo:{APP}", "HDD", lambda _: None)["ok"]
    removed = flatpak_ops.user_dir() / ".removed"
    removed.unlink()  # what `flatpak repair` does ("Erasing .removed") …
    removed.mkdir()  # … and Flatpak recreates it on the system drive
    [row] = service.list_apps()
    assert service.uninstall(row["installation_id"], False, lambda _: None)["ok"]  # no cross-device failure
    assert removed.is_symlink()


def test_links_are_not_recreated_while_the_drive_is_unplugged(tmp_path):
    base, store = tmp_path / "user", tmp_path / "hdd/.cygnus-flatpak-user"
    for name in ("repo", "app", "runtime"):
        (store / name).mkdir(parents=True)
        base.mkdir(exist_ok=True)
        (base / name).symlink_to(store / name)
    import shutil

    shutil.rmtree(tmp_path / "hdd")  # unplugged: the mount point is empty
    assert flatpak_ops.ensure_relocation_links(base) == []
    assert not (tmp_path / "hdd").exists()


def test_failed_runtime_cleanup_does_not_undo_an_uninstall(repo, hdd, monkeypatch):
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", *repo)
    assert service.install_flatpak_ref(f"vendorrepo:{APP}", "HDD", lambda _: None)["ok"]
    [row] = service.list_apps()
    real = flatpak_ops.run_transaction

    def cancelled(inst, **kw):
        if kw.get("uninstalls") and kw["uninstalls"][0].startswith("runtime/"):
            return flatpak_ops.TransactionResult(ok=False, error="Authorization cancelled")
        return real(inst, **kw)

    monkeypatch.setattr(flatpak_ops, "run_transaction", cancelled)
    assert service.uninstall(row["installation_id"], False, lambda _: None)["ok"]
    assert service.list_apps() == []  # the app is gone and stays forgotten
    assert _refs(flatpak_ops.installation_for("user")) == [f"runtime/{RUNTIME}"]  # left, not lost


def test_user_installation_location_is_matched_by_path_not_prefix(tmp_path, monkeypatch):
    from cygnus.core.registry.db import StorageLocation

    reg = open_registry()
    for lid, path in (("data", tmp_path / "data"), ("data2", tmp_path / "data2")):
        path.mkdir()
        reg.add_location(StorageLocation(id=lid, label=lid, fs_uuid=lid, fs_type="ext4", location_class="user-owned",
                                         canonical_mount=str(path), capabilities=CAPS))
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(
        loc, True, loc.canonical_mount))
    monkeypatch.setattr(flatpak_ops, "relocation_state", lambda base=None: type(
        "S", (), {"relocated_to": tmp_path / "data2/.cygnus-flatpak-user"})())
    assert service._user_flatpak_location(reg) == "data2"


def test_repair_puts_back_a_vanished_app_and_storage_link(repo, hdd):
    builders.add_ostree_remote(flatpak_ops.user_dir(), "vendorrepo", *repo)
    assert service.install_flatpak_ref(f"vendorrepo:{APP}", "HDD", lambda _: None)["ok"]
    [row] = service.list_apps()
    assert service.diagnose_app(row["installation_id"]) == []
    ref = f"app/{APP}/x86_64/stable"
    assert flatpak_ops.run_transaction(flatpak_ops.installation_for("user"), uninstalls=[ref]).ok  # behind our back
    (flatpak_ops.user_dir() / ".removed").unlink()  # e.g. `flatpak repair`
    problems = {(p["what"], p["state"]) for p in service.diagnose_app(row["installation_id"])}
    assert problems == {("storage link", "missing"), ("application", "missing")}
    lines = []
    result = service.repair_app(row["installation_id"], lines.append)
    assert result["ok"], (result, lines)
    assert f"Install {APP}…" in lines and "Check every file of your Flatpak installation…" in lines
    assert ref in _refs(flatpak_ops.installation_for("user"))
    assert service.diagnose_app(row["installation_id"]) == []
