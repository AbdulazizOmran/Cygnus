"""Libraries a package needs: which ones really block it, and how a missing one is found (Chrome's Qt 5 case).

The files are real compiled programs and libraries, because what is under test is what `readelf` says about them."""

import subprocess

import pytest

import builders
from cygnus.core.backends import filedb, foreign
from cygnus.core.detect import detect_file
from cygnus.core.errors import CygnusError
from cygnus.core.ops import convert

pytestmark = pytest.mark.needs_tool("gcc", "readelf", "bsdtar")

HOST = {"libc.so.6"}  # what "this computer" has: nothing else, so libghost is missing


@pytest.fixture(scope="module")
def elf(tmp_path_factory):
    d = tmp_path_factory.mktemp("elf")

    def cc(*args):
        subprocess.run(["gcc", *args], cwd=d, check=True, capture_output=True)

    (d / "main.c").write_text("int main(void){return 0;}\n")
    (d / "lib.c").write_text("int f(void){return 1;}\n")
    cc("-shared", "-fPIC", "-Wl,-soname,libghost.so.5", "-o", "libghost.so.5", "lib.c")
    cc("-shared", "-fPIC", "-Wl,-soname,libghost2.so.3", "-o", "libghost2.so.3", "lib.c")
    cc("-o", "plain", "main.c")  # a program that needs only libc
    cc("-o", "needs_ghost", "main.c", "-L.", "-l:libghost.so.5")  # a program that cannot start without libghost
    cc("-shared", "-fPIC", "-o", "libshim.so", "lib.c", "-L.", "-l:libghost.so.5")  # loaded on demand, wants libghost
    cc("-shared", "-fPIC", "-Wl,-soname,libbundled.so.1", "-o", "libbundled.so.1.0", "lib.c", "-L.", "-l:libghost2.so.3")
    cc("-o", "uses_bundled", "main.c", "-L.", "-l:libbundled.so.1.0")  # program -> bundled library -> missing library
    return {name: (d / name).read_bytes() for name in
            ("plain", "needs_ghost", "libshim.so", "libbundled.so.1.0", "uses_bundled")}


def _deb(tmp_path, files):
    # a few files outside the program's own folder, as a real package has (menu entry, icon, docs): otherwise the
    # analysis rightly calls it a self-contained application that is extracted rather than converted
    spread = {f"usr/share/hello/{n}.txt": b"x\n" for n in "abc"}
    return detect_file(str(builders.build_deb(tmp_path, files={**files, **spread})))


def _satisfy(provided=()):
    """The repositories: they offer the packages named in `provided` (as repo packages), and nothing by provides."""
    return lambda deps: {d: {"installed": None, "repo": {"name": d, "version": "1", "repo": "extra"}} if d in provided else
                         {"installed": None, "repo": None} for d in deps}


def _analyse(cand, **kw):
    return foreign.analyse(cand, sonames_on_host=HOST, glibc_on_host=(2, 40), **kw)


def _codes(verdict):
    return {i.code: i.severity.value for i in verdict.issues}


def test_chromes_case_a_library_only_an_on_demand_part_wants_is_optional(tmp_path, elf):
    cand = _deb(tmp_path, {"opt/hello/hello": elf["plain"], "opt/hello/libshim.so": elf["libshim.so"]})
    verdict = _analyse(cand, satisfy=_satisfy({"ghost-pkg"}), locate=lambda s: {"libghost.so.5": "ghost-pkg"})
    assert verdict.strategy in ("convert", "portable") and "LIB_SONAME_MISSING" not in _codes(verdict)
    assert verdict.repo_dependencies == []
    assert verdict.optional_dependencies == [{"package": "ghost-pkg", "libraries": ["libghost.so.5"],
                                              "files": ["opt/hello/libshim.so"]}]


def test_an_optional_library_nobody_provides_is_only_a_notice(tmp_path, elf):
    cand = _deb(tmp_path, {"opt/hello/hello": elf["plain"], "opt/hello/libshim.so": elf["libshim.so"]})
    verdict = _analyse(cand, satisfy=_satisfy(), locate=lambda s: {})
    assert _codes(verdict).get("LIB_OPTIONAL_MISSING") == "notice" and "LIB_SONAME_MISSING" not in _codes(verdict)
    assert verdict.strategy != "refuse" and verdict.optional_dependencies == []
    [issue] = [i for i in verdict.issues if i.code == "LIB_OPTIONAL_MISSING"]
    assert "libshim.so" in issue.explanation and "libghost.so.5" in issue.explanation


