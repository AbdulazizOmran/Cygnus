"""What a Flatpak may do, in plain words: the application's own permissions, the override files in Flatpak's order, "!" taking an item away, what is
said about each, and files that are damaged, huge or not files at all. Read-only; the libflatpak side is faked."""

import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cygnus.core import flatpak_permissions as fp
from cygnus.core.errors import CygnusError

OBS = """[Context]
shared=ipc;network;
sockets=fallback-x11;pulseaudio;wayland;
devices=all;
filesystems=xdg-run/pipewire-0;host;

[Session Bus Policy]
org.kde.StatusNotifierWatcher=talk
org.freedesktop.Flatpak=talk
org.freedesktop.Notifications=talk

[Environment]
VST_PATH=/app/extensions/Plugins/vst
"""


def _items(*layers):
    return {i["raw"]: i for i in fp.describe(fp.merge(list(layers)))}


def _layer(text):
    return fp.parse_keyfile(text)


# -- reading the file ---------------------------------------------------------------------------------------------------------------------
def test_a_real_metadata_file_is_read_group_by_group():
    data = _layer(OBS)
    assert data["Context"]["shared"] == "ipc;network;" and data["Session Bus Policy"]["org.freedesktop.Flatpak"] == "talk"
    assert data["Environment"]["VST_PATH"] == "/app/extensions/Plugins/vst"


@pytest.mark.parametrize("bad", ["", "no groups at all", "[Context\nshared=network", "\x00\x01\x02", "= = =\n[x]x", "just a line\nanother"])
def test_a_damaged_file_gives_nothing_instead_of_stopping_the_page(bad):
    assert fp.parse_keyfile(bad) == {}


def test_a_group_or_key_that_appears_twice_is_merged_not_a_reason_to_give_up():
    assert fp.parse_keyfile("[a]\nx=1\n[a]\nx=2\ny=3\n") == {"a": {"x": "2", "y": "3"}}


def test_a_default_group_is_an_ordinary_group_as_in_glibs_key_files_and_is_not_inherited():
    data = fp.parse_keyfile("[DEFAULT]\nshared=network\n[Context]\nfilesystems=home\n")
    assert data["Context"] == {"filesystems": "home"} and data["DEFAULT"] == {"shared": "network"}


def test_a_file_with_many_groups_before_the_ones_that_matter_keeps_them():
    text = "".join(f"[Extension org.x.E{i}]\ndirectory=x\n" for i in range(120)) + "[Session Bus Policy]\norg.freedesktop.Flatpak=talk\n"
    assert fp.parse_keyfile(text)["Session Bus Policy"] == {"org.freedesktop.Flatpak": "talk"}


def test_environment_names_keep_their_case_and_percent_signs_are_not_interpolated():
    data = _layer("[Environment]\nXCURSOR_PATH=/run/host/%s/icons\nlower=1\n")
    assert data["Environment"] == {"XCURSOR_PATH": "/run/host/%s/icons", "lower": "1"}


def test_a_file_with_thousands_of_entries_is_cut_not_swallowed():
    data = _layer("[Context]\nshared=network\n[Session Bus Policy]\n" + "".join(f"org.example.N{i}=talk\n" for i in range(5000)))
    assert len(data["Session Bus Policy"]) <= fp.MAX_ITEMS


# -- the order of the layers -----------------------------------------------------------------------------------------------------------
def test_an_override_adds_what_the_application_did_not_ask_for_and_says_who_added_it():
    items = _items(("app", _layer(OBS)), ("you", _layer("[Context]\nsockets=x11\nfilesystems=xdg-download\n[Session Bus Policy]\norg.example.Mine=talk\n")))
    assert items["sockets=x11"]["source"] == "you" and items["filesystems=xdg-download"]["source"] == "you"
    assert items["shared=network"]["source"] == "app" and items["session bus talk org.example.Mine"]["source"] == "you"


