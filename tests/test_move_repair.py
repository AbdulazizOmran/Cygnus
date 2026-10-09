"""Moving an AppImage between locations, and repairing what Cygnus set up."""

import shutil
from pathlib import Path

import pytest

import builders
from cygnus.core.desktop import integrate
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation
from cygnus.core.storage import locations
from cygnus.gui import service

pytestmark = pytest.mark.needs_tool("mksquashfs", "unsquashfs")
CAPS = {"exec_allowed": True, "chmod_persists": True, "symlinks": True, "hardlinks": True, "atomic_rename": True}


@pytest.fixture
def adopted(tmp_path, monkeypatch):
    hdd = tmp_path / "hdd"
    hdd.mkdir()
    reg = open_registry()
    reg.add_location(StorageLocation(id="hdd", label="HDD", fs_uuid="ABCD-1234", fs_type="ext4",
                                     location_class="user-owned", canonical_mount=str(hdd), capabilities=CAPS))
    reg.close()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(loc, True, str(hdd)))
    (tmp_path / "build").mkdir()
    home_apps = tmp_path / "home-apps"
    home_apps.mkdir()
    app = home_apps / "Hello-x86_64.AppImage"
    shutil.copy2(builders.build_appimage(tmp_path / "build"), app)
    assert service.adopt_appimage(str(app), lambda _: None)["ok"]
    [row] = service.list_apps()
    return {"app": app, "hdd": hdd, "iid": row["installation_id"]}


def _shim():
    return next((integrate.shim_dir()).iterdir())


def test_move_to_another_drive(adopted):
    lines = []
    result = service.move_app(adopted["iid"], "HDD", lines.append)
    assert result["ok"], (result, lines)
    dest = adopted["hdd"] / "Applications/Hello World/Hello-x86_64.AppImage"
    assert dest.is_file() and not adopted["app"].exists()
    shim = _shim().read_text()
    assert f"TARGET='{dest}'" in shim and "DRIVE_UUID='ABCD-1234'" in shim  # launcher follows the move
    [row] = regdb.list_installations(open_registry())
    assert row["source"]["path"] == str(dest) and row["location_id"] == "hdd"
    assert service.diagnose_app(adopted["iid"]) == []


def test_failed_move_leaves_the_original(adopted, monkeypatch):
    from cygnus.core.ops import appimage_ops

    def broken(registry, params):
        raise OSError("disk full")

    monkeypatch.setattr(appimage_ops, "_update_record", broken)
    shim_before = _shim().read_bytes()
    result = service.move_app(adopted["iid"], "HDD", lambda _: None)
    assert not result["ok"] and result["state"] == "rolled_back"
    assert adopted["app"].is_file()
    assert not any((adopted["hdd"]).rglob("*.AppImage"))
    assert _shim().read_bytes() == shim_before


def test_move_refuses_unknown_or_unsuitable_location(adopted):
    reg = open_registry()
    reg.add_location(StorageLocation(id="usb", label="USB", fs_uuid="F", fs_type="vfat", location_class="user-owned",
                                     canonical_mount="/nonexistent", capabilities={**CAPS, "chmod_persists": False}))
    reg.close()
    with pytest.raises(Exception, match="does not keep file permissions"):
        service.move_app(adopted["iid"], "USB", lambda _: None)
    with pytest.raises(Exception, match="no storage location"):
        service.move_app(adopted["iid"], "Nowhere", lambda _: None)


def test_repair_rewrites_missing_and_changed_files(adopted):
    desktop = integrate.xdg_data_home() / "applications/hello.desktop"
    good = desktop.read_bytes()
    desktop.unlink()
    _shim().write_text("#!/bin/sh\nexec /tmp/evil\n")
    problems = service.diagnose_app(adopted["iid"])
    assert {(p["what"], p["state"]) for p in problems} == {("desktop_entry", "missing"), ("launcher_shim", "changed")}
    result = service.repair_app(adopted["iid"], lambda _: None)
    assert result["ok"] and len(result["repaired"]) == 2
    assert desktop.read_bytes() == good
    assert "/tmp/evil" not in _shim().read_text()
    assert service.diagnose_app(adopted["iid"]) == []
    assert service.repair_app(adopted["iid"], lambda _: None) == {"ok": True, "repaired": []}


