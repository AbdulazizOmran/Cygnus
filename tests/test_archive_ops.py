"""Installing a program from an archive: unpacked carefully onto the chosen drive, integrated, recorded, and removed only when it is
really the folder Cygnus made. Everything happens in scratch folders; the program inside is never run."""

import json
import os
import stat
from pathlib import Path

import pytest

from cygnus.core.desktop import integrate
from cygnus.core.detect import detect_file
from cygnus.core.errors import CygnusError
from cygnus.core.ops import archive_ops as ops
from cygnus.core.ops import executor_for
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation
from tests import builders as b

HDD = StorageLocation(id="hdd", label="HDD", fs_uuid="ABCD", fs_type="ext4", location_class="user-owned")


@pytest.fixture
def env(tmp_path):
    reg = open_registry(":memory:")
    reg.add_location(StorageLocation(id="hdd", label="HDD", fs_uuid="ABCD", fs_type="ext4", location_class="user-owned", canonical_mount=str(tmp_path)))
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    return reg, tmp_path, downloads


def program(tmp_path, entries=None, name="Discord.tar.gz", compression="gz"):
    downloads = tmp_path / "downloads"
    downloads.mkdir(exist_ok=True)
    return detect_file(b.build_tarball(downloads / name, entries or b.portable_app_entries(), compression=compression))


def install(reg, tmp_path, cand, main=None):
    apps = tmp_path / "Applications"
    steps = ops.plan_install(cand, app_key=ops.app_key_for(cand), location=HDD, apps_dir=apps, main=main or cand.metadata["main"])
    return ops.executor(reg).run("install", steps), apps


def files_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink()}


def test_the_program_is_unpacked_integrated_and_recorded_and_its_scripts_are_never_run(env):
    reg, tmp, _ = env
    sentinel = tmp / "SENTINEL"
    entries = b.portable_app_entries(extra=[])
    entries = [(n, k, (p.replace("systemctl reload apparmor", f"touch {sentinel}") if isinstance(p, str) else p), m) for n, k, p, m in entries]
    cand = program(tmp, entries)
    report, apps = install(reg, tmp, cand)
    assert report.state == "succeeded", report.error
    folder = apps / "Discord"
    assert {p.name for p in folder.iterdir()} == {"discord", "updater_bootstrap", "discord.desktop", "postinst.sh", "discord.png", ops.MARKER}
    assert not sentinel.exists()  # postinst.sh is there, listed, and never run
    assert (folder / "discord").stat().st_mode & 0o7777 == 0o755 and (folder / "discord.png").stat().st_mode & 0o7777 == 0o644
    [inst] = regdb.list_installations(reg)
    assert inst["format"] == "archive" and inst["name"] == "Discord" and inst["source"]["path"] == str(folder) and inst["source"]["main"] == "discord"
    entry = integrate.xdg_data_home() / "applications/discord.desktop"
    text = entry.read_text()
    assert "X-Cygnus-Managed=true" in text and "launch/archive.discord" in text and "/usr/bin" not in text and "Path=" not in text
    assert "--url -- %u" in text and "MimeType=x-scheme-handler/discord;" in text
    shim = integrate.shim_dir() / "archive.discord"
    assert os.access(shim, os.X_OK) and f"TARGET='{folder}/discord'" in shim.read_text() and "DRIVE_UUID='ABCD'" in shim.read_text()
    assert (integrate.xdg_data_home() / "icons/hicolor/64x64/apps/discord.png").is_file()
    marker = json.loads((folder / ops.MARKER).read_text())
    assert marker["installation"] == inst["id"] and marker["files"] == 5 and marker["archive_sha256"] == inst["source"]["archive_sha256"]
    assert {a["kind"] for a in regdb.artifacts_of(reg, inst["id"])} >= {"tree", "launcher_shim", "desktop_entry", "icon"}


def test_removing_the_program_keeps_or_deletes_its_folder_as_asked_and_the_folder_goes_last(env):
    reg, tmp, _ = env
    cand = program(tmp)
    _, apps = install(reg, tmp, cand)
    [inst] = regdb.list_installations(reg)
    keep = ops.plan_uninstall(reg, inst["id"], remove_payload=False)
    assert all(s.kind != "archive.remove_tree" for s in keep)
    assert ops.executor(reg).run("uninstall", keep).state == "succeeded"
    assert (apps / "Discord" / "discord").is_file() and regdb.list_installations(reg) == []
    assert not (integrate.xdg_data_home() / "applications/discord.desktop").exists() and not (integrate.shim_dir() / "archive.discord").exists()

    # install again from scratch and delete everything
    (apps / "Discord" / ops.MARKER).unlink()
    import shutil

    shutil.rmtree(apps / "Discord")
    _, apps = install(reg, tmp, cand)
    [inst] = regdb.list_installations(reg)
    steps = ops.plan_uninstall(reg, inst["id"], remove_payload=True)
    assert steps[-1].kind == "archive.remove_tree"  # irreversible, so after everything else
    assert ops.executor(reg).run("uninstall", steps).state == "succeeded"
    assert not (apps / "Discord").exists() and regdb.list_installations(reg) == []


