"""The helper coexists with other package managers (paru, Shelly, Discover…)."""

import pytest

from cygnus.helper import actions
from cygnus.helper.ledger import Ledger


@pytest.fixture
def db(tmp_path, monkeypatch):
    dbpath = tmp_path / "pacman"
    (dbpath / "local").mkdir(parents=True)
    (dbpath / "local/ncdu-2.9-1").mkdir()
    (dbpath / "sync").mkdir()
    monkeypatch.setattr(actions, "DBPATH", str(dbpath))
    monkeypatch.setattr(actions, "PACMAN_LOG", str(tmp_path / "pacman.log"))
    return dbpath


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_no_lock_no_wait(db):
    said = []
    actions.wait_for_pacman_lock(said.append, running=lambda: pytest.fail("not needed"))
    assert said == []


def test_waits_for_the_named_package_manager(db):
    (db / "db.lck").touch()
    clock, said = Clock(), []

    def running():
        if clock.t >= 10:
            (db / "db.lck").unlink()
        return ["paru (pid 4242)"]

    actions.wait_for_pacman_lock(said.append, running=running, sleep=clock.sleep, clock=clock)
    assert said == ["Waiting for paru (pid 4242) to finish…"]


def test_a_lock_nobody_holds_is_reported_not_removed(db):
    (db / "db.lck").touch()
    clock = Clock()
    with pytest.raises(actions.PlanRefused, match="no package manager is running"):
        actions.wait_for_pacman_lock(lambda _: None, running=lambda: [], sleep=clock.sleep, clock=clock)
    assert (db / "db.lck").exists()


def test_gives_up_after_the_timeout(db):
    (db / "db.lck").touch()
    clock = Clock()
    with pytest.raises(actions.PlanRefused, match="over 1 minutes"):
        actions.wait_for_pacman_lock(lambda _: None, running=lambda: ["pacman (pid 1)"], timeout=60,
                                     sleep=clock.sleep, clock=clock)


def test_changes_by_another_tool_are_attributed(db, tmp_path):
    (tmp_path / "pacman.log").write_text(
        "[2026-10-07T09:00:00+0300] [PACMAN] Running 'pacman -S foo'\n"
        "[2026-10-07T09:00:01+0300] [ALPM] installed foo (1-1)\n"
        "[2026-10-07T10:00:00+0300] [PACMAN] Running 'pacman -Syu'\n"
        "[2026-10-07T10:00:05+0300] [ALPM] upgraded bar (1-1 -> 2-1)\n"
        "[2026-10-07T10:00:06+0300] [ALPM] upgraded baz (1-1 -> 2-1)\n"
        "[2026-10-07T10:00:07+0300] [ALPM-SCRIPTLET] noise\n")
    from datetime import datetime

    since = datetime.fromisoformat("2026-10-07T09:30:00+03:00").timestamp()
    assert actions.changes_since(since) == "pacman -Syu (upgraded 2)"


def test_a_command_started_before_the_plan_is_still_named(db, tmp_path):
    # Real test 9: `pacman -S cowsay` was waiting at its prompt when Cygnus made its plan.
    (tmp_path / "pacman.log").write_text(
        "[2026-10-07T18:05:24+0300] [PACMAN] Running 'pacman -S cowsay'\n"
        "[2026-10-07T18:06:11+0300] [ALPM] installed cowsay (3.8.4-1)\n")
    from datetime import datetime

    since = datetime.fromisoformat("2026-10-07T18:05:40+03:00").timestamp()
    assert actions.changes_since(since) == "pacman -S cowsay (installed 1)"


def _plan(**kw):
    return actions.HelperPlan(id="p", kind="packages", caller_uid=1000, caller_sender=":1.1", action_id="a",
                              message="m", summary={}, commands=[["pacman", "-S", "--noconfirm", "--", "ncdu"]],
                              expires=1e18, **kw)


