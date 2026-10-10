"""Cleaning pacman's package cache: only real package files and their signatures, in folders that are root's own; the installed version
and the newest few older ones stay (by libalpm's rules, not by date); the files removed are exactly the ones shown, removed through the
folder's descriptor, and anything that changed in between is skipped."""

import os
from functools import cmp_to_key
from pathlib import Path

import pyalpm
import pytest

from cygnus.core import cacheclean, pkgcache
from cygnus.core.backends import pacman as pm


def config(*folders):
    return pm.PacmanConfig(arch=("x86_64", "x86_64_v3"), dbpath="/nonexistent", cachedirs=tuple(str(f) + "/" for f in folders), gpgdir="",
                           repos=(), servers={}, hold=())


class Worker:
    """The worker's sort_versions and installed_versions, in process (libalpm's own comparison)."""

    def __init__(self, installed):
        self.installed, self.asked = installed, []

    def __call__(self, cfg, request):
        self.asked.append(request["op"])
        if request["op"] == "sort_versions":
            key = cmp_to_key(pyalpm.vercmp)
            return {"ok": True, "orders": [sorted(group, key=key, reverse=True) for group in request["groups"]]}
        if request["op"] == "installed_versions":
            return {"ok": True, "versions": self.installed}
        raise AssertionError(request)


@pytest.fixture(autouse=True)
def as_root(monkeypatch):
    monkeypatch.setattr(pkgcache, "ROOT_UID", os.getuid())  # the scratch folders are the test's own: it stands in for root


def make(folder: Path, *names, size=100):
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o755)
    for n in names:
        (folder / n).write_bytes(b"x" * size)
    return folder


def pkg(name, version, arch="x86_64", ext="zst"):
    return f"{name}-{version}-{arch}.pkg.tar.{ext}"


def doomed(folder, installed, keep=2, uninstalled=False, extra=()):
    chosen, problems, looked = cacheclean.selection(config(folder, *extra), keep, uninstalled, worker=Worker(installed))
    return sorted(e.filename for e in chosen), problems


@pytest.fixture
def cache(tmp_path):
    names = []
    for v in ("1.0-1", "1.1-1", "1.2-1", "1.3-1", "1.4-1", "1.5-1"):
        names += [pkg("foo", v), pkg("foo", v) + ".sig"]
    names += [pkg("old", "1-1"), pkg("old", "2-1"), pkg("old", "3-1"), pkg("old", "3-1") + ".sig"]  # not installed
    names += [pkg("bar", "1-1"), pkg("bar", "2-1", "x86_64_v3"), pkg("bar", "3-1", "x86_64_v3")]  # two architectures
    return make(tmp_path / "pkg", *names)


# -- what is chosen --------------------------------------------------------------------------------------------------------------------

def test_the_installed_version_and_the_newest_older_ones_stay_and_the_rest_with_their_signatures_go(cache):
    gone, problems = doomed(cache, {"foo": "1.4-1", "bar": "3-1"}, keep=2)
    assert problems == []
    # foo installed at 1.4: stays; the two newest others (1.5, 1.3) stay; 1.2, 1.1, 1.0 go, each with its signature
    assert [n for n in gone if n.startswith("foo")] == sorted([pkg("foo", v) + s for v in ("1.0-1", "1.1-1", "1.2-1") for s in ("", ".sig")])


def test_the_order_is_libalpms_not_the_files_dates_or_names(tmp_path):
    folder = make(tmp_path / "pkg", pkg("x", "1.9-1"), pkg("x", "1.10-1"), pkg("x", "1:0.1-1"), pkg("x", "1.2rc1-1"), pkg("x", "1.2-1"))
    for i, n in enumerate(sorted(os.listdir(folder))):
        os.utime(folder / n, (1000 + i * 1000, 1000 + i * 1000))  # file dates in an unrelated order
    gone, _ = doomed(folder, {"x": "1.9-1"}, keep=1)
    # newest by libalpm: 1:0.1-1 (epoch) > 1.10-1 > 1.9-1 > 1.2-1 > 1.2rc1-1. Installed 1.9 stays + the one newest other (1:0.1-1)
    assert gone == sorted([pkg("x", "1.10-1"), pkg("x", "1.2-1"), pkg("x", "1.2rc1-1")])