@pytest.mark.parametrize("tamper", ["no marker", "another installation's marker", "a link instead of the folder", "the marker is a link", "the marker is junk",
                                    "the marker is a folder"])
def test_a_folder_that_is_not_the_one_cygnus_made_is_never_removed(env, tamper):
    reg, tmp, _ = env
    _, apps = install(reg, tmp, program(tmp))
    [inst] = regdb.list_installations(reg)
    folder = apps / "Discord"
    marker = folder / ops.MARKER
    original = marker.read_text()
    precious = tmp / "precious"
    precious.mkdir()
    (precious / "my-photos.jpg").write_bytes(b"irreplaceable")
    if tamper == "no marker":
        marker.unlink()
    elif tamper == "another installation's marker":
        marker.write_text(json.dumps({"installation": "someone-else"}))
    elif tamper == "a link instead of the folder":
        (precious / ops.MARKER).write_text(original)  # even with the right marker inside, a link is not the folder
        folder.rename(tmp / "moved")
        folder.symlink_to(precious)
    elif tamper == "the marker is a link":
        (tmp / "marker-elsewhere").write_text(original)
        marker.unlink()
        marker.symlink_to(tmp / "marker-elsewhere")
    elif tamper == "the marker is junk":
        marker.write_text("{not json")
    else:
        marker.unlink()
        marker.mkdir()
    report = ops.executor(reg).run("uninstall", ops.plan_uninstall(reg, inst["id"], remove_payload=True))
    assert report.state == "rolled_back"
    assert (precious / "my-photos.jpg").read_bytes() == b"irreplaceable"
    assert regdb.list_installations(reg)  # nothing was forgotten when the removal was refused
    assert (integrate.xdg_data_home() / "applications/discord.desktop").exists() and (integrate.shim_dir() / "archive.discord").exists()


def test_a_failed_install_leaves_nothing_behind(env):
    reg, tmp, _ = env
    cand = program(tmp)
    apps = tmp / "Applications"
    ex = ops.executor(reg)

    def boom(params):
        raise RuntimeError("the registry is broken")

    ex.handlers["registry.record"] = boom
    before = files_under(integrate.xdg_data_home().parent)
    report = ex.run("install", ops.plan_install(cand, app_key=ops.app_key_for(cand), location=HDD, apps_dir=apps, main="discord"))
    assert report.state == "rolled_back" and not report.compensation_errors
    assert {f for f in files_under(integrate.xdg_data_home().parent) if not f.endswith(".cache")} == before
    assert not (apps / "Discord").exists() and not list(apps.glob(".*cygnus*"))


def test_an_archive_that_changed_after_it_was_shown_is_not_unpacked(env):
    reg, tmp, _ = env
    cand = program(tmp)
    steps = ops.plan_install(cand, app_key="archive.discord", location=HDD, apps_dir=tmp / "Applications", main="discord")
    Path(cand.source).write_bytes(b.build_tarball(tmp / "other.tar.gz", [("x/evil", "file", b.ELF_STUB, 0o755)]).read_bytes())
    report = ops.executor(reg).run("install", steps)
    assert report.state == "rolled_back" and "changed" in (report.error or "")
    assert not (tmp / "Applications" / "Discord").exists()
    with pytest.raises(CygnusError, match="changed after Cygnus showed"):
        ops.plan_install(cand, app_key="x", location=HDD, apps_dir=tmp / "A", main="discord", sha256="0" * 64)


def test_a_main_program_that_is_not_in_the_archive_is_refused(env):
    reg, tmp, _ = env
    with pytest.raises(CygnusError, match="not a program"):
        ops.plan_install(program(tmp), app_key="x", location=HDD, apps_dir=tmp / "A", main="../../bin/sh")


def test_a_folder_that_is_already_there_and_not_ours_is_never_touched(env):
    reg, tmp, _ = env
    apps = tmp / "Applications"
    (apps / "Discord").mkdir(parents=True)
    (apps / "Discord" / "mine.txt").write_text("keep me")
    report, _ = install(reg, tmp, program(tmp))
    assert report.state == "rolled_back" and (apps / "Discord" / "mine.txt").read_text() == "keep me"
    assert regdb.list_installations(reg) == [] and not list(apps.glob(".*cygnus*"))