def test_outdated_plan_is_refused_with_attribution(db, tmp_path):
    plan = _plan(local_fingerprint=actions.local_db_fingerprint())
    (db / "local/ncdu-2.9-1").rename(db / "local/ncdu-3.0-1")  # someone upgraded meanwhile
    (tmp_path / "pacman.log").write_text("[2099-01-01T00:00:00+0000] [PACMAN] Running 'pacman -Syu'\n")
    ran = []
    ok, detail = actions.execute(plan, run=lambda argv, sink: ran.append(argv) or 0,
                                 ledger=Ledger(tmp_path / "l.db"), op_id="o")
    assert not ok and ran == []
    assert "changed after this plan was made, by pacman -Syu" in detail


def test_retries_when_another_tool_grabs_the_lock_first(db, tmp_path):
    attempts = []

    def run(argv, sink):
        attempts.append(argv)
        if len(attempts) == 1:
            sink("error: failed to init transaction (unable to lock database)")
            return 1
        return 0

    ok, detail = actions.execute(_plan(local_fingerprint=actions.local_db_fingerprint()), run=run,
                                 ledger=Ledger(tmp_path / "l.db"), op_id="o")
    assert ok and len(attempts) == 2 and "Another package manager started" in detail


def test_sync_snapshot_is_installed_under_pacman_lock(db, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(actions, "install_sync_snapshot", lambda snap: seen.append((db / "db.lck").exists()))
    plan = _plan(local_fingerprint=actions.local_db_fingerprint(), sync_snapshot=str(tmp_path / "snap"))
    ok, _ = actions.execute(plan, run=lambda argv, sink: 0, ledger=Ledger(tmp_path / "l.db"), op_id="o")
    assert ok and seen == [True] and not (db / "db.lck").exists()


def test_retry_after_a_lock_race_rechecks_the_plan(db, tmp_path, monkeypatch):
    attempts = []

    def run(argv, sink):
        attempts.append(argv)
        if len(attempts) == 1:
            (db / "local/foo-1.0-1").mkdir()  # the other tool installed something while we waited
            sink("error: failed to init transaction (unable to lock database)")
            return 1
        return 0

    ok, detail = actions.execute(_plan(local_fingerprint=actions.local_db_fingerprint()), run=run,
                                 ledger=Ledger(tmp_path / "l.db"), op_id="o")
    assert not ok and len(attempts) == 1 and "changed after this plan was made" in detail


def test_plan_is_bound_to_the_repository_databases(db, tmp_path):
    (db / "sync/core.db").write_bytes(b"old")
    plan = _plan(local_fingerprint=actions.local_db_fingerprint(), sync_fingerprint=actions.sync_db_fingerprint())
    import os
    import time

    (db / "sync/core.db").write_bytes(b"newer database")  # someone ran pacman -Sy
    os.utime(db / "sync/core.db", ns=(time.time_ns(), time.time_ns() + 10**9))
    ran = []
    ok, detail = actions.execute(plan, run=lambda argv, sink: ran.append(argv) or 0,
                                 ledger=Ledger(tmp_path / "l.db"), op_id="o")
    assert not ok and ran == [] and "package databases changed" in detail


def test_our_own_commands_do_not_count_as_outside_changes(db, tmp_path):
    plan = _plan(local_fingerprint=actions.local_db_fingerprint())
    plan.commands = [["pacman", "-S", "--noconfirm", "--", "a"], ["pacman", "-S", "--noconfirm", "--", "b"]]
    calls = []

    def run(argv, sink):
        calls.append(argv)
        if len(calls) == 1:
            (db / "local/a-1-1").mkdir()  # our first command installed a
            return 0
        if len(calls) == 2:
            sink("error: failed to init transaction (unable to lock database)")
            return 1
        return 0

    ok, detail = actions.execute(plan, run=run, ledger=Ledger(tmp_path / "l.db"), op_id="o")
    assert ok and len(calls) == 3, detail


# -- local package files ----------------------------------------------------------------------------------
def _local(name="hello", version="1.0-1", **kw):
    return {"path": "/staged/x.pkg.tar", "name": name, "version": version, "sha256": "0" * 64, "signed": False,
            "has_install_script": False, **kw}


def _plan_local(item, satisfy=lambda deps: {}, analyse=None, installed=lambda names: {}):
    from cygnus.core.backends import pacman as pm

    def default_analyse(targets, **kw):
        return pm.PacmanAnalysis(targets=targets, to_add=[{"name": t, "version": "9-1", "repo": "extra"} for t in targets])

    class L:
        def owns_package(self, n):
            return True

    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=("core",), servers={}, hold=())
    return actions.plan_packages({}, caller_uid=1000, caller_sender=":1.1", ledger=L(), local=[item], config=cfg,
                                 satisfy=satisfy, analyse=analyse or default_analyse, installed=installed)