def test_packages_that_are_not_installed_keep_the_newest_few_or_nothing(cache):
    gone, _ = doomed(cache, {"foo": "1.5-1", "bar": "3-1"}, keep=1)
    assert [n for n in gone if n.startswith("old")] == [pkg("old", "1-1"), pkg("old", "2-1")]  # "old" 3-1 and its signature stay
    gone, _ = doomed(cache, {"foo": "1.5-1", "bar": "3-1"}, keep=1, uninstalled=True)
    # nothing of a package that is not installed stays (only 3-1 has a signature)
    assert [n for n in gone if n.startswith("old")] == sorted([pkg("old", "1-1"), pkg("old", "2-1"), pkg("old", "3-1"), pkg("old", "3-1") + ".sig"])
    # foo is installed (1.5): it stays with the one newest other (1.4); the rest go, each with its signature
    assert [n for n in gone if n.startswith("foo")] == sorted([pkg("foo", v) + s for v in ("1.0-1", "1.1-1", "1.2-1", "1.3-1") for s in ("", ".sig")])


def test_each_architecture_is_kept_on_its_own(cache):
    gone, _ = doomed(cache, {"foo": "1.5-1", "bar": "3-1"}, keep=0)
    assert pkg("bar", "1-1") in gone  # x86_64: the installed 3-1 is the v3 build, so nothing of this architecture is "the installed version"
    assert pkg("bar", "2-1", "x86_64_v3") in gone and pkg("bar", "3-1", "x86_64_v3") not in gone


def test_zero_keeps_only_the_installed_version(cache):
    gone, _ = doomed(cache, {"foo": "1.5-1"}, keep=0)
    assert [n for n in gone if n.startswith("foo")] == sorted([pkg("foo", v) + s for v in ("1.0-1", "1.1-1", "1.2-1", "1.3-1", "1.4-1") for s in ("", ".sig")])


def test_a_signature_alone_is_never_judged(tmp_path):
    folder = make(tmp_path / "pkg", pkg("lonely", "1-1") + ".sig", pkg("other", "1-1"))
    gone, _ = doomed(folder, {"other": "1-1"})
    assert gone == []


def test_only_package_files_are_ever_touched(tmp_path):
    folder = make(tmp_path / "pkg", pkg("foo", "1-1"), pkg("foo", "2-1"), pkg("foo", "3-1"), pkg("foo", "3-1") + ".part", "notes.txt",
                  "foo-1-1-x86_64.pkg.tar.zst.bak", "Foo-9-1-x86_64.pkg.tar.zst", "-s-1-1-x86_64.pkg.tar.zst", "foo-1.0-x86_64.pkg.tar.zst")
    (folder / "download-abc").mkdir()
    (folder / pkg("foo", "0.5-1")).mkdir()  # a folder with a package's name
    (folder / pkg("foo", "0.4-1")).symlink_to(folder / pkg("foo", "3-1"))  # a link
    gone, _ = doomed(folder, {"foo": "3-1"}, keep=0)
    assert gone == [pkg("foo", "1-1"), pkg("foo", "2-1")]


def test_a_folder_that_is_not_roots_alone_is_left_alone_and_said_so(tmp_path, monkeypatch):
    folder = make(tmp_path / "pkg", pkg("foo", "1-1"), pkg("foo", "2-1"))
    monkeypatch.setattr(pkgcache, "ROOT_UID", os.getuid() + 12345)  # someone else's folder
    gone, problems = doomed(folder, {"foo": "2-1"}, keep=0)
    assert gone == [] and len(problems) == 1 and "so it is not used" in problems[0]
    monkeypatch.setattr(pkgcache, "ROOT_UID", os.getuid())
    os.chmod(folder, 0o775)  # group-writable
    assert doomed(folder, {"foo": "2-1"}, keep=0)[0] == []
    os.chmod(folder, 0o757)  # world-writable
    assert doomed(folder, {"foo": "2-1"}, keep=0)[0] == []