def test_a_drive_without_room_is_said_so_before_anything_is_written(env, monkeypatch):
    reg, tmp, _ = env
    real = ops.free_space_problem
    assert real(tmp, 10**18) and real(tmp / "does/not/exist/yet", 10**18) and real(tmp, 1000) is None
    monkeypatch.setattr(ops, "free_space_problem", lambda parent, needed: "not enough room: it needs about 5000 MB and only 10 MB are free")
    report, apps = install(reg, tmp, program(tmp))
    assert report.state == "rolled_back" and "not enough room" in (report.error or "")
    assert not (apps / "Discord").exists() and not list(apps.glob(".*cygnus*"))


# -- unpacking what is hostile ------------------------------------------------------------------------------------------------------
def unpack_into(tmp_path, entries, *, root="x", compression="gz"):
    arc = b.build_tarball(tmp_path / "evil.tar", entries, compression=compression)
    staging = tmp_path / "staging"
    staging.mkdir()
    return arc, staging


OUTSIDE = "outside-marker"


@pytest.mark.parametrize("name, entries", [
    ("an absolute path", [("x/ok", "file", "ok", 0o644), ("/tmp-evil-abs", "file", "x", 0o644)]),
    ("a climbing path", [("x/ok", "file", "ok", 0o644), ("x/../../evil", "file", "x", 0o644)]),
    ("a hard link", [("x/ok", "file", "ok", 0o644), ("x/h", "hardlink", "x/ok", 0o644)]),
    ("a device", [("x/ok", "file", "ok", 0o644), ("x/null", "chr", None, 0o666)]),
    ("a pipe", [("x/p", "fifo", None, 0o644)]),
    ("a link out", [("x/ok", "file", "ok", 0o644), ("x/l", "symlink", "../../../etc", 0o777)]),
    ("an absolute link", [("x/l", "symlink", "/etc", 0o777)]),
    ("a write through a link", [("x/l", "symlink", ".", 0o777), ("x/l/evil", "file", "x", 0o644)]),
    ("a duplicate", [("x/a", "file", "one", 0o644), ("x/a", "file", "two", 0o644)]),
    ("a file where a folder was", [("x/a", "dir", None, 0o755), ("x/a", "file", "two", 0o644)]),
    ("the marker name", [("x/.cygnus-installation", "file", "{}", 0o644)]),
    ("an entry outside the folder", [("x/ok", "file", "ok", 0o644), ("y/other", "file", "x", 0o644)]),
])
def test_unpacking_refuses_anything_unsafe_and_writes_nothing_outside_the_folder(tmp_path, name, entries):
    arc, staging = unpack_into(tmp_path, entries)
    before = {p for p in tmp_path.rglob("*") if p != arc}
    with pytest.raises(CygnusError):
        ops.unpack(arc, "gz", staging, root="x")
    after = {p for p in tmp_path.rglob("*") if p != arc and staging not in p.parents and p != staging}
    assert after == {p for p in before if staging not in p.parents and p != staging}  # not one new file anywhere but in the staging folder
    assert not Path("/tmp-evil-abs").exists() and not (tmp_path.parent / "evil").exists()


def test_a_link_that_only_leads_out_through_another_link_is_caught_on_the_unpacked_tree(tmp_path):
    entries = [("x/a", "symlink", "b", 0o777), ("x/b", "symlink", ".", 0o777), ("x/c", "symlink", "a/../..", 0o777)]
    arc, staging = unpack_into(tmp_path, entries)
    with pytest.raises(CygnusError, match="leads outside"):
        ops.unpack(arc, "gz", staging, root="x")


def test_unpacking_makes_every_file_plain_and_sets_the_marks_nobody_should_have_to_trust(tmp_path):
    entries = [("x", "dir", None, 0o700), ("x/bin", "dir", None, 0o500), ("x/bin/tool", "file", b.ELF_STUB, 0o4755), ("x/bin/sgid", "file", "x", 0o2755),
               ("x/open", "file", "x", 0o666), ("x/data/readme", "file", "hello", 0o600), ("x/lib", "dir", None, 0o755),
               ("x/lib/libz.so.1", "file", b.ELF_STUB, 0o755), ("x/lib/libz.so", "symlink", "libz.so.1", 0o777)]
    arc, staging = unpack_into(tmp_path, entries)
    info = ops.unpack(arc, "gz", staging, root="x")
    assert info["files"] == 5 and info["links"] == 1 and info["bytes"] > 0
    for path in staging.rglob("*"):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        assert not mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX), path
        assert not mode & 0o022, path
        assert (mode & 0o755) == 0o755 if path.is_dir() else (mode & 0o7777) in (0o755, 0o644), path
    assert (staging / "lib/libz.so").resolve() == (staging / "lib/libz.so.1").resolve()
    assert (staging / "bin/tool").stat().st_mode & 0o7777 == 0o755 and (staging / "open").stat().st_mode & 0o7777 == 0o644
    assert (staging / "data/readme").read_text() == "hello"  # a folder the archive never listed is made