@pytest.mark.parametrize("item,match", [
    (_local(name="linux-cachyos"), "protected"),
    (_local(name="pacman"), "protected"),
    (_local(name="firefox (official repository, signed). Install nothing"), "invalid name"),
    (_local(version="1.0 (verified)"), "invalid name or version"),
    (_local(conflicts=["glibc"]), "conflicts with or replaces protected"),
    (_local(replaces=["sudo"]), "conflicts with or replaces protected"),
])
def test_local_packages_are_checked_like_any_other(db, item, match):
    with pytest.raises(actions.PlanRefused, match=match):
        _plan_local(item)


def test_dependencies_of_a_local_package_are_part_of_the_plan(db):
    sat = {"cowsay": {"installed": None, "repo": {"name": "cowsay", "version": "3.8-1", "repo": "extra"}},
           "glibc>=2.30": {"installed": "glibc", "repo": None}}
    plan = _plan_local(_local(depends=["cowsay", "glibc>=2.30"]), satisfy=lambda deps: sat)
    assert plan.commands[0] == ["pacman", "-S", "--noconfirm", "--needed", "--asdeps", "--", "cowsay"]
    assert plan.commands[1][:2] == ["pacman", "-U"]
    assert [p["name"] for p in plan.summary["install"]] == ["cowsay"]
    assert "cowsay" in plan.message


def test_unavailable_dependencies_refuse_the_plan(db):
    with pytest.raises(actions.PlanRefused, match="none of your repositories provide: libfoo.so"):
        _plan_local(_local(depends=["libfoo.so"]), satisfy=lambda deps: {"libfoo.so": {"installed": None, "repo": None}})


# -- staging ------------------------------------------------------------------------------------------------
def test_staging_accepts_only_regular_files_and_leaves_nothing_behind(tmp_path):
    import os

    staging = tmp_path / "staging"
    r, w = os.pipe()  # a pipe that is never closed must not stall the helper
    try:
        with pytest.raises(actions.PlanRefused, match="regular file"):
            actions.stage_local_package(r, "0" * 64, staging)
    finally:
        os.close(r), os.close(w)
    f = tmp_path / "p.pkg.tar"
    f.write_bytes(b"data")
    with open(f, "rb") as fh:
        with pytest.raises(actions.PlanRefused, match="does not match"):
            actions.stage_local_package(fh.fileno(), "0" * 64, staging)
    assert list(staging.iterdir()) == []


def test_fresh_snapshot_folder_is_reachable_by_pacmans_download_user(monkeypatch, tmp_path):
    """pacman 7 downloads as user `alpm`; a 0700 temp folder made every system upgrade fail."""
    import os
    import stat
    import tempfile

    from cygnus.core.util import proc

    seen = {}

    def fake_run(argv, **kw):
        seen["mode"] = stat.S_IMODE(os.stat(argv[argv.index("--dbpath") + 1]).st_mode)
        return proc.Result(argv, 0, "", "", False, b"")

    monkeypatch.setattr(proc, "run", fake_run)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    snap = actions.fresh_sync_snapshot(str(tmp_path))
    assert seen["mode"] == 0o755 and os.path.islink(os.path.join(snap, "local"))