def test_a_library_the_program_itself_needs_still_blocks(tmp_path, elf):
    cand = _deb(tmp_path, {"opt/hello/hello": elf["needs_ghost"]})
    verdict = _analyse(cand, satisfy=_satisfy(), locate=lambda s: {})
    assert _codes(verdict)["LIB_SONAME_MISSING"] == "blocker" and verdict.strategy == "refuse"
    assert verdict.unresolved_sonames == ["libghost.so.5"]


def test_a_library_the_program_needs_is_installed_from_the_repositories_when_they_have_it(tmp_path, elf):
    cand = _deb(tmp_path, {"opt/hello/hello": elf["needs_ghost"]})
    verdict = _analyse(cand, satisfy=_satisfy({"ghost-pkg"}), locate=lambda s: {"libghost.so.5": "ghost-pkg"})
    assert verdict.repo_dependencies == ["ghost-pkg"] and verdict.optional_dependencies == []
    assert "LIB_SONAME_MISSING" not in _codes(verdict)


def test_a_library_reached_through_a_bundled_library_is_required(tmp_path, elf):
    files = {"opt/hello/hello": elf["uses_bundled"], "opt/hello/libbundled.so.1.0": elf["libbundled.so.1.0"]}
    verdict = _analyse(_deb(tmp_path, files), satisfy=_satisfy(), locate=lambda s: {})
    assert _codes(verdict)["LIB_SONAME_MISSING"] == "blocker" and verdict.unresolved_sonames == ["libghost2.so.3"]


def test_a_package_with_no_program_of_its_own_gets_no_leniency(tmp_path, elf):
    verdict = _analyse(_deb(tmp_path, {"usr/lib/hello/libshim.so": elf["libshim.so"]}), satisfy=_satisfy(),
                       locate=lambda s: {})
    assert _codes(verdict)["LIB_SONAME_MISSING"] == "blocker"


def test_the_cheap_lookup_by_declared_provides_comes_first(tmp_path, elf):
    cand = _deb(tmp_path, {"opt/hello/hello": elf["needs_ghost"]})

    def satisfy(deps):  # the repositories declare libghost.so=5-64 as provided by ghost-pkg
        return {d: {"installed": None, "repo": {"name": "ghost-pkg"} if d == "libghost.so=5-64" else None} for d in deps}

    verdict = _analyse(cand, satisfy=satisfy, locate=lambda s: pytest.fail("the file lists are not needed here"))
    assert verdict.repo_dependencies == ["ghost-pkg"]


def test_a_package_name_the_repositories_do_not_offer_is_never_trusted(tmp_path, elf):
    cand = _deb(tmp_path, {"opt/hello/hello": elf["needs_ghost"]})
    verdict = _analyse(cand, satisfy=_satisfy(), locate=lambda s: {"libghost.so.5": "invented-package"})
    assert verdict.unresolved_sonames == ["libghost.so.5"] and verdict.repo_dependencies == []


def test_when_the_file_lists_cannot_be_had_the_reason_is_shown(tmp_path, elf):
    def locate(_):
        raise CygnusError("could not download the package file lists: offline")

    verdict = _analyse(_deb(tmp_path, {"opt/hello/hello": elf["needs_ghost"]}), satisfy=_satisfy(), locate=locate)
    [issue] = [i for i in verdict.issues if i.code == "LIB_SONAME_MISSING"]
    assert "Looking for a provider failed: could not download the package file lists: offline" in issue.explanation


def test_a_package_already_needed_is_not_also_offered_as_optional(tmp_path, elf):
    files = {"opt/hello/hello": elf["needs_ghost"], "opt/hello/libshim.so": elf["libshim.so"]}
    verdict = _analyse(_deb(tmp_path, files), satisfy=_satisfy({"ghost-pkg"}),
                       locate=lambda s: {"libghost.so.5": "ghost-pkg"})
    assert verdict.repo_dependencies == ["ghost-pkg"] and verdict.optional_dependencies == []


def test_when_not_every_file_could_be_checked_nothing_is_called_optional():
    pa = foreign.PayloadAnalysis(programs={"opt/x/prog"}, elf_unchecked=3, needed={"libq.so.5": ["opt/x/libshim.so"]})
    assert pa.split_missing(["libq.so.5"]) == (["libq.so.5"], {})