def test_a_folder_the_archive_made_unreadable_is_enterable_afterwards(tmp_path):
    arc, staging = unpack_into(tmp_path, [("x/locked", "dir", None, 0o000), ("x/locked/f", "file", "x", 0o644)])
    ops.unpack(arc, "gz", staging, root="x")
    assert (staging / "locked").stat().st_mode & 0o777 == 0o755 and (staging / "locked/f").read_text() == "x"


def test_without_a_top_folder_everything_goes_straight_in_and_a_bomb_is_stopped(tmp_path, monkeypatch):
    arc, staging = unpack_into(tmp_path, [("a", "file", "1", 0o644), ("sub/b", "file", "2", 0o644)])
    ops.unpack(arc, "gz", staging, root="")
    assert (staging / "a").read_text() == "1" and (staging / "sub/b").read_text() == "2"
    bomb = b.build_tarball(tmp_path / "bomb.tar.gz", [("x/big", "file", b"\0" * 3_000_000, 0o644)])
    monkeypatch.setattr(ops.ad, "MAX_BYTES", 10_000)
    monkeypatch.setattr(ops.ad, "_STREAM_SLACK", 20_000)
    second = tmp_path / "staging2"
    second.mkdir()
    with pytest.raises(CygnusError, match="size Cygnus allows"):
        ops.unpack(bomb, "gz", second, root="x")


def test_a_damaged_archive_stops_the_unpacking_with_a_plain_message(tmp_path):
    good = b.build_tarball(tmp_path / "good.tar.xz", [("x/f", "file", bytes(range(256)) * 200, 0o644)], compression="xz")
    cut = tmp_path / "cut.tar.xz"
    cut.write_bytes(good.read_bytes()[:-40])
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(CygnusError, match="damaged"):
        ops.unpack(cut, "xz", staging, root="x")


def test_an_interrupted_unpacking_is_cleaned_up_by_the_abort_step_and_a_restart_replaces_the_leftover(env):
    reg, tmp, _ = env
    cand = program(tmp)
    apps = tmp / "Applications"
    steps = ops.plan_install(cand, app_key="archive.discord", location=HDD, apps_dir=apps, main="discord")
    extract = steps[0]
    iid = extract.params["installation_id"]
    staging = ops._staging_for(apps / "Discord", iid)
    staging.mkdir(parents=True)
    (staging / "half").write_text("a run that died")
    ops._extract_abort(extract.params)
    assert not staging.exists()
    staging.mkdir(parents=True)
    (staging / "half").write_text("a run that died")
    assert ops.executor(reg).run("install", steps).state == "succeeded"  # the leftover is replaced, not tripped over
    assert not staging.exists() and (apps / "Discord" / "discord").is_file()
    ops._extract_abort(extract.params)  # after a finished one: the folder with our marker goes too
    assert not (apps / "Discord").exists()
    other = apps / "Discord"
    other.mkdir()
    (other / "mine").write_text("not ours")
    ops._extract_abort(extract.params)
    assert (other / "mine").exists()


def test_the_unpacking_step_can_be_undone_and_finished_again_by_recovery(env):
    reg, tmp, _ = env
    for kind in ("archive.extract", "archive.extract.abort", "archive.remove_tree"):
        assert kind in executor_for(reg).handlers  # a journaled install can be finished or undone by `cygnus recover`


# -- what is in the folder now, against what was unpacked --------------------------------------------------------------------------------
def test_the_folder_is_described_as_changed_or_added_for_the_question_before_it_is_deleted(env):
    reg, tmp, _ = env
    _, apps = install(reg, tmp, program(tmp))
    [inst] = regdb.list_installations(reg)
    fresh = ops.info(reg, inst["id"])
    assert fresh["present"] and fresh["ours"] and fresh["files"] == 5 and (fresh["changed"], fresh["added"], fresh["removed"]) == (0, 0, 0)
    folder = apps / "Discord"
    (folder / "discord").write_text("#!/bin/sh\nexec something-else\n")
    (folder / "downloaded-update").write_text("the program updated itself")
    (folder / "discord.png").unlink()
    changed = ops.info(reg, inst["id"])
    assert (changed["changed"], changed["added"], changed["removed"]) == (1, 1, 1)
    folder_moved = tmp / "gone"
    folder.rename(folder_moved)
    assert ops.info(reg, inst["id"]) == {"path": str(folder), "present": False}
    assert ops.info(reg, "unknown") == {"path": "", "present": False}