# -- stale lock ------------------------------------------------------------------------------------------
def test_stale_lock_is_removed_only_when_nothing_runs_and_it_is_the_same_file(db, tmp_path):
    lock = db / "db.lck"
    with pytest.raises(actions.PlanRefused, match="not locked"):
        actions.plan_clear_stale_lock(caller_uid=1000, caller_sender=":1.1", running=lambda: [])
    lock.touch()
    with pytest.raises(actions.PlanRefused, match="paru \\(pid 7\\) is running"):
        actions.plan_clear_stale_lock(caller_uid=1000, caller_sender=":1.1", running=lambda: ["paru (pid 7)"])
    plan = actions.plan_clear_stale_lock(caller_uid=1000, caller_sender=":1.1", running=lambda: [])
    assert plan.action_id.endswith("recovery.manage") and "no package manager is running" in plan.message.lower()

    # a package manager started after the plan was approved
    with pytest.raises(actions.PlanRefused, match="started meanwhile"):
        actions.clear_stale_lock(plan, running=lambda: ["pacman (pid 9)"])
    # the lock was replaced by a new, live one
    lock.unlink()
    lock.touch()
    import os

    os.utime(lock, ns=(1, 1))
    with pytest.raises(actions.PlanRefused, match="changed since you approved"):
        actions.clear_stale_lock(plan, running=lambda: [])
    assert lock.exists()


def test_stale_lock_plan_runs_through_execute(db, tmp_path, monkeypatch):
    (db / "db.lck").touch()
    monkeypatch.setattr(actions, "package_managers_running", lambda proc_root="/proc": [])
    plan = actions.plan_clear_stale_lock(caller_uid=1000, caller_sender=":1.1", running=lambda: [])
    ok, detail = actions.execute(plan, run=lambda argv, sink: pytest.fail("no commands"),
                                 ledger=Ledger(tmp_path / "l.db"), op_id="o")
    assert ok and "removed" in detail and not (db / "db.lck").exists()


# -- review round 2: resources any local user could make root hold --------------------------------------
def test_a_refused_upgrade_plan_removes_its_database_snapshot(db, tmp_path, monkeypatch):
    from cygnus.core.backends import pacman as pm
    from cygnus.core.recovery.model import Issue, IssueSeverity

    # not the real /etc/pacman.conf of whatever machine this runs on: one that matches the fixture's folder
    monkeypatch.setattr(pm, "read_config", lambda: pm.PacmanConfig(
        arch=("x86_64",), dbpath=actions.DBPATH, cachedirs=(), gpgdir="", repos=("core",), servers={}, hold=()))

    snaps = []

    def fresh():
        d = tmp_path / f"snap{len(snaps)}"
        (d / "sync").mkdir(parents=True)
        snaps.append(d)
        return str(d)

    def analyse(targets, **kw):
        return pm.PacmanAnalysis(targets=targets, issues=[Issue(code="X", severity=IssueSeverity.BLOCKER, title="no",
                                                                explanation="")])

    class L:
        def owns_package(self, n):
            return True

    with pytest.raises(actions.PlanRefused):
        actions.plan_packages({"sysupgrade": True}, caller_uid=1000, caller_sender=":1.1", ledger=L(),
                              analyse=analyse, config=None, sync_fresh=fresh)
    assert snaps and not snaps[0].exists()


def test_discarding_a_plan_frees_its_files(tmp_path):
    staged, snap = tmp_path / "a.pkg.tar", tmp_path / "snap"
    staged.write_bytes(b"x")
    (snap / "sync").mkdir(parents=True)
    actions.discard_plan(_plan(staged=[str(staged)], sync_snapshot=str(snap)))
    assert not staged.exists() and not snap.exists()


def test_lock_holders_are_found_by_open_file_whatever_their_name(db):
    lock = db / "db.lck"
    lock.touch()
    with open(lock):  # this process, whatever its name, holds the lock open like libalpm does
        import os

        assert any(f"pid {os.getpid()}" in h for h in actions.lock_holders(lock))
    assert actions.lock_holders(lock) == []


def test_staging_room_is_bounded(tmp_path, monkeypatch):
    import os

    from cygnus.helper import service as hs

    monkeypatch.setenv("CYGNUS_HELPER_STAGING_DIR", str(tmp_path / "staging"))
    helper = hs.Helper.__new__(hs.Helper)
    helper.plans = {}
    with pytest.raises(actions.PlanRefused, match="too large to install in one go"):
        helper._check_staging_room(1000, actions.MAX_STAGED_PER_REQUEST + 1)
    big = tmp_path / "big"
    big.write_bytes(b"x")
    monkeypatch.setattr(os.path, "getsize", lambda p: actions.MAX_STAGED_PER_USER)
    helper.plans = {"p": _plan(staged=[str(big)])}
    with pytest.raises(actions.PlanRefused, match="too much is waiting"):
        helper._check_staging_room(1000, 10)
    helper.plans = {}
    monkeypatch.setattr(os, "statvfs", lambda p: type("S", (), {"f_bavail": 1, "f_frsize": 4096})())
    with pytest.raises(actions.PlanRefused, match="not enough free disk space"):
        helper._check_staging_room(1000, 10)



