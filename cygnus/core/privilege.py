"""Client for cygnus-helper (architecture §16.3): plan → show summary → Commit → progress → finished."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from cygnus.core.errors import CygnusError
from cygnus.core.util.fs import open_regular
from cygnus.helper import BUS_NAME, INTERFACE, OBJECT_PATH


class HelperError(CygnusError):
    pass


class HelperTimeout(HelperError):
    """No answer in time. The operation itself may still be running or may have finished: the ledger knows."""

    def __init__(self, op_id: str):
        super().__init__("timed out waiting for the helper")
        self.op_id = op_id


@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    summary: dict[str, Any]
    message: str  # exactly what the authentication dialog will say


class HelperClient:
    def __init__(self, address: str | None = None):
        import gi  # noqa: F401
        from gi.repository import Gio, GLib

        self.Gio, self.GLib = Gio, GLib
        if address:
            flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                     | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
            self.conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        else:
            self.conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def _call(self, method: str, params, reply: str, fd_list=None, timeout_ms: int = 600_000):
        try:
            if fd_list is not None:
                result, _ = self.conn.call_with_unix_fd_list_sync(
                    BUS_NAME, OBJECT_PATH, INTERFACE, method, params, self.GLib.VariantType(reply),
                    self.Gio.DBusCallFlags.NONE, timeout_ms, fd_list, None)
            else:
                result = self.conn.call_sync(BUS_NAME, OBJECT_PATH, INTERFACE, method, params,
                                             self.GLib.VariantType(reply), self.Gio.DBusCallFlags.NONE,
                                             timeout_ms, None)
        except self.GLib.Error as exc:
            msg = exc.message
            remote = self.Gio.DBusError.get_remote_error(exc) or ""
            if remote:
                self.Gio.DBusError.strip_remote_error(exc)
                msg = exc.message
            raise HelperError(msg) from exc
        return result.unpack()

    def plan_packages(self, *, install_repo: list[str] = (), local_files: list[tuple[str, str]] = (),
                      remove: list[str] = (), sysupgrade: bool = False, asdeps: bool = False,
                      confirm_not_installed_by_cygnus: bool = False) -> Plan:
        V = self.GLib.Variant
        request = {"install_repo": V("as", list(install_repo)), "remove": V("as", list(remove)),
                   "sysupgrade": V("b", sysupgrade), "asdeps": V("b", asdeps),
                   "confirm_not_installed_by_cygnus": V("b", confirm_not_installed_by_cygnus)}
        fd_list = self.Gio.UnixFDList.new()
        handles, shas, opened = [], [], []
        try:
            for path, sha in local_files:
                fd = open_regular(path, follow_symlinks=False)
                opened.append(fd)
                handles.append(fd_list.append(fd))
                shas.append(sha)
            plan_id, summary, message = self._call(
                "PlanPackages", V("(a{sv}ahas)", (request, handles, shas)), "(sss)", fd_list=fd_list)
        finally:
            for fd in opened:
                os.close(fd)
        return Plan(plan_id, json.loads(summary), message)

    def plan_unit(self, unit: str, action: str) -> Plan:
        plan_id, summary, message = self._call("PlanUnit", self.GLib.Variant("(ss)", (unit, action)), "(sss)")
        return Plan(plan_id, json.loads(summary), message)

    def plan_clear_stale_lock(self) -> Plan:
        plan_id, summary, message = self._call("PlanClearStaleLock", None, "(sss)")
        return Plan(plan_id, json.loads(summary), message)

    def plan_group(self, group: str, op: str) -> Plan:
        plan_id, summary, message = self._call("PlanGroup", self.GLib.Variant("(ss)", (group, op)), "(sss)")
        return Plan(plan_id, json.loads(summary), message)

    def commit(self, plan: Plan, *, on_progress: Callable[[str], None] = lambda line: None,
               timeout_s: float = 4 * 3600) -> tuple[bool, str]:
        """Commit a plan and block until the helper reports completion."""
        GLib = self.GLib
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        done: dict[str, Any] = {}
        try:
            def on_signal(conn, sender, path, iface, signal, params):
                values = params.unpack()
                if done.get("op_id") and values[0] != done["op_id"]:
                    return
                if signal == "Progress":
                    on_progress(values[1])
                elif signal == "Finished":
                    done["result"] = (values[1], values[2])

            sub = self.conn.signal_subscribe(BUS_NAME, INTERFACE, None, OBJECT_PATH, None,
                                             self.Gio.DBusSignalFlags.NONE, on_signal)
            (op_id,) = self._call("Commit", GLib.Variant("(s)", (plan.plan_id,)), "(s)")
            done["op_id"] = op_id
            deadline = GLib.get_monotonic_time() + int(timeout_s * 1e6)
            while "result" not in done and GLib.get_monotonic_time() < deadline:
                ctx.iteration(True)
            self.conn.signal_unsubscribe(sub)
        finally:
            ctx.pop_thread_default()
        if "result" not in done:
            raise HelperTimeout(done["op_id"])
        return done["result"]

    def operation_state(self, op_id: str, timeout_ms: int = 600_000) -> str | None:
        """'running', 'succeeded', 'failed' or 'interrupted' (the helper stopped before it finished) as the helper's
        ledger has it; None when it has no such operation."""
        try:
            (data,) = self._call("GetOperation", self.GLib.Variant("(s)", (op_id,)), "(s)", timeout_ms=timeout_ms)
            row = json.loads(data)
            return row["state"] if isinstance(row, dict) else None
        except HelperError as exc:
            if "UnknownMethod" not in str(exc) and "Unknown method" not in str(exc) and "No such method" not in str(exc):
                raise
        # a helper from before this call existed (still running after an upgrade): the list of recent operations
        return next((row["state"] for row in self.ledger(timeout_ms) if row["id"] == op_id), None)

    def ledger(self, timeout_ms: int = 600_000) -> list[dict[str, Any]]:
        (data,) = self._call("GetLedger", None, "(s)", timeout_ms=timeout_ms)
        return json.loads(data)