def test_names_and_keys_are_made_safe(tmp_path):
    cand = program(tmp_path)
    assert ops.app_key_for(cand) == "archive.discord"
    assert ops.app_key_for(cand, "org.example.Thing") == "org.example.Thing"
    assert ops.folder_name("My App: the <best>/one") == "My App_ the _best__one" and ops.folder_name("..") == "Application" and ops.folder_name("") == "Application"
    assert len(ops.folder_name("x" * 300)) <= 80


def test_a_big_program_unpacks_in_reasonable_time(tmp_path):
    import time

    entries = [("big", "dir", None, 0o755)] + [(f"big/d{i % 40}/f{i}.dat", "file", b"x" * 40, 0o644) for i in range(20_000)] + [("big/run", "file", b.ELF_STUB, 0o755)]
    arc = b.build_tarball(tmp_path / "big.tar.gz", entries)
    staging = tmp_path / "staging"
    staging.mkdir()
    started = time.monotonic()
    info = ops.unpack(arc, "gz", staging, root="big")
    assert time.monotonic() - started < 30 and info["files"] == 20_001


def test_a_link_that_looks_inside_by_its_text_but_leads_out_through_another_link_is_caught_on_the_unpacked_tree(tmp_path):
    # x/f -> d/e/.. reads as staying inside, but d/e is itself a link to the folder above, so ".." from there leaves it
    entries = [("x/d", "dir", None, 0o755), ("x/d/e", "symlink", "..", 0o777), ("x/f", "symlink", "d/e/..", 0o777)]
    arc, staging = unpack_into(tmp_path, entries)
    with pytest.raises(CygnusError, match="leads outside the program's folder"):
        ops.unpack(arc, "gz", staging, root="x")


def test_the_step_refuses_an_archive_whose_contents_differ_from_what_was_shown_and_cleans_up(env):
    reg, tmp, _ = env
    cand = program(tmp)
    apps = tmp / "Applications"
    for change, message in (({"expect_files": 99}, "number of files differs"), ({"main": "no-such-program"}, "is not in it")):
        steps = ops.plan_install(cand, app_key="archive.discord", location=HDD, apps_dir=apps, main="discord")
        steps[0].params.update(change)
        report = ops.executor(reg).run("install", steps)
        assert report.state == "rolled_back" and message in (report.error or ""), change
        assert not (apps / "Discord").exists() and not list(apps.glob(".*cygnus*")) and regdb.list_installations(reg) == []


def test_an_existing_folder_is_refused_even_when_it_is_empty_and_so_is_a_link_in_its_place(env):
    reg, tmp, _ = env
    apps = tmp / "Applications"
    (apps / "Discord").mkdir(parents=True)  # empty: renaming onto it would quietly replace it
    report, _ = install(reg, tmp, program(tmp))
    assert report.state == "rolled_back" and "already exists" in (report.error or "") and list((apps / "Discord").iterdir()) == []
    (apps / "Discord").rmdir()
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    (apps / "Discord").symlink_to(elsewhere)
    report, _ = install(reg, tmp, program(tmp))
    assert report.state == "rolled_back" and "already exists" in (report.error or "") and (apps / "Discord").is_symlink() and list(elsewhere.iterdir()) == []


def test_removal_never_follows_a_link_even_when_the_folder_it_points_to_has_the_right_marker(env):
    reg, tmp, _ = env
    _, apps = install(reg, tmp, program(tmp))
    [inst] = regdb.list_installations(reg)
    real = apps / "Discord"
    moved = tmp / "moved"
    real.rename(moved)
    real.symlink_to(moved)  # the marker inside `moved` is genuine, and the path now leads there through a link
    with pytest.raises(CygnusError, match="not a plain folder"):
        ops._remove_tree({"path": str(real), "installation_id": inst["id"]})
    assert (moved / "discord").is_file() and real.is_symlink()


# -- what the second review found: removal and unpacking ------------------------------------------------------------------------------
MARKER_NAME = ops.MARKER


def fake_mounts(*points):
    from cygnus.core.storage.mountinfo import Mount

    return [Mount(mount_id=100 + i, parent_id=1, major=0, minor=0, root="/", mountpoint=str(p), options=frozenset(), fstype="ext4", source="x")
            for i, p in enumerate(points)]