def test_replacing_an_installed_package_is_said_in_the_dialog(db):
    plan = _plan_local(_local(name="git", version="1.0-1"),
                       installed=lambda names: {"git": {"name": "git", "version": "2.51.0-1"}})
    assert "REPLACING the installed git 2.51.0-1" in plan.message


def test_a_case_only_name_difference_still_finds_the_installed_package(tmp_path, monkeypatch):
    import builders
    from cygnus.core.backends import pacman as pm
    from cygnus.core.detect import detect_file
    from cygnus.gui import service

    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    monkeypatch.setattr(pm, "read_config", lambda: cfg)

    def worker(c, req):
        if req["op"] == "info":
            return {"packages": {n: {"local": {"name": n, "version": "2.51.0-1"} if n == "git" else None,
                                     "sync": None} for n in req["names"]}}
        return {"satisfiers": {}}

    monkeypatch.setattr(pm, "run_worker", worker)
    monkeypatch.setattr("cygnus.core.backends.aur.info", lambda names, **kw: {})
    monkeypatch.setattr("cygnus.core.backends.sources._flathub_search", lambda q: [])
    cand = detect_file(builders.build_deb(tmp_path, name="Git", depends=""))
    verdict = service._foreign_verdict(cand)
    assert verdict.strategy == "native-alternative" and verdict.summary.startswith("Not needed")


def test_a_change_between_two_commands_of_one_plan_stops_the_second(db, tmp_path, monkeypatch):
    """Another package manager acting after our first command must not let the second one run unreviewed."""
    plan = actions.HelperPlan(id="p", kind="packages", caller_uid=1000, caller_sender=":1.1", action_id="a",
                              message="m", summary={}, expires=1e18, local_fingerprint=actions.local_db_fingerprint(),
                              commands=[["pacman", "-S", "--noconfirm", "--", "ncdu"],
                                        ["pacman", "-S", "--noconfirm", "--", "htop"]])
    ran, checked = [], []
    real_check = actions.check_command

    def check_then_interfere(argv, staged):
        real_check(argv, staged)
        checked.append(argv)
        if len(checked) == 2:  # after our first command and its re-baseline, before the second starts
            (db / "local/ncdu-2.9-1").rename(db / "local/ncdu-3.0-1")

    monkeypatch.setattr(actions, "check_command", check_then_interfere)
    ok, detail = actions.execute(plan, run=lambda argv, sink: ran.append(argv) or 0,
                                 ledger=Ledger(tmp_path / "l.db"), op_id="o")
    assert not ok and len(ran) == 1
    assert "changed after this plan was made" in detail


def test_a_protected_package_cannot_be_removed_on_the_way_to_a_dependency(db):
    from cygnus.core.backends import pacman as pm

    def analyse(targets, **kw):
        return pm.PacmanAnalysis(targets=targets, to_add=[{"name": "libfoo", "version": "1-1", "repo": "extra"}],
                                 to_remove=[{"name": "linux", "version": "7.2-1"}])

    sat = {"libfoo": {"installed": None, "repo": {"name": "libfoo", "version": "1-1", "repo": "extra"}}}
    with pytest.raises(actions.PlanRefused, match="protected system packages: linux"):
        _plan_local(_local(depends=["libfoo"]), satisfy=lambda deps: sat, analyse=analyse)


def test_a_failing_ledger_still_cleans_up_what_was_staged(db, tmp_path):
    staged = tmp_path / "x.pkg.tar"
    staged.write_bytes(b"x")

    class BrokenLedger:
        def begin(self, *a, **k):
            raise OSError("disk full")

        def finish(self, *a, **k):
            pass

    plan = _plan(local_fingerprint=actions.local_db_fingerprint())
    plan.staged = [str(staged)]
    ok, detail = actions.execute(plan, run=lambda argv, sink: pytest.fail("nothing may run"),
                                 ledger=BrokenLedger(), op_id="o")
    assert not ok and "disk full" in detail and not staged.exists()