def test_an_item_written_with_a_bang_takes_the_item_away():
    merged = fp.merge([("app", _layer(OBS)), ("you", _layer("[Context]\nshared=!network\nsockets=!wayland\ndevices=!all\nfilesystems=!host\n"))])
    assert "network" not in merged["lists"]["shared"] and "ipc" in merged["lists"]["shared"]
    assert "wayland" not in merged["lists"]["sockets"] and "all" not in merged["lists"]["devices"] and "host" not in merged["lists"]["filesystems"]
    assert {(k, n, s) for k, n, s in merged["removed"]} == {("shared", "network", "you"), ("sockets", "wayland", "you"), ("devices", "all", "you"),
                                                             ("filesystems", "host", "you")}
    said = fp.removed_text(merged)
    assert "You removed: Use the internet and your local network" in said and "You removed: Every file on your computer" in said


def test_taking_away_something_that_was_never_there_is_not_reported():
    merged = fp.merge([("app", _layer("[Context]\nshared=network\n")), ("you", _layer("[Context]\nsockets=!x11\n"))])
    assert merged["removed"] == []


def test_a_bang_removes_a_folder_whatever_its_mode_and_a_later_layer_changes_the_mode():
    merged = fp.merge([("app", _layer("[Context]\nfilesystems=xdg-download:rw;~/Games:ro\n")), ("you", _layer("[Context]\nfilesystems=!xdg-download:rw;~/Games\n"))])
    assert merged["lists"]["filesystems"] == {"~/Games": ("", "you")} and ("filesystems", "xdg-download", "you") in merged["removed"]


def test_a_bus_policy_of_none_blocks_a_name_the_application_asked_for():
    merged = fp.merge([("app", _layer(OBS)), ("you", _layer("[Session Bus Policy]\norg.freedesktop.Flatpak=none\n"))])
    assert "org.freedesktop.Flatpak" not in merged["bus"]["session"] and ("session-bus", "org.freedesktop.Flatpak", "you") in merged["removed"]
    assert any("Start programs outside the sandbox" in line for line in fp.removed_text(merged))


def test_a_later_layer_wins_for_an_environment_variable_and_a_bus_policy():
    merged = fp.merge([("app", _layer("[Environment]\nA=1\n[Session Bus Policy]\nx.y=see\n")), ("you", _layer("[Environment]\nA=2\n[Session Bus Policy]\nx.y=own\n"))])
    assert merged["env"]["A"] == ("2", "you") and merged["bus"]["session"]["x.y"] == ("own", "you")


def test_an_unknown_policy_word_is_ignored_not_trusted():
    merged = fp.merge([("app", _layer("[Session Bus Policy]\nx.y=sudo\nz.w=talk\n"))])
    assert list(merged["bus"]["session"]) == ["z.w"]


# -- what is said about each one -----------------------------------------------------------------------------------------------------
def test_the_worst_are_named_for_what_they_are():
    items = _items(("app", _layer(OBS)))
    assert items["filesystems=host"]["level"] == "escape" and "Every file on your computer" in items["filesystems=host"]["text"]
    assert items["session bus talk org.freedesktop.Flatpak"]["level"] == "escape"
    assert items["devices=all"]["level"] == "broad" and items["shared=network"]["level"] == "normal"
    assert items["session bus talk org.kde.StatusNotifierWatcher"]["text"].startswith("Put an icon in the system tray")


@pytest.mark.parametrize("name,mode,level,words", [
    ("host", "", "escape", "Every file"), ("host-os", "ro", "escape", "read only"), ("host-etc", "", "escape", "/etc"), ("home", "", "broad", "whole home folder"),
    ("~", "", "broad", "whole home folder"), ("/", "", "escape", "Every file"), ("xdg-download", "", "normal", "Downloads"),
    ("xdg-pictures", "ro", "normal", "Pictures"), ("xdg-config", "", "broad", "~/.config"), ("xdg-config/kdeglobals", "ro", "normal", "~/.config/kdeglobals"),
    ("xdg-config/autostart", "", "escape", "start when you log in"), ("xdg-config/autostart/x", "", "escape", "start when you log in"),
    ("xdg-run/pipewire-0", "", "normal", "PipeWire"), ("xdg-run/something-else", "", "normal", "runtime file something-else"), ("~/.steam", "", "normal", "~/.steam"), ("/usr/share/fonts", "ro", "broad", "system folder"),
    ("/run/media/me/USB", "", "normal", "/run/media/me/USB"), ("/mnt/data", "create", "normal", "creates the folder")])