def test_repair_accepts_a_self_updated_payload_of_the_same_app(adopted, tmp_path):
    (tmp_path / "v2").mkdir()
    shutil.copyfile(builders.build_appimage(tmp_path / "v2", version="2.7.0"), adopted["app"])
    assert service.diagnose_app(adopted["iid"]) == [
        {"what": "payload", "state": "changed", "path": str(adopted["app"])}]
    assert service.repair_app(adopted["iid"], lambda _: None)["ok"]
    [row] = regdb.list_installations(open_registry())
    assert row["version"] == "2.7.0"
    assert service.diagnose_app(adopted["iid"]) == []


def test_repair_refuses_a_payload_that_became_another_app(adopted, tmp_path):
    (tmp_path / "other").mkdir()
    shutil.copyfile(builders.build_appimage(tmp_path / "other", app_id="org.attacker.Other"), adopted["app"])
    with pytest.raises(Exception, match="different application"):
        service.repair_app(adopted["iid"], lambda _: None)


def test_repair_cannot_conjure_a_missing_payload(adopted):
    adopted["app"].unlink()
    with pytest.raises(Exception, match="is missing"):
        service.repair_app(adopted["iid"], lambda _: None)


def test_cli_move_and_repair(adopted, capsys):
    from cygnus.cli.main import main

    assert main(["repair", "--yes", "Hello World"]) == 0
    assert "nothing to repair" in capsys.readouterr().out
    (integrate.xdg_data_home() / "applications/hello.desktop").unlink()
    assert main(["repair", "--yes", "Hello World"]) == 0
    assert (integrate.xdg_data_home() / "applications/hello.desktop").exists()
    assert main(["move", "--yes", "Hello World", "--to", "HDD"]) == 0
    assert (adopted["hdd"] / "Applications/Hello World/Hello-x86_64.AppImage").is_file()


def _pinned_manifest(app_id="org.example.Hello"):
    from types import SimpleNamespace as NS

    app = NS(id=app_id, appstream_ids=[app_id], flatpak_ids=[], package_names=[], desktop_ids=[], name="Hello World",
             vendor=NS(name="Example Vendor"))
    return NS(trust_level="curated", can_drive_actions=True, authentic=True,
              manifest=NS(application=app, sources=[NS(format="appimage", verification=NS(openpgp_fingerprint="F" * 40))]))


def test_repair_requires_the_vendor_signature_for_a_changed_payload(adopted, tmp_path, monkeypatch):
    from cygnus.core.backends import appimage as ab
    from cygnus.core.manifest import catalog

    [row] = regdb.list_installations(open_registry())
    monkeypatch.setattr(catalog, "bundled_manifests", lambda: {row["app_id"]: _pinned_manifest(row["app_id"])})
    (tmp_path / "v2").mkdir()
    shutil.copyfile(builders.build_appimage(tmp_path / "v2", version="2.7.0"), adopted["app"])
    monkeypatch.setattr(ab, "verify_signature", lambda p, f: ab.SignatureResult(status="unsigned", detail=""))
    with pytest.raises(Exception, match="not signed by Example Vendor"):
        service.repair_app(adopted["iid"], lambda _: None)
    [row] = regdb.list_installations(open_registry())
    assert row["version"] != "2.7.0"  # nothing was accepted
    monkeypatch.setattr(ab, "verify_signature", lambda p, f: ab.SignatureResult(status="verified", detail=""))
    assert service.repair_app(adopted["iid"], lambda _: None)["ok"]


def test_repair_judges_identity_from_the_file_not_the_record(adopted, tmp_path, monkeypatch):
    from cygnus.core.manifest import catalog

    [row] = regdb.list_installations(open_registry())
    manifest = _pinned_manifest(row["app_id"])
    manifest.manifest.sources = []  # nothing pinned: only the identity check stands between
    monkeypatch.setattr(catalog, "bundled_manifests", lambda: {row["app_id"]: manifest})
    (tmp_path / "other").mkdir()
    shutil.copyfile(builders.build_appimage(tmp_path / "other", app_id="org.attacker.Other"), adopted["app"])
    with pytest.raises(Exception, match="different application"):
        service.repair_app(adopted["iid"], lambda _: None)