@pytest.mark.parametrize("name", ["vim\n", "vim\n\n", "vi m", "-vim"])
def test_names_with_a_trailing_newline_or_a_space_are_rejected_everywhere(name):
    from cygnus.core.desktop import integrate
    from cygnus.core.ops import aur_ops, convert

    assert not actions.PKG_NAME.match(name)
    assert not aur_ops.PKGBASE.match(name)
    assert not convert._PKG_NAME.match(name)
    assert not integrate._SAFE_KEY.match(name)
    assert actions.PKG_NAME.match("vim") and aur_ops.PKGBASE.match("vim")


def test_unit_names_and_versions_reject_a_trailing_newline():
    assert actions.UNIT_NAME.match("foo.service") and not actions.UNIT_NAME.match("foo.service\n")
    assert actions.PKG_VERSION.match("1.0-1") and not actions.PKG_VERSION.match("1.0-1\n")


@pytest.mark.parametrize("members, primary, recorded", [([], 9999, True), (["u1000"], 9999, False), ([], 1234, False)])
def test_a_group_add_is_recorded_only_when_membership_really_changes(monkeypatch, members, primary, recorded):
    import grp
    import pwd
    from types import SimpleNamespace as NS

    monkeypatch.setattr(pwd, "getpwuid", lambda uid: NS(pw_name="u1000", pw_gid=primary))
    monkeypatch.setattr(grp, "getgrnam", lambda name: NS(gr_gid=1234, gr_mem=members))

    class L:
        def added_group(self, user, group):
            return True

    plan = actions.plan_group("input", "add", caller_uid=1000, caller_sender=":1.1", ledger=L())
    assert plan.commands == [["gpasswd", "-a", "u1000", "input"]]  # the (harmless) command is the same either way
    assert bool(plan.ledger_ops) is recorded
    # removing is still allowed only for what the ledger says Cygnus added
    assert actions.plan_group("input", "remove", caller_uid=1000, caller_sender=":1.1", ledger=L()).ledger_ops


def test_the_ledger_is_readable_by_root_only_including_its_sidecar_files(tmp_path):
    import os
    import stat

    path = tmp_path / "state" / "ledger.db"
    path.parent.mkdir()
    path.touch(mode=0o644)  # as created by an older release
    ledger = Ledger(path)
    ledger.begin("o1", "packages", 1000, {}, [])  # make sure the WAL/SHM files exist
    modes = {suffix: stat.S_IMODE(os.stat(f"{path}{suffix}").st_mode) for suffix in ("", "-wal", "-shm")
             if os.path.exists(f"{path}{suffix}")}
    assert "" in modes and modes and set(modes.values()) == {0o600}, modes


def test_a_running_plans_staged_files_still_count_against_the_users_quota(tmp_path, monkeypatch):
    import os

    from cygnus.helper import service as hs

    monkeypatch.setenv("CYGNUS_HELPER_STAGING_DIR", str(tmp_path / "staging"))
    helper = hs.Helper.__new__(hs.Helper)
    helper.plans = {}  # the executing plan has already been dropped from the table...
    big = tmp_path / "big"
    big.write_bytes(b"x")
    helper.running = _plan(staged=[str(big)])  # ...but is still being installed
    monkeypatch.setattr(os.path, "getsize", lambda p: actions.MAX_STAGED_PER_USER)
    with pytest.raises(actions.PlanRefused, match="too much is waiting"):
        helper._check_staging_room(1000, 10)
    helper._check_staging_room(2000, 10)  # another user is not affected
    helper.running = None
    helper._check_staging_room(1000, 10)


