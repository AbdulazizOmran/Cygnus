"""A commit that times out must not lose the registry update: the helper's ledger says how it ended (gui 6)."""

import pytest

from cygnus.core.privilege import HelperTimeout
from cygnus.gui import fixes


class Client:
    def __init__(self, *, timeouts=("op-1",), states=None, ledger_down=False):
        self.timeouts, self.states, self.ledger_down, self.committed = list(timeouts), states or {}, ledger_down, []

    def commit(self, plan, on_progress=None):
        if self.timeouts:
            raise HelperTimeout(self.timeouts.pop(0))
        self.committed.append(plan)
        return True, "ok"

    def operation_state(self, op_id, timeout_ms=None):
        self.timeouts_asked = getattr(self, "timeouts_asked", []) + [timeout_ms]
        if self.ledger_down:
            raise RuntimeError("no bus")
        return self.states.get(op_id)


@pytest.fixture(autouse=True)
def clean():
    fixes._PENDING.clear(); fixes._ON_SUCCESS.clear(); fixes._LATE.clear()
    yield
    fixes._PENDING.clear(); fixes._ON_SUCCESS.clear(); fixes._LATE.clear()


def _commit(client, plans=("plan",), hook=None):
    fixes._PENDING["t"] = (client, list(plans))
    if hook is not None:
        fixes._ON_SUCCESS["t"] = hook
    return fixes.commit("t", lambda line: None)


def test_a_timeout_after_the_helper_succeeded_still_records_the_installation():
    done = []
    answer = _commit(Client(states={"op-1": "succeeded"}), hook=lambda: done.append("recorded"))
    assert answer == {"ok": True} and done == ["recorded"] and not fixes._LATE


def test_a_timeout_after_the_helper_failed_records_nothing():
    done = []
    answer = _commit(Client(states={"op-1": "failed"}), hook=lambda: done.append("recorded"))
    assert answer["ok"] is False and "failed" in answer["detail"] and done == [] and not fixes._LATE


@pytest.mark.parametrize("client", [Client(states={"op-1": "running"}), Client(states={}), Client(ledger_down=True)])
def test_while_the_outcome_is_unknown_the_record_waits_and_the_user_is_told(client):
    done = []
    answer = _commit(client, hook=lambda: done.append("recorded"))
    assert answer["ok"] is False and "lost contact" in answer["detail"]
    assert done == [] and list(fixes._LATE) == ["op-1"]


def test_a_waiting_record_is_applied_once_when_the_operation_has_finished():
    done = []
    client = Client(states={"op-1": "running"})
    _commit(client, hook=lambda: done.append("recorded"))
    fixes.settle_late_commits()
    assert done == [] and list(fixes._LATE) == ["op-1"]  # still running: keep waiting
    client.states["op-1"] = "succeeded"
    fixes.settle_late_commits()
    fixes.settle_late_commits()
    assert done == ["recorded"] and not fixes._LATE  # applied exactly once


def test_a_waiting_record_is_dropped_when_the_operation_failed():
    done = []
    client = Client(states={"op-1": "running"})
    _commit(client, hook=lambda: done.append("recorded"))
    client.states["op-1"] = "failed"
    fixes.settle_late_commits()
    assert done == [] and not fixes._LATE


def test_a_broken_ledger_during_settling_changes_nothing_and_does_not_raise():
    client = Client(states={"op-1": "running"})
    _commit(client, hook=lambda: None)
    client.ledger_down = True
    fixes.settle_late_commits()
    assert list(fixes._LATE) == ["op-1"]


def test_a_timeout_on_the_first_of_two_plans_is_not_reported_as_a_finished_change():
    done = []
    client = Client(states={"op-1": "succeeded"})
    answer = _commit(client, plans=("first", "second"), hook=lambda: done.append("recorded"))
    assert answer["ok"] is False and "remaining steps were not run" in answer["detail"]
    assert done == [] and not fixes._LATE and client.committed == []  # the second plan was never committed


def test_an_update_that_fails_after_a_late_success_is_reported_not_raised():
    def boom():
        raise OSError("disk full")

    answer = _commit(Client(states={"op-1": "succeeded"}), hook=boom)
    assert answer["ok"] is False and "could not update its records" in answer["detail"]


