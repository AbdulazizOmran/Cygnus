import sqlite3

import pytest

from cygnus.core.errors import RegistryError
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation
from cygnus.core.registry.schema import MIGRATIONS


def loc(label, uuid, default=False, **kw):
    return StorageLocation(id="", label=label, fs_uuid=uuid, fs_type="btrfs", location_class="posix",
                           canonical_mount="/mnt/x", is_default=default, **kw)


def test_fresh_registry_is_at_latest_schema(tmp_path):
    reg = open_registry(tmp_path / "r.db")
    assert reg.schema_version == MIGRATIONS[-1][0]
    tables = {r[0] for r in reg.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"storage_location", "application", "installation", "component", "artifact", "operation",
            "operation_step", "issue", "recovery_event", "health_result", "manifest", "history"} <= tables
    assert reg.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_registry_survives_reopen(tmp_path):
    path = tmp_path / "r.db"
    reg = open_registry(path)
    reg.add_location(loc("HDD", "u1", default=True, capabilities={"symlinks": True}))
    reg.close()
    reg2 = open_registry(path)
    [l] = reg2.list_locations()
    assert l.label == "HDD" and l.capabilities == {"symlinks": True} and l.is_default


def test_single_default(tmp_path):
    reg = open_registry(":memory:")
    a = reg.add_location(loc("A", "u1", default=True))
    b = reg.add_location(loc("B", "u2", default=True))
    assert reg.default_location().id == b.id
    a.is_default = True
    reg.update_location(a)
    assert reg.default_location().id == a.id
    assert sum(l.is_default for l in reg.list_locations()) == 1


def test_duplicate_location_refused():
    reg = open_registry(":memory:")
    reg.add_location(loc("A", "u1", subpath="Apps"))
    with pytest.raises(RegistryError):
        reg.add_location(loc("B", "u1", subpath="Apps"))
    reg.add_location(loc("C", "u1", subpath="Other"))  # same fs, other folder is fine


def test_location_in_use_cannot_be_removed():
    reg = open_registry(":memory:")
    l = reg.add_location(loc("A", "u1"))
    reg.conn.execute("INSERT INTO application(id, display_name) VALUES ('app','App')")
    reg.conn.execute("INSERT INTO installation(id, application_id, format, location_id, origin) "
                     "VALUES ('i','app','appimage',?, 'installed')", (l.id,))
    with pytest.raises(RegistryError, match="still used"):
        reg.remove_location(l.id)


def test_invalid_class_rejected_by_schema():
    reg = open_registry(":memory:")
    bad = loc("A", "u1")
    bad.location_class = "magic"
    with pytest.raises(RegistryError):
        reg.add_location(bad)


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "r.db"
    open_registry(path).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    with pytest.raises(RegistryError, match="newer"):
        open_registry(path)


def test_migration_backs_up_existing_database(tmp_path, monkeypatch):
    path = tmp_path / "r.db"
    open_registry(path).close()
    extra = MIGRATIONS + [(MIGRATIONS[-1][0] + 1, "test", "CREATE TABLE extra_test(x INTEGER) STRICT;")]
    monkeypatch.setattr(regdb, "MIGRATIONS", extra)
    reg = open_registry(path)
    assert reg.schema_version == extra[-1][0]
    assert (tmp_path / f"r.db.bak-v{MIGRATIONS[-1][0]}").exists()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "r.db"
    open_registry(path).close()
    v = MIGRATIONS[-1][0]
    bad = MIGRATIONS + [(v + 1, "broken", "CREATE TABLE ok_part(x INTEGER); THIS IS NOT SQL;")]
    monkeypatch.setattr(regdb, "MIGRATIONS", bad)
    with pytest.raises(RegistryError):
        open_registry(path)
    monkeypatch.setattr(regdb, "MIGRATIONS", MIGRATIONS)
    reg = open_registry(path)
    assert reg.schema_version == v
    assert not reg.conn.execute("SELECT name FROM sqlite_master WHERE name='ok_part'").fetchone()


def test_an_artifact_has_one_owner():
    import pytest

    from cygnus.core.errors import RegistryError
    from cygnus.core.registry import db as regdb
    from cygnus.core.registry import open_registry

    reg = open_registry(":memory:")
    regdb.add_application(reg, app_id="app", display_name="App")
    i1 = regdb.add_installation(reg, app_id="app", fmt="flatpak", source={}, version=None, location_id=None,
                                origin="installed", update_provider={})
    i2 = regdb.add_installation(reg, app_id="app", fmt="flatpak", source={}, version=None, location_id=None,
                                origin="adopted", update_provider={})
    regdb.add_artifact(reg, installation_id=i1, kind="flatpak_ref", locator="app:x")
    with pytest.raises(RegistryError, match="belongs to another installation"):
        regdb.add_artifact(reg, installation_id=i2, kind="flatpak_ref", locator="app:x", ownership="adopted",
                           on_uninstall="keep")
    assert regdb.artifacts_of(reg, i1)[0]["on_uninstall"] == "remove" and regdb.artifacts_of(reg, i2) == []
    regdb.add_artifact(reg, installation_id=i2, kind="flatpak_ref", locator="app:x", ownership="adopted",
                       on_uninstall="keep", takeover=True)
    [moved] = regdb.artifacts_of(reg, i2)
    assert (moved["ownership"], moved["on_uninstall"]) == ("adopted", "keep") and regdb.artifacts_of(reg, i1) == []
