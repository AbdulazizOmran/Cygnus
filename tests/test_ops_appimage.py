import os
from pathlib import Path

import pytest

import builders
from cygnus.core.desktop import integrate
from cygnus.core.detect import detect_file
from cygnus.core.ops import appimage_ops as ops
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation

pytestmark = pytest.mark.needs_tool("mksquashfs", "unsquashfs")
HDD = StorageLocation(id="hdd", label="HDD", fs_uuid="ABCD", fs_type="ntfs3", location_class="user-owned")


@pytest.fixture
def env(tmp_path):
    reg = open_registry(":memory:")
    reg.add_location(StorageLocation(id="hdd", label="HDD", fs_uuid="ABCD", fs_type="ntfs3",
                                     location_class="user-owned", canonical_mount=str(tmp_path)))
    src_dir = tmp_path / "downloads"
    src_dir.mkdir()
    cand = detect_file(builders.build_appimage(src_dir))
    return reg, ops.Ops(reg), cand, tmp_path


def files_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_adopt_then_uninstall_keeps_payload(env):
    reg, o, cand, tmp = env
    key = ops.app_key_for(cand)
    rep = o.executor().run("adopt", ops.plan_adopt(cand, app_key=key, location=HDD))
    assert rep.state == "succeeded"
    [inst] = regdb.list_installations(reg)
    assert inst["origin"] == "adopted" and inst["name"] == "Hello World"
    entry = integrate.xdg_data_home() / "applications/hello.desktop"
    assert entry.exists() and "launch/org.example.Hello" in entry.read_text()
    shim = integrate.shim_dir() / key
    assert os.access(shim, os.X_OK) and "DRIVE_UUID='ABCD'" in shim.read_text()
    assert (integrate.xdg_data_home() / "icons/hicolor/1x1/apps/hello.png").exists()

    rep = o.executor().run("uninstall", ops.plan_uninstall(reg, inst["id"], remove_payload=False))
    assert rep.state == "succeeded"
    assert not entry.exists() and not shim.exists()
    assert Path(cand.source).exists()  # adopted payload kept
    assert regdb.list_installations(reg) == []


def test_install_copies_to_location_and_uninstall_deletes_last(env):
    reg, o, cand, tmp = env
    apps = tmp / "Applications"
    rep = o.executor().run("install", ops.plan_install(cand, app_key=ops.app_key_for(cand), location=HDD,
                                                         apps_dir=apps))
    assert rep.state == "succeeded"
    [inst] = regdb.list_installations(reg)
    dest = Path(inst["source"]["path"])
    assert dest.parent.parent == apps and dest.exists() and os.access(dest, os.X_OK)
    steps = ops.plan_uninstall(reg, inst["id"], remove_payload=True)
    assert steps[-1].kind == "appimage.remove"  # irreversible step last
    assert o.executor().run("uninstall", steps).state == "succeeded"
    assert not dest.exists() and Path(cand.source).exists()


def test_failed_install_rolls_everything_back(env):
    reg, o, cand, tmp = env
    apps = tmp / "Applications"
    ex = o.executor()
    original = ex.handlers["registry.record"]

    def boom(params):
        raise RuntimeError("disk full")

    ex.handlers["registry.record"] = boom
    before = files_under(integrate.xdg_data_home().parent)
    rep = ex.run("install", ops.plan_install(cand, app_key=ops.app_key_for(cand), location=HDD, apps_dir=apps))
    assert rep.state == "rolled_back" and not rep.compensation_errors
    after = {f for f in files_under(integrate.xdg_data_home().parent) if not f.endswith(".cache")}
    assert after == before
    cache = integrate.xdg_data_home() / "applications/mimeinfo.cache"
    assert not cache.exists() or "hello.desktop" not in cache.read_text()
    assert not any(apps.rglob("*.AppImage"))
    ex.handlers["registry.record"] = original


def test_uninstall_refuses_changed_payload_and_restores(env):
    reg, o, cand, tmp = env
    apps = tmp / "Applications"
    o.executor().run("install", ops.plan_install(cand, app_key=ops.app_key_for(cand), location=HDD, apps_dir=apps))
    [inst] = regdb.list_installations(reg)
    Path(inst["source"]["path"]).write_bytes(b"replaced by someone else")
    rep = o.executor().run("uninstall", ops.plan_uninstall(reg, inst["id"], remove_payload=True))
    assert rep.state == "rolled_back"
    assert regdb.list_installations(reg)  # registry restored
    assert (integrate.xdg_data_home() / "applications/hello.desktop").exists()  # integration restored


def test_adopted_autostart_is_kept_unless_asked(env, tmp_path):
    reg, o, cand, tmp = env
    auto_dir = integrate.xdg_config_home() / "autostart"
    auto_dir.mkdir(parents=True)
    auto = auto_dir / "hello.desktop"
    auto.write_text(f"[Desktop Entry]\nType=Application\nExec={cand.source} --minimized\n")
    o.executor().run("adopt", ops.plan_adopt(cand, app_key=ops.app_key_for(cand), location=HDD, autostart=[auto]))
    assert "launch/" in auto.read_text()
    [inst] = regdb.list_installations(reg)
    o.executor().run("uninstall", ops.plan_uninstall(reg, inst["id"], remove_payload=False))
    assert auto.exists()  # adopted: left in place