def test_two_refreshes_settling_at_once_apply_the_record_once():
    import threading
    import time

    done, gate = [], threading.Barrier(2)

    class Slow(Client):
        armed = False

        def operation_state(self, op_id, timeout_ms=None):
            if not self.armed:
                return "running"
            gate.wait(timeout=5)  # both refreshes have looked at the ledger before either goes on
            time.sleep(0.05)
            return "succeeded"

    client = Slow(states={})
    _commit(client, hook=lambda: done.append("recorded"))
    client.armed = True
    threads = [threading.Thread(target=fixes.settle_late_commits) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert done == ["recorded"] and not fixes._LATE


def test_settling_asks_the_ledger_with_a_short_timeout():
    client = Client(states={"op-1": "running"})
    _commit(client, hook=lambda: None)
    fixes.settle_late_commits()
    assert client.timeouts_asked[-1] == fixes.SETTLE_TIMEOUT_MS < 60_000


def test_an_operation_the_helper_left_unfinished_is_not_recorded_and_the_user_is_told():
    done = []
    answer = _commit(Client(states={"op-1": "interrupted"}), hook=lambda: done.append("recorded"))
    assert answer["ok"] is False and "stopped before it finished" in answer["detail"]
    assert done == [] and not fixes._LATE
    client = Client(states={"op-1": "running"})  # ...and one that becomes so while waiting is dropped, not kept forever
    _commit(client, hook=lambda: done.append("recorded"))
    client.states["op-1"] = "interrupted"
    fixes.settle_late_commits()
    assert done == [] and not fixes._LATE


def test_a_waiting_record_is_given_up_after_the_limit(monkeypatch):
    done = []
    _commit(Client(states={"op-1": "running"}), hook=lambda: done.append("recorded"))
    later = fixes.time.time() + fixes.LATE_LIMIT_S + 1
    monkeypatch.setattr(fixes.time, "time", lambda: later)
    fixes.settle_late_commits()
    assert done == [] and not fixes._LATE


# -- a commit that is still waiting when Cygnus is closed is picked up again when it is opened ---------------------------
RECORD = {"kind": "local", "name": "thing", "version": "1.0", "source": "/x/thing.pkg.tar.zst"}


def _restart(monkeypatch, client):
    fixes._LATE.clear()
    fixes._reset_loaded()  # a new run of Cygnus: only the file is left
    monkeypatch.setattr(fixes, "HelperClient", lambda: client)


def test_a_waiting_record_is_kept_in_a_file_and_made_by_the_next_run_once_the_helper_has_finished(monkeypatch):
    from cygnus.core import paths
    from cygnus.gui import service

    made = []
    monkeypatch.setattr(service, "record_package_install", lambda name, version, **kw: made.append((name, version, kw["source"])))
    answer = _commit(Client(states={"op-1": "running"}), hook=fixes._recording(RECORD))
    assert answer["ok"] is False and (paths.state_dir() / "late-commits.json").exists()
    _restart(monkeypatch, Client(states={"op-1": "succeeded"}))
    fixes.settle_late_commits()
    fixes.settle_late_commits()
    assert made == [("thing", "1.0", {"file": "/x/thing.pkg.tar.zst"})]  # once, by the new run
    assert not fixes._LATE and not (paths.state_dir() / "late-commits.json").exists()


def test_a_waiting_record_whose_operation_failed_is_dropped_after_a_restart(monkeypatch):
    from cygnus.gui import service

    made = []
    monkeypatch.setattr(service, "record_package_install", lambda *a, **k: made.append(1))
    _commit(Client(states={"op-1": "running"}), hook=fixes._recording(RECORD))
    _restart(monkeypatch, Client(states={"op-1": "failed"}))
    fixes.settle_late_commits()
    assert made == [] and not fixes._LATE


def test_a_forget_is_kept_too_and_a_file_that_makes_no_sense_is_ignored(monkeypatch):
    from cygnus.core import paths
    from cygnus.gui import service

    gone = []
    monkeypatch.setattr(service, "forget_installation", lambda iid: gone.append(iid))
    _commit(Client(states={"op-1": "running"}), hook=fixes._Action({"forget": "inst-1"}, lambda: service.forget_installation("inst-1")))
    _restart(monkeypatch, Client(states={"op-1": "succeeded"}))
    fixes.settle_late_commits()
    assert gone == ["inst-1"]
    (paths.state_dir() / "late-commits.json").write_text('{"op-9": {"spec": {"evil": 1}, "since": 1}, "op-8": "x", "op-7": {"spec": {"forget": "a"}}}')
    _restart(monkeypatch, Client(states={"op-9": "succeeded", "op-8": "succeeded", "op-7": "succeeded"}))
    fixes.settle_late_commits()
    assert gone == ["inst-1"] and not fixes._LATE  # nothing it could not understand was acted on


def test_without_the_helper_on_the_bus_the_file_waits_for_the_next_time(monkeypatch):
    from cygnus.core import paths
    from cygnus.gui import service

    monkeypatch.setattr(service, "record_package_install", lambda *a, **k: None)
    _commit(Client(states={"op-1": "running"}), hook=fixes._recording(RECORD))
    fixes._LATE.clear()
    fixes._reset_loaded()

    def no_bus():
        raise RuntimeError("no bus")

    monkeypatch.setattr(fixes, "HelperClient", no_bus)
    fixes.settle_late_commits()
    assert (paths.state_dir() / "late-commits.json").exists() and not fixes._LATE


# -- round 10: damaged files, a helper that stopped half-way, and an old removal ----------------------------------------
def test_an_entry_with_a_kind_that_is_not_text_does_not_break_the_list(monkeypatch):
    from cygnus.core import paths

    paths.state_dir().mkdir(parents=True, exist_ok=True)
    (paths.state_dir() / "late-commits.json").write_text(
        '{"op-a": {"spec": {"record": {"kind": ["x"], "name": "n", "version": "1", "source": "s"}}, "since": 1},'
        ' "op-b": {"spec": {"record": {"kind": {"y": 1}}}, "since": 1}}')
    _restart(monkeypatch, Client(states={}))
    fixes.settle_late_commits()  # must not raise
    assert not fixes._LATE


def _pacman_says(monkeypatch, installed):
    """What `pacman -Q NAME` says: {name: version}."""
    from types import SimpleNamespace as NS

    def run(argv, **kw):
        name = argv[-1]
        return NS(returncode=0 if name in installed else 1, stdout=f"{name} {installed[name]}\n" if name in installed else "")

    monkeypatch.setattr(fixes.proc, "run", run)


@pytest.mark.parametrize("record, installed, expected", [
    ({"kind": "aur", "pkgbase": "app", "commit": "c1", "name": "app", "version": "2-1"}, {"app": "2-1"}, True),
    ({"kind": "aur", "pkgbase": "app", "commit": "c1", "name": "app", "version": "2-1"}, {"app": "1-1"}, False),  # the old one is still there
    ({"kind": "aur", "pkgbase": "app", "commit": "c1", "name": "app", "version": "2-1"}, {}, False),
    ({"kind": "local", "name": "thing", "version": "1:3.0-2"}, {"thing": "1:3.0-2"}, True),
    ({"kind": "converted", "name": "chrome", "version": "156.0.1-1"}, {"chrome": "156.0.1-3"}, True),  # Cygnus's own release number
    ({"kind": "converted", "name": "chrome", "version": "156.0.1-1"}, {"chrome": "155.0.1-3"}, False),
])
def test_after_a_helper_that_stopped_half_way_the_system_decides_whether_the_change_happened(monkeypatch, record, installed, expected):
    full = {"source": "/x", **record}
    _pacman_says(monkeypatch, installed)
    assert fixes._outcome_is_in_place(fixes._recording(full)) is expected


def test_an_install_that_pacman_finished_before_the_helper_went_away_is_recorded_not_lost(monkeypatch):
    from cygnus.gui import service

    made = []
    monkeypatch.setattr(service, "record_package_install", lambda name, version, **kw: made.append(name))
    _pacman_says(monkeypatch, {"thing": "1.0"})
    answer = _commit(Client(states={"op-1": "interrupted"}), hook=fixes._recording(RECORD))
    assert answer == {"ok": True} and made == ["thing"]
    made.clear()
    _pacman_says(monkeypatch, {})  # ...and when it did not get that far: told, nothing recorded
    answer = _commit(Client(states={"op-1": "interrupted"}), hook=fixes._recording(RECORD))
    assert answer["ok"] is False and "do it again" in answer["detail"] and made == []


def test_a_waiting_record_whose_operation_turned_out_interrupted_is_recorded_only_if_it_really_happened(monkeypatch):
    from cygnus.gui import service

    made = []
    monkeypatch.setattr(service, "record_package_install", lambda name, version, **kw: made.append(name))
    client = Client(states={"op-1": "running"})
    _commit(client, hook=fixes._recording(RECORD))
    client.states["op-1"] = "interrupted"
    _pacman_says(monkeypatch, {"thing": "1.0"})
    fixes.settle_late_commits()
    assert made == ["thing"] and not fixes._LATE


def test_an_old_removal_is_not_applied_to_a_program_that_was_installed_again_since(monkeypatch):
    from cygnus.gui import service

    gone = []
    monkeypatch.setattr(service, "forget_installation", lambda iid: gone.append(iid))
    monkeypatch.setattr(service, "package_names", lambda iid: ["foo"])
    forget = lambda: fixes._Action({"forget": "pkg.foo"}, lambda: service.forget_installation("pkg.foo"))  # noqa: E731
    client = Client(states={"op-1": "running"})
    _commit(client, hook=forget())
    client.states["op-1"] = "succeeded"
    _pacman_says(monkeypatch, {"foo": "2.0"})  # foo is installed again: the new record stays
    fixes.settle_late_commits()
    assert gone == []
    _commit(Client(states={"op-2": "running"}, timeouts=("op-2",)), hook=forget())
    _restart_state = {"op-2": "succeeded"}
    fixes._LATE["op-2"][0].states.update(_restart_state)
    _pacman_says(monkeypatch, {})  # foo is gone, as the removal said: forget it
    fixes.settle_late_commits()
    assert gone == ["pkg.foo"]
