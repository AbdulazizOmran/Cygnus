"""Update checks: AppImage zsync/GitHub providers (fake network) and a real local Flatpak remote."""

import hashlib
import subprocess

import pytest

import builders
from cygnus.core import updates
from cygnus.core.ops import flatpak_ops
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.util.http import HttpError
from cygnus.gui import service

appimage_tools = pytest.mark.needs_tool("mksquashfs", "unsquashfs")


def _adopt(app):
    assert service.adopt_appimage(str(app), lambda _: None)["ok"]
    return open_registry()


def _zsync_for(path, *, same=True):
    data = path.read_bytes()
    sha1 = hashlib.sha1(data if same else data + b"x").hexdigest()
    return f"zsync: 0.6.2\nFilename: Hello.AppImage\nMTime: today\nLength: {len(data) + (0 if same else 1)}\n" \
           f"SHA-1: {sha1}\n\n".encode()


@appimage_tools
def test_zsync_up_to_date_then_newer_build(tmp_path):
    app = builders.build_appimage(tmp_path)
    reg = _adopt(app)
    [st] = updates.check_all(reg, fetch=lambda url, **kw: _zsync_for(app))
    assert st.status == updates.UP_TO_DATE and st.provider == "zsync"
    [st] = updates.check_all(reg, fetch=lambda url, **kw: _zsync_for(app, same=False))
    assert st.status == updates.AVAILABLE
    assert st.facts["download_url"] == "https://example.invalid/Hello.AppImage"
    assert len(st.facts["expected_sha1"]) == 40
    [known] = updates.last_known(reg)  # stored, no network needed
    assert known.status == updates.AVAILABLE and known.checked_at


@appimage_tools
def test_network_failure_is_unknown_not_up_to_date(tmp_path):
    app = builders.build_appimage(tmp_path)
    reg = _adopt(app)

    def offline(url, **kw):
        raise HttpError("network error for https://example.invalid: no route")

    [st] = updates.check_all(reg, fetch=offline)
    assert st.status == updates.UNKNOWN and "no route" in st.detail
    assert service.update_overview()[0]["status"] == updates.UNKNOWN


@appimage_tools
def test_missing_drive_is_reported_as_offline(tmp_path):
    app = builders.build_appimage(tmp_path)
    reg = _adopt(app)
    app.unlink()
    [st] = updates.check_all(reg, fetch=lambda url, **kw: pytest.fail("must not go online"))
    assert st.status == updates.OFFLINE


@appimage_tools
def test_github_release_provider(tmp_path):
    app = builders.build_appimage(tmp_path, update_info="gh-releases-zsync|vendor|hello|latest|Hello-*-x86_64.AppImage.zsync")
    reg = _adopt(app)
    release = {"tag_name": "v9.0.0", "published_at": "2026-10-01T00:00:00Z", "assets": [
        {"name": "Hello-9.0.0-x86_64.AppImage", "browser_download_url": "https://example.invalid/Hello-9.AppImage",
         "digest": "sha256:" + "ab" * 32}]}
    [st] = updates.check_all(reg, fetch_json=lambda url, **kw: release)
    assert st.status == updates.AVAILABLE and st.available == "9.0.0"
    assert st.facts["expected_sha256"] == "ab" * 32


def test_version_change_invalidates_the_stored_result(tmp_path):
    reg = open_registry()
    regdb.add_application(reg, app_id="x", display_name="X", trust_level="unverified")
    iid = regdb.add_installation(reg, app_id="x", fmt="appimage", source={"path": str(tmp_path / "x")},
                                 version="1.0", location_id=None, origin="adopted", update_provider={"type": "none"})
    [st] = updates.check_all(reg)
    assert st.status == updates.MANUAL
    reg.conn.execute("UPDATE installation SET version='2.0' WHERE id=?", (iid,))
    [known] = updates.last_known(reg)
    assert known.status == updates.UNKNOWN and known.detail == "Not checked yet."


def test_system_packages_are_not_checked_individually(tmp_path):
    reg = open_registry()
    regdb.add_application(reg, app_id="p", display_name="P", trust_level="unverified")
    regdb.add_installation(reg, app_id="p", fmt="pacman", source={"name": "p"}, version="1-1", location_id=None,
                           origin="installed", update_provider={"type": "pacman"})
    [st] = updates.check_all(reg)
    assert st.status == updates.SYSTEM
    assert reg.conn.execute("SELECT COUNT(*) FROM update_check").fetchone()[0] == 0