def test_a_folder_reached_through_a_link_or_an_odd_path_is_not_cleaned(tmp_path):
    folder = make(tmp_path / "real", pkg("foo", "1-1"), pkg("foo", "2-1"))
    link = tmp_path / "link"
    link.symlink_to(folder)
    assert doomed(link, {"foo": "2-1"}, keep=0)[0] == []
    assert doomed(tmp_path / "real" / ".." / "real", {"foo": "2-1"}, keep=0)[0] == []
    with pytest.raises(cacheclean.CacheRefused):
        cacheclean.open_folder("relative/folder")
    with pytest.raises(cacheclean.CacheRefused):
        cacheclean.open_folder(str(tmp_path / "nothing"))
    (tmp_path / "file").write_text("x")
    with pytest.raises(cacheclean.CacheRefused):
        cacheclean.open_folder(str(tmp_path / "file"))


def test_two_folders_are_each_judged_and_a_repeated_one_is_looked_at_once(tmp_path):
    a = make(tmp_path / "a", pkg("foo", "1-1"), pkg("foo", "2-1"))
    b = make(tmp_path / "b", pkg("foo", "3-1"))
    chosen, problems, looked = cacheclean.selection(config(a, b, a), 0, False, worker=Worker({"foo": "3-1"}))
    assert sorted(e.filename for e in chosen) == [pkg("foo", "1-1"), pkg("foo", "2-1")] and looked == 3
    assert all(e.folder == str(a) for e in chosen)


def test_what_the_worker_says_about_the_order_must_be_about_exactly_the_cached_versions(tmp_path):
    folder = make(tmp_path / "pkg", pkg("foo", "1-1"), pkg("foo", "2-1"))

    def odd(cfg, request):
        if request["op"] == "sort_versions":
            return {"ok": True, "orders": [["2-1"] for _ in request["groups"]]}  # one version missing from the order
        return {"ok": True, "versions": {}}

    chosen, _, _ = cacheclean.selection(config(folder), 0, True, worker=odd)
    assert chosen == []  # an order that does not match is never guessed at


def test_a_worker_that_failed_or_answered_oddly_stops_it_in_words(tmp_path):
    folder = make(tmp_path / "pkg", pkg("foo", "1-1"))

    def died(cfg, request):
        raise pm.WorkerCrash("died")

    with pytest.raises(cacheclean.CacheRefused, match="could not be read"):
        cacheclean.selection(config(folder), 1, False, worker=died)
    with pytest.raises(cacheclean.CacheRefused):
        cacheclean.selection(config(folder), 1, False, worker=lambda c, r: {"ok": False})
    with pytest.raises(cacheclean.CacheRefused, match="did not expect"):
        cacheclean.selection(config(folder), 1, False, worker=lambda c, r: {"ok": True, "orders": "x", "versions": []})


@pytest.mark.parametrize("keep, uninstalled", [(-1, False), (11, False), (True, False), (2.5, False), ("2", False), (None, False), (2, "yes"), (2, 1), (2, None)])
def test_the_two_choices_are_checked(keep, uninstalled):
    with pytest.raises(cacheclean.CacheRefused):
        cacheclean.check_request(keep, uninstalled)


def test_the_preview_says_what_would_go_and_removes_nothing(cache):
    before = sorted(os.listdir(cache))
    shown = cacheclean.preview(config(cache), 1, False, worker=Worker({"foo": "1.5-1", "bar": "3-1"}))
    assert shown["files"] > 0 and shown["bytes"] == shown["files"] * 100 and shown["packages"] >= 4 and shown["problems"] == []
    assert "foo 1.0-1" in shown["examples"] and shown["folders"] == [str(cache)] and shown["keep"] == 1
    assert sorted(os.listdir(cache)) == before


# -- the removal ---------------------------------------------------------------------------------------------------------------------------

def chosen_and_allowed(folder, installed, **kw):
    chosen, _, _ = cacheclean.selection(config(folder), kw.get("keep", 0), kw.get("uninstalled", False), worker=Worker(installed))
    return chosen, {e.key for e in chosen}