# -- converting: optional libraries become real dependencies only when asked for ---------------------------------
@pytest.mark.needs_tool("makepkg", "fakeroot", "zstd")
@pytest.mark.parametrize("chosen, depend, optdepend", [((), False, True), (("ghost-pkg",), True, False)])
def test_a_converted_package_lists_the_optional_library_as_optional_or_as_needed(tmp_path, elf, chosen, depend, optdepend):
    cand = _deb(tmp_path, {"opt/hello/hello": elf["plain"], "opt/hello/libshim.so": elf["libshim.so"]})
    verdict = _analyse(cand, satisfy=_satisfy({"ghost-pkg"}), locate=lambda s: {"libghost.so.5": "ghost-pkg"})
    pkg, _ = convert.convert(cand, verdict, tmp_path / "out", install_optional=chosen)
    info = subprocess.run(["bsdtar", "-xOf", str(pkg), ".PKGINFO"], capture_output=True, text=True).stdout.splitlines()
    assert ("depend = ghost-pkg" in info) is depend
    assert any(line.startswith("optdepend = ghost-pkg: needed by libshim.so") for line in info) is optdepend


def test_conversion_refuses_an_optional_library_the_analysis_did_not_offer(tmp_path, elf):
    cand = _deb(tmp_path, {"opt/hello/hello": elf["plain"], "opt/hello/libshim.so": elf["libshim.so"]})
    verdict = _analyse(cand, satisfy=_satisfy({"ghost-pkg"}), locate=lambda s: {"libghost.so.5": "ghost-pkg"})
    with pytest.raises(CygnusError, match="not an optional library of this package"):
        convert.convert(cand, verdict, tmp_path / "out", install_optional=["something-else"])


# -- the file lists ------------------------------------------------------------------------------------------------
class Cfg:
    repos = ("cachyos", "extra")


class Runner:
    def __init__(self, listing="", returncode=0, refresh_returncode=0, refresh_writes=(),
                 refresh_error="error: failed retrieving file 'x.files'"):
        self.calls, self.listing, self.returncode, self.refresh_error = [], listing, returncode, refresh_error
        self.refresh_returncode, self.refresh_writes = refresh_returncode, refresh_writes

    def __call__(self, argv, **kw):
        from types import SimpleNamespace as NS

        self.calls.append(argv)
        if "-Fy" in argv:
            for f in self.refresh_writes:
                f.write_text("x")
            return NS(returncode=self.refresh_returncode, stdout="", stderr=self.refresh_error)
        return NS(returncode=self.returncode, stdout=self.listing, stderr="")


@pytest.fixture
def db(tmp_path, monkeypatch):
    from cygnus.core import updates

    folder = tmp_path / "db"
    (folder / "sync").mkdir(parents=True)
    monkeypatch.setattr(updates, "private_db", lambda cfg: folder)
    monkeypatch.setattr(filedb.proc, "which", lambda name: "/usr/bin/fakeroot")
    return folder


def _fresh_lists(db):
    """Lists that were fetched just now, with the server's (old) times on the files, as libalpm leaves them."""
    import os
    import time

    for name in ("cachyos", "extra"):
        f = db / "sync" / f"{name}.files"
        f.write_text("x")
        os.utime(f, (time.time() - 30 * 86400,) * 2)
    (db / filedb.STAMP).write_text("1")


def _listing(*rows):
    return "\n".join("\0".join(r) for r in rows) + "\n"


def test_only_the_library_in_usr_lib_counts_not_copies_bundled_by_other_packages(db):
    _fresh_lists(db)
    run = Runner(_listing(("cachyos", "davinci-resolve", "21", "opt/resolve/libs/libQt5Core.so.5"),
                          ("cachyos-extra-v3", "qt5-base", "5.15", "usr/lib/libQt5Core.so.5"),
                          ("extra", "qt5-base", "5.15", "usr/lib/libQt5Core.so.5"),
                          ("extra", "other", "1", "usr/lib32/libQt5Core.so.5")))
    assert filedb.locate(["libQt5Core.so.5"], run=run, cfg=Cfg()) == {"libQt5Core.so.5": "qt5-base"}
    assert len(run.calls) == 1  # fresh file lists: nothing is downloaded


def test_a_fresh_download_is_not_repeated_however_old_the_servers_dates_on_the_files_are(db):
    # libalpm gives a downloaded list the server's modification time: judging by it would refetch ~80 MB for nothing
    _fresh_lists(db)
    run = Runner(_listing())
    filedb.locate(["libfoo.so.1"], run=run, cfg=Cfg())
    assert all("-Fy" not in call for call in run.calls)


