"""A browser AppImage set up by an older Cygnus (its menu entry does not say it opens web links) is offered Repair, which writes the entry again with
what a browser needs; nothing is offered for a program that is not a browser or an entry that is complete."""

import shutil

import pytest

import builders
from cygnus.core import defaultbrowser as db
from cygnus.core.desktop import integrate
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation
from cygnus.core.storage import locations
from cygnus.gui import service

pytestmark = pytest.mark.needs_tool("mksquashfs", "unsquashfs")
CAPS = {"exec_allowed": True, "chmod_persists": True, "symlinks": True, "hardlinks": True, "atomic_rename": True}
BROWSER = builders.DESKTOP.replace("Categories=Utility;", "Categories=Network;WebBrowser;").replace("MimeType=x-scheme-handler/hello;text/plain;", "MimeType=text/plain;")


def _adopt(tmp_path, monkeypatch, desktop, *, older_cygnus):
    hdd = tmp_path / "hdd"
    hdd.mkdir()
    reg = open_registry()
    reg.add_location(StorageLocation(id="hdd", label="HDD", fs_uuid="ABCD-1234", fs_type="ext4", location_class="user-owned", canonical_mount=str(hdd),
                                     capabilities=CAPS))
    reg.close()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(loc, True, str(hdd)))
    (tmp_path / "build").mkdir()
    app = tmp_path / "apps" / "Hello-x86_64.AppImage"
    app.parent.mkdir()
    shutil.copy2(builders.build_appimage(tmp_path / "build", desktop=desktop), app)
    if older_cygnus:  # the version that wrote the entry exactly as the AppImage had it
        monkeypatch.setattr(integrate, "complete_browser_types", lambda mime, categories: (mime, []))
    assert service.adopt_appimage(str(app), lambda _: None)["ok"]
    [row] = service.list_apps()
    return row["installation_id"]


def test_a_browser_entry_written_before_the_claims_were_added_is_flagged_and_repaired(tmp_path, monkeypatch):
    real = integrate.complete_browser_types
    iid = _adopt(tmp_path, monkeypatch, BROWSER, older_cygnus=True)
    monkeypatch.setattr(integrate, "complete_browser_types", real)  # Cygnus as it is now
    entry = integrate.xdg_data_home() / "applications/hello.desktop"
    assert "x-scheme-handler/http" not in entry.read_text()
    problems = service.diagnose_app(iid)
    assert [(p["what"], p["state"]) for p in problems] == [("desktop_entry", "is a browser but does not say it opens web links")]
    assert service.repair_app(iid, lambda _: None)["ok"]
    assert set(db.MIME_TYPES) <= set(db._load(entry).get_list("MimeType"))
    assert service.diagnose_app(iid) == []
    assert all("does not say" not in b for b in db.assess("hello.desktop")["blockers"])  # and the chooser's own rule agrees it qualifies now


def test_a_complete_entry_and_a_program_that_is_no_browser_are_left_alone(tmp_path, monkeypatch):
    assert service.diagnose_app(_adopt(tmp_path, monkeypatch, builders.DESKTOP, older_cygnus=False)) == []


def test_an_entry_written_by_cygnus_now_already_has_the_claims_and_needs_no_repair(tmp_path, monkeypatch):
    iid = _adopt(tmp_path, monkeypatch, BROWSER, older_cygnus=False)
    assert set(db.MIME_TYPES) <= set(db._load(integrate.xdg_data_home() / "applications/hello.desktop").get_list("MimeType"))
    assert service.diagnose_app(iid) == []


def test_lacks_browser_types_reads_the_file_on_disk(tmp_path):
    path = tmp_path / "x.desktop"
    path.write_text("[Desktop Entry]\nType=Application\nName=X\nCategories=Network;WebBrowser;\nMimeType=text/html;\n")
    assert integrate.lacks_browser_types(path) is True
    path.write_text("[Desktop Entry]\nType=Application\nName=X\nCategories=Utility;\nMimeType=text/html;\n")
    assert integrate.lacks_browser_types(path) is False
    assert integrate.lacks_browser_types(tmp_path / "missing.desktop") is False