def test_removal_deletes_exactly_what_was_shown_and_counts_it(cache):
    chosen, allowed = chosen_and_allowed(cache, {"foo": "1.5-1", "bar": "3-1"})
    result = cacheclean.delete(chosen, allowed)
    assert result["removed"] == len(chosen) and result["bytes"] == len(chosen) * 100 and result["skipped"] == []
    left = set(os.listdir(cache))
    assert pkg("foo", "1.5-1") in left and pkg("foo", "1.5-1") + ".sig" in left and pkg("foo", "1.0-1") not in left


def test_a_file_that_was_not_in_the_shown_list_is_not_removed(cache):
    chosen, allowed = chosen_and_allowed(cache, {"foo": "1.5-1"})
    victim = chosen[0]
    allowed.discard(victim.key)
    result = cacheclean.delete(chosen, allowed)
    assert (cache / victim.filename).exists() and any(name == victim.filename and "not in the list" in why for name, why in result["skipped"])


def test_a_file_replaced_after_it_was_listed_is_skipped(cache):
    chosen, allowed = chosen_and_allowed(cache, {"foo": "1.5-1"})
    victim = next(e for e in chosen if e.filename == pkg("foo", "1.0-1"))
    path = cache / victim.filename
    path.unlink()
    path.write_bytes(b"a different file")  # another inode under the same name
    result = cacheclean.delete(chosen, allowed)
    assert path.exists() and any(name == victim.filename and "changed" in why for name, why in result["skipped"])


def test_a_link_put_in_place_of_a_listed_file_is_never_followed(cache, tmp_path):
    chosen, allowed = chosen_and_allowed(cache, {"foo": "1.5-1"})
    victim = next(e for e in chosen if e.filename == pkg("foo", "1.0-1"))
    precious = tmp_path / "precious.txt"
    precious.write_text("keep me")
    (cache / victim.filename).unlink()
    (cache / victim.filename).symlink_to(precious)
    cacheclean.delete(chosen, allowed)
    assert precious.read_text() == "keep me" and (cache / victim.filename).is_symlink()


def test_a_folder_swapped_for_a_link_after_listing_is_not_followed(cache, tmp_path):
    chosen, allowed = chosen_and_allowed(cache, {"foo": "1.5-1"})
    elsewhere = make(tmp_path / "elsewhere", *[e.filename for e in chosen])
    moved = tmp_path / "moved"
    cache.rename(moved)
    cache.symlink_to(elsewhere)
    result = cacheclean.delete(chosen, allowed)
    assert result["removed"] == 0 and all((elsewhere / e.filename).exists() for e in chosen)
    assert result["skipped"] and all("link" in why or "odd" in why for _n, why in result["skipped"])


def test_a_file_that_is_already_gone_is_not_an_error(cache):
    chosen, allowed = chosen_and_allowed(cache, {"foo": "1.5-1"})
    (cache / chosen[0].filename).unlink()
    result = cacheclean.delete(chosen, allowed)
    assert result["removed"] == len(chosen) - 1 and result["skipped"] == []


def test_the_descriptors_are_closed(cache):
    chosen, allowed = chosen_and_allowed(cache, {"foo": "1.5-1"})
    before = len(os.listdir("/proc/self/fd"))
    cacheclean.delete(chosen, allowed)
    cacheclean.selection(config(cache), 1, False, worker=Worker({}))
    assert len(os.listdir("/proc/self/fd")) <= before


def test_a_forged_entry_for_a_file_that_is_not_a_package_is_never_removed_even_if_it_is_in_the_allowed_list(tmp_path):
    folder = make(tmp_path / "pkg", "notes.txt", "foo-1-1-x86_64.pkg.tar.zst.part")
    forged = []
    for name in ("notes.txt", "foo-1-1-x86_64.pkg.tar.zst.part"):
        st = os.stat(folder / name)
        forged.append(cacheclean.Entry(str(folder), name, "foo", "1-1", "x86_64", st.st_size, False, st.st_dev, st.st_ino))
    result = cacheclean.delete(forged, {e.key for e in forged})
    assert result["removed"] == 0 and (folder / "notes.txt").exists() and (folder / "foo-1-1-x86_64.pkg.tar.zst.part").exists()
    assert all("not a package's" in why for _name, why in result["skipped"]) and len(result["skipped"]) == 2


