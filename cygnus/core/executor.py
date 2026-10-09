"""User-side executor with a write-ahead operation journal (architecture §11).

Every operation is a list of steps. Each step records its intent before it runs and its result
after; each has a compensation that undoes it. If a step fails, the completed steps are
compensated in reverse order (rollback). If the process dies mid-way, the next start finds the
operation still `running` and offers resume / roll back.

Steps are registered handlers keyed by action kind. A handler returns a result dict and a
compensation (another action) or None when nothing needs undoing.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from cygnus.core import paths
from cygnus.core.errors import CygnusError
from cygnus.core.registry.db import Registry


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True, kw_only=True)
class Step:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(slots=True, kw_only=True)
class StepOutcome:
    result: dict[str, Any] = field(default_factory=dict)
    compensation: Step | None = None


Handler = Callable[[dict[str, Any]], StepOutcome]


class OperationBusy(CygnusError):
    pass


def _lock_path(op_id: str):
    d = paths.runtime_dir() / "operations"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / f"{op_id}.lock"


@contextmanager
def owning(op_id: str):
    """Hold the operation's lock while working on it. The kernel drops the lock when the process
    dies, so a `running` operation whose lock is free (or whose lock file is gone) was interrupted,
    not merely in progress. The owner removes the file before releasing the lock; whoever locked
    a file that has since been removed or replaced holds nothing, so it checks and tries again."""
    path = _lock_path(op_id)
    for _ in range(5):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise OperationBusy("another Cygnus process is working on this operation") from None
        try:
            current = os.stat(path)
        except FileNotFoundError:
            current = None
        held = os.fstat(fd)
        if current is None or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            os.close(fd)  # we locked a file the previous owner already removed
            continue
        try:
            yield
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            os.close(fd)
        return
    raise OperationBusy("another Cygnus process is working on this operation")


def _flock_held(path) -> bool | None:
    """Whether any process holds a flock on `path`, read from /proc/locks (without taking it)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    try:
        text = open("/proc/locks").read()
    except OSError:
        return None
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    return any("FLOCK" in line.split() and key in line.split() for line in text.splitlines())