def test_a_folder_with_something_mounted_inside_it_is_never_deleted_and_a_lookalike_name_is_not_a_mount_inside(env, monkeypatch):
    from cygnus.core.storage import mountinfo

    reg, tmp, _ = env
    _, apps = install(reg, tmp, program(tmp))
    [inst] = regdb.list_installations(reg)
    folder = (apps / "Discord").resolve()
    (folder / "photos").mkdir()
    (folder / "photos" / "mine.jpg").write_text("precious")  # what a bind mount of the person's own folder would show here
    before = files_under(folder)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mountinfo, "read", lambda: fake_mounts(folder / "photos"))
        with pytest.raises(CygnusError, match="mounted inside"):
            ops._remove_tree({"path": str(folder), "installation_id": inst["id"]})
        report = ops.executor(reg).run("uninstall", ops.plan_uninstall(reg, inst["id"], remove_payload=True))
        assert report.state == "rolled_back" and "mounted" in (report.error or "")
        mp.setattr(mountinfo, "read", lambda: fake_mounts(str(folder) + "-other", folder.parent))  # a name that merely starts alike, and the parent
        assert ops._mounts_inside(folder) == []
    assert files_under(folder) == before and (folder / "photos" / "mine.jpg").read_text() == "precious"


def test_a_folder_on_another_device_inside_the_program_folder_is_never_deleted(env, monkeypatch):
    reg, tmp, _ = env
    _, apps = install(reg, tmp, program(tmp))
    [inst] = regdb.list_installations(reg)
    folder = apps / "Discord"
    (folder / "other-drive").mkdir()
    (folder / "other-drive" / "f").write_text("x")
    real_lstat = os.lstat

    def lstat(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if str(path) != str(folder):
            return st
        fields = list(tuple(st)[:10])
        fields[2] += 1  # the device number
        return os.stat_result(fields)

    with pytest.MonkeyPatch.context() as mp:  # the program folder reads as being on one device and its sub folders on another
        mp.setattr(os, "lstat", lstat)
        with pytest.raises(CygnusError, match="mounted inside"):
            ops._remove_tree({"path": str(folder), "installation_id": inst["id"]})
    assert (folder / "other-drive" / "f").is_file() and (folder / MARKER_NAME).is_file()


def test_a_removal_that_stops_half_way_keeps_the_marker_so_it_can_be_finished_and_read_only_things_do_not_stop_it(env, monkeypatch):
    reg, tmp, _ = env
    _, apps = install(reg, tmp, program(tmp, [("D", "dir", None, 0o755), ("D/run", "file", b.ELF_STUB, 0o755), ("D/ro", "dir", None, 0o555),
                                              ("D/ro/f", "file", "x", 0o444), ("D/deep", "dir", None, 0o755), ("D/deep/g", "file", "y", 0o644)]))
    [inst] = regdb.list_installations(reg)
    folder = next(apps.iterdir())
    (folder / "ro").chmod(0o555)
    real_rmdir = os.rmdir

    def failing_rmdir(path, *a, **k):
        if str(path).endswith("/deep"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_rmdir(path, *a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "rmdir", failing_rmdir)
        with pytest.raises(CygnusError, match="partly removed"):
            ops._remove_tree({"path": str(folder), "installation_id": inst["id"]})
    assert (folder / MARKER_NAME).is_file() and ops.read_marker(folder)["installation"] == inst["id"]  # still ours: it can be finished
    assert not (folder / "run").exists() and not (folder / "ro" / "f").exists()  # the read-only folder did not stop the files going
    ops._remove_tree({"path": str(folder), "installation_id": inst["id"]})  # the second try finishes it
    assert not folder.exists()


def test_the_places_cygnus_will_never_remove_are_refused_even_with_a_marker_that_says_yes(env, monkeypatch):
    reg, tmp, _ = env
    _, apps = install(reg, tmp, program(tmp))
    [inst] = regdb.list_installations(reg)
    folder = apps / "Discord"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "home", classmethod(lambda cls: folder))  # as if the program had been put in the home folder itself
        with pytest.raises(CygnusError, match="not a place"):
            ops._remove_tree({"path": str(folder), "installation_id": inst["id"]})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ops, "read_marker", lambda p: {"installation": inst["id"]})
        for odd in ("/", "/tmp"):
            with pytest.raises(CygnusError, match="not a place"):
                ops._remove_tree({"path": odd, "installation_id": inst["id"]})
    assert (folder / "discord").is_file()


def test_a_marker_that_is_too_big_to_be_ours_is_not_read(env, monkeypatch):
    reg, tmp, _ = env
    install(reg, tmp, program(tmp))
    folder = tmp / "Applications" / "Discord"
    assert ops.read_marker(folder)["files"] > 0
    monkeypatch.setattr(ops, "MAX_MARKER_BYTES", 40)
    assert ops.read_marker(folder) is None