def test_each_kind_of_folder_gets_the_right_words_and_level(name, mode, level, words):
    text, got, group = fp.describe_filesystem(name, mode)
    assert got == level and words in text and group == "files"


@pytest.mark.parametrize("name,policy,level", [("org.freedesktop.Flatpak", "talk", "escape"), ("org.freedesktop.Flatpak", "own", "escape"),
                                               ("org.freedesktop.Flatpak.*", "talk", "escape"), ("org.freedesktop.systemd1", "talk", "escape"),
                                               ("org.freedesktop.PolicyKit1", "talk", "escape"), ("org.freedesktop.Flatpak", "see", "normal"),
                                               ("org.freedesktop.Notifications", "talk", "normal"), ("org.example.Thing", "own", "normal")])
def test_only_bus_names_that_start_programs_outside_the_sandbox_are_an_escape(name, policy, level):
    assert fp.describe_bus("session", name, policy)[1] == level


def test_a_name_made_of_markup_or_control_characters_is_shown_safely():
    text, _ = fp.describe_bus("session", "org.x.\x1b[31mBad<b>", "talk")
    assert "\x1b" not in text
    folder, _, _ = fp.describe_filesystem("~/a\x07b\n/c", "")
    assert "\x07" not in folder and "\n" not in folder
    items = _items(("app", _layer("[Context]\nsockets=\x1bweird\n[Environment]\nV=\x07\n")))
    assert all(c.isprintable() for i in items.values() for c in i["text"])


def test_a_permission_cygnus_does_not_know_is_said_so_not_hidden():
    items = _items(("app", _layer("[Context]\nsockets=quantum-link\ndevices=holodeck\nfeatures=time-travel\nshared=telepathy\n")))
    assert {i["raw"] for i in items.values()} == {"sockets=quantum-link", "devices=holodeck", "features=time-travel", "shared=telepathy"}
    assert all("does not know" in i["text"] or "Cygnus does not know" in i["text"] for i in items.values())


def test_the_list_is_grouped_and_the_worst_come_first_inside_each_group():
    result = fp.describe(fp.merge([("app", _layer(OBS))]))
    groups = [i["group"] for i in result]
    assert groups == sorted(groups, key=[g for g, _ in fp.GROUPS].index)
    files = [i for i in result if i["group"] == "files"]
    assert files[0]["level"] == "escape"
    assert fp.summary(result) == {"escape": 2, "broad": 1, "total": len(result)}


# -- reading the files ---------------------------------------------------------------------------------------------------------------------
def test_an_override_file_is_read_as_a_regular_file_up_to_a_limit(tmp_path):
    (tmp_path / "ok").write_text("[Context]\nshared=network\n")
    assert "network" in fp.read_text(tmp_path / "ok")
    (tmp_path / "big").write_bytes(b"x" * (fp.MAX_BYTES + 1))
    assert fp.read_text(tmp_path / "big") is None  # refused, not half read
    (tmp_path / "dir").mkdir()
    os.mkfifo(tmp_path / "pipe")
    for name in ("dir", "pipe", "missing"):
        assert fp.read_text(tmp_path / name) is None  # and a pipe does not hold the page up
    (tmp_path / "dotfiles").mkdir()
    (tmp_path / "dotfiles" / "real").write_text("[Context]\nsockets=x11\n")
    (tmp_path / "link").symlink_to(tmp_path / "dotfiles" / "real")  # the person's own file kept in a dotfiles folder
    assert "x11" in fp.read_text(tmp_path / "link")
    (tmp_path / "bytes").write_bytes(b"[Context]\nshared=\xff\xfe\n")
    assert fp.read_text(tmp_path / "bytes") is not None