def test_a_link_swapped_in_after_the_path_was_checked_is_still_not_followed(tmp_path, monkeypatch):
    real = make(tmp_path / "real", pkg("foo", "1-1"))
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setattr(os.path, "realpath", lambda p: p)  # the window between the path check and the open: the check saw a folder
    with pytest.raises(cacheclean.CacheRefused, match="cannot be opened"):
        cacheclean.open_folder(str(link))  # opening with O_NOFOLLOW still refuses a link


def test_deleting_with_the_same_identity_still_needs_the_file_to_be_a_regular_file_not_a_link_to_one(tmp_path):
    folder = make(tmp_path / "pkg", pkg("foo", "1-1"))
    victim = folder / pkg("foo", "1-1")
    st = os.stat(victim)
    entry = cacheclean.Entry(str(folder), victim.name, "foo", "1-1", "x86_64", st.st_size, False, st.st_dev, st.st_ino)
    moved = tmp_path / "moved"
    victim.rename(moved)
    victim.symlink_to(moved)  # a link to the very file that was listed (same device and inode through the link)
    result = cacheclean.delete([entry], {entry.key})
    assert moved.exists() and result["removed"] == 0 and victim.is_symlink()


@pytest.mark.parametrize("database", ["custom.db", "custom.db.tar.zst", "custom.files", "custom.files.tar.gz", "custom.db.tar.zst.sig", "aur.db.tar"])
def test_a_folder_that_is_a_local_repository_is_left_alone_and_said_so(tmp_path, database):
    repo = make(tmp_path / "localrepo", pkg("mine", "1-1"), pkg("mine", "2-1"), pkg("mine", "3-1"), database)
    other = make(tmp_path / "cache", pkg("foo", "1-1"), pkg("foo", "2-1"))
    chosen, problems, looked = cacheclean.selection(config(repo, other), 0, True, worker=Worker({}))
    assert sorted(e.filename for e in chosen) == [pkg("foo", "1-1"), pkg("foo", "2-1")]  # the cache folder is cleaned; the repository's packages stay
    assert len(problems) == 1 and "local repository" in problems[0] and str(repo) in problems[0] and database in problems[0]
    assert looked == 2 and all(e.folder == str(other) for e in chosen)



def test_a_folder_on_the_way_that_someone_else_can_change_is_not_trusted(tmp_path):
    outer = tmp_path / "outer"
    folder = make(outer / "pkg", pkg("foo", "1-1"), pkg("foo", "2-1"))
    assert doomed(folder, {"foo": "2-1"}, keep=0)[0] == [pkg("foo", "1-1")]
    os.chmod(outer, 0o777)  # anyone could put another folder or a link in its place
    gone, problems = doomed(folder, {"foo": "2-1"}, keep=0)
    assert gone == [] and "on the way" in problems[0]
    os.chmod(outer, 0o1777)  # sticky (like /tmp): others cannot replace what is in it
    assert doomed(folder, {"foo": "2-1"}, keep=0)[0] == [pkg("foo", "1-1")]
    os.chmod(outer, 0o775)  # the group can write
    assert doomed(folder, {"foo": "2-1"}, keep=0)[0] == []


def test_a_link_anywhere_on_the_path_is_refused_not_only_at_the_end(tmp_path):
    real = make(tmp_path / "real" / "deeper" / "pkg", pkg("foo", "1-1"))
    (tmp_path / "shortcut").symlink_to(tmp_path / "real")
    with pytest.raises(cacheclean.CacheRefused, match="link anywhere"):
        cacheclean.open_folder(str(tmp_path / "shortcut" / "deeper" / "pkg"))
    fd = cacheclean.open_folder(str(real))
    os.close(fd)


def test_a_folder_others_cannot_look_into_is_not_planned_for_them_but_is_for_root_and_for_the_person_themselves(tmp_path):
    folder = make(tmp_path / "pkg", pkg("foo", "1-1"), pkg("foo", "2-1"))
    os.chmod(folder, 0o750)
    for viewer in (None, 0):
        chosen, problems, _ = cacheclean.selection(config(folder), 0, False, worker=Worker({"foo": "2-1"}), viewer_uid=viewer)
        assert [e.filename for e in chosen] == [pkg("foo", "1-1")] and problems == []
    chosen, problems, _ = cacheclean.selection(config(folder), 0, False, worker=Worker({"foo": "2-1"}), viewer_uid=1000)
    assert chosen == [] and "others cannot look into" in problems[0]


