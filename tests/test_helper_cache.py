"""The helper's cache-cleaning action: a client gives two choices and nothing else; the helper chooses the files itself, shows exactly that
list, and at commit (under pacman's lock) removes only what was shown AND is still chosen."""

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from cygnus.core import cacheclean, pkgcache
from cygnus.core.backends import pacman as pm
from cygnus.core.privilege import HelperError
from cygnus.helper import actions
from tests.test_cacheclean import Worker, make, pkg

REPO = Path(__file__).resolve().parent.parent


class Ledger:
    def __init__(self):
        self.began, self.finished = [], []

    def begin(self, op_id, kind, uid, summary, argv):
        self.began.append((op_id, kind))

    def finish(self, op_id, state, log):
        self.finished.append((op_id, state, log))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(pkgcache, "ROOT_UID", os.getuid())  # the scratch folder is the test's own: it stands in for root
    folder = make(tmp_path / "pkg", *[pkg("foo", v) for v in ("1.0-1", "1.1-1", "1.2-1", "1.3-1")], *[pkg("foo", v) + ".sig" for v in ("1.0-1", "1.1-1")],
                  pkg("gone", "1-1"), pkg("gone", "2-1"), size=1024 * 1024)
    dbpath = tmp_path / "pacman"
    (dbpath / "local").mkdir(parents=True)
    monkeypatch.setattr(actions, "DBPATH", str(dbpath))
    monkeypatch.setattr(actions, "PACMAN_LOG", str(tmp_path / "pacman.log"))
    config = pm.PacmanConfig(arch=("x86_64",), dbpath=str(dbpath), cachedirs=(str(folder) + "/",), gpgdir="", repos=(), servers={}, hold=())
    monkeypatch.setattr(actions, "_pacman_config_for_cache", lambda: config)
    monkeypatch.setattr(pm, "run_worker", Worker({"foo": "1.3-1"}))  # foo 1.3 is installed; "gone" is not
    return config


@pytest.fixture
def cache(cfg):
    return Path(cfg.cachedirs[0].rstrip("/"))


def plan(cache, keep=1, uninstalled=False, **kw):
    return actions.plan_cache_clean(keep, uninstalled, caller_uid=1000, caller_sender=":1.7", **kw)


def test_the_plan_lists_what_the_helper_chose_and_says_so_in_the_password_dialog(cache):
    p = plan(cache)
    assert p.kind == "cache" and p.commands == [] and p.action_id.endswith(".cache.clean") and p.caller_uid == 1000 and p.caller_sender == ":1.7"
    # foo: installed 1.3 stays + the 1 newest other (1.2); 1.1 and 1.0 go with their signatures. gone: not installed, keep 1 -> 1-1 goes
    assert sorted(f for _d, f, _dev, _ino in p.cache_files) == sorted([pkg("foo", "1.0-1"), pkg("foo", "1.0-1") + ".sig", pkg("foo", "1.1-1"),
                                                                       pkg("foo", "1.1-1") + ".sig", pkg("gone", "1-1")])
    assert p.summary["files"] == 5 and p.summary["bytes"] == 5 * 1024 * 1024 and p.summary["packages"] == 3 and p.summary["keep"] == 1
    assert "Delete 5 cached package files (5.0 MiB)" in p.message and str(cache) in p.message and "keeping the installed version" in p.message
    assert "1 newest older one." in p.message and "not installed" not in p.message
    assert "lose all their cached versions" in plan(cache, 1, True).message and "newest older ones" in plan(cache, 2).message


def test_the_helper_refuses_a_bad_request_itself_before_it_reads_anything(cache):
    for keep, uninstalled in ((11, False), (-1, True), (True, False), (2, "yes")):
        with pytest.raises(actions.PlanRefused):
            plan(cache, keep, uninstalled, selection=lambda *a: pytest.fail("nothing is read for a request that is not the two choices"))