def test_override_files_come_in_flatpaks_order(tmp_path):
    system, user = tmp_path / "system", tmp_path / "user"
    assert [(w, p.name, p.parent.parent.name) for w, p in fp.override_files(system, "a.b.C", user)] == [
        ("system", "global", "system"), ("system", "a.b.C", "system"), ("you", "global", "user"), ("you", "a.b.C", "user")]
    assert [w for w, _ in fp.override_files(user, "a.b.C", user)] == ["you", "you"]  # the user's own installation: one set of files


class _Ref:
    def __init__(self, name, meta, kind="APP"):
        self.name, self.meta, self.kind = name, meta, kind

    def get_name(self):
        return self.name

    def get_kind(self):
        return self.kind

    def load_metadata(self, _cancel):
        return NS(get_data=lambda: self.meta.encode())

    def get_appdata_name(self):
        return "Some \x1bName"

    def get_origin(self):
        return "flathub"


def _env(tmp_path, *refs, installation="user", path=None):
    inst = NS(get_path=lambda: NS(get_path=lambda: str(path or tmp_path / installation)), get_is_user=lambda: installation == "user")
    return NS(Flatpak=NS(RefKind=NS(APP="APP")), installed_refs=lambda: [(inst, r) for r in refs], inst_id=lambda i: installation)


def test_an_installed_application_is_read_with_its_overrides_in_order(tmp_path):
    user = tmp_path / "user"
    (user / "overrides").mkdir(parents=True)
    (user / "overrides" / "global").write_text("[Context]\nsockets=!x11\n")
    (user / "overrides" / "com.obsproject.Studio").write_text("[Context]\nfilesystems=!host;xdg-videos\n")
    meta = OBS.replace("sockets=fallback-x11;", "sockets=x11;fallback-x11;")  # OBS asks for X11 here; your global override takes it away
    result = fp.read("com.obsproject.Studio", _env(tmp_path, _Ref("com.obsproject.Studio", meta)), user_path=user)
    raws = {i["raw"]: i for i in result["items"]}
    assert "filesystems=host" not in raws and "sockets=x11" not in raws and raws["filesystems=xdg-videos"]["source"] == "you"
    assert result["changed_by_you"] is True and any("Every file on your computer" in t for t in result["removed"])
    assert any(t.startswith("You removed: Show windows through X11") for t in result["removed"])
    assert result["name"] == "Some  Name" and "\x1b" not in result["name"] and result["origin"] == "flathub"
    assert [g["id"] for g in result["groups"]][0] == "network"


def test_the_systems_overrides_apply_before_yours_for_an_application_in_the_system_installation(tmp_path):
    system, user = tmp_path / "system", tmp_path / "user"
    (system / "overrides").mkdir(parents=True)
    (user / "overrides").mkdir(parents=True)
    (system / "overrides" / "a.b.C").write_text("[Context]\nshared=!network\n")
    (user / "overrides" / "a.b.C").write_text("[Context]\nshared=network\n")  # you put it back
    env = _env(tmp_path, _Ref("a.b.C", "[Context]\nshared=network\n"), installation="system", path=system)
    result = fp.read("a.b.C", env, user_path=user)
    network = next(i for i in result["items"] if i["raw"] == "shared=network")
    assert network["source"] == "you" and result["removed"] == ["The system removed: Use the internet and your local network"]


def test_an_application_that_is_not_installed_is_said_in_words(tmp_path):
    env = _env(tmp_path, _Ref("x.y.Z", ""), _Ref("org.freedesktop.Platform", "", kind="RUNTIME"))
    for name in ("a.b.C", "org.freedesktop.Platform"):
        with pytest.raises(CygnusError, match="not installed as a Flatpak"):
            fp.read(name, env, user_path=tmp_path)


