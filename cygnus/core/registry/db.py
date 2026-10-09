"""SQLite-backed registry with versioned migrations."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from cygnus.core import paths
from cygnus.core.errors import RegistryError
from cygnus.core.registry.schema import MIGRATIONS


@dataclass(slots=True, kw_only=True)
class StorageLocation:
    id: str
    label: str
    fs_uuid: str | None
    fs_type: str
    location_class: str
    canonical_mount: str | None = None
    view_root: str = "/"  # filesystem subtree visible at canonical_mount (btrfs subvolume / bind root)
    subpath: str = ""
    partuuid: str | None = None
    removable: bool = False
    rotational: bool = False
    capabilities: dict[str, Any] = field(default_factory=dict)
    probed_at: str | None = None
    probe_boot_id: str | None = None
    user_apps_dir: str | None = None
    is_default: bool = False
    reserve_bytes: int = 0
    state: str = "online"

    @property
    def root(self) -> str | None:
        """Absolute path of this location (mountpoint + subpath)."""
        if self.canonical_mount is None:
            return None
        return str(Path(self.canonical_mount) / self.subpath) if self.subpath else self.canonical_mount


class Registry:
    def __init__(self, conn: sqlite3.Connection, path: Path | None):
        self.conn = conn
        self.path = path

    # -- lifecycle ---------------------------------------------------------------------------
    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            if self.conn.in_transaction:  # SQLite may already have rolled back (disk full, I/O error)
                self.conn.execute("ROLLBACK")
            raise
        else:
            try:
                self.conn.execute("COMMIT")
            except BaseException:  # a failed COMMIT must not leave the transaction open for every later caller
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    @contextmanager
    def joined(self, within: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        """Run inside the caller's transaction when one is given, otherwise in a transaction of its own."""
        if within is not None:
            yield within
        else:
            with self.transaction() as c:
                yield c

    def migrate(self) -> list[int]:
        current = self.schema_version
        latest = MIGRATIONS[-1][0]
        if current > latest:
            raise RegistryError(
                f"registry schema version {current} is newer than this Cygnus ({latest}); refusing to touch it"
            )
        pending = [m for m in MIGRATIONS if m[0] > current]
        if pending and current > 0 and self.path is not None:
            self._backup(current)
        applied = []
        for version, _desc, script in pending:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                # Re-check under the write lock: another process may have migrated meanwhile.
                if self.conn.execute("PRAGMA user_version").fetchone()[0] >= version:
                    self.conn.execute("COMMIT")
                    continue
                for statement in _split_sql(script):
                    self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version = {int(version)}")
                self.conn.execute("COMMIT")
            except sqlite3.Error as exc:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise RegistryError(f"migration {version} failed: {exc}") from exc
            applied.append(version)
        return applied

    def _backup(self, version: int) -> None:
        assert self.path is not None
        dest = self.path.with_name(f"{self.path.name}.bak-v{version}")
        with sqlite3.connect(dest) as backup:
            self.conn.backup(backup)

    # -- storage locations -------------------------------------------------------------------
    _LOC_COLUMNS = (
        "id, label, fs_uuid, fs_type, class, canonical_mount, view_root, subpath, partuuid, removable, rotational, "
        "capabilities, probed_at, probe_boot_id, user_apps_dir, is_default, reserve_bytes, state"
    )

    def add_location(self, loc: StorageLocation) -> StorageLocation:
        if not loc.id:
            loc.id = str(uuid.uuid4())
        with self.transaction() as c:
            if loc.is_default:
                c.execute("UPDATE storage_location SET is_default = 0 WHERE is_default = 1")
            try:
                c.execute(
                    f"INSERT INTO storage_location ({self._LOC_COLUMNS}) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    self._loc_row(loc),
                )
            except sqlite3.IntegrityError as exc:
                raise RegistryError(f"location already registered or invalid: {exc}") from exc
        return loc

    def update_location(self, loc: StorageLocation) -> None:
        with self.transaction() as c:
            if loc.is_default:
                c.execute("UPDATE storage_location SET is_default = 0 WHERE is_default = 1 AND id != ?", (loc.id,))
            row = self._loc_row(loc)
            c.execute(
                "UPDATE storage_location SET label=?, fs_uuid=?, fs_type=?, class=?, canonical_mount=?, view_root=?, "
                "subpath=?, partuuid=?, removable=?, rotational=?, capabilities=?, probed_at=?, probe_boot_id=?, "
                "user_apps_dir=?, is_default=?, reserve_bytes=?, state=? WHERE id=?",
                (*row[1:], loc.id),
            )

    def remove_location(self, loc_id: str) -> None:
        with self.transaction() as c:
            used = c.execute("SELECT COUNT(*) FROM installation WHERE location_id = ?", (loc_id,)).fetchone()[0]
            if used:
                raise RegistryError(f"location is still used by {used} installation(s)")
            if c.execute("DELETE FROM storage_location WHERE id = ?", (loc_id,)).rowcount == 0:
                raise RegistryError(f"there is no storage location with the id {loc_id!r}")

    def get_location(self, loc_id: str) -> StorageLocation | None:
        row = self.conn.execute(
            f"SELECT {self._LOC_COLUMNS} FROM storage_location WHERE id = ?", (loc_id,)
        ).fetchone()
        return self._loc_from_row(row) if row else None

    def list_locations(self) -> list[StorageLocation]:
        rows = self.conn.execute(
            f"SELECT {self._LOC_COLUMNS} FROM storage_location ORDER BY is_default DESC, label"
        ).fetchall()
        return [self._loc_from_row(r) for r in rows]

    def default_location(self) -> StorageLocation | None:
        row = self.conn.execute(
            f"SELECT {self._LOC_COLUMNS} FROM storage_location WHERE is_default = 1"
        ).fetchone()
        return self._loc_from_row(row) if row else None

    @staticmethod
    def _loc_row(loc: StorageLocation) -> tuple:
        return (
            loc.id, loc.label, loc.fs_uuid, loc.fs_type, loc.location_class, loc.canonical_mount, loc.view_root,
            loc.subpath,
            loc.partuuid, int(loc.removable), int(loc.rotational), json.dumps(loc.capabilities, sort_keys=True),
            loc.probed_at, loc.probe_boot_id, loc.user_apps_dir, int(loc.is_default), loc.reserve_bytes, loc.state,
        )

    @staticmethod
    def _loc_from_row(row: tuple) -> StorageLocation:
        return StorageLocation(
            id=row[0], label=row[1], fs_uuid=row[2], fs_type=row[3], location_class=row[4],
            canonical_mount=row[5], view_root=row[6], subpath=row[7], partuuid=row[8], removable=bool(row[9]),
            rotational=bool(row[10]), capabilities=json.loads(row[11]), probed_at=row[12],
            probe_boot_id=row[13], user_apps_dir=row[14], is_default=bool(row[15]),
            reserve_bytes=row[16], state=row[17],
        )