def test_installing_a_vendor_signed_app_requires_its_signature(tmp_path, monkeypatch):
    from cygnus.core.backends import appimage as ab
    from cygnus.core.detect import detect_file
    from cygnus.core.ops import appimage_ops

    (tmp_path / "b").mkdir()
    cand = detect_file(str(builders.build_appimage(tmp_path / "b")))
    loc = StorageLocation(id="hdd", label="HDD", fs_uuid="U", fs_type="ext4", location_class="user-owned",
                          canonical_mount=str(tmp_path), capabilities=CAPS)
    monkeypatch.setattr(ab, "verify_signature", lambda p, f: ab.SignatureResult(status="wrong-key", detail=""))
    with pytest.raises(Exception, match="not signed by Example Vendor"):
        appimage_ops.plan_install(cand, app_key="org.example.Hello", location=loc, apps_dir=tmp_path / "apps",
                                  manifest=_pinned_manifest())
    assert appimage_ops.plan_install(cand, app_key="org.example.Hello", location=loc, apps_dir=tmp_path / "apps",
                                     manifest=None)  # nothing pinned: nothing to require


def test_an_icon_that_is_not_cygnuss_is_used_not_overwritten(tmp_path):
    from cygnus.core.detect import detect_file
    from cygnus.core.ops import appimage_ops

    (tmp_path / "i").mkdir()
    cand = detect_file(str(builders.build_appimage(tmp_path / "i")))
    steps, artifacts, _ = appimage_ops.integration_steps(cand, app_key="org.example.Hello", payload_path="/x",
                                                         location=None)
    [icon] = [a["locator"] for a in artifacts if a["kind"] == "icon"]
    Path(icon).parent.mkdir(parents=True, exist_ok=True)
    Path(icon).write_bytes(b"the user's own icon")
    steps, artifacts, _ = appimage_ops.integration_steps(cand, app_key="org.example.Hello", payload_path="/x",
                                                         location=None)
    assert not any(a["kind"] == "icon" for a in artifacts) and Path(icon).read_bytes() == b"the user's own icon"
    steps, artifacts, _ = appimage_ops.integration_steps(cand, app_key="org.example.Hello", payload_path="/x",
                                                         location=None, owned=frozenset({icon}))
    assert any(a["kind"] == "icon" for a in artifacts)  # Cygnus's own icon is rewritten (updates, repairs)


def test_an_update_keeps_its_own_icon(adopted, tmp_path):
    from cygnus.core.detect import detect_file
    from cygnus.core.ops import appimage_ops

    [before] = [a for a in regdb.artifacts_of(open_registry(), adopted["iid"]) if a["kind"] == "icon"]
    (tmp_path / "v2").mkdir()
    new = builders.build_appimage(tmp_path / "v2", version="2.7.0")
    steps = appimage_ops.plan_update(open_registry(), adopted["iid"], new, detect_file(str(new)), location=None)
    assert not any(s.kind == "fs.remove_owned" and s.params["path"] == before["locator"] for s in steps)


def test_a_kept_autostart_entry_runs_the_application_after_uninstall(tmp_path, monkeypatch):
    from cygnus.core.desktop import entry

    home_apps = tmp_path / "apps"
    home_apps.mkdir()
    (tmp_path / "b").mkdir()
    app = home_apps / "Hello-x86_64.AppImage"
    shutil.copy2(builders.build_appimage(tmp_path / "b"), app)
    auto = integrate.xdg_config_home() / "autostart/hello.desktop"
    auto.parent.mkdir(parents=True, exist_ok=True)
    auto.write_text(f"[Desktop Entry]\nType=Application\nName=Hello\nExec={app} --minimized\n")
    assert service.adopt_appimage(str(app), lambda _: None)["ok"]
    assert str(integrate.shim_dir()) in auto.read_text()  # it now runs the launcher
    [row] = service.list_apps()
    assert service.uninstall(row["installation_id"], False, lambda _: None)["ok"]
    de = entry.parse(auto.read_text())
    assert entry.split_exec(de.get("Exec")) == [str(app), "--minimized"] and de.get("X-Cygnus-Managed") is None