def test_a_damaged_override_does_not_hide_the_application_and_says_it_was_not_read(tmp_path):
    user = tmp_path / "user"
    (user / "overrides").mkdir(parents=True)
    os.mkfifo(user / "overrides" / "a.b.C")
    (user / "overrides" / "global").write_bytes(b"x" * (fp.MAX_BYTES + 5))
    result = fp.read("a.b.C", _env(tmp_path, _Ref("a.b.C", "[Context]\nshared=network\n")), user_path=user)
    assert [i["raw"] for i in result["items"]] == ["shared=network"]
    assert len(result["notes"]) == 2 and all("could not be read" in n for n in result["notes"])
    assert result["changed_by_you"] is False


def test_a_huge_metadata_file_is_refused_not_read(tmp_path):
    with pytest.raises(CygnusError, match="far larger"):
        fp.read("a.b.C", _env(tmp_path, _Ref("a.b.C", "x" * (fp.MAX_BYTES + 1))), user_path=tmp_path / "user")


# -- the pieces around it: service, terminal ----------------------------------------------------------------------------------------------
def _flatpak_row(fmt="flatpak", ref="app/com.example.App/x86_64/stable"):
    from cygnus.core.registry import db as regdb
    from cygnus.core.registry import open_registry

    reg = open_registry()
    regdb.add_application(reg, app_id="com.example.App", display_name="Example")
    return regdb.add_installation(reg, app_id="com.example.App", fmt=fmt, source={"ref": ref}, version="1", location_id=None, origin="installed")


def _fake_flatpak(monkeypatch, tmp_path, meta="[Context]\nshared=network\nfilesystems=home:ro\n"):
    from cygnus.core.backends import flatpak as fb

    monkeypatch.setattr(fb, "FlatpakEnv", lambda *a, **k: _env(tmp_path, _Ref("com.example.App", meta), path=tmp_path / "inst"))
    monkeypatch.setattr(fb, "user_dir", lambda: tmp_path / "user")


def test_the_service_reads_the_flatpak_the_registry_row_names(tmp_path, monkeypatch):
    from cygnus.gui import service

    _fake_flatpak(monkeypatch, tmp_path)
    result = service.flatpak_permissions(_flatpak_row())
    assert result["app"] == "com.example.App" and {i["raw"] for i in result["items"]} == {"shared=network", "filesystems=home:ro"}


def test_the_terminal_command_lists_the_permissions_by_group_and_marks_the_worst(tmp_path, monkeypatch, capsys):
    from cygnus.cli import main as cli

    _fake_flatpak(monkeypatch, tmp_path, "[Context]\nfilesystems=host;xdg-download:ro\nsockets=wayland\n[Session Bus Policy]\norg.freedesktop.Flatpak=talk\n")
    assert cli.main(["permissions", "com.example.App"]) == 0
    out = capsys.readouterr().out
    assert "Example" in out or "Some  Name" in out
    assert "!! Every file on your computer" in out and "!! Start programs outside the sandbox" in out and "   Show windows through Wayland" in out
    assert "2 that can leave the sandbox" in out and "Files\n" in out
    assert cli.main(["permissions", "com.example.App", "--json"]) == 0
    import json

    assert json.loads(capsys.readouterr().out)["summary"]["escape"] == 2


def test_the_terminal_command_says_when_it_is_not_installed(tmp_path, monkeypatch, capsys):
    from cygnus.cli import main as cli

    _fake_flatpak(monkeypatch, tmp_path)
    assert cli.main(["permissions", "org.nothing.Here"]) == 2
    assert "not installed as a Flatpak" in capsys.readouterr().err


# -- round 17 findings ------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    return "/home/tester"


@pytest.mark.parametrize("name,level", [("host-root", "escape"), ("~/.config/autostart", "escape"), ("/home/tester/.config/autostart", "escape"),
                                        ("~/.config/autostart/", "escape"), ("~", "broad"), ("/home/tester", "broad"), ("/home/tester/", "broad"),
                                        ("~/.local/share/applications", "broad"), ("xdg-data/applications", "broad"), ("/home/tester/.local/share/applications/x", "broad"),
                                        ("~/.ssh", "broad"), ("/home/tester/.gnupg", "broad"), ("/home/tester/Games", "normal"), ("~/Games", "normal"),
                                        ("/home/other", "normal"), ("xdg-download", "normal")])