def _split_sql(script: str) -> list[str]:
    """Split a migration script into statements (sqlite3.complete_statement aware)."""
    statements, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements


def open_registry(path: Path | str | None = None, *, migrate: bool = True) -> Registry:
    """Open (and by default migrate) the registry. ``":memory:"`` gives a throw-away registry."""
    db_path = None if path == ":memory:" else (Path(path) if path else paths.registry_path())
    try:
        if db_path is None:
            conn = sqlite3.connect(":memory:", isolation_level=None)
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
    except (sqlite3.Error, OSError) as exc:
        raise RegistryError(f"cannot open registry {db_path or ':memory:'}: {exc}") from exc
    reg = Registry(conn, db_path)
    if migrate:
        reg.migrate()
    return reg


# -- applications, installations, artifacts -----------------------------------------------------------
def _json(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def add_application(reg: Registry, *, app_id: str, display_name: str, vendor: str | None = None,
                    appstream_id: str | None = None, trust_level: str | None = None,
                    within: sqlite3.Connection | None = None) -> str:
    with reg.joined(within) as c:
        c.execute("INSERT INTO application(id, display_name, vendor, appstream_id, trust_level) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name, "
                  "vendor=COALESCE(excluded.vendor, vendor), trust_level=COALESCE(excluded.trust_level, trust_level)",
                  (app_id, display_name, vendor, appstream_id, trust_level))
    return app_id


def add_installation(reg: Registry, *, app_id: str, fmt: str, source: dict, version: str | None,
                     location_id: str | None, origin: str, update_provider: dict | None = None,
                     installation_id: str | None = None, within: sqlite3.Connection | None = None) -> str:
    iid = installation_id or str(uuid.uuid4())
    with reg.joined(within) as c:
        c.execute("INSERT INTO installation(id, application_id, format, source, version, location_id, origin, "
                  "update_provider) VALUES (?,?,?,?,?,?,?,?)",
                  (iid, app_id, fmt, _json(source), version, location_id, origin, _json(update_provider or {})))
        c.execute("UPDATE application SET primary_installation_id = COALESCE(primary_installation_id, ?) "
                  "WHERE id = ?", (iid, app_id))
    return iid


def remove_installation(reg: Registry, installation_id: str) -> None:
    with reg.transaction() as c:
        app = c.execute("SELECT application_id FROM installation WHERE id=?", (installation_id,)).fetchone()
        c.execute("DELETE FROM installation WHERE id=?", (installation_id,))
        if app and not c.execute("SELECT 1 FROM installation WHERE application_id=?", app).fetchone():
            c.execute("DELETE FROM application WHERE id=?", app)
        elif app:
            c.execute("UPDATE application SET primary_installation_id = (SELECT id FROM installation WHERE "
                      "application_id=? LIMIT 1) WHERE id=?", (app[0], app[0]))


def add_artifact(reg: Registry, *, installation_id: str | None, kind: str, locator: str, scope: str = "user",
                 sha256: str | None = None, op_id: str | None = None, ownership: str = "created",
                 on_uninstall: str = "remove", takeover: bool = False,
                 within: sqlite3.Connection | None = None) -> None:
    """Record something an installation owns. Each thing has one owner: claiming what another
    installation owns is refused, unless `takeover` moves it over entirely (its owner, ownership and
    what happens to it on uninstall all change together; used for packages, which move to the entry
    that installed them last). With `within`, this is part of the caller's transaction."""
    with reg.joined(within) as c:
        row = c.execute("SELECT installation_id FROM artifact WHERE kind=? AND locator=?", (kind, locator)).fetchone()
        if row and row[0] and row[0] != installation_id and not takeover and \
                c.execute("SELECT 1 FROM installation WHERE id=?", (row[0],)).fetchone():
            raise RegistryError(f"{locator} already belongs to another installation; remove, repair or move "
                                "that one instead")
        c.execute("INSERT INTO artifact(id, installation_id, kind, locator, scope, sha256, created_by_op, ownership, "
                  "on_uninstall) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, locator) DO UPDATE SET "
                  "sha256=excluded.sha256, installation_id=excluded.installation_id, scope=excluded.scope, "
                  "ownership=excluded.ownership, on_uninstall=excluded.on_uninstall",
                  (str(uuid.uuid4()), installation_id, kind, locator, scope, sha256, op_id, ownership, on_uninstall))


def artifacts_of(reg: Registry, installation_id: str) -> list[dict[str, Any]]:
    rows = reg.conn.execute("SELECT kind, locator, scope, sha256, ownership, on_uninstall FROM artifact "
                            "WHERE installation_id=? ORDER BY kind, locator", (installation_id,)).fetchall()
    return [dict(zip(("kind", "locator", "scope", "sha256", "ownership", "on_uninstall"), r)) for r in rows]


def list_installations(reg: Registry) -> list[dict[str, Any]]:
    rows = reg.conn.execute(
        "SELECT i.id, i.application_id, a.display_name, i.format, i.source, i.version, i.location_id, i.origin, "
        "i.state, i.update_provider FROM installation i JOIN application a ON a.id = i.application_id "
        "ORDER BY a.display_name").fetchall()
    keys = ("id", "app_id", "name", "format", "source", "version", "location_id", "origin", "state",
            "update_provider")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["source"], d["update_provider"] = json.loads(d["source"]), json.loads(d["update_provider"])
        out.append(d)
    return out


def add_history(reg: Registry, installation_id: str | None, kind: str, details: dict | None = None,
                within: sqlite3.Connection | None = None) -> None:
    with reg.joined(within) as c:
        c.execute("INSERT INTO history(installation_id, kind, details) VALUES (?,?,?)",
                  (installation_id, kind, _json(details or {})))