def test_what_changed_in_the_folder_is_judged_by_size_and_by_time_to_the_second(env):
    reg, tmp, _ = env
    install(reg, tmp, program(tmp, [("D", "dir", None, 0o755), ("D/run", "file", b.ELF_STUB, 0o755), ("D/a", "file", "aaaa", 0o644),
                                    ("D/b", "file", "bbbb", 0o644), ("D/c", "file", "cccc", 0o644), ("D/l", "symlink", "a", 0o777)]))
    folder = next((tmp / "Applications").iterdir())
    marker = ops.read_marker(folder)
    assert ops.tree_changes(folder, marker) == {"files": 5, "bytes": 3 * 4 + len(b.ELF_STUB), "changed": 0, "added": 0, "removed": 0}  # the link has no size
    stat_a = (folder / "a").stat()
    os.utime(folder / "a", ns=(stat_a.st_atime_ns, stat_a.st_mtime_ns + 1_000_000_000))  # a second off: a drive rounding times, not a change
    stat_b = (folder / "b").stat()
    (folder / "b").write_text("bbbbb")                                                    # another size, the very same time: changed
    os.utime(folder / "b", ns=(stat_b.st_atime_ns, stat_b.st_mtime_ns))
    stat_c = (folder / "c").stat()
    os.utime(folder / "c", ns=(stat_c.st_atime_ns, stat_c.st_mtime_ns + 5_000_000_000))   # five seconds off: changed
    (folder / "new").write_text("n")
    (folder / "run").unlink()
    result = ops.tree_changes(folder, marker)
    assert (result["changed"], result["added"], result["removed"]) == (2, 1, 1)
    assert ops.tree_changes(folder, None)["changed"] is None  # no record: only the counts


def test_a_folder_left_from_an_earlier_installation_is_said_to_be_that(env):
    reg, tmp, _ = env
    report, apps = install(reg, tmp, program(tmp))
    [inst] = regdb.list_installations(reg)
    ops.executor(reg).run("uninstall", ops.plan_uninstall(reg, inst["id"], remove_payload=False))  # the folder stays, with its marker
    assert (apps / "Discord" / "discord").is_file()
    report, _ = install(reg, tmp, program(tmp))
    assert report.state == "rolled_back" and "left over from an earlier installation" in (report.error or "")
    assert "did not make it" not in (report.error or "")


def test_a_file_that_changes_while_it_is_unpacked_is_not_what_was_approved(tmp_path):
    import hashlib

    arc, staging = unpack_into(tmp_path, [("x/f", "file", "one", 0o644)])
    approved = hashlib.sha256(arc.read_bytes()).hexdigest()
    other = b.build_tarball(tmp_path / "other.tar", [("x/f", "file", "two", 0o644)])
    with pytest.raises(CygnusError, match="changed while"):
        ops.unpack(other, "gz", staging, root="x", expected_sha256=approved)  # the file read is not the one that was approved
    second = tmp_path / "staging2"
    second.mkdir()
    assert ops.unpack(arc, "gz", second, root="x", expected_sha256=approved)["files"] == 1
    trailing = tmp_path / "trailing.tar"
    trailing.write_bytes(arc.read_bytes() + b"\0" * 64)  # bytes added after the archive's own end are hashed too
    third = tmp_path / "staging3"
    third.mkdir()
    with pytest.raises(CygnusError, match="changed while"):
        ops.unpack(trailing, "gz", third, root="x", expected_sha256=approved)


def test_a_folder_that_a_link_leads_into_is_checked_for_where_it_really_is(tmp_path):
    entries = [("x/sub", "dir", None, 0o755), ("x/l", "symlink", "sub", 0o777), ("x/l/f", "file", "x", 0o644)]
    arc, staging = unpack_into(tmp_path, entries)
    with pytest.raises(CygnusError):
        ops.unpack(arc, "gz", staging, root="x")
    assert not (staging / "sub" / "f").exists()


@pytest.mark.parametrize("kind", ["gnu", "pax"])
def test_a_huge_header_stops_the_unpacking_too(tmp_path, kind):
    import gzip
    import io
    import tarfile

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT if kind == "gnu" else tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("x/" + "n" * (3 * 1024 * 1024) if kind == "gnu" else "x/f")
        if kind == "pax":
            info.pax_headers = {"comment": "c" * (3 * 1024 * 1024)}
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    arc = tmp_path / "huge.tar.gz"
    arc.write_bytes(gzip.compress(raw.getvalue(), 1))
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(CygnusError, match="header is far bigger"):
        ops.unpack(arc, "gz", staging, root="x")
    assert list(staging.iterdir()) == []