def test_the_places_that_let_a_program_out_are_recognised_whichever_way_they_are_written(home, name, level):
    assert fp.describe_filesystem(name, "")[1] == level


def test_one_place_written_two_ways_is_one_permission_and_a_bang_removes_it_either_way(home):
    merged = fp.merge([("app", _layer("[Context]\nfilesystems=~/.config/autostart;/home/tester/Games:ro\n")),
                       ("you", _layer("[Context]\nfilesystems=!/home/tester/.config/autostart;!~/Games\n"))])
    assert merged["lists"]["filesystems"] == {} and {n for _k, n, _s in merged["removed"]} == {"xdg-config/autostart", "~/Games"}


@pytest.mark.parametrize("name,policy,level", [("org.freedesktop.*", "talk", "escape"), ("org.*", "own", "escape"), ("org.freedesktop.Flatpak.*", "talk", "escape"),
                                               ("org.kde.*", "talk", "normal"), ("org.freedesktop.Notifications", "talk", "normal"),
                                               ("org.freedesktop.*", "see", "normal"), ("org.freedesktop.Flatpak", "see", "normal")])
def test_a_wildcard_bus_name_is_an_escape_when_it_reaches_a_name_that_starts_programs(name, policy, level):
    assert fp.describe_bus("session", name, policy)[1] == level


def test_a_name_that_may_only_be_seen_is_not_described_as_something_it_can_do():
    text, level = fp.describe_bus("session", "org.freedesktop.Flatpak", "see")
    assert text.startswith("See that org.freedesktop.Flatpak exists") and level == "normal" and "Start programs" not in text
    assert "Put an icon" not in fp.describe_bus("session", "org.kde.StatusNotifierWatcher", "see")[0]


def test_a_bang_with_reset_drops_every_filesystem_permission_that_came_before():
    merged = fp.merge([("app", _layer("[Context]\nfilesystems=host;xdg-download:ro;~/Games\nshared=network\n")), ("you", _layer("[Context]\nfilesystems=!host:reset;xdg-music\n"))])
    assert list(merged["lists"]["filesystems"]) == ["xdg-music"] and merged["lists"]["shared"] == {"network": ("", "app")}
    assert {n for k, n, s in merged["removed"] if k == "filesystems"} == {"host", "xdg-download", "~/Games"}


def test_a_permission_granted_only_under_a_condition_is_described_as_what_it_is_with_the_condition():
    items = _items(("app", _layer("[Context]\nsockets=if:session-bus:has-foo;if:x11:!has-wayland;wayland\ndevices=if:all:cond\nfeatures=if:devel:x\n")))
    session = items["sockets=if:session-bus:has-foo"]
    assert session["level"] == "escape" and session["text"].endswith("(only when has-foo)") and "does not know" not in session["text"]
    assert items["sockets=if:x11:!has-wayland"]["level"] == "broad" and "X11" in items["sockets=if:x11:!has-wayland"]["text"]
    assert items["devices=if:all:cond"]["level"] == "broad" and items["features=if:devel:x"]["level"] == "broad"
    assert items["sockets=wayland"]["level"] == "normal"


def test_host_reset_and_the_condition_syntax_do_not_break_an_ordinary_filesystem_name():
    assert fp._fs_name("xdg-download:ro") == ("xdg-download", "ro") and fp._fs_name("host:reset") == ("host", "reset") and fp._fs_name("a:b") == ("a:b", "")


