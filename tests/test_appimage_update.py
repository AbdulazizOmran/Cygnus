"""Applying AppImage updates: verified download, same-application check, journaled swap, rollback."""

import hashlib
import shutil
from pathlib import Path

import pytest

import builders
from cygnus.core import updates
from cygnus.core.desktop import integrate
from cygnus.core.ops import appimage_ops
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.gui import service

pytestmark = pytest.mark.needs_tool("mksquashfs", "unsquashfs")


def _zsync(data: bytes) -> bytes:
    return (f"zsync: 0.6.2\nFilename: Hello-x86_64.AppImage\nMTime: today\nLength: {len(data)}\n"
            f"SHA-1: {hashlib.sha1(data).hexdigest()}\n\n").encode()


@pytest.fixture
def installed(tmp_path):
    """Version 2.5.0 adopted in place, plus a vendor 'server' offering `new` (set per test)."""
    apps = tmp_path / "Applications"
    apps.mkdir()
    (tmp_path / "build-old").mkdir()
    old = apps / "Hello-x86_64.AppImage"
    shutil.copy2(builders.build_appimage(tmp_path / "build-old"), old)
    assert service.adopt_appimage(str(old), lambda _: None)["ok"]
    [row] = regdb.list_installations(open_registry())
    server = {"published": None, "served": None}

    def fetch(url, **kw):
        return _zsync(server["published"].read_bytes())

    def download(url, dest_dir, *, expected_sha256=None, progress=None):
        out = Path(dest_dir) / "Hello-x86_64.AppImage"
        shutil.copyfile(server["served"] or server["published"], out)
        return out

    def build(version="2.6.0", app_id="org.example.Hello"):
        d = tmp_path / f"build-{version}-{app_id}"
        d.mkdir()
        return builders.build_appimage(d, version=version, app_id=app_id)

    return {"old": old, "row": row, "server": server, "fetch": fetch, "download": download, "build": build}


def _apply(env, lines=None):
    return service.apply_update(env["row"]["id"], (lines if lines is not None else []).append,
                                download=env["download"], fetch=env["fetch"])


def test_update_replaces_the_file_in_place(installed):
    old_bytes = installed["old"].read_bytes()
    desktop = integrate.xdg_data_home() / "applications/hello.desktop"
    installed["server"]["published"] = new = installed["build"]()
    lines = []
    result = _apply(installed, lines)
    assert result == {"ok": True, "version": "2.6.0"}, lines
    path = installed["old"]
    assert path.read_bytes() == new.read_bytes() != old_bytes
    assert sorted(p.name for p in path.parent.iterdir()) == ["Hello-x86_64.AppImage"]  # no backup, no staging
    [row] = regdb.list_installations(open_registry())
    assert row["version"] == "2.6.0" and row["source"]["sha256"] == appimage_ops.sha256_file(path)
    assert desktop.exists()
    [st] = updates.last_known(open_registry())
    assert st.status == updates.UP_TO_DATE
    assert lines[-1] == "Updated."


def test_a_different_application_is_never_installed(installed):
    before = installed["old"].read_bytes()
    installed["server"]["published"] = installed["build"](app_id="org.attacker.Other")
    with pytest.raises(Exception, match="different application"):
        _apply(installed)
    assert installed["old"].read_bytes() == before
    assert sorted(p.name for p in installed["old"].parent.iterdir()) == ["Hello-x86_64.AppImage"]


def test_download_must_match_the_published_checksum(installed):
    before = installed["old"].read_bytes()
    installed["server"]["published"] = installed["build"]()
    installed["server"]["served"] = installed["build"](version="6.6.6")  # tampered in transit or on the mirror
    with pytest.raises(Exception, match="published checksum"):
        _apply(installed)
    assert installed["old"].read_bytes() == before
    assert sorted(p.name for p in installed["old"].parent.iterdir()) == ["Hello-x86_64.AppImage"]


def test_failure_after_the_swap_puts_the_previous_version_back(installed, monkeypatch):
    before = installed["old"].read_bytes()
    desktop = integrate.xdg_data_home() / "applications/hello.desktop"
    desktop_before = desktop.read_bytes()
    installed["server"]["published"] = installed["build"]()

    def broken(registry, params):
        raise OSError("disk full")

    monkeypatch.setattr(appimage_ops, "_update_record", broken)
    result = _apply(installed)
    assert not result["ok"] and result["state"] == "rolled_back" and "put back" in result["error"]
    assert installed["old"].read_bytes() == before
    assert desktop.read_bytes() == desktop_before
    assert sorted(p.name for p in installed["old"].parent.iterdir()) == ["Hello-x86_64.AppImage"]
    [row] = regdb.list_installations(open_registry())
    assert row["version"] == "2.5.0"


def test_running_application_is_not_replaced(installed, monkeypatch):
    from cygnus.core import inventory

    installed["server"]["published"] = installed["build"]()
    monkeypatch.setattr(inventory, "running_appimages", lambda: {str(installed["old"]): [4242]})
    result = _apply(installed)
    assert not result["ok"] and "is running" in result["error"]


def test_nothing_to_update(installed):
    installed["server"]["published"] = installed["old"]
    result = _apply(installed)
    assert not result["ok"] and result["error"].startswith("No update to install")