def test_missing_or_stale_file_lists_are_downloaded_first_and_told_to_the_user(db):
    import os
    import time

    said = []
    run = Runner(_listing(("extra", "qt5-base", "5.15", "usr/lib/libQt5Core.so.5")),
                 refresh_writes=[db / "sync" / "extra.files"])
    filedb.locate(["libQt5Core.so.5"], run=run, cfg=Cfg(), progress=said.append)
    assert "-Fy" in run.calls[0] and "fakeroot" in run.calls[0][0] and "--dbpath" in run.calls[0]
    assert run.calls[1][:3] == ["pacman", "--dbpath", str(db)] and said and "about 80 MB" in said[0]
    assert (db / filedb.STAMP).exists()  # remembered by our own stamp
    stamp = db / filedb.STAMP
    os.utime(stamp, (time.time() - 8 * 86400,) * 2)
    run2 = Runner(_listing())
    filedb.locate(["libQt5Core.so.5"], run=run2, cfg=Cfg())
    assert "-Fy" in run2.calls[0]  # a week old by the stamp: refreshed


def test_a_repository_without_a_file_list_does_not_spoil_the_search(db):
    # pacman reports an error for the repository that has none, but fetched the others
    run = Runner(_listing(("extra", "qt5-base", "5.15", "usr/lib/libQt5Core.so.5")), refresh_returncode=1,
                 refresh_writes=[db / "sync" / "extra.files"])
    assert filedb.locate(["libQt5Core.so.5"], run=run, cfg=Cfg()) == {"libQt5Core.so.5": "qt5-base"}
    assert (db / filedb.STAMP).exists()
    run2 = Runner(_listing())
    filedb.locate(["libfoo.so.1"], run=run2, cfg=Cfg())
    assert all("-Fy" not in call for call in run2.calls)  # not retried at every analysis


def test_when_no_file_list_could_be_fetched_at_all_it_is_an_error(db):
    with pytest.raises(CygnusError, match="could not download the package file lists"):
        filedb.locate(["libfoo.so.1"], run=Runner(refresh_returncode=1), cfg=Cfg())
    assert not (db / filedb.STAMP).exists()


@pytest.mark.parametrize("name", ["--root=/tmp/x", "-Fy", "libfoo.so.1; rm", "../../etc/passwd", "lib foo.so.1", "", "x" * 300 + ".so.1"])
def test_a_name_that_is_not_a_library_name_never_reaches_pacman(db, name):
    run = Runner()
    assert filedb.locate([name], run=run, cfg=Cfg()) == {} and run.calls == []


def test_the_names_are_passed_after_a_double_dash(db):
    _fresh_lists(db)
    run = Runner()
    filedb.locate(["libfoo.so.1", "libbar.so.2"], run=run, cfg=Cfg())
    assert run.calls[0][-3:] == ["--", "libbar.so.2", "libfoo.so.1"]


def test_a_failed_search_is_an_error_not_an_empty_answer(db):
    _fresh_lists(db)
    with pytest.raises(CygnusError, match="could not search"):
        filedb.locate(["libfoo.so.1"], run=Runner(returncode=2), cfg=Cfg())


# -- a lock left by a killed pacman in Cygnus's private database -----------------------------------------------------
def test_a_lock_nobody_holds_is_removed_from_the_private_database(tmp_path):
    from cygnus.core import updates

    folder = tmp_path / "pacman-db"
    folder.mkdir()
    (folder / "db.lck").write_text("")
    updates.clear_stale_lock(folder)
    assert not (folder / "db.lck").exists()
    updates.clear_stale_lock(folder)  # nothing there: nothing to do


def test_a_lock_held_by_a_running_pacman_is_left_alone(tmp_path):
    import subprocess
    import sys

    from cygnus.core import updates

    folder = tmp_path / "pacman-db"
    folder.mkdir()
    (folder / "db.lck").write_text("")
    busy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "pacman", str(folder)])
    try:
        updates.clear_stale_lock(folder)
        assert (folder / "db.lck").exists()
    finally:
        busy.kill()
        busy.wait()
    updates.clear_stale_lock(folder)
    assert not (folder / "db.lck").exists()


def _tree(tmp_path, files, links=()):
    for rel, data in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)
    for rel, target in links:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).symlink_to(target)
    return foreign.analyse_payload(tmp_path)


def test_a_dangling_link_or_a_text_file_with_a_library_name_is_not_a_bundled_library(tmp_path, elf):
    pa = _tree(tmp_path, {"opt/hello/hello": elf["needs_ghost"], "opt/hello/libtext.so.2": b"/* a linker script, not a library */\n" * 4},
               links=[("opt/hello/libghost.so.5", "libghost.so.5.0")])  # leads to a file the package does not have
    assert "libghost.so.5" not in pa.bundled_sonames and "libtext.so.2" not in pa.bundled_sonames