def test_an_override_that_is_not_a_keyfile_is_said_to_be_not_understood(tmp_path):
    user = tmp_path / "user"
    (user / "overrides").mkdir(parents=True)
    (user / "overrides" / "a.b.C").write_text("this line has no equals sign\n[Context]\nsockets=x11\n")  # GLib rejects the whole file
    (user / "overrides" / "global").write_text("# only a comment\n\n")  # not damaged: nothing to say
    result = fp.read("a.b.C", _env(tmp_path, _Ref("a.b.C", "[Context]\nshared=network\n")), user_path=user)
    assert [i["raw"] for i in result["items"]] == ["shared=network"] and any("a.b.C" in n and "could not be understood" in n for n in result["notes"])
    assert not any("global" in n for n in result["notes"])


def _two_copies(tmp_path):
    user_inst = NS(get_path=lambda: NS(get_path=lambda: str(tmp_path / "user")), get_is_user=lambda: True)
    system_inst = NS(get_path=lambda: NS(get_path=lambda: str(tmp_path / "system")), get_is_user=lambda: False)
    system_ref, user_ref = _Ref("a.b.C", "[Context]\nshared=ipc\n"), _Ref("a.b.C", "[Context]\nshared=network\n")
    return NS(Flatpak=NS(RefKind=NS(APP="APP")), installed_refs=lambda: [(system_inst, system_ref), (user_inst, user_ref)],  # the system's come first
              inst_id=lambda i: "user" if i is user_inst else "system")


def test_an_app_installed_twice_shows_the_copy_flatpak_run_would_start_unless_the_registry_names_one(tmp_path):
    env = _two_copies(tmp_path)
    assert [i["raw"] for i in fp.read("a.b.C", env, user_path=tmp_path / "user")["items"]] == ["shared=network"]  # the per-user copy
    assert [i["raw"] for i in fp.read("a.b.C", env, user_path=tmp_path / "user", installation="system")["items"]] == ["shared=ipc"]
    assert [i["raw"] for i in fp.read("a.b.C", env, user_path=tmp_path / "user", installation="user")["items"]] == ["shared=network"]
    assert [i["raw"] for i in fp.read("a.b.C", env, user_path=tmp_path / "user", installation="other")["items"]] == ["shared=network"]  # unknown: the default


def test_the_service_asks_for_the_installation_the_registry_row_names(tmp_path, monkeypatch):
    from cygnus.core.backends import flatpak as fb
    from cygnus.core.registry import db as regdb
    from cygnus.core.registry import open_registry
    from cygnus.gui import service

    monkeypatch.setattr(fb, "FlatpakEnv", lambda *a, **k: _two_copies(tmp_path))
    monkeypatch.setattr(fb, "user_dir", lambda: tmp_path / "user")
    reg = open_registry()
    regdb.add_application(reg, app_id="a.b.C", display_name="C")
    iid = regdb.add_installation(reg, app_id="a.b.C", fmt="flatpak", source={"ref": "app/a.b.C/x86_64/stable", "installation": "system"}, version="1",
                                 location_id=None, origin="installed")
    assert [i["raw"] for i in service.flatpak_permissions(iid)["items"]] == ["shared=ipc"]


@pytest.mark.parametrize("fmt,ref", [("appimage", ""), ("flatpak", ""), ("flatpak", "app"), ("flatpak", "app//x86_64/stable")])
def test_the_service_refuses_a_row_that_is_not_a_flatpak_it_can_name(tmp_path, monkeypatch, fmt, ref):
    from cygnus.gui import service

    _fake_flatpak(monkeypatch, tmp_path)
    with pytest.raises(CygnusError):
        service.flatpak_permissions(_flatpak_row(fmt, ref))


@pytest.mark.parametrize("asked", ["no-such-installation-id", "", None, 5, ["x"], {"a": 1}])
def test_the_service_refuses_an_installation_that_does_not_exist_and_anything_that_is_not_an_id(tmp_path, monkeypatch, asked):
    from cygnus.gui import service

    _fake_flatpak(monkeypatch, tmp_path)
    _flatpak_row()  # a real flatpak row exists: only the asked-for id is wrong
    with pytest.raises(CygnusError, match="not a Flatpak application Cygnus manages"):
        service.flatpak_permissions(asked)