def test_a_client_can_only_choose_how_many_to_keep_and_whether_to_drop_uninstalled_packages(cache):
    for keep, uninstalled in ((-1, False), (11, False), (True, False), ("2", False), (2.0, False), (None, False), (2, "yes"), (2, 1), (2, None)):
        with pytest.raises(actions.PlanRefused):
            plan(cache, keep, uninstalled)


def test_nothing_to_clean_is_said_not_planned(cache, cfg):
    chosen = cacheclean.selection(cfg, 0, True, worker=Worker({"foo": "1.3-1"}))[0]
    cacheclean.delete(chosen, {e.key for e in chosen})  # clean it all first
    with pytest.raises(actions.PlanRefused, match="nothing to clean"):
        plan(cache, 0, True)


def test_a_folder_that_is_not_roots_is_left_out_and_said_when_nothing_else_is_left(cache, monkeypatch):
    monkeypatch.setattr(pkgcache, "ROOT_UID", os.getuid() + 999)
    with pytest.raises(actions.PlanRefused, match="so it is not used"):
        plan(cache)


def test_a_cache_planned_where_pacman_keeps_its_databases_elsewhere_is_refused(monkeypatch):
    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/somewhere/else", cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    monkeypatch.setattr(pm, "read_config", lambda: cfg)
    with pytest.raises(actions.PlanRefused, match="supports only"):
        actions.plan_cache_clean(2, False, caller_uid=1000, caller_sender=":1.7")  # the real _pacman_config_for_cache, not the fixture's
    monkeypatch.setattr(pm, "read_config", lambda: (_ for _ in ()).throw(pm.CygnusError("no pacman-conf")))
    with pytest.raises(actions.PlanRefused, match="could not be read"):
        actions.plan_cache_clean(2, False, caller_uid=1000, caller_sender=":1.7")


def test_committing_removes_exactly_the_listed_files_under_pacmans_lock_and_says_how_much(cache, tmp_path, monkeypatch):
    p = plan(cache)
    ledger = Ledger()
    lock = tmp_path / "pacman" / "db.lck"
    seen = []
    real_delete = cacheclean.delete

    def watching(entries, allowed):
        seen.append(lock.exists())  # pacman's lock is held while files are removed
        return real_delete(entries, allowed)

    monkeypatch.setattr(cacheclean, "delete", watching)
    ok, detail = actions.execute(p, run=lambda argv, sink: pytest.fail("a cache cleaning runs no command"), ledger=ledger, op_id="op")
    assert ok and seen == [True] and not lock.exists()  # the lock is let go afterwards
    assert "Removed 5 files (5.0 MiB) from the package cache" in detail and ledger.finished[0][1] == "succeeded" and ledger.began == [("op", "cache")]
    left = sorted(os.listdir(cache))
    assert left == sorted([pkg("foo", "1.2-1"), pkg("foo", "1.3-1"), pkg("gone", "2-1")])


def test_a_package_installed_since_the_plan_can_only_shrink_what_is_removed(cache, monkeypatch):
    p = plan(cache)
    monkeypatch.setattr(pm, "run_worker", Worker({"foo": "1.3-1", "gone": "1-1"}))  # "gone" 1-1 is installed now
    ok, detail = actions.execute(p, run=None, ledger=Ledger(), op_id="op")
    assert ok and pkg("gone", "1-1") in os.listdir(cache) and pkg("foo", "1.0-1") not in os.listdir(cache)
    assert "Removed 4 files" in detail


def test_something_that_appeared_after_the_plan_is_not_removed_even_if_the_rules_would_choose_it(cache):
    p = plan(cache)
    (cache / pkg("foo", "0.5-1")).write_bytes(b"x" * 10)  # a new old file: not in the plan
    ok, detail = actions.execute(p, run=None, ledger=Ledger(), op_id="op")
    assert ok and (cache / pkg("foo", "0.5-1")).exists()
    assert "not in the list that was shown" in detail