def test_a_menu_entry_from_the_archive_cannot_borrow_another_programs_window_group_or_icon(env, monkeypatch):
    reg, tmp, _ = env
    entries = [("T", "dir", None, 0o755), ("T/run", "file", b.ELF_STUB, 0o755),
               ("T/t.desktop", "file", "[Desktop Entry]\nName=T\nExec=/usr/bin/run\nIcon=firefox\nStartupWMClass=firefox\n", 0o644)]
    cand = program(tmp, entries, name="T.tar.gz")
    monkeypatch.setattr(ops.integrate, "desktop_id_taken", lambda desktop_id: desktop_id == "firefox.desktop")
    steps, _, _ = ops.integration(cand, app_key="archive.t", program=tmp / "T" / "run", location=HDD)
    written = b"".join(bytes.fromhex(s.params["data_hex"]) for s in steps if s.kind == "fs.write_owned").decode(errors="replace")
    assert "StartupWMClass=firefox" not in written and "Icon=firefox" not in written
    assert "StartupWMClass=t\n" in written  # the program's own name instead, so its windows group under its own entry


@pytest.mark.parametrize("kernel_refuses", [True, False])
def test_a_folder_is_renamed_into_place_but_never_onto_anything_that_is_there(tmp_path, monkeypatch, kernel_refuses):
    import ctypes
    from types import SimpleNamespace

    from cygnus.core.util.fs import rename_noreplace

    if not kernel_refuses:  # a file system that cannot refuse by itself: the path is looked at right before instead
        monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: SimpleNamespace())
    for n, kind in enumerate(("empty folder", "folder with a file", "file", "link", "broken link")):
        src = tmp_path / f"src{n}"
        src.mkdir()
        (src / "payload").write_text("new")
        dst = tmp_path / f"dst{n}"
        if kind == "empty folder":
            dst.mkdir()
        elif kind == "folder with a file":
            dst.mkdir()
            (dst / "mine").write_text("old")
        elif kind == "file":
            dst.write_text("old")
        elif kind == "link":
            dst.symlink_to(tmp_path)
        else:
            dst.symlink_to(tmp_path / "nowhere")
        before = (dst.is_symlink(), dst.is_dir() and sorted(p.name for p in dst.iterdir()))
        with pytest.raises(FileExistsError):
            rename_noreplace(src, dst)
        assert (src / "payload").read_text() == "new" and (dst.is_symlink(), dst.is_dir() and sorted(p.name for p in dst.iterdir())) == before, kind
    free = tmp_path / "free"
    rename_noreplace(tmp_path / "src0", free)
    assert (free / "payload").read_text() == "new" and not (tmp_path / "src0").exists()


def test_the_hash_covers_the_whole_file_even_the_part_after_the_archive_ends(tmp_path):
    import hashlib

    arc = b.build_tarball(tmp_path / "padded.tar", [("x/f", "file", "one", 0o644)], compression="")
    noise = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(100_000))  # 3 MB the tar reader never gets to
    arc.write_bytes(arc.read_bytes() + noise)
    approved = hashlib.sha256(arc.read_bytes()).hexdigest()
    staging = tmp_path / "staging"
    staging.mkdir()
    assert ops.unpack(arc, "", staging, root="x", expected_sha256=approved)["files"] == 1  # an untouched file is accepted
    changed = tmp_path / "changed.tar"
    changed.write_bytes(arc.read_bytes()[:-5] + b"XXXXX")  # only the very end differs: nothing the archive says is affected
    second = tmp_path / "staging2"
    second.mkdir()
    with pytest.raises(CygnusError, match="changed while"):
        ops.unpack(changed, "", second, root="x", expected_sha256=approved)


def test_a_folder_is_only_used_when_it_really_is_inside_the_unpacked_folder_whatever_the_links_say(tmp_path):
    staging = tmp_path / "staging"
    (staging / "real" / "deeper").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (staging / "out").symlink_to(outside)  # as if something had slipped a link out past the other rules
    (staging / "in").symlink_to(staging / "real")
    base = os.path.realpath(staging)
    known: set[str] = set()
    assert ops._inside(staging / "real" / "deeper", base, known) and str(staging / "real" / "deeper") in known
    assert ops._inside(staging / "in" / "deeper", base, set())            # a link that stays inside is fine
    assert not ops._inside(staging / "out", base, set())                   # one that leaves is not
    assert not ops._inside(staging / "out" / "x", base, set())
    assert not ops._inside(tmp_path / "staging-sibling", base, set())      # a name that merely starts alike is not inside
    (tmp_path / "staging-sibling").mkdir()
    assert not ops._inside(tmp_path / "staging-sibling", base, set())