@pytest.mark.needs_tool("ostree", "flatpak")
def test_flatpak_remote_gains_a_newer_build(tmp_path):
    from test_flatpak_isolated import _commit, make_installation

    repo = builders.build_flatpak_repo(tmp_path / "r", {"org.cygnus.TestPlatform/x86_64/1": None})
    meta = ("[Application]\nname=org.cygnus.TestApp\nruntime=org.cygnus.TestPlatform/x86_64/1\n"
            "command=testapp\n")
    app = tmp_path / "app"
    (app / "files/bin").mkdir(parents=True)
    (app / "metadata").write_text(meta)
    (app / "files/bin/testapp").write_text("#!/bin/sh\necho 1\n")
    _commit(repo, "app/org.cygnus.TestApp/x86_64/stable", app, meta)
    env = {"FLATPAK_USER_DIR": str(tmp_path / "unused"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    subprocess.run(["flatpak", "build-update-repo", str(repo)], check=True, capture_output=True, env=env)
    ref = "app/org.cygnus.TestApp/x86_64/stable"
    inst = make_installation(flatpak_ops.user_dir(), repo)
    assert flatpak_ops.run_transaction(inst, installs=[("cygnus-test", ref)]).ok

    reg = open_registry()
    regdb.add_application(reg, app_id="org.cygnus.TestApp", display_name="Test App", trust_level="unverified")
    regdb.add_installation(reg, app_id="org.cygnus.TestApp", fmt="flatpak", source={"ref": ref, "installation": "user"},
                           version="1", location_id=None, origin="installed",
                           update_provider={"type": "flatpak-remote"})
    [st] = updates.check_all(reg)
    assert st.status == updates.UP_TO_DATE, st.detail

    (app / "files/bin/testapp").write_text("#!/bin/sh\necho 2\n")
    _commit(repo, ref, app, meta)
    subprocess.run(["flatpak", "build-update-repo", str(repo)], check=True, capture_output=True, env=env)
    [st] = updates.check_all(reg)
    assert st.status == updates.AVAILABLE and st.facts["origin"] == "cygnus-test"

    lines = []
    [row] = service.list_apps()
    result = service.apply_update(row["installation_id"], lines.append)
    assert result["ok"], (result, lines)
    deployed = flatpak_ops.installation_for("user").get_installed_ref(0, "org.cygnus.TestApp", "x86_64", "stable",
                                                                      None)
    assert (deployed.get_deploy_dir() + "/files/bin/testapp").endswith("testapp")
    assert open(deployed.get_deploy_dir() + "/files/bin/testapp").read() == "#!/bin/sh\necho 2\n"
    [known] = updates.last_known(open_registry())
    assert known.status == updates.UP_TO_DATE


@appimage_tools
def test_cli_updates_shows_last_check_without_network(tmp_path, capsys):
    from cygnus.cli.main import main

    app = builders.build_appimage(tmp_path)
    reg = _adopt(app)
    updates.check_all(reg, fetch=lambda url, **kw: _zsync_for(app, same=False))
    assert main(["updates"]) == 1
    assert "update-available" in capsys.readouterr().out


def test_system_updates_are_checked_without_root_and_cached(monkeypatch, tmp_path):
    from cygnus.core.backends import pacman as pm
    from cygnus.core.util import proc

    calls = []
    monkeypatch.setattr(proc, "which", lambda name: "/usr/bin/fakeroot" if name == "fakeroot" else None)
    monkeypatch.setattr(pm, "read_config", lambda: pm.PacmanConfig(arch=("x86_64",), dbpath=str(tmp_path / "db"),
                                                                    cachedirs=(), gpgdir="", repos=(), servers={},
                                                                    hold=()))
    monkeypatch.setattr(proc, "run", lambda argv, **kw: calls.append(argv) or proc.Result(argv, 0, "", "", False, b""))
    monkeypatch.setattr(pm, "run_worker", lambda cfg, req: {"outdated": [
        {"name": "linux-cachyos", "installed": "7.2.8-1", "available": "7.2.9-1", "repo": "cachyos"},
        {"name": "7zip", "installed": "26.03-1", "available": "26.04-1", "repo": "extra"}]})
    result = updates.check_system_updates()
    assert calls[0][:4] == ["/usr/bin/fakeroot", "--", "pacman", "-Sy"]  # never root, never /var/lib/pacman
    assert "--disable-sandbox-filesystem" in calls[0] and "/var/lib/pacman" not in " ".join(calls[0])
    assert [p["name"] for p in result["packages"]] == ["7zip", "linux-cachyos"] and result["kernel"]
    assert updates.last_system_updates()["packages"] == result["packages"]


def test_system_upgrade_plan_carries_the_details_to_show(monkeypatch):
    from cygnus.core.privilege import Plan
    from cygnus.gui import fixes

    class FakeClient:
        def plan_packages(self, **kw):
            assert kw == {"sysupgrade": True}
            return Plan("p1", {"upgrade": [{"name": "7zip", "from": "26.03-1", "version": "26.04-1"}],
                               "install": [], "remove": [{"name": "old", "reason": "replaced by new"}],
                               "download_bytes": 5 << 20}, "Upgrade the whole system (1 packages). Remove old.")

    monkeypatch.setattr(fixes, "HelperClient", FakeClient)
    plan = fixes.plan_system_upgrade()
    assert plan["upgrades"] == [{"name": "7zip", "from": "26.03-1", "to": "26.04-1"}]
    assert plan["removals"] == [{"name": "old", "reason": "replaced by new"}] and plan["download_bytes"] == 5 << 20
    assert plan["token"] in fixes._PENDING


@appimage_tools
def test_checking_updates_reports_which_application_of_how_many(tmp_path):
    app = builders.build_appimage(tmp_path)
    reg = _adopt(app)
    seen = []
    updates.check_all(reg, seen.append, fetch=lambda url, **kw: _zsync_for(app))
    assert [(str(s), s.fraction) for s in seen] == [("Checking Hello World…", 0.0)]