def test_a_link_to_a_real_library_of_the_package_is_a_bundled_library_through_links_and_absolute_targets(tmp_path, elf):
    pa = _tree(tmp_path, {"opt/hello/hello": elf["uses_bundled"], "opt/hello/libbundled.so.1.0": elf["libbundled.so.1.0"]},
               links=[("opt/hello/libmid.so.1", "libbundled.so.1.0"), ("opt/hello/libtop.so.1", "/opt/hello/libmid.so.1")])
    assert {"libbundled.so.1", "libmid.so.1", "libtop.so.1"} <= pa.bundled_sonames
    assert {"libmid.so.1", "libtop.so.1"} <= pa.elf_names["opt/hello/libbundled.so.1.0"]


def test_a_library_reached_only_through_an_absolute_link_still_passes_its_own_needs_on(tmp_path, elf):
    # the program names libbundled.so.1.0 through an absolute link: its own missing library is required, not optional
    pa = _tree(tmp_path, {"opt/hello/hello": elf["uses_bundled"], "opt/hello/libreal.so.9": elf["libbundled.so.1.0"]},
               links=[("opt/hello/libbundled.so.1.0", "/opt/hello/libreal.so.9")])
    required, optional = pa.split_missing(["libghost2.so.3"])
    assert required == ["libghost2.so.3"] and optional == {}


def test_old_lists_are_not_called_fresh_when_the_download_failed_because_of_the_network(db):
    import os
    import time

    old = db / "sync" / "extra.files"
    old.write_text("x")
    stamp = db / filedb.STAMP
    stamp.write_text("1")
    os.utime(stamp, (time.time() - 30 * 86400,) * 2)
    run = Runner(_listing(("extra", "qt5-base", "5.15", "usr/lib/libQt5Core.so.5")), refresh_returncode=1,
                 refresh_error="error: failed retrieving file 'extra.files' from mirror : Could not resolve host: mirror")
    assert filedb.locate(["libQt5Core.so.5"], run=run, cfg=Cfg()) == {"libQt5Core.so.5": "qt5-base"}  # the old lists still serve
    assert time.time() - stamp.stat().st_mtime > 29 * 86400  # but the next analysis tries to renew them
    run2 = Runner(_listing())
    filedb.locate(["libfoo.so.1"], run=run2, cfg=Cfg())
    assert "-Fy" in run2.calls[0]


def test_lists_fetched_with_errors_are_used_but_looked_at_again_after_a_day(db):
    import os
    import time

    run = Runner(_listing(("extra", "qt5-base", "5.15", "usr/lib/libQt5Core.so.5")), refresh_returncode=1,
                 refresh_error="error: failed retrieving file 'extra.files' from mirror : The requested URL returned error: 404",
                 refresh_writes=[db / "sync" / "extra.files"])
    assert filedb.locate(["libQt5Core.so.5"], run=run, cfg=Cfg()) == {"libQt5Core.so.5": "qt5-base"}
    stamp = db / filedb.STAMP
    age = time.time() - stamp.stat().st_mtime
    assert filedb.MAX_AGE - filedb.RETRY_AFTER - 60 < age < filedb.MAX_AGE - filedb.RETRY_AFTER + 60  # one day left, not a week
    os.utime(stamp, (time.time() - filedb.MAX_AGE - 3600,) * 2)  # the day has passed: the stamp is now over a week old
    run2 = Runner(_listing())
    filedb.locate(["libfoo.so.1"], run=run2, cfg=Cfg())
    assert "-Fy" in run2.calls[0]


def test_a_library_found_through_a_link_to_a_folder_never_leaves_the_package(tmp_path, elf):
    outside = tmp_path / "host"
    outside.mkdir()
    (outside / "libz.so.1").write_bytes(elf["libshim.so"])  # a real library, but on the "host", not in the package
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    pa = _tree(pkg, {"opt/hello/hello": elf["needs_ghost"]},
               links=[("opt/hello/lib", str(outside)), ("usr/lib/libz.so.1", "../../opt/hello/lib/libz.so.1")])
    assert "libz.so.1" not in pa.bundled_sonames
    assert foreign._resolved_file(pkg, "usr/lib/libz.so.1") is None


def test_a_chain_of_links_inside_the_package_ends_at_its_file(tmp_path, elf):
    pa = _tree(tmp_path, {"opt/hello/hello": elf["plain"], "opt/hello/real/libx.so.1.2": elf["libbundled.so.1.0"]},
               links=[("opt/hello/lib", "real"), ("usr/lib/libx.so.1", "/opt/hello/lib/libx.so.1.2")])
    assert foreign._resolved_file(tmp_path, "usr/lib/libx.so.1") == "opt/hello/real/libx.so.1.2"
    assert foreign._resolved_file(tmp_path, "usr/lib/missing.so") is None