def test_a_user_cannot_keep_the_planning_slot_busy_with_back_to_back_full_upgrade_plans(monkeypatch):
    from gi.repository import GLib

    from cygnus.helper import service as hs

    helper = hs.Helper.__new__(hs.Helper)
    helper.sysupgrade_planned_at = {}
    monkeypatch.setattr(helper, "_caller_uid", lambda sender: 1000)
    now = [1000.0]
    monkeypatch.setattr(hs.time, "monotonic", lambda: now[0])
    upgrade = {"sysupgrade": GLib.Variant("b", True)}
    plain = {"sysupgrade": GLib.Variant("b", False), "install_repo": GLib.Variant("as", ["ncdu"])}
    assert helper._sysupgrade_cooldown_uid(":1.5", plain) is None  # ordinary plans are never limited
    assert helper._sysupgrade_cooldown_uid(":1.5", upgrade) == 1000  # the first full-upgrade plan is fine
    helper.sysupgrade_planned_at[1000] = now[0]  # ...it finished just now
    now[0] += 5
    with pytest.raises(actions.PlanRefused, match="planned a moment ago; try again in 56 seconds"):
        helper._sysupgrade_cooldown_uid(":1.5", upgrade)
    assert helper._sysupgrade_cooldown_uid(":1.5", plain) is None
    monkeypatch.setattr(helper, "_caller_uid", lambda sender: 2000)  # another user is not held up by it
    assert helper._sysupgrade_cooldown_uid(":1.6", upgrade) == 2000
    monkeypatch.setattr(helper, "_caller_uid", lambda sender: 1000)
    now[0] += hs.SYSUPGRADE_PLAN_COOLDOWN
    assert helper._sysupgrade_cooldown_uid(":1.5", upgrade) == 1000  # a minute later it is allowed again


def test_a_pacman_setup_with_another_database_folder_is_refused_instead_of_misjudged(db, monkeypatch):
    from cygnus.core.backends import pacman as pm

    def config(dbpath):
        return pm.PacmanConfig(arch=("x86_64",), dbpath=dbpath, cachedirs=(), gpgdir="", repos=("core",), servers={},
                               hold=())

    class L:
        def owns_package(self, n):
            return True

    request = {"install_repo": ["ncdu"]}
    monkeypatch.setattr(pm, "read_config", lambda: config("/srv/pacman-db"))
    with pytest.raises(actions.PlanRefused, match="databases in /srv/pacman-db"):
        actions.plan_packages(request, caller_uid=1000, caller_sender=":1.1", ledger=L())
    # the default folder (also with a trailing slash) is fine: the plan gets as far as analysing
    monkeypatch.setattr(pm, "read_config", lambda: config(actions.DBPATH + "/"))
    plan = actions.plan_packages(request, caller_uid=1000, caller_sender=":1.1", ledger=L(),
                                 analyse=lambda targets, **kw: pm.PacmanAnalysis(
                                     targets=targets, to_add=[{"name": "ncdu", "version": "2.9-1", "repo": "extra"}]))
    assert plan.commands[0][:2] == ["pacman", "-S"]


def _committing_helper(monkeypatch, plan):
    import threading

    from cygnus.helper import service as hs

    helper = hs.Helper.__new__(hs.Helper)
    helper.plans = {plan.id: plan}
    helper.plans_lock = threading.Lock()
    helper.busy = threading.Lock()
    helper.running = None
    monkeypatch.setattr(helper, "_caller_uid", lambda sender: plan.caller_uid)
    return hs, helper


def test_a_plan_that_runs_out_of_time_as_it_starts_does_not_lose_its_staged_file(tmp_path, monkeypatch):
    staged = tmp_path / "pkg.tar.zst"
    staged.write_bytes(b"x")
    plan = _plan(staged=[str(staged)])
    hs, helper = _committing_helper(monkeypatch, plan)

    class Starting:
        def __init__(self, target=None, args=(), daemon=None):
            pass

        def start(self):  # the plan's time runs out, and another request sweeps expired plans, right now
            plan.expires = 0.0
            helper._drop_expired()

    monkeypatch.setattr(hs.threading, "Thread", Starting)
    helper._commit(":1.1", "p")
    assert staged.exists() and helper.running is plan and plan.used