@pytest.mark.parametrize("name", [pkg("foo", "1-1") + "\n", pkg("foo", "1-1") + ".sig\n", "foo-1-1\n-x86_64.pkg.tar.zst", "foo-1\n-1-x86_64.pkg.tar.zst"])
def test_a_name_with_a_newline_in_it_is_not_a_package_file(name):
    assert pkgcache.parse_filename(name) is None and pkgcache.parse_filename(name[:-4] if name.endswith(".sig\n") else name) is None


def test_a_version_with_a_newline_is_not_a_version():
    assert pkgcache._VERSION.match("1-1\n") is None and pkgcache._VERSION.match("1-1") is not None


def test_a_folder_with_too_many_entries_is_left_alone_whole_and_said_so(tmp_path, monkeypatch):
    folder = make(tmp_path / "pkg", *[pkg("foo", f"{i}-1") for i in range(8)])
    monkeypatch.setattr(cacheclean, "MAX_ENTRIES", 5)
    chosen, problems, looked = cacheclean.selection(config(folder), 0, True, worker=Worker({}))
    assert chosen == [] and looked == 0 and len(problems) == 1 and "too many to judge" in problems[0]
    monkeypatch.setattr(cacheclean, "MAX_ENTRIES", 7)  # more entries than the limit, by one
    assert cacheclean.selection(config(folder), 0, True, worker=Worker({}))[0] == []
    monkeypatch.setattr(cacheclean, "MAX_ENTRIES", 8)  # exactly the limit is fine: all of it was seen
    assert len(cacheclean.selection(config(folder), 0, True, worker=Worker({}))[0]) == 8


def test_the_repository_rule_cannot_be_dodged_by_where_the_database_comes_in_the_listing(tmp_path, monkeypatch):
    # a database that comes late in the listing (past what a cap would have looked at) still leaves the whole folder alone
    for first in (True, False):
        folder = make(tmp_path / f"repo{first}", *[pkg("mine", f"{i}-1") for i in range(6)])
        if first:
            (folder / "mine.db").write_bytes(b"db")
        monkeypatch.setattr(cacheclean, "MAX_ENTRIES", 4)
        assert cacheclean.selection(config(folder), 0, True, worker=Worker({}))[0] == []
        monkeypatch.setattr(cacheclean, "MAX_ENTRIES", 1000)
        (folder / "mine.db").write_bytes(b"db")
        assert cacheclean.selection(config(folder), 0, True, worker=Worker({}))[0] == []


def test_the_limit_on_entries_is_under_what_the_worker_will_order_in_one_question():
    from cygnus.core.backends import alpm_worker

    assert cacheclean.MAX_ENTRIES <= alpm_worker.MAX_VERSIONS  # so a folder that is judged can always be ordered


def test_removal_goes_through_the_descriptor_that_was_checked_not_through_the_path(tmp_path, monkeypatch):
    chosen_folder = make(tmp_path / "pkg", pkg("foo", "1-1"), pkg("foo", "2-1"))
    chosen, allowed = chosen_and_allowed(chosen_folder, {"foo": "2-1"})
    decoy = make(tmp_path / "decoy", pkg("foo", "1-1"), pkg("foo", "2-1"))
    moved = tmp_path / "moved"
    real_stat = os.stat
    swapped = []

    def swapping_stat(path, *args, **kwargs):
        if not swapped and kwargs.get("dir_fd") is not None and str(path).endswith(".pkg.tar.zst"):
            swapped.append(True)
            chosen_folder.rename(moved)  # the folder is swapped for a link to somewhere else, after it was opened
            chosen_folder.symlink_to(decoy)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", swapping_stat)
    result = cacheclean.delete(chosen, allowed)
    assert swapped and result["removed"] == 1
    assert not (moved / pkg("foo", "1-1")).exists() and (moved / pkg("foo", "2-1")).exists()  # the folder that was opened lost its file
    assert (decoy / pkg("foo", "1-1")).exists() and (decoy / pkg("foo", "2-1")).exists()  # the place the path now leads to is untouched