def is_live(op_id: str) -> bool:
    held = _flock_held(_lock_path(op_id))
    if held is not None:
        return held
    try:  # no /proc/locks: probe by trying the lock (briefly)
        fd = os.open(_lock_path(op_id), os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


class StepFailed(CygnusError):
    def __init__(self, seq: int, kind: str, cause: BaseException):
        super().__init__(f"step {seq} ({kind}) failed: {cause}")
        self.seq, self.kind, self.cause = seq, kind, cause


@dataclass(slots=True, kw_only=True)
class OperationReport:
    op_id: str
    state: str
    completed: list[int]
    failed_step: int | None = None
    error: str | None = None
    compensated: list[int] = field(default_factory=list)
    compensation_errors: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # things deliberately left in place (e.g. files you edited)
    kept_reasons: dict[str, str] = field(default_factory=dict)  # path -> why it was left


class Executor:
    def __init__(self, registry: Registry, handlers: dict[str, Handler] | None = None):
        self.registry = registry
        self.handlers: dict[str, Handler] = dict(handlers or {})

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    # -- journal helpers ------------------------------------------------------------------------
    def _create(self, op_id: str, kind: str, app_id: str | None, steps: list[Step]) -> str:
        plan = [{"kind": s.kind, "params": s.params, "description": s.description} for s in steps]
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        with self.registry.transaction() as c:
            c.execute("INSERT INTO operation(id, kind, app_id, plan, plan_digest, state, started) "
                      "VALUES (?,?,?,?,?, 'running', ?)", (op_id, kind, app_id, json.dumps(plan), digest, _now()))
            for seq, s in enumerate(steps):
                c.execute("INSERT INTO operation_step(op_id, seq, action, state) VALUES (?,?,?, 'pending')",
                          (op_id, seq, json.dumps({"kind": s.kind, "params": s.params})))
        return op_id

    def _mark(self, op_id: str, seq: int, state: str, *, result: dict | None = None,
              compensation: Step | None = None) -> None:
        col = "started" if state == "running" else "finished"
        with self.registry.transaction() as c:
            c.execute(f"UPDATE operation_step SET state=?, {col}=?, "
                      "result=COALESCE(?, result), compensation=COALESCE(?, compensation) WHERE op_id=? AND seq=?",
                      (state, _now(), json.dumps(result) if result is not None else None,
                       json.dumps({"kind": compensation.kind, "params": compensation.params})
                       if compensation else None, op_id, seq))

    def _finish(self, op_id: str, state: str) -> None:
        with self.registry.transaction() as c:
            c.execute("UPDATE operation SET state=?, finished=? WHERE id=?", (state, _now(), op_id))

    # -- running ----------------------------------------------------------------------------------
    def run(self, kind: str, steps: list[Step], *, app_id: str | None = None,
            rollback_on_failure: bool = True, progress: Callable[[int, int, Step], None] | None = None
            ) -> OperationReport:
        for s in steps:
            if s.kind not in self.handlers:
                raise CygnusError(f"no handler for action {s.kind!r}")
        op_id = str(uuid.uuid4())
        with owning(op_id):  # taken before the journal entry exists, so it is never seen unowned
            self._create(op_id, kind, app_id, steps)
            return self._run_from(op_id, steps, 0, rollback_on_failure, progress)

    def _run_from(self, op_id: str, steps: list[Step], start: int, rollback: bool, progress) -> OperationReport:
        completed = list(range(start))
        kept: list[str] = []
        reasons: dict[str, str] = {}
        for seq in range(start, len(steps)):
            step = steps[seq]
            if progress:
                progress(seq, len(steps), step)
            self._mark(op_id, seq, "running")
            try:
                outcome = self.handlers[step.kind](step.params)
            except Exception as exc:  # noqa: BLE001 - every failure is journaled and reported
                self._mark(op_id, seq, "failed", result={"error": str(exc), "type": type(exc).__name__})
                report = OperationReport(op_id=op_id, state="failed", completed=completed, failed_step=seq,
                                         error=str(exc), kept=kept, kept_reasons=reasons)
                if rollback:
                    self._rollback(op_id, report)
                else:
                    self._finish(op_id, "needs_attention")
                    report.state = "needs_attention"
                return report
            self._mark(op_id, seq, "done", result=outcome.result, compensation=outcome.compensation)
            completed.append(seq)
            if isinstance(outcome.result, dict) and outcome.result.get("kept"):
                for left in outcome.result.get("left_over") or [outcome.result.get("path")]:
                    if left:
                        kept.append(str(left))
                        reasons[str(left)] = str(outcome.result.get("reason") or "left in place")
        self._finish(op_id, "succeeded")
        return OperationReport(op_id=op_id, state="succeeded", completed=completed, kept=kept,
                               kept_reasons=reasons)

    def _rollback(self, op_id: str, report: OperationReport) -> None:
        # A step the process died in has no compensation yet; if its kind has an "<kind>.abort"
        # handler, that puts its half-done work back first (e.g. a partly copied folder).
        interrupted = self.registry.conn.execute(
            "SELECT seq, action FROM operation_step WHERE op_id=? AND state IN ('running','failed') ORDER BY seq DESC",
            (op_id,)).fetchall()
        for seq, action in interrupted:
            a = json.loads(action)
            abort = self.handlers.get(a["kind"] + ".abort")
            if abort is None:
                state = self.registry.conn.execute("SELECT state FROM operation_step WHERE op_id=? AND seq=?",
                                                   (op_id, seq)).fetchone()[0]
                # A step that failed cleaned up after itself, and refresh steps only rebuild caches; anything
                # else the process died in may have done part of its work, which nothing here can undo.
                if state == "running" and not a["params"].get("rerun_after_rollback"):
                    report.compensation_errors.append(
                        f"step {seq} ({a.get('description') or a['kind']}) was interrupted; what it had already done "
                        "could not be undone automatically")
                continue
            try:
                abort(a["params"])
                self._mark(op_id, seq, "compensated")
                report.compensated.append(seq)
            except Exception as exc:  # noqa: BLE001
                report.compensation_errors.append(f"step {seq} (interrupted): {exc}")
        rows = self.registry.conn.execute(
            "SELECT seq, compensation FROM operation_step WHERE op_id=? AND state='done' ORDER BY seq DESC",
            (op_id,)).fetchall()
        for seq, comp in rows:
            if comp is None:
                continue
            c = json.loads(comp)
            handler = self.handlers.get(c["kind"])
            try:
                if handler is None:
                    raise CygnusError(f"no handler for compensation {c['kind']!r}")
                handler(c["params"])
                self._mark(op_id, seq, "compensated")
                report.compensated.append(seq)
            except Exception as exc:  # noqa: BLE001
                report.compensation_errors.append(f"step {seq}: {exc}")
        # Cache-refresh style steps are re-run once the rollback is complete, so derived caches
        # (menus, MIME databases) describe the restored state rather than the abandoned one.
        done = self.registry.conn.execute(
            "SELECT action FROM operation_step WHERE op_id=? AND state IN ('done','compensated') ORDER BY seq",
            (op_id,)).fetchall()
        for (action,) in done:
            a = json.loads(action)
            if a["params"].get("rerun_after_rollback") and a["kind"] in self.handlers:
                try:
                    self.handlers[a["kind"]](a["params"])
                except Exception as exc:  # noqa: BLE001
                    report.compensation_errors.append(f"re-running {a['kind']}: {exc}")
        state = "rolled_back" if not report.compensation_errors else "needs_attention"
        self._finish(op_id, state)
        report.state = state

    # -- recovery ---------------------------------------------------------------------------------
    def incomplete(self) -> list[dict[str, Any]]:
        """Operations that were interrupted (process died) or need attention. Operations another
        process is still working on are not listed."""
        rows = self.registry.conn.execute(
            "SELECT id, kind, app_id, state, started, plan FROM operation "
            "WHERE state IN ('running','needs_attention') ORDER BY started").fetchall()
        out = []
        for r in rows:
            if is_live(r[0]):
                continue
            done = [s for (s,) in self.registry.conn.execute(
                "SELECT state FROM operation_step WHERE op_id=? ORDER BY seq", (r[0],))]
            plan = json.loads(r[5])
            out.append({"id": r[0], "kind": r[1], "app_id": r[2], "state": r[3], "started": r[4],
                        "steps": [{"description": p.get("description") or p["kind"], "state": st}
                                  for p, st in zip(plan, done)]})
        return out

    def _state(self, op_id: str) -> str | None:
        row = self.registry.conn.execute("SELECT state FROM operation WHERE id=?", (op_id,)).fetchone()
        return row[0] if row else None

    def _load_steps(self, op_id: str) -> tuple[list[Step], list[str]]:
        rows = self.registry.conn.execute(
            "SELECT action, state FROM operation_step WHERE op_id=? ORDER BY seq", (op_id,)).fetchall()
        steps = [Step(kind=json.loads(a)["kind"], params=json.loads(a)["params"]) for a, _ in rows]
        return steps, [s for _, s in rows]

    def resume(self, op_id: str, *, progress=None) -> OperationReport:
        """Continue an interrupted operation. Steps must be idempotent: a step left 'running' is re-run."""
        with owning(op_id):
            if self._state(op_id) not in ("running", "needs_attention"):
                raise CygnusError("this operation is not interrupted")
            steps, states = self._load_steps(op_id)
            missing = {s.kind for s in steps} - set(self.handlers)
            if missing:
                raise CygnusError(f"cannot resume: no handler for {', '.join(sorted(missing))}")
            start = next((i for i, s in enumerate(states) if s != "done"), len(steps))
            with self.registry.transaction() as c:
                c.execute("UPDATE operation SET state='running' WHERE id=?", (op_id,))
            return self._run_from(op_id, steps, start, True, progress)

    def roll_back(self, op_id: str) -> OperationReport:
        with owning(op_id):
            if self._state(op_id) not in ("running", "needs_attention"):
                raise CygnusError("this operation is not interrupted")
            report = OperationReport(op_id=op_id, state="failed", completed=[])
            self._rollback(op_id, report)
            return report
