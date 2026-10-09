"""D-Bus service for cygnus-helper (Gio). Run as root via D-Bus/systemd activation:

    python3 -I -m cygnus.helper.service

Environment (tests only): CYGNUS_HELPER_BUS=session uses the session bus instead of the system
bus; CYGNUS_HELPER_FAKE_EXEC=1 logs commands instead of running them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import gi

from gi.repository import Gio, GLib  # noqa: E402

from cygnus.core.util import proc
from cygnus.helper import BUS_NAME, INTERFACE, OBJECT_PATH, actions, staging_dir, state_dir
from cygnus.helper.ledger import Ledger

IDLE_EXIT_SECONDS = 120
SYSUPGRADE_PLAN_COOLDOWN = 60.0  # a user who just had a full-upgrade plan prepared waits before asking again
PLANNING_BUDGET_SECONDS = 120.0  # one request may spend this long reading package files before it is stopped
TURN_WAIT_SECONDS = 30.0  # how long a user who was turned away counts as waiting for the planning slot
TURN_PAUSE_SECONDS = 10.0  # the user who had the slot last waits this long when someone else is waiting for it
MESSAGE_ID = "6f1c3a0e9b8d4c7a9e2f51c0d3b4a6e7"  # journald message id for Cygnus helper audit records

XML = f"""
<node>
  <interface name="{INTERFACE}">
    <method name="PlanPackages">
      <arg type="a{{sv}}" name="request" direction="in"/>
      <arg type="ah" name="local_fds" direction="in"/>
      <arg type="as" name="local_sha256" direction="in"/>
      <arg type="s" name="plan_id" direction="out"/>
      <arg type="s" name="summary_json" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="PlanUnit">
      <arg type="s" name="unit" direction="in"/>
      <arg type="s" name="action" direction="in"/>
      <arg type="s" name="plan_id" direction="out"/>
      <arg type="s" name="summary_json" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="PlanGroup">
      <arg type="s" name="group" direction="in"/>
      <arg type="s" name="op" direction="in"/>
      <arg type="s" name="plan_id" direction="out"/>
      <arg type="s" name="summary_json" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="PlanClearStaleLock">
      <arg type="s" name="plan_id" direction="out"/>
      <arg type="s" name="summary_json" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="Commit">
      <arg type="s" name="plan_id" direction="in"/>
      <arg type="s" name="op_id" direction="out"/>
    </method>
    <method name="GetLedger">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="GetOperation">
      <arg type="s" name="op_id" direction="in"/>
      <arg type="s" name="json" direction="out"/>
    </method>
    <signal name="Progress"><arg type="s" name="op_id"/><arg type="s" name="line"/></signal>
    <signal name="Finished"><arg type="s" name="op_id"/><arg type="b" name="ok"/><arg type="s" name="detail"/></signal>
  </interface>
