"""The Install screen and the terminal, for a program in an archive: what is shown before anything is unpacked, the choice of the
program, drives that cannot hold it, what happens afterwards (libraries, removal), and what the update check says."""

import argparse
from pathlib import Path

import pytest

from cygnus.cli import main as cli
from cygnus.core import updates
from cygnus.core.backends import portable
from cygnus.core.errors import CygnusError
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation
from cygnus.core.storage import locations
from cygnus.gui import service
from tests import builders as b

CAPS = {"exec_allowed": True, "chmod_persists": True, "symlinks": True, "hardlinks": True, "atomic_rename": True, "case_sensitive": True,
        "special_chars": True}


def drive(tmp_path, monkeypatch, *, fs="ext4", caps=None, label="HDD"):
    mount = tmp_path / "drive"
    mount.mkdir(exist_ok=True)
    reg = open_registry()
    reg.add_location(StorageLocation(id="hdd", label=label, fs_uuid="X", fs_type=fs, location_class="user-owned", canonical_mount=str(mount),
                                     capabilities=CAPS if caps is None else caps))
    reg.close()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(loc, True, str(mount)))
    return mount


def archive(tmp_path, entries=None, name="Discord.tar.gz"):
    d = tmp_path / "downloads"
    d.mkdir(exist_ok=True)
    return str(b.build_tarball(d / name, entries or b.portable_app_entries()))


def codes(plan):
    return {i["code"] for i in plan["issues"]}