def test_a_file_replaced_after_the_plan_is_not_removed(cache):
    p = plan(cache)
    victim = cache / pkg("foo", "1.0-1")
    victim.unlink()
    victim.write_bytes(b"another file")  # another inode under the same name: it is not what was shown
    ok, detail = actions.execute(p, run=None, ledger=Ledger(), op_id="op")
    assert ok and victim.exists() and "not in the list that was shown" in detail


def test_a_file_swapped_between_the_last_look_and_the_removal_is_skipped_too(cache, monkeypatch):
    p = plan(cache)
    victim = cache / pkg("foo", "1.0-1")
    real_delete = cacheclean.delete

    def swapping(entries, allowed):
        victim.unlink()
        victim.write_bytes(b"a link-sized surprise")  # after the rules chose it again, before it is removed
        return real_delete(entries, allowed)

    monkeypatch.setattr(cacheclean, "delete", swapping)
    ok, detail = actions.execute(p, run=None, ledger=Ledger(), op_id="op")
    assert ok and victim.exists() and "changed after it was listed" in detail


def test_a_link_put_where_a_listed_file_was_is_never_followed(cache, tmp_path, monkeypatch):
    p = plan(cache)
    precious = tmp_path / "precious"
    precious.write_text("keep me")
    victim = cache / pkg("gone", "1-1")
    real_delete = cacheclean.delete

    def linking(entries, allowed):
        victim.unlink()
        victim.symlink_to(precious)
        return real_delete(entries, allowed)

    monkeypatch.setattr(cacheclean, "delete", linking)
    actions.execute(p, run=None, ledger=Ledger(), op_id="op")
    assert precious.read_text() == "keep me" and victim.is_symlink()


def test_another_package_manager_holding_the_lock_is_waited_for_not_overridden(cache, tmp_path, monkeypatch):
    p = plan(cache)
    lock = tmp_path / "pacman" / "db.lck"
    lock.write_text("")
    waited = []
    monkeypatch.setattr(actions, "wait_for_pacman_lock", lambda sink, **kw: waited.append(True) or lock.unlink())
    ok, _ = actions.execute(p, run=None, ledger=Ledger(), op_id="op")
    assert ok and waited == [True]
    # and when the lock appears between the wait and the removal, nothing is removed and the failure is recorded
    p = plan(cache, 0, True)
    monkeypatch.setattr(actions, "wait_for_pacman_lock", lambda sink, **kw: lock.write_text(""))
    ledger = Ledger()
    ok, detail = actions.execute(p, run=None, ledger=ledger, op_id="op2")
    assert not ok and ledger.finished[-1][1] == "failed" and "another package manager" in detail
    assert pkg("foo", "1.2-1") in os.listdir(cache)


def test_a_plan_has_no_command_and_check_command_is_never_involved(cache):
    p = plan(cache)
    assert p.commands == [] and p.staged == [] and p.ledger_ops == []  # nothing is recorded as installed or forgotten


def test_the_client_sends_only_the_two_choices():
    from gi.repository import GLib

    from cygnus.core.privilege import HelperClient

    sent = []
    client = HelperClient.__new__(HelperClient)
    client.GLib = GLib
    client._call = lambda method, params, reply, **kw: sent.append((method, params.unpack(), reply)) or ("p1", "{}", "msg")
    client.plan_cache_clean(3, False)
    client.plan_cache_clean(0, True)
    assert sent == [("PlanCacheClean", (3, False), "(sss)"), ("PlanCacheClean", (0, True), "(sss)")]  # the two choices, as given


@pytest.mark.parametrize("remote", ["Unknown method PlanCacheClean", "UnknownMethod", "No such method 'PlanCacheClean'"])
def test_a_helper_from_before_this_action_existed_says_what_to_do(remote):
    from gi.repository import GLib

    from cygnus.core.privilege import HelperClient

    client = HelperClient.__new__(HelperClient)
    client.GLib = GLib

    def old_helper(method, params, reply, **kw):
        raise HelperError(remote)

    client._call = old_helper
    with pytest.raises(HelperError, match="older version of Cygnus.*systemctl restart cygnus-helper"):
        client.plan_cache_clean(2, False)