</node>
"""


def audit(event: str, **fields: str) -> None:
    try:
        from systemd import journal

        journal.send(event, MESSAGE_ID=MESSAGE_ID, SYSLOG_IDENTIFIER="cygnus-helper",
                     **{f"CYGNUS_{k.upper()}": str(v) for k, v in fields.items()})
    except Exception:  # noqa: BLE001 - auditing must not break the helper
        print(f"[audit] {event} {fields}", file=sys.stderr)


def real_runner(argv: list[str], sink) -> int:
    env = proc.clean_env()
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=env, text=True, errors="replace")
    assert p.stdout is not None
    for line in p.stdout:
        sink(line.rstrip("\n")[:2000])
    return p.wait()


def fake_runner(argv: list[str], sink) -> int:
    sink("[fake] " + " ".join(argv))
    return 0


class Helper:
    # Package plans are prepared in a worker thread (they can download databases and run libalpm for
    # minutes), one at a time, so the helper keeps answering everyone else meanwhile.
    plans_lock = threading.RLock()
    planning = threading.Lock()
    running: actions.HelperPlan | None = None  # the plan being executed: its staged files still count
    sysupgrade_planned_at: dict[int, float] = {}  # uid -> when that user's last full-upgrade plan was prepared
    waiting_for_plan: dict[int, float] = {}
    last_planner: int | None = None
    last_plan_done = 0.0
    last_plan_started = 0.0

    def __init__(self, conn: Gio.DBusConnection, loop: GLib.MainLoop):
        self.conn, self.loop = conn, loop
        self.plans: dict[str, actions.HelperPlan] = {}
        self.sysupgrade_planned_at = {}
        self.waiting_for_plan: dict[int, float] = {}  # uid -> when it was last turned away from the planning slot
        self.last_planner: int | None = None
        self.last_plan_done = 0.0
        self.last_plan_started = 0.0
        self.busy = threading.Lock()
        self.last_activity = time.monotonic()
        self.ledger = Ledger(Path(state_dir()) / "ledger.db")
        self.run = fake_runner if os.environ.get("CYGNUS_HELPER_FAKE_EXEC") == "1" else real_runner
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        conn.register_object_with_closures2(OBJECT_PATH, node.interfaces[0], self._on_call, None, None)
        GLib.timeout_add_seconds(15, self._idle_check)

    def take_over(self) -> None:
        """Clean up after the helper that ran before. Called only once this process owns the bus name: a second process that
        is about to be told the name is taken must not touch what the running helper is working on."""
        if self.ledger.interrupt_unfinished():
            audit("operations left unfinished by an earlier helper were marked interrupted")
        shutil.rmtree(staging_dir(), ignore_errors=True)  # nothing staged survives a restart

    # -- caller identity / polkit ---------------------------------------------------------------------
    def _caller_uid(self, sender: str) -> int:
        res = self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "GetConnectionUnixUser", GLib.Variant("(s)", (sender,)),
                                  GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
        return int(res.unpack()[0])

    def _authorized(self, sender: str, action_id: str, message: str) -> bool:
        subject = ("system-bus-name", {"name": GLib.Variant("s", sender)})
        details = {"polkit.message": message}
        try:
            res = self.conn.call_sync(
                "org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority",
                "org.freedesktop.PolicyKit1.Authority", "CheckAuthorization",
                GLib.Variant("((sa{sv})sa{ss}us)", (subject, action_id, details, 1, "")),
                GLib.VariantType("((bba{ss}))"), Gio.DBusCallFlags.NONE, 10 * 60 * 1000, None)
        except GLib.Error as exc:
            audit("polkit check failed", action=action_id, error=exc.message)
            return False
        return bool(res.unpack()[0][0])

    # -- dispatch --------------------------------------------------------------------------------------
    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        self.last_activity = time.monotonic()
        try:
            if method == "PlanPackages":
                request, _fds, shas = params.unpack()
                fd_list = invocation.get_message().get_unix_fd_list()  # its descriptors are duplicated on use
                cooldown_uid = self._sysupgrade_cooldown_uid(sender, request)
                planner = self._caller_uid(sender)
                self._planning_turn(planner)
                if not self.planning.acquire(blocking=False):
                    self.waiting_for_plan[planner] = time.monotonic()  # turned away: next in line
                    raise actions.PlanRefused("another package plan is being prepared; try again in a moment")
                self.last_plan_started = time.monotonic()
                try:
                    threading.Thread(target=self._plan_packages_async, daemon=True,
                                     args=(sender, request, fd_list, list(shas), invocation, cooldown_uid, planner)).start()
                except BaseException:
                    self.planning.release()
                    raise
            elif method == "PlanUnit":
                unit, action = params.unpack()
                plan = actions.plan_unit(unit, action, caller_uid=self._caller_uid(sender), caller_sender=sender,
                                         owner_of=actions.unit_owner)
                self._store(plan)
                invocation.return_value(GLib.Variant("(sss)", (plan.id, json.dumps(plan.summary), plan.message)))
            elif method == "PlanGroup":
                group, op = params.unpack()
                plan = actions.plan_group(group, op, caller_uid=self._caller_uid(sender), caller_sender=sender,
                                          ledger=self.ledger)
                self._store(plan)
                invocation.return_value(GLib.Variant("(sss)", (plan.id, json.dumps(plan.summary), plan.message)))
            elif method == "PlanClearStaleLock":
                plan = actions.plan_clear_stale_lock(caller_uid=self._caller_uid(sender), caller_sender=sender)
                self._store(plan)
                invocation.return_value(GLib.Variant("(sss)", (plan.id, json.dumps(plan.summary), plan.message)))
            elif method == "Commit":
                (plan_id,) = params.unpack()
                op_id = self._commit(sender, plan_id)
                invocation.return_value(GLib.Variant("(s)", (op_id,)))
            elif method == "GetLedger":
                uid = self._caller_uid(sender)  # your own operations only (root sees everyone's)
                rows = self.ledger.conn.execute(
                    "SELECT id, kind, caller_uid, summary, state, started, finished FROM operation "
                    + ("" if uid == 0 else "WHERE caller_uid = ? ") + "ORDER BY started DESC LIMIT 100",
                    () if uid == 0 else (uid,)).fetchall()
                keys = ("id", "kind", "caller_uid", "summary", "state", "started", "finished")
                invocation.return_value(GLib.Variant("(s)", (json.dumps([dict(zip(keys, r)) for r in rows]),)))
            elif method == "GetOperation":
                (op_id,) = params.unpack()
                uid = self._caller_uid(sender)  # one of your own operations (root sees any), however many came after it
                row = None
                if isinstance(op_id, str) and 0 < len(op_id) <= 64:
                    found = self.ledger.conn.execute(
                        "SELECT id, kind, caller_uid, summary, state, started, finished FROM operation WHERE id = ?",
                        (op_id,)).fetchone()
                    if found is not None and (uid == 0 or found[2] == uid):
                        row = dict(zip(("id", "kind", "caller_uid", "summary", "state", "started", "finished"), found))
                invocation.return_value(GLib.Variant("(s)", (json.dumps(row),)))
            else:
                invocation.return_dbus_error(f"{INTERFACE}.Error.UnknownMethod", method)
        except actions.PlanRefused as exc:
            invocation.return_dbus_error(f"{INTERFACE}.Error.Refused", str(exc))
        except Exception as exc:  # noqa: BLE001 - never crash the bus service
            audit("internal error", method=method, error=repr(exc))
            invocation.return_dbus_error(f"{INTERFACE}.Error.Failed", f"internal error: {exc}")

    def _sysupgrade_cooldown_uid(self, sender, request) -> int | None:
        """A full-upgrade plan downloads databases and can take minutes while it holds the one planning slot.
        So that one local user cannot keep that slot busy for everyone, a user who just had one prepared must
        wait a minute before the next. Returns the user's id if this request is such a plan."""
        wanted = request.get("sysupgrade") if hasattr(request, "get") else None
        if isinstance(wanted, GLib.Variant):
            wanted = wanted.unpack()
        if not wanted:
            return None
        uid = self._caller_uid(sender)
        wait = SYSUPGRADE_PLAN_COOLDOWN - (time.monotonic() - self.sysupgrade_planned_at.get(uid, -1e9))
        if wait > 0:
            raise actions.PlanRefused(f"a full upgrade was planned a moment ago; try again in {int(wait) + 1} seconds")
        return uid

    def _planning_turn(self, uid: int) -> None:
        """The planning slot is shared by every user. Whoever had it last waits a moment when another user was turned away
        from it in the meantime, so one user asking again and again cannot keep everyone else out."""
        now = time.monotonic()
        # someone turned away while the last plan was being prepared is still waiting, however long that took
        waiting = {u: t for u, t in self.waiting_for_plan.items() if now - t < TURN_WAIT_SECONDS or t >= self.last_plan_started}
        self.waiting_for_plan = waiting
        if self.last_planner == uid and any(u != uid for u in waiting):
            wait = TURN_PAUSE_SECONDS - (now - self.last_plan_done)
            if wait > 0:
                raise actions.PlanRefused(f"another user is waiting to prepare a package plan; try again in {int(wait) + 1} seconds")
        self.waiting_for_plan.pop(uid, None)  # it is this user's turn now

    def _plan_packages_async(self, sender, request, fd_list, shas, invocation, cooldown_uid=None, planner=None) -> None:
        def reply(fn):
            GLib.idle_add(lambda: (fn(), False)[1])

        try:
            plan = self._plan_packages(sender, request, fd_list, shas)
        except actions.PlanRefused as exc:
            msg = str(exc)
            reply(lambda: invocation.return_dbus_error(f"{INTERFACE}.Error.Refused", msg))
        except Exception as exc:  # noqa: BLE001 - never crash the bus service
            audit("internal error", method="PlanPackages", error=repr(exc))
            msg = f"internal error: {exc}"
            reply(lambda: invocation.return_dbus_error(f"{INTERFACE}.Error.Failed", msg))
        else:
            reply(lambda: invocation.return_value(
                GLib.Variant("(sss)", (plan.id, json.dumps(plan.summary), plan.message))))
        finally:
            self.last_activity = time.monotonic()
            self.last_planner, self.last_plan_done = planner, time.monotonic()
            if cooldown_uid is not None:
                self.sysupgrade_planned_at[cooldown_uid] = time.monotonic()  # counted from when it finished
            self.planning.release()

    def _drop_expired(self) -> None:
        now = time.monotonic()
        with self.plans_lock:
            expired = [self.plans.pop(p) for p, pl in list(self.plans.items()) if pl.expires < now or pl.used]
        for old in expired:
            if not old.used:
                actions.discard_plan(old)  # expired: never installed, so its files go

    def _check_staging_room(self, uid: int, incoming: int) -> None:
        """Bound what any caller can make root store: per request, per user, and the disk's free space."""
        self._drop_expired()
        if incoming > actions.MAX_STAGED_PER_REQUEST:
            raise actions.PlanRefused("these package files are too large to install in one go")
        with self.plans_lock:
            staged = {f for pl in self.plans.values() if pl.caller_uid == uid for f in pl.staged}
            running = self.running  # a plan that is executing has left self.plans but still holds its files
            if running is not None and running.caller_uid == uid:
                staged.update(running.staged)
        pending = sum(os.path.getsize(f) for f in staged if os.path.exists(f))
        if pending + incoming > actions.MAX_STAGED_PER_USER:
            raise actions.PlanRefused("too much is waiting to be installed already; commit or cancel it first")
        staging = Path(staging_dir())
        staging.mkdir(parents=True, exist_ok=True, mode=0o711)
        st = os.statvfs(staging)
        if incoming + actions.STAGING_FREE_MARGIN > st.f_bavail * st.f_frsize:
            raise actions.PlanRefused("not enough free disk space to receive these package files")

    def _store(self, plan: actions.HelperPlan) -> None:
        self._drop_expired()
        with self.plans_lock:
            if plan.sync_snapshot:  # one pending full-upgrade plan per user: a new one replaces the old
                for pid in [p for p, pl in self.plans.items() if pl.caller_uid == plan.caller_uid and pl.sync_snapshot]:
                    actions.discard_plan(self.plans.pop(pid))
            mine = sum(1 for pl in self.plans.values() if pl.caller_uid == plan.caller_uid)
            if len(self.plans) >= 32 or mine >= 4:
                actions.discard_plan(plan)
                raise actions.PlanRefused("too many pending plans; commit or let earlier ones expire first")
            self.plans[plan.id] = plan
        audit("plan created", plan=plan.id, kind=plan.kind, uid=str(plan.caller_uid), summary=plan.message)

    def _plan_packages(self, sender, request, fd_list, shas) -> actions.HelperPlan:
        uid = self._caller_uid(sender)
        local = []
        fds: list[int] = []
        staged: list[str] = []
        try:
            for i in range(fd_list.get_length() if fd_list else 0):  # every received descriptor gets closed
                fds.append(fd_list.get(i))
            if len(fds) != len(shas):
                raise actions.PlanRefused("each local package needs exactly one checksum")
            if len(fds) > actions.MAX_TARGETS:
                raise actions.PlanRefused("too many package files in one request")
            sizes = [os.fstat(fd).st_size for fd in fds]  # measured once: charged to the budget AND copied
            self._check_staging_room(uid, sum(sizes))
            started = time.monotonic()
            for fd, sha, size in zip(fds, shas, sizes):
                if time.monotonic() - started > PLANNING_BUDGET_SECONDS:  # the slot is not one user's to keep
                    raise actions.PlanRefused("reading these package files is taking too long; try fewer files at a time")
                path, digest = actions.stage_local_package(fd, sha, Path(staging_dir()), expected_size=size)
                staged.append(str(path))
                # The facts that decide the plan and the authentication message come from libalpm itself, so they
                # are exactly what pacman will install (never Cygnus's own reading of .PKGINFO). They are read by
                # a process that has given up administrator rights: this one never opens the file.
                alpm = actions.inspect_package(str(path))
                local.append({"path": str(path), "name": alpm["name"], "version": alpm["version"],
                              "sha256": digest, "signed": False, "has_install_script": alpm["has_scriptlet"],
                              "depends": alpm["depends"], "conflicts": alpm["conflicts"],
                              "provides": alpm["provides"], "replaces": alpm["replaces"],
                              "reader_uid": alpm["reader_uid"]})
            req = {k: (v.unpack() if isinstance(v, GLib.Variant) else v) for k, v in request.items()}
            plan = actions.plan_packages(req, caller_uid=uid, caller_sender=sender, ledger=self.ledger, local=local,
                                         sync_fresh=actions.fresh_sync_snapshot)
        except BaseException:
            actions.discard_staged(staged)  # refused or broken: nothing may stay in root's cache
            raise
        finally:
            for fd in fds:
                os.close(fd)
        self._store(plan)
        return plan

    def _commit(self, sender: str, plan_id: str) -> str:
        with self.plans_lock:
            plan = self.plans.get(plan_id)
        if plan is None or plan.used or plan.expires < time.monotonic():
            raise actions.PlanRefused("unknown or expired plan")
        if plan.caller_sender != sender or plan.caller_uid != self._caller_uid(sender):
            raise actions.PlanRefused("this plan belongs to another caller")
        if not self.busy.acquire(blocking=False):
            raise actions.PlanRefused("another operation is in progress")
        with self.plans_lock:
            # Claimed before the worker starts: a plan that runs out of time at this very moment must not have its
            # staged files thrown away by the expiry sweep while pacman is about to read them.
            if plan.used or plan.expires < time.monotonic():
                self.busy.release()
                raise actions.PlanRefused("unknown or expired plan")
            self.running = plan
            plan.used = True
        op_id = str(uuid.uuid4())
        try:
            threading.Thread(target=self._worker, args=(plan, op_id, sender), daemon=True).start()
        except BaseException:
            with self.plans_lock:
                self.running = None
                plan.used = False
                self.plans.setdefault(plan.id, plan)  # the sweep may already have taken it out: put it back
            self.busy.release()  # nothing started: the plan and the helper stay usable
            raise
        return op_id

    def _emit(self, name: str, value: GLib.Variant, destination: str | None = None) -> None:
        GLib.idle_add(lambda: (self.conn.emit_signal(destination, OBJECT_PATH, INTERFACE, name, value), False)[1])

    def _worker(self, plan: actions.HelperPlan, op_id: str, sender: str) -> None:
        inhibit_fd = None
        try:
            if not self._authorized(sender, plan.action_id, plan.message):
                actions.discard_plan(plan)
                audit("authorization denied", plan=plan.id, action=plan.action_id)
                self._emit("Finished", GLib.Variant("(sbs)", (op_id, False, "Authorization was denied.")), sender)
                return
            audit("operation started", op=op_id, plan=plan.id, summary=plan.message, uid=str(plan.caller_uid))
            inhibit_fd = self._inhibit()
            ok, detail = actions.execute(
                plan, run=self.run, ledger=self.ledger, op_id=op_id,
                progress=lambda line: self._emit("Progress", GLib.Variant("(ss)", (op_id, line)), sender))
            audit("operation finished", op=op_id, ok=str(ok))
            self._emit("Finished", GLib.Variant("(sbs)", (op_id, ok, detail)), sender)
        except Exception as exc:  # noqa: BLE001
            audit("operation crashed", op=op_id, error=repr(exc))
            self._emit("Finished", GLib.Variant("(sbs)", (op_id, False, f"internal error: {exc}")), sender)
        finally:
            if inhibit_fd is not None:
                os.close(inhibit_fd)
            self.running = None
            self.busy.release()
            self.last_activity = time.monotonic()

    def _inhibit(self) -> int | None:
        """Hold a logind 'block' inhibitor so the system cannot shut down mid-transaction."""
        try:
            msg = self.conn.call_with_unix_fd_list_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "Inhibit",
                GLib.Variant("(ssss)", ("shutdown:sleep", "Cygnus", "Installing or removing software", "block")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 5000, None, None)
            result, fd_list = msg
            return fd_list.get(result.unpack()[0])
        except GLib.Error:
            return None

    def _idle_check(self) -> bool:
        busy = self.busy.locked() or self.planning.locked()
        with self.plans_lock:
            pending = any(not p.used and p.expires > time.monotonic() for p in self.plans.values())
        if not busy and not pending and time.monotonic() - self.last_activity > IDLE_EXIT_SECONDS:
            for plan in self.plans.values():  # nothing a plan held may outlive the helper
                if not plan.used:
                    actions.discard_plan(plan)
            self.loop.quit()
            return False
        return True


def main() -> int:
    if os.environ.get("CYGNUS_HELPER_BUS") != "session" and os.geteuid() != 0:
        print("cygnus-helper must run as root (it is started automatically over D-Bus)", file=sys.stderr)
        return 1
    bus_type = Gio.BusType.SESSION if os.environ.get("CYGNUS_HELPER_BUS") == "session" else Gio.BusType.SYSTEM
    loop = GLib.MainLoop()
    state = {}

    def on_bus(conn, name):
        state["helper"] = Helper(conn, loop)

    def on_name(conn, name):
        state["helper"].take_over()

    def on_lost(conn, name):
        print(f"could not own {name}", file=sys.stderr)
        loop.quit()

    Gio.bus_own_name(bus_type, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_bus, on_name, on_lost)
    loop.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