def test_the_screen_shows_what_will_happen_before_anything_is_unpacked(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    plan = service.analyse(archive(tmp_path), "HDD")
    assert (plan["kind"], plan["format"], plan["name"], plan["target"]) == ("plan", "archive", "Discord", "HDD")
    assert plan["installable"] and not plan["blocked"] and len(plan["sha256"]) == 64
    assert plan["archive"]["main"] == "discord" and plan["archive"]["needs_choice"] is False and plan["archive"]["files"] == 5
    assert plan["archive"]["choices"] == ["discord", "updater_bootstrap"]  # its install script is never offered as the program to start
    payload = next(p for p in plan["placements"] if p["role"] == "payload")
    assert payload["location"] == "HDD" and payload["bytes"] > 0 and payload["reason"] == ""
    text = " ".join(plan["notes"])
    assert "never runs them" in text and "who made this archive" in text and "does not update it" in text and "libraries" in text
    assert plan["scripts"]["texts"].keys() == {"postinst.sh"}
    by = {m["command"]: m for m in plan["scripts"]["mentions"]}
    assert {"systemctl", "pkill", "rm", "update-desktop-database"} <= by.keys()
    assert by["systemctl"]["important"] and by["systemctl"]["scripts"] == ["postinst.sh"] and "translated" not in by["systemctl"]["what"]
    assert not by["pkill"]["important"] and "list of programs" in by["update-desktop-database"]["what"] and "Cygnus does this itself" in by["update-desktop-database"]["what"]
    assert plan["scripts"]["mentions"][0]["important"]  # what a program may count on comes first
    assert not (mount / "Applications").exists()  # analysing writes nothing


def test_an_archive_that_is_refused_says_why_and_cannot_be_installed(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    cases = {"ARCHIVE_UNSAFE_PATH": [("../../x", "file", "x", 0o644)] + b.portable_app_entries(),
             "ARCHIVE_SOURCE_CODE": [("p-1.0", "dir", None, 0o755), ("p-1.0/configure", "file", "#!/bin/sh\n", 0o755), ("p-1.0/a.c", "file", "x", 0o644)],
             "ARCHIVE_SYSTEM_LAYOUT": [("usr/bin/t", "file", b.ELF_STUB, 0o755), ("etc/t.conf", "file", "x", 0o644)]}
    for code, entries in cases.items():
        plan = service.analyse(archive(tmp_path, entries, f"{code}.tar.gz"), "HDD")
        assert code in codes(plan) and plan["blocked"] and not plan["installable"], code
        issue = next(i for i in plan["issues"] if i["code"] == code)
        assert issue["severity"] == "blocker" and issue["title"] and issue["explanation"].endswith(".")
    with pytest.raises(CygnusError, match="not installed"):
        service.install_archive(archive(tmp_path, cases["ARCHIVE_UNSAFE_PATH"], "again.tar.gz"), "HDD", "0" * 64, "discord", lambda line: None)


def test_when_the_program_cannot_be_told_the_person_chooses_and_nothing_starts_without_a_choice(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    entries = [("tools", "dir", None, 0o755), ("tools/bin", "dir", None, 0o755), ("tools/bin/alpha", "file", b.ELF_STUB, 0o755),
               ("tools/bin/beta", "file", b.ELF_STUB, 0o755), ("tools/bin/gamma", "file", b.ELF_STUB, 0o755)]
    path = archive(tmp_path, entries, "tools.tar.gz")
    plan = service.analyse(path, "HDD")
    assert plan["archive"]["main"] is None and plan["archive"]["needs_choice"] and plan["installable"]
    assert plan["archive"]["choices"] == ["bin/alpha", "bin/beta", "bin/gamma"]
    with pytest.raises(CygnusError, match="not a program"):
        service.install_archive(path, "HDD", plan["sha256"], "", lambda line: None)
    with pytest.raises(CygnusError, match="not a program"):
        service.install_archive(path, "HDD", plan["sha256"], "bin/../../etc/passwd", lambda line: None)
    result = service.install_archive(path, "HDD", plan["sha256"], "bin/beta", lambda line: None)
    assert result["ok"] and result["installation_id"]
    reg = open_registry()
    launcher = next(a for a in regdb.artifacts_of(reg, result["installation_id"]) if a["kind"] == "launcher_shim")  # the shim names the chosen program
    assert "TARGET='" + str(mount / "Applications" / "tools" / "bin" / "beta") in Path(launcher["locator"]).read_text()


def test_install_then_remove_through_the_screens_functions(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path)
    plan = service.analyse(path, "HDD")
    steps = []
    result = service.install_archive(path, "HDD", plan["sha256"], plan["archive"]["main"], steps.append)
    assert result["ok"] and result["state"] == "succeeded" and steps
    folder = mount / "Applications" / "Discord"
    assert (folder / "discord").is_file()
    apps = service.list_apps()
    row = next(a for a in apps if a["format"] == "archive")
    assert (row["name"], row["location"], row["path"]) == ("Discord", "HDD", str(folder)) and row["icon"] == "discord"
    info = service.archive_info(result["installation_id"])
    assert info["present"] and info["ours"] and info["files"] == 5 and (info["changed"], info["added"]) == (0, 0)
    again = service.analyse(path, "HDD")  # the same program again: refused with the way out
    assert "ARCHIVE_ALREADY_THERE" in codes(again) and again["blocked"]
    assert "Uninstall" in next(i for i in again["issues"] if i["code"] == "ARCHIVE_ALREADY_THERE")["explanation"]
    kept = service.uninstall(result["installation_id"], False, lambda line: None)
    assert kept["ok"] and (folder / "discord").is_file() and not any(a["format"] == "archive" for a in service.list_apps())
    assert "ARCHIVE_ALREADY_THERE" in codes(service.analyse(path, "HDD"))  # the folder is still there, and no longer known as ours
    left = next(i for i in service.analyse(path, "HDD")["issues"] if i["code"] == "ARCHIVE_ALREADY_THERE")
    assert "earlier installation" in left["title"] and "kept when the program was removed" in left["explanation"]  # it IS recognisably Cygnus's own leftover


def test_a_folder_of_that_name_that_is_not_ours_blocks_the_install_and_is_left_alone(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    (mount / "Applications" / "Discord").mkdir(parents=True)
    (mount / "Applications" / "Discord" / "mine.txt").write_text("keep")
    plan = service.analyse(archive(tmp_path), "HDD")
    assert "ARCHIVE_ALREADY_THERE" in codes(plan) and not plan["installable"]
    result = service.install_archive(archive(tmp_path), "HDD", plan["sha256"], "discord", lambda line: None)
    assert not result["ok"] and (mount / "Applications" / "Discord" / "mine.txt").read_text() == "keep"


def test_a_drive_that_cannot_hold_the_program_means_the_system_drive_is_used_and_the_plan_says_why(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch, fs="ntfs3", caps={**CAPS, "symlinks": False, "case_sensitive": False})
    sys_mount = tmp_path / "ssd"
    sys_mount.mkdir()
    reg = open_registry()
    reg.add_location(StorageLocation(id="ssd", label="SSD", fs_uuid=None, fs_type="ext4", location_class="system", canonical_mount=str(sys_mount),
                                     capabilities=CAPS))
    reg.close()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(
        loc, True, str(sys_mount if loc.id == "ssd" else tmp_path / "drive")))
    withlink = b.portable_app_entries(extra=[("Discord/lib", "dir", None, 0o755), ("Discord/lib/liba.so.1", "file", b.ELF_STUB, 0o755),
                                             ("Discord/lib/liba.so", "symlink", "liba.so.1", 0o777)])
    plan = service.analyse(archive(tmp_path, withlink, "links.tar.gz"), "HDD")
    payload = next(p for p in plan["placements"] if p["role"] == "payload")
    assert payload["location"] == "SSD" and "symlinks" in payload["reason"] and "STORAGE_INCAPABLE" in codes(plan)
    clash = b.portable_app_entries(extra=[("Discord/Readme", "file", "a", 0o644), ("Discord/README", "file", "b", 0o644), ("Discord/a:b", "file", "c", 0o644)])
    plan = service.analyse(archive(tmp_path, clash, "clash.tar.gz"), "HDD")
    payload = next(p for p in plan["placements"] if p["role"] == "payload")
    assert payload["location"] == "SSD" and "cannot all be kept there" in payload["reason"]


def test_names_that_a_windows_style_drive_cannot_keep_are_found_and_a_linux_drive_has_no_such_problem():
    names = ["a/Readme", "a/README", "b/x:y", "c/trailing.", "d/CON.txt", "e/ok.txt", "f/" + "x" * 300]
    found = portable.name_problems(names, fs_type="ntfs3", caps={})
    text = " ".join(found)
    assert "capital letters" in text and "character" in text and len(found) <= 5
    assert "longer than 255" in " ".join(portable.name_problems(["f/" + "x" * 300], fs_type="ext4"))
    assert portable.name_problems(["a/Readme", "a/README", "b/x:y", "c/trailing.", "d/CON.txt"], fs_type="ext4", caps={"case_sensitive": True, "special_chars": True}) == []
    assert "capital letters" in " ".join(portable.name_problems(["a/Readme", "a/README"], fs_type="ext4", caps={"case_sensitive": False}))
    assert portable.name_problems(["same", "same"], fs_type="ntfs3") == []  # one name twice is the archive's own problem, not the drive's


def test_the_libraries_a_program_lacks_are_found_with_the_packages_that_have_them(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    path = archive(tmp_path)
    plan = service.analyse(path, "HDD")
    result = service.install_archive(path, "HDD", plan["sha256"], "discord", lambda line: None)
    from cygnus.core.backends import filedb, foreign
    from cygnus.core.backends import pacman as pm

    monkeypatch.setattr(foreign, "_readelf", lambda p: (["libc.so.6", "libgone.so.2", "libnss3.so", "libbundled.so.1"], [], (2, 99), None, True))
    monkeypatch.setattr(foreign, "host_sonames", lambda: {"libc.so.6"})
    monkeypatch.setattr(foreign, "host_glibc", lambda: (2, 43))
    monkeypatch.setattr(pm, "read_config", lambda: object())
    monkeypatch.setattr(pm, "run_worker", lambda cfg, req: {"satisfiers": {"libgone.so=2-64": {"repo": {"name": "gone"}}}})
    monkeypatch.setattr(filedb, "locate", lambda sonames, progress=None, cfg=None: {"libnss3.so": "nss"} if "libnss3.so" in sonames else {})
    said = []
    out = service.archive_libraries(result["installation_id"], said.append)
    assert out["libraries"]["libgone.so.2"] == "gone" and "gone" in out["packages"]
    assert out["glibc"] == {"needs": "2.99", "have": "2.43"} and out["checked"] >= 1 and out["bits"] == 64
    assert isinstance(out["unresolved"], list)


def test_a_library_that_the_program_carries_itself_is_never_called_missing(tmp_path):
    from cygnus.core.backends import foreign

    root = tmp_path / "app"
    (root / "lib").mkdir(parents=True)
    (root / "run").write_bytes(b.ELF_STUB + b"\0" * 100)
    (root / "lib" / "libmine.so.3").write_bytes(b.ELF_STUB + b"\0" * 100)
    real = foreign._readelf
    foreign._readelf = lambda p: (["libmine.so.3", "libc.so.6"], [], (2, 20), None, p.name == "run")
    try:
        out = portable.library_check(root, satisfy=lambda d: {}, locate=lambda s: {}, sonames_on_host={"libc.so.6"}, glibc_on_host=(2, 43))
    finally:
        foreign._readelf = real
    assert out["packages"] == [] and out["unresolved"] == [] and out["glibc"] is None


def test_the_libraries_of_something_that_is_not_ours_or_not_there_are_not_looked_at(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path)
    result = service.install_archive(path, "HDD", service.analyse(path, "HDD")["sha256"], "discord", lambda line: None)
    with pytest.raises(CygnusError, match="not a program installed from an archive"):
        service.archive_libraries("no-such-installation")
    (mount / "Applications" / "Discord").rename(mount / "Applications" / "elsewhere")
    with pytest.raises(CygnusError, match="is not there"):
        service.archive_libraries(result["installation_id"])


def test_the_update_check_says_it_cannot_check_and_does_not_claim_the_system_updates_it(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    path = archive(tmp_path)
    service.install_archive(path, "HDD", service.analyse(path, "HDD")["sha256"], "discord", lambda line: None)
    reg = open_registry()
    [row] = [r for r in regdb.list_installations(reg) if r["format"] == "archive"]
    status = updates.check(row)
    assert status.status == updates.MANUAL and "plain archive" in status.detail and "system" not in status.detail.lower().replace("remove it", "")
    assert status.provider == "manual"


# -- the terminal -----------------------------------------------------------------------------------------------------------------------
def args(file, **kw):
    return argparse.Namespace(file=file, to=kw.get("to", "HDD"), yes=kw.get("yes", True), program=kw.get("program"), registry=None,
                              with_optional=False)


def test_the_terminal_installs_shows_the_scripts_and_removes(tmp_path, monkeypatch, capsys):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path)
    monkeypatch.setattr(service, "archive_libraries", lambda *a, **k: {"glibc": None, "packages": ["gtk3"], "unresolved": ["libx.so.1"]})
    assert cli.cmd_install(args(path)) == 0
    out = capsys.readouterr().out
    assert "Discord" in out and "the install scripts mention systemctl" in out and "never run" in out and "done" in out
    assert "sudo pacman -S --needed --asdeps gtk3" in out and "libx.so.1" in out
    assert (mount / "Applications" / "Discord" / "discord").is_file()
    assert cli.cmd_uninstall(argparse.Namespace(app="Discord", delete_file=True, remove_autostart=False, yes=True, registry=None)) == 0
    assert not (mount / "Applications" / "Discord").exists()


def test_the_terminal_asks_for_the_program_when_it_cannot_tell_and_refuses_one_that_is_not_there(tmp_path, monkeypatch, capsys):
    drive(tmp_path, monkeypatch)
    entries = [("t", "dir", None, 0o755), ("t/a", "file", b.ELF_STUB, 0o755), ("t/b", "file", b.ELF_STUB, 0o755)]
    path = archive(tmp_path, entries, "t.tar.gz")
    assert cli.cmd_install(args(path)) == 1
    assert "--program" in capsys.readouterr().out
    with pytest.raises(CygnusError, match="not one of the programs"):
        cli.cmd_install(args(path, program="../../bin/sh"))
    monkeypatch.setattr(service, "archive_libraries", lambda *a, **k: {"glibc": None, "packages": [], "unresolved": []})
    assert cli.cmd_install(args(path, program="b")) == 0


def test_the_terminal_does_not_move_or_repair_a_program_from_an_archive_and_says_so(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    path = archive(tmp_path)
    monkeypatch.setattr(service, "archive_libraries", lambda *a, **k: {"glibc": None, "packages": [], "unresolved": []})
    cli.cmd_install(args(path))
    with pytest.raises(CygnusError, match="not supported yet"):
        cli.cmd_move(argparse.Namespace(app="Discord", to="HDD", yes=True, registry=None))
    with pytest.raises(CygnusError, match="not supported yet"):
        cli.cmd_repair(argparse.Namespace(app="Discord", yes=True, registry=None))


def test_installing_onto_a_drive_that_cannot_keep_the_names_puts_the_program_on_the_system_drive_instead(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch, fs="ntfs3", caps={**CAPS, "case_sensitive": False})
    sys_mount = tmp_path / "ssd"
    sys_mount.mkdir()
    reg = open_registry()
    reg.add_location(StorageLocation(id="ssd", label="SSD", fs_uuid=None, fs_type="ext4", location_class="system", canonical_mount=str(sys_mount),
                                     capabilities=CAPS))
    reg.close()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(
        loc, True, str(sys_mount if loc.id == "ssd" else tmp_path / "drive")))
    clash = b.portable_app_entries(extra=[("Discord/Readme", "file", "a", 0o644), ("Discord/README", "file", "b", 0o644)])
    path = archive(tmp_path, clash, "clash.tar.gz")
    plan = service.analyse(path, "HDD")
    result = service.install_archive(path, "HDD", plan["sha256"], plan["archive"]["main"], lambda line: None)
    assert result["ok"]
    assert (sys_mount / "Applications" / "Discord" / "discord").is_file() and not (tmp_path / "drive" / "Applications").exists()
    assert (sys_mount / "Applications" / "Discord" / "Readme").read_text() == "a" and (sys_mount / "Applications" / "Discord" / "README").read_text() == "b"


def test_the_libraries_are_only_looked_at_in_the_folder_cygnus_made(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path)
    result = service.install_archive(path, "HDD", service.analyse(path, "HDD")["sha256"], "discord", lambda line: None)
    folder = mount / "Applications" / "Discord"
    marker = folder / ".cygnus-installation"
    marker.write_text('{"installation": "somebody-else"}')
    with pytest.raises(CygnusError, match="not the one Cygnus made"):
        service.archive_libraries(result["installation_id"])
    marker.unlink()
    with pytest.raises(CygnusError, match="not the one Cygnus made"):
        service.archive_libraries(result["installation_id"])


# -- what the second review found ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sha", ["", None, "abc", "G" * 64, "A" * 64, "0" * 63, "0" * 65])
def test_nothing_is_unpacked_without_the_checksum_that_was_shown(tmp_path, monkeypatch, sha):
    mount = drive(tmp_path, monkeypatch)
    with pytest.raises(CygnusError, match="checksum"):
        service.install_archive(archive(tmp_path), "HDD", sha, "discord", lambda line: None)
    assert not (mount / "Applications").exists()


def test_an_install_script_cannot_be_made_the_program_to_start_by_asking_for_it_directly(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path)
    sha = service.analyse(path, "HDD")["sha256"]
    for main in ("postinst.sh", "no/such/thing"):
        with pytest.raises(CygnusError, match="not a program in this archive"):
            service.install_archive(path, "HDD", sha, main, lambda line: None)
    assert not (mount / "Applications" / "Discord").exists()


def test_the_id_returned_is_the_one_recorded_and_a_failed_install_returns_none(tmp_path, monkeypatch):
    mount = drive(tmp_path, monkeypatch)
    path = archive(tmp_path)
    sha = service.analyse(path, "HDD")["sha256"]
    (mount / "Applications" / "Discord").mkdir(parents=True)  # in the way: the install fails
    failed = service.install_archive(path, "HDD", sha, "discord", lambda line: None)
    assert not failed["ok"] and failed["installation_id"] == ""
    (mount / "Applications" / "Discord").rmdir()
    result = service.install_archive(path, "HDD", sha, "discord", lambda line: None)
    reg = open_registry()
    assert [r["id"] for r in regdb.list_installations(reg) if r["format"] == "archive"] == [result["installation_id"]]


def test_a_drive_without_room_stays_a_blocker_and_does_not_quietly_move_the_program_to_the_system_drive(tmp_path, monkeypatch):
    from cygnus.core.ops import archive_ops

    mount = drive(tmp_path, monkeypatch)
    monkeypatch.setattr(archive_ops, "free_space_problem", lambda parent, needed: "not enough room: it needs about 900 MB and only 5 MB are free")
    path = archive(tmp_path)
    plan = service.analyse(path, "HDD")
    assert "ARCHIVE_NO_ROOM" in codes(plan) and plan["blocked"] and not plan["installable"]
    assert next(p for p in plan["placements"] if p["role"] == "payload")["location"] == "HDD"  # the chosen drive: no silent move
    result = service.install_archive(path, "HDD", plan["sha256"], "discord", lambda line: None)
    assert not result["ok"] and "not enough room" in result["error"] and not (mount / "Applications" / "Discord").exists()


def test_a_program_buried_deep_in_the_archive_is_found_and_offered(tmp_path, monkeypatch):
    drive(tmp_path, monkeypatch)
    entries = [("Tool", "dir", None, 0o755), ("Tool/a/b/c/prog", "file", b.ELF_STUB, 0o755)]
    plan = service.analyse(archive(tmp_path, entries, "deep.tar.gz"), "HDD")
    assert plan["installable"] and plan["archive"]["main"] == "a/b/c/prog" and plan["archive"]["needs_choice"] is False
    two = service.analyse(archive(tmp_path, entries + [("Tool/a/b/c/other", "file", b.ELF_STUB, 0o755)], "deep2.tar.gz"), "HDD")
    assert two["archive"]["needs_choice"] and two["archive"]["choices"] == ["a/b/c/other", "a/b/c/prog"] and two["installable"]


def test_a_windows_style_drive_is_judged_letter_by_letter_as_it_does_and_refuses_a_backslash():
    ntfs = {"fs_type": "ntfs3", "caps": {}}
    assert portable.name_problems(["a/back\\slash"], **ntfs) and portable.name_problems(["a/back\\slash"], fs_type="ext4") == []
    assert portable.name_problems(["a/straße", "a/STRASSE"], **ntfs) == []  # NTFS does not fold "ß" into "SS"
    assert "capital letters" in " ".join(portable.name_problems(["a/Ärger", "a/ärger"], **ntfs))
    assert portable.name_problems(["a/ok", "a/Ok"], fs_type="vfat", caps={}) != []


@pytest.mark.parametrize("script, found, not_found", [
    ('echo "now run systemctl restart x"\n# rm -rf /\nprintf "pkill me\\n"\n', set(), {"systemctl", "rm", "pkill"}),
    ("sudo systemctl enable thing\n/usr/bin/rm -rf /opt/x\nenv FOO=1 pkill thing\n", {"systemctl", "rm", "pkill", "sudo"}, set()),
    ("if true; then useradd bob; fi\ncd /tmp && wget http://x && chmod +x y\n", {"useradd", "wget", "chmod"}, set()),
    ("cat <<EOF\nsystemctl restart something\nEOF\nFOO=bar\n", set(), {"systemctl", "EOF"}),
    ("cat <<'EOF' > /etc/x.conf\nsudo rm -rf /\nEOF\npkill thing\n", {"pkill"}, {"rm", "sudo"}),
    ("cat <<-END\n\tsystemctl x\n\tEND\nmodprobe fuse\n", {"modprobe"}, {"systemctl"}),
    ("bash <<EOF\nsystemctl restart x\nEOF\n", {"systemctl"}, set()),  # a shell is reading that text: it is commands
    ("cat <<< hello\nrm -rf /y\n", {"rm"}, set()),                    # a here-string is one word, not a block
    ("x=$(rm -rf /y)\nnohup killall thing &\n", {"killall"}, set()),
])
def test_a_script_is_read_for_the_commands_it_runs_not_for_the_words_it_contains(script, found, not_found):
    by = {m["command"] for m in portable.mentions({"s.sh": script})}
    assert found <= by and not (not_found & by), by