def test_the_polkit_action_always_asks_for_an_administrators_password_and_is_never_remembered():
    root = ET.parse(REPO / "data/polkit/io.github.omranabdulaziz.Cygnus.policy").getroot()
    mine = {a.get("id"): a for a in root.findall("action")}["io.github.omranabdulaziz.Cygnus.cache.clean"]
    assert {d.tag: d.text for d in mine.find("defaults")} == {"allow_any": "no", "allow_inactive": "no", "allow_active": "auth_admin"}
    assert "package cache" in mine.find("message").text


def test_the_interface_declares_the_method_with_exactly_two_inputs():
    from cygnus.helper import service

    xml = ET.fromstring(service.XML)
    method = next(m for m in xml.iter("method") if m.get("name") == "PlanCacheClean")
    assert [(a.get("type"), a.get("direction")) for a in method.findall("arg")] == [("y", "in"), ("b", "in"), ("s", "out"), ("s", "out"), ("s", "out")]


@pytest.fixture
def bus(tmp_path):
    from tests.test_helper_bus import Bus

    b = Bus(tmp_path)
    yield b
    b.close()


@pytest.mark.needs_tool("dbus-daemon")
def test_the_method_exists_on_the_bus_and_a_choice_out_of_range_is_refused_by_the_helper(bus):
    # (only a refusal is exercised here: planning would read this computer's real package cache, and nothing is ever committed from a test)
    with pytest.raises(HelperError, match="from 0 to 10"):
        bus.client.plan_cache_clean(11, False)
    assert bus.polkit_calls() == []  # asking for a plan never asks polkit: only a commit does


def test_a_plan_is_never_shown_for_a_folder_the_caller_could_not_look_into_themselves(cache):
    os.chmod(cache, 0o750)  # root's folder that others cannot enter: the names of what is in it are not theirs to learn from a plan
    with pytest.raises(actions.PlanRefused, match="others cannot look into"):
        plan(cache)
    os.chmod(cache, 0o755)
    assert plan(cache).summary["examples"]
    root_asking = actions.plan_cache_clean(1, False, caller_uid=0, caller_sender=":1.1")
    os.chmod(cache, 0o750)
    assert actions.plan_cache_clean(1, False, caller_uid=0, caller_sender=":1.1").summary["files"] == root_asking.summary["files"]  # root may


def test_sizes_and_the_number_kept_are_put_in_words_that_are_true(cache, tmp_path):
    one = plan(cache, 1).message
    zero = plan(cache, 0).message
    assert "keeping only the installed version of every package." in zero and "newest older" not in zero
    assert "the 1 newest older one." in one and "the 3 newest older ones" in plan(cache, 3, True).message  # (with 3 kept nothing else would go: only the packages that are not installed)
    small = make(tmp_path / "small", pkg("bar", "1-1"), pkg("bar", "2-1"), size=300)
    import cygnus.core.backends.pacman as pmod

    cfg = pmod.PacmanConfig(arch=("x86_64",), dbpath=cache.parent.as_posix() + "/pacman", cachedirs=(str(small) + "/",), gpgdir="", repos=(), servers={}, hold=())
    pm.run_worker.installed = {"bar": "2-1"}
    kib = actions.plan_cache_clean(0, False, caller_uid=1000, caller_sender=":1.7", config=cfg)
    assert "(1 KiB)" in kib.message and "0 MiB" not in kib.message  # a few hundred bytes are not "0 MiB"


def test_a_folder_that_became_private_after_the_plan_is_not_cleaned_for_a_caller_who_cannot_look_into_it(cache):
    p = plan(cache)  # planned while others could look into the folder
    before = sorted(os.listdir(cache))
    os.chmod(cache, 0o750)  # ... and made private before the commit
    ok, detail = actions.execute(p, run=None, ledger=Ledger(), op_id="op")
    assert ok and sorted(os.listdir(cache)) == before  # nothing was removed
    assert "left alone" in detail and "others cannot look into" in detail and "Removed 0 files" in detail
