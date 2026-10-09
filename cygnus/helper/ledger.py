"""The system ledger: root-owned record of every privileged change Cygnus made (architecture §5).

A user-writable database must never be able to tell root what to delete, so removals of
packages, units and group memberships are authorized against this ledger (and pacman's DB).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS operation (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, caller_uid INTEGER NOT NULL, summary TEXT NOT NULL,
    argv TEXT NOT NULL, state TEXT NOT NULL, started TEXT NOT NULL, finished TEXT, log TEXT,
    snapper_pre INTEGER, snapper_post INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS package (
    name TEXT PRIMARY KEY, version TEXT, op_id TEXT, reason TEXT NOT NULL, installed_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS unit_change (
    unit TEXT NOT NULL, action TEXT NOT NULL, op_id TEXT NOT NULL, at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS group_change (
    username TEXT NOT NULL, grp TEXT NOT NULL, action TEXT NOT NULL, op_id TEXT NOT NULL, at TEXT NOT NULL
) STRICT;
"""


class _Locked:
    """Serialize every use of the shared connection."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._c, self._l = conn, lock

    def execute(self, *args):
        with self._l:
            return _Rows(self._c.execute(*args).fetchall())

    def executescript(self, script: str):
        with self._l:
            return self._c.executescript(script)


class _Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        # Used from the D-Bus main thread and the (single) operation worker thread.
        self._lock = threading.RLock()
        # The database holds every user's commands and full pacman output; the UI reads it through the
        # helper's own, per-user filtered GetLedger call. So only root may open the file, including the
        # -wal/-shm files SQLite creates beside it (they are created with the process umask).
        old_umask = os.umask(0o077)
        try:
            self._conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
            self.conn = _Locked(self._conn, self._lock)
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = FULL")
            self.conn.executescript(SCHEMA)
        finally:
            os.umask(old_umask)
        for suffix in ("", "-wal", "-shm"):  # files from an older release were created 0644
            try:
                os.chmod(f"{path}{suffix}", 0o600)
            except OSError:
                pass

    def begin(self, op_id: str, kind: str, uid: int, summary: dict, argv: list[list[str]]) -> None:
        self.conn.execute("INSERT INTO operation(id, kind, caller_uid, summary, argv, state, started) "
                          "VALUES (?,?,?,?,?, 'running', ?)",
                          (op_id, kind, uid, json.dumps(summary), json.dumps(argv), _now()))

    def finish(self, op_id: str, state: str, log: str) -> None:
        self.conn.execute("UPDATE operation SET state=?, finished=?, log=? WHERE id=?",
                          (state, _now(), log[-65536:], op_id))

    def record_package(self, name: str, version: str | None, op_id: str, reason: str) -> None:
        self.conn.execute("INSERT INTO package(name, version, op_id, reason, installed_at) VALUES (?,?,?,?,?) "
                          "ON CONFLICT(name) DO UPDATE SET version=excluded.version, op_id=excluded.op_id",
                          (name, version, op_id, reason, _now()))

    def forget_package(self, name: str, op_id: str | None = None) -> None:
        self.conn.execute("DELETE FROM package WHERE name=?", (name,))

    def owns_package(self, name: str) -> bool:
        return self.conn.execute("SELECT 1 FROM package WHERE name=?", (name,)).fetchone() is not None

    def record_unit(self, unit: str, action: str, op_id: str) -> None:
        self.conn.execute("INSERT INTO unit_change VALUES (?,?,?,?)", (unit, action, op_id, _now()))

    def record_group(self, username: str, group: str, action: str, op_id: str) -> None:
        self.conn.execute("INSERT INTO group_change VALUES (?,?,?,?,?)", (username, group, action, op_id, _now()))

    def added_group(self, username: str, group: str) -> bool:
        row = self.conn.execute("SELECT action FROM group_change WHERE username=? AND grp=? ORDER BY rowid DESC "
                                "LIMIT 1", (username, group)).fetchone()
        return bool(row and row[0] == "add")

    def interrupt_unfinished(self) -> int:
        """Called when the helper starts: only one helper runs at a time, so an operation still marked 'running' belongs to
        a helper that died. Whether it got as far as changing anything is unknown, so it is not called failed."""
        count = len(self.incomplete())
        self.conn.execute("UPDATE operation SET state='interrupted', finished=? WHERE state='running'", (_now(),))
        return count

    def incomplete(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT id FROM operation WHERE state='running'")]