def test_a_worker_that_cannot_start_leaves_the_plan_usable(tmp_path, monkeypatch):
    plan = _plan()
    hs, helper = _committing_helper(monkeypatch, plan)

    class Failing:
        def __init__(self, *a, **k):
            pass

        def start(self):
            helper._drop_expired()  # a sweep in the meantime must not lose the plan either
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(hs.threading, "Thread", Failing)
    with pytest.raises(RuntimeError):
        helper._commit(":1.1", "p")
    assert not plan.used and helper.running is None and helper.plans["p"] is plan
    assert helper.busy.acquire(blocking=False)  # not left locked


def test_operations_a_dead_helper_left_running_are_marked_interrupted_when_the_next_one_starts(tmp_path):
    ledger = Ledger(tmp_path / "state" / "ledger.db")
    ledger.begin("o1", "packages", 1000, {}, [])
    ledger.begin("o2", "packages", 1000, {}, [])
    ledger.finish("o2", "succeeded", "")
    assert ledger.interrupt_unfinished() == 1
    states = dict(ledger.conn.execute("SELECT id, state FROM operation").fetchall())
    assert states == {"o1": "interrupted", "o2": "succeeded"}
    assert ledger.interrupt_unfinished() == 0


# -- the planning slot is shared fairly ----------------------------------------------------------------------------------
def _helper_for_turns(monkeypatch, now):
    from cygnus.helper import service as hs

    helper = hs.Helper.__new__(hs.Helper)
    helper.waiting_for_plan, helper.last_planner, helper.last_plan_done, helper.last_plan_started = {}, None, 0.0, 900.0
    monkeypatch.setattr(hs.time, "monotonic", lambda: now[0])
    return hs, helper


def test_a_user_who_just_had_the_planning_slot_waits_when_someone_else_was_turned_away(monkeypatch):
    now = [1000.0]
    hs, helper = _helper_for_turns(monkeypatch, now)
    helper._planning_turn(1000)  # nobody else is waiting: nothing to hold back
    helper.last_planner, helper.last_plan_done = 1000, now[0]
    helper._planning_turn(1000)  # ...so the same user may go on
    helper.waiting_for_plan[2000] = now[0]  # another user was turned away while it was busy
    with pytest.raises(actions.PlanRefused, match="another user is waiting"):
        helper._planning_turn(1000)
    helper._planning_turn(2000)  # the one who waited gets its turn at once
    assert 2000 not in helper.waiting_for_plan
    helper.waiting_for_plan[2000] = now[0]
    now[0] += hs.TURN_PAUSE_SECONDS + 0.1
    helper._planning_turn(1000)  # a few seconds later the first user may ask again


def test_whoever_waited_through_a_long_plan_is_still_waiting_when_it_ends_but_an_old_turn_away_is_forgotten(monkeypatch):
    now = [5000.0]
    hs, helper = _helper_for_turns(monkeypatch, now)
    helper.last_planner, helper.last_plan_started, helper.last_plan_done = 1000, now[0] - 700, now[0]  # a ten-minute plan
    helper.waiting_for_plan[2000] = now[0] - 500  # turned away in the middle of it
    with pytest.raises(actions.PlanRefused, match="another user is waiting"):
        helper._planning_turn(1000)
    helper.last_plan_started = now[0] - 5  # a later, short plan started after that turn-away, which is long past
    helper._planning_turn(1000)  # nobody is waiting any more
    assert helper.waiting_for_plan == {}


def test_one_request_cannot_keep_reading_package_files_for_ever(monkeypatch, tmp_path):
    import os

    from cygnus.helper import service as hs

    helper = hs.Helper.__new__(hs.Helper)
    monkeypatch.setattr(helper, "_caller_uid", lambda sender: 1000)
    monkeypatch.setattr(helper, "_check_staging_room", lambda uid, incoming: None)
    monkeypatch.setattr(hs, "PLANNING_BUDGET_SECONDS", -1.0)
    path = tmp_path / "f"
    path.write_bytes(b"x")
    fds = [os.open(path, os.O_RDONLY) for _ in range(2)]

    class Fds:
        def get_length(self):
            return len(fds)

        def get(self, i):
            return os.dup(fds[i])

    with pytest.raises(actions.PlanRefused, match="taking too long"):
        helper._plan_packages(":1.1", {}, Fds(), ["ab" * 32, "cd" * 32])
    for fd in fds:
        os.close(fd)
