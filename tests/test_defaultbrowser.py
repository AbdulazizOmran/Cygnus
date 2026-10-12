"""Making a browser the default: what qualifies, what is set, what is read back, what is put back. The real xdg-settings,
xdg-mime, gio and KDE config tools run in the test's own isolated folders (no session variables, no D-Bus), so what happens
here is what happens on a machine and nothing real is touched."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cygnus.core import defaultbrowser as db
from cygnus.core import handlers, preferences
from cygnus.core.desktop import integrate
from cygnus.core.errors import CygnusError
from cygnus.core.util import proc

real = pytest.mark.needs_tool("xdg-settings", "xdg-mime", "gio")
kde = pytest.mark.needs_tool("xdg-settings", "xdg-mime", "gio", "kwriteconfig6", "kreadconfig6")
ALL = ";".join(db.MIME_TYPES) + ";"
PROGRAM = shutil.which("true") or "/usr/bin/true"  # an absolute path: xdg-settings resolves the entry's program itself


@pytest.fixture(autouse=True)
def _session(tmp_path, monkeypatch):
    """No desktop session at all (the tests may run inside a real KDE one), and the machine's own application folders out of
    sight, so a browser installed here cannot leak in. D-Bus is gone so no KDE tool can reach a running session."""
    for var in ("XDG_CURRENT_DESKTOP", "KDE_FULL_SESSION", "KDE_SESSION_VERSION", "DESKTOP_SESSION", "XDG_SESSION_DESKTOP",
                "GNOME_DESKTOP_SESSION_ID", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "no-system-data"))


@pytest.fixture
def apps():
    folder = integrate.xdg_data_home() / "applications"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _browser(apps: Path, name: str, *, claims: str = ALL, categories: str = "Network;WebBrowser;", exec_line: str = f"{PROGRAM} %U",
             extra: str = "") -> str:
    (apps / name).write_text(f"[Desktop Entry]\nType=Application\nName={name.removesuffix('.desktop').title()}\n"
                             f"Exec={exec_line}\nCategories={categories}\nMimeType={claims}\n{extra}")
    return name


def _config() -> Path:
    return Path(os.environ["XDG_CONFIG_HOME"])


def _mimeapps() -> Path:
    return _config() / "mimeapps.list"


def _seed(**defaults):
    _mimeapps().parent.mkdir(parents=True, exist_ok=True)
    _mimeapps().write_text("[Default Applications]\n" + "".join(f"{m}={d}\n" for m, d in defaults.items()))


def _parsed() -> dict[str, list[str]]:
    """What the desktop reads from the list for every type Cygnus looks at (a trailing ";" or the spacing of a line makes no difference)."""
    return {m: db._default_list(_mimeapps(), m) for m in db.TRACKED if db._default_list(_mimeapps(), m)}


def _lines() -> dict[str, str]:
    return {k: v for k, _, v in (line.partition("=") for line in _mimeapps().read_text().splitlines()) if k in db.MIME_TYPES}


# -- which entries can be the browser -------------------------------------------------------------------------------
def test_an_entry_that_claims_links_and_pages_and_starts_can_be_chosen(apps):
    verdict = db.assess(_browser(apps, "helium.desktop"))
    assert verdict["blockers"] == [] and verdict["name"] == "Helium"


def test_an_entry_that_leaves_a_type_out_is_refused_and_the_type_is_named(apps):
    verdict = db.assess(_browser(apps, "half.desktop", claims="x-scheme-handler/http;"))
    assert len(verdict["blockers"]) == 1
    assert "https links" in verdict["blockers"][0] and "web page files" in verdict["blockers"][0]
    assert "http links" not in verdict["blockers"][0]


def test_a_cygnus_entry_that_files_itself_as_a_browser_is_told_to_repair(apps):
    name = _browser(apps, "shy.desktop", claims="text/html;", extra="X-Cygnus-Managed=true\n")
    verdict = db.assess(name)
    assert verdict["repairable"] and "Repairing" in verdict["blockers"][0]
    other = db.assess(_browser(apps, "foreign.desktop", claims="text/html;"))
    assert not other["repairable"] and "Repairing" not in other["blockers"][0]


def test_an_entry_whose_program_is_missing_is_refused(apps):
    verdict = db.assess(_browser(apps, "gone.desktop", exec_line="/no/such/browser %U"))
    assert any("is not there" in b for b in verdict["blockers"])
    assert any("no program" in b for b in db.assess(_browser(apps, "empty.desktop", exec_line=""))["blockers"])


def test_hidden_and_unknown_and_odd_names_are_refused(apps):
    assert db.assess(_browser(apps, "h.desktop", extra="Hidden=true\n"))["blockers"]
    for odd in ("nothing.desktop", "-x.desktop", "../x.desktop", "a b.desktop", "x.desktop\n", "x"):
        assert "No menu entry" in " ".join(db.assess(odd)["blockers"]), odd
    _browser(apps, "-x.desktop")  # a file that really exists under a name that would read as an option
    _browser(apps, "a b.desktop")
    assert db.find_entry("-x.desktop") is None and db.find_entry("a b.desktop") is None
    with pytest.raises(CygnusError, match="No menu entry"):
        db.enable("-x.desktop")
    assert db.find_entry("../applications/h.desktop") is None


def test_a_browser_not_filed_as_one_is_allowed_with_a_note(apps):
    verdict = db.assess(_browser(apps, "plain.desktop", categories="Network;"))
    assert verdict["blockers"] == [] and any("Default applications" in n for n in verdict["notes"])


def test_a_fallback_name_is_explained(apps):
    verdict = db.assess(_browser(apps, "cygnus-helium.desktop", extra="X-Cygnus-Managed=true\n"))
    assert any("own menu name" in n for n in verdict["notes"])
    assert not db.assess(_browser(apps, "helium.desktop", extra="X-Cygnus-Managed=true\n"))["notes"]


def test_a_program_on_a_drive_that_can_be_unplugged_says_so(apps):
    shim = integrate.shim_dir() / "net.imput.helium"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nDRIVE_UUID='01DC1F07B0E23880'\nexec true \"$@\"\n")
    shim.chmod(0o755)
    notes = db.assess(_browser(apps, "helium.desktop", exec_line=f"{shim} %U"))["notes"]
    assert any("drive that can be unplugged" in n for n in notes)
    local = integrate.shim_dir() / "local"
    local.write_text("#!/bin/sh\nDRIVE_UUID=''\nexec true\n")
    local.chmod(0o755)
    assert not db.assess(_browser(apps, "local.desktop", exec_line=f"{local} %U"))["notes"]


def test_names_from_a_file_are_made_safe_to_show(apps):
    _browser(apps, "evil.desktop")
    (apps / "evil.desktop").write_text((apps / "evil.desktop").read_text().replace("Name=Evil", "Name=Ev\x1b[31mil\x07"))
    name = db.assess("evil.desktop")["name"]
    assert "\x1b" not in name and "\x07" not in name


# -- finding the browsers -------------------------------------------------------------------------------------------
def test_only_real_browsers_are_listed_and_your_own_entry_hides_the_systems(apps, tmp_path, monkeypatch):
    system = tmp_path / "system-data"
    (system / "applications").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_DIRS", str(system))
    _browser(apps, "mine.desktop", extra="X-Cygnus-Managed=true\n")
    _browser(system / "applications", "mine.desktop", claims="x-scheme-handler/http;")  # hidden by yours: not a browser as written there
    _browser(system / "applications", "other.desktop")
    _browser(system / "applications", "twin.desktop", extra="NoDisplay=true\n")  # Chrome's second entry
    _browser(system / "applications", "mail.desktop", categories="Network;Email;")  # claims links but is no browser
    _browser(system / "applications", "nolinks.desktop", claims="text/html;")
    found = {b["desktop_id"]: b for b in db.candidates()}
    assert set(found) == {"mine.desktop", "other.desktop"}
    assert found["mine.desktop"]["managed"] and not found["other.desktop"]["managed"]


def test_a_huge_folder_is_read_only_up_to_a_limit(apps, monkeypatch):
    monkeypatch.setattr(db, "MAX_FOUND", 3)
    for n in range(8):
        _browser(apps, f"b{n}.desktop")
    assert len(db.candidates()) == 3


# -- making one the default, for real ---------------------------------------------------------------------------------
@real
def test_choosing_sets_all_three_types_and_remembers_what_was_there(apps):
    _browser(apps, "helium.desktop")
    _browser(apps, "firefox.desktop")
    _seed(**{"x-scheme-handler/https": "firefox.desktop"})
    state = db.enable("helium.desktop")
    assert state["changed"] and state["default"] == "helium.desktop" and state["consistent"]
    assert _lines() == {m: "helium.desktop" for m in db.MIME_TYPES}
    assert state["explicit"] == _lines() and state["chosen_here"] == "helium.desktop"
    memory = preferences.load()["browser_backup"]
    assert memory["mime"]["x-scheme-handler/https"] == "firefox.desktop" and memory["mime"]["text/html"] == ""
    assert memory["chosen"] == "helium.desktop"


@real
def test_choosing_again_changes_nothing_and_choosing_another_keeps_the_first_memory(apps):
    _browser(apps, "helium.desktop")
    _browser(apps, "zen.desktop")
    _seed(**{"text/html": "firefox.desktop"})
    db.enable("helium.desktop")
    assert db.enable("helium.desktop")["changed"] is False
    db.enable("zen.desktop")
    memory = preferences.load()["browser_backup"]
    assert memory["mime"]["text/html"] == "firefox.desktop" and memory["chosen"] == "zen.desktop"  # still what it was before Cygnus
    assert db.status()["default"] == "zen.desktop"


@pytest.mark.needs_tool("xdg-settings", "xdg-mime", "gio", "update-desktop-database")
def test_being_first_by_accident_is_not_the_same_as_being_chosen(apps):
    _browser(apps, "helium.desktop")
    assert proc.run(["update-desktop-database", "-q", str(apps)]).ok  # what Cygnus does whenever it writes an entry
    state = db.status()
    assert state["default"] == "helium.desktop" and state["explicit"] == {m: "" for m in db.MIME_TYPES}  # nothing chosen yet
    assert db.enable("helium.desktop")["changed"] is True  # so it IS set, on purpose
    assert _lines() == {m: "helium.desktop" for m in db.MIME_TYPES}


@real
def test_putting_back_restores_what_each_type_was(apps):
    _browser(apps, "helium.desktop")
    _browser(apps, "firefox.desktop")
    _seed(**{"x-scheme-handler/https": "firefox.desktop"})
    db.enable("helium.desktop")
    state = db.restore()
    assert state["left_alone"] == []
    assert _parsed() == {"x-scheme-handler/https": ["firefox.desktop"]}  # the other two had nothing: no leftover line
    assert "helium" not in _mimeapps().read_text()  # not even for the about: and unknown-link types xdg-settings also sets
    assert "browser_backup" not in preferences.load()
    with pytest.raises(CygnusError, match="nothing to put back"):
        db.restore()


@real
def test_a_type_changed_since_is_left_as_it_was_set(apps):
    _browser(apps, "helium.desktop")
    _browser(apps, "firefox.desktop")
    db.enable("helium.desktop")
    _mimeapps().write_text(_mimeapps().read_text().replace("text/html=helium.desktop", "text/html=firefox.desktop"))
    state = db.restore()
    assert state["left_alone"] == ["web page files"]
    assert _lines() == {"text/html": "firefox.desktop"}


@real
def test_a_name_added_to_a_list_after_the_choice_is_yours_and_stays(apps):
    """Found by review: undo looked only at the first name, so a list Cygnus had left as [helium] and you made [helium, vivaldi] was deleted whole."""
    for name in ("helium.desktop", "firefox.desktop", "vivaldi.desktop"):
        _browser(apps, name)
    _seed(**{"x-scheme-handler/https": "firefox.desktop"})
    db.enable("helium.desktop")
    handlers._write_default("x-scheme-handler/http", ["helium.desktop", "vivaldi.desktop"])
    state = db.restore()
    assert state["left_alone"] == ["http links"]
    assert _parsed()["x-scheme-handler/http"] == ["helium.desktop", "vivaldi.desktop"]  # untouched
    assert _parsed()["x-scheme-handler/https"] == ["firefox.desktop"] and "text/html" not in _parsed()  # the others went back


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to a read-only folder")
@real
def test_an_undo_that_failed_keeps_the_memory_so_it_can_be_tried_again(apps):
    """Found by review: restore() forgot the remembered choice before it knew the undo had worked, so a failure left nothing to retry with."""
    _browser(apps, "helium.desktop")
    _browser(apps, "firefox.desktop")
    _seed(**{"x-scheme-handler/https": "firefox.desktop"})
    db.enable("helium.desktop")
    _config().chmod(0o500)
    try:
        with pytest.raises(CygnusError, match="could not be put back.*still remembers"):
            db.restore()
    finally:
        _config().chmod(0o700)
    assert db.status()["chosen_here"] == "helium.desktop"  # the "Put back" offer is still there
    state = db.restore()
    assert state["left_alone"] == []  # what the first try had already put back is not reported as somebody else's
    assert _parsed() == {"x-scheme-handler/https": ["firefox.desktop"]} and "browser_backup" not in preferences.load()


@real
def test_an_undo_finished_in_two_tries_does_not_call_the_first_tries_work_somebody_elses(apps):
    _browser(apps, "helium.desktop")
    _browser(apps, "firefox.desktop")
    _seed(**{"x-scheme-handler/https": "firefox.desktop"})
    db.enable("helium.desktop")
    handlers._write_default("x-scheme-handler/https", ["firefox.desktop"])  # what a first, half-finished try had already put back
    state = db.restore()
    assert state["left_alone"] == []
    assert _parsed() == {"x-scheme-handler/https": ["firefox.desktop"]} and "browser_backup" not in preferences.load()


@real
def test_a_second_choice_that_fails_and_cannot_be_undone_keeps_its_own_fresh_memory(apps, monkeypatch):
    _browser(apps, "a.desktop")
    _browser(apps, "b.desktop")
    db.enable("a.desktop")
    assert "after" in preferences.load()["browser_backup"]
    write = handlers._write_default

    def run(argv, **kw):
        if argv[:2] == ["xdg-settings", "set"]:  # a half-done choice: one line written, then a failure
            write("x-scheme-handler/http", ["b.desktop"])
            return NS(stdout="", stderr="boom", returncode=1)
        return proc.run(argv, **kw)

    monkeypatch.setattr(db.handlers, "_write_default", lambda *a: (_ for _ in ()).throw(OSError(13, "Permission denied")))
    with pytest.raises(CygnusError, match="could not be put back"):
        db.enable("b.desktop", run)
    memory = preferences.load()["browser_backup"]
    assert memory["chosen"] == "b.desktop"  # the undo failed, so what must still be undone is remembered
    assert "after" not in memory  # and the first choice's leftovers do not describe this one


@real
def test_names_glib_accepts_but_a_menu_entry_could_not_have_survive_the_round_trip(apps):
    _browser(apps, "helium.desktop")
    _browser(apps, "firefox.desktop")
    _seed(**{"x-scheme-handler/http": "ex@ample.desktop;firefox.desktop;"})
    db.enable("helium.desktop")
    db.restore()
    assert _parsed() == {"x-scheme-handler/http": ["ex@ample.desktop", "firefox.desktop"]}


def test_a_remembered_choice_with_a_bad_after_list_counts_as_nothing_remembered(apps):
    prefs = preferences.load()
    good = {"mime": {}, "chosen": "helium.desktop", "after": {"text/html": ["helium.desktop"]}}
    prefs["browser_backup"] = good
    preferences.save(prefs)
    assert db._backup() == good
    for bad in ({"text/html": "helium.desktop"}, {"text/html": [1]}, {"text/html": ["../x"]}, [], "x"):
        prefs["browser_backup"] = {**good, "after": bad}
        preferences.save(prefs)
        assert db._backup() is None, bad


@real
def test_an_entry_that_cannot_be_the_browser_changes_nothing(apps):
    _browser(apps, "half.desktop", claims="x-scheme-handler/http;")
    with pytest.raises(CygnusError, match="cannot be the default browser"):
        db.enable("half.desktop")
    assert not _mimeapps().exists() and "browser_backup" not in preferences.load()


@real
def test_a_desktop_specific_list_that_wins_is_named_and_everything_is_put_back(apps, monkeypatch):
    _browser(apps, "helium.desktop")
    _browser(apps, "firefox.desktop")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    _config().mkdir(parents=True, exist_ok=True)
    (_config() / "kde-mimeapps.list").write_text("[Default Applications]\nx-scheme-handler/http=firefox.desktop\n")
    _seed(**{"text/html": "firefox.desktop"})
    before = _parsed()
    with pytest.raises(CygnusError) as err:
        db.enable("helium.desktop")
    assert "kde-mimeapps.list" in str(err.value) and "http links still open with firefox.desktop" in str(err.value)
    assert "put back" in str(err.value)
    assert _parsed() == before and "browser_backup" not in preferences.load()


@real
def test_a_choice_that_does_not_stick_is_reported_and_undone(apps):
    _browser(apps, "helium.desktop")
    _seed(**{"text/html": "firefox.desktop"})
    before = _parsed()

    def run(argv, **kw):  # xdg-settings writes the lines; the desktop keeps answering "Firefox"
        if argv[0] == "gio":
            return NS(stdout="Default application for “x”: firefox.desktop\nRegistered applications:\n", stderr="", returncode=0)
        return proc.run(argv, **kw)

    with pytest.raises(CygnusError, match="http links still open with firefox.desktop.*put back as they were"):
        db.enable("helium.desktop", run)
    assert _parsed() == before and "helium" not in _mimeapps().read_text()  # what xdg-settings had written is gone again
    assert "browser_backup" not in preferences.load()


@real
def test_a_failed_second_choice_keeps_the_memory_of_the_first(apps):
    _browser(apps, "helium.desktop")
    _browser(apps, "zen.desktop")
    db.enable("helium.desktop")
    memory = dict(preferences.load()["browser_backup"])

    def run(argv, **kw):  # the second choice runs into a timeout half way
        if argv[:2] == ["xdg-settings", "set"]:
            raise CygnusError("timed out")
        return proc.run(argv, **kw)

    with pytest.raises(CygnusError, match="timed out.*put back as they were"):
        db.enable("zen.desktop", run)
    assert preferences.load()["browser_backup"] == memory  # still says helium was chosen, and helium still is
    assert db.status()["default"] == "helium.desktop" and _lines() == {m: "helium.desktop" for m in db.MIME_TYPES}


# -- KDE's own setting --------------------------------------------------------------------------------------------------
def _kde(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("KDE_SESSION_VERSION", "6")
    monkeypatch.setenv("KDE_FULL_SESSION", "true")


@kde
def test_on_kde_the_browser_setting_is_written_and_read_back(apps, monkeypatch):
    _kde(monkeypatch)
    _browser(apps, "helium.desktop")
    state = db.enable("helium.desktop")
    assert state["kde"] == "helium.desktop" and "BrowserApplication=helium.desktop" in (_config() / "kdeglobals").read_text()


@kde
def test_without_the_session_variables_xdg_settings_could_not_write_it(apps, monkeypatch):
    """Regression: the process helper passes almost no environment, and without KDE_SESSION_VERSION xdg-settings runs a command
    that does not exist and fails. The module passes the variables on; this proves they are what makes the difference."""
    _kde(monkeypatch)
    _browser(apps, "helium.desktop")
    monkeypatch.setattr(db, "_SESSION", tuple(v for v in db._SESSION if v != "KDE_SESSION_VERSION"))
    with pytest.raises(CygnusError):
        db.enable("helium.desktop")
    assert "BrowserApplication" not in ((_config() / "kdeglobals").read_text() if (_config() / "kdeglobals").exists() else "")
    assert not _mimeapps().exists() or "helium" not in _mimeapps().read_text()  # and what it half did was undone


@kde
def test_a_kde_setting_that_does_not_agree_is_reported_and_everything_is_undone(apps, monkeypatch):
    _kde(monkeypatch)
    _browser(apps, "helium.desktop")

    def run(argv, **kw):  # the lists are fine, but KDE's own setting keeps saying Firefox
        if argv[0] == "kreadconfig6":
            return NS(stdout="firefox.desktop\n", stderr="", returncode=0)
        return proc.run(argv, **kw)

    with pytest.raises(CygnusError, match="KDE's own browser setting says firefox.desktop.*put back as they were"):
        db.enable("helium.desktop", run)
    assert not _mimeapps().exists() or "helium" not in _mimeapps().read_text()
    assert "browser_backup" not in preferences.load()


@kde
def test_on_kde_putting_back_restores_or_clears_the_setting(apps, monkeypatch):
    _kde(monkeypatch)
    _browser(apps, "helium.desktop")
    _browser(apps, "firefox.desktop")
    db.enable("helium.desktop")
    assert db.restore()["kde"] == ""  # it had no value before: the key is gone again
    assert "BrowserApplication" not in (_config() / "kdeglobals").read_text()
    (_config() / "kdeglobals").write_text("[General]\nBrowserApplication=firefox.desktop\n")
    db.enable("helium.desktop")
    assert db.status()["kde"] == "helium.desktop"
    assert db.restore()["kde"] == "firefox.desktop"


@kde
def test_a_kde_value_changed_since_is_not_overwritten_when_putting_back(apps, monkeypatch):
    _kde(monkeypatch)
    _browser(apps, "helium.desktop")
    db.enable("helium.desktop")
    (_config() / "kdeglobals").write_text("[General]\nBrowserApplication=vivaldi.desktop\n")
    assert db.restore()["kde"] == "vivaldi.desktop"


# -- the small rules ----------------------------------------------------------------------------------------------------
def test_without_the_tools_it_says_which_are_missing(apps, monkeypatch):
    _browser(apps, "helium.desktop")
    monkeypatch.setattr(db.shutil, "which", lambda name: None)
    state = db.status()
    assert state["available"] is False and "xdg-settings and xdg-mime and gio" in state["why"]
    with pytest.raises(CygnusError, match="not installed"):
        db.enable("helium.desktop")
    with pytest.raises(CygnusError, match="not installed"):
        db.restore()


def test_a_hand_edited_memory_that_has_the_wrong_shape_counts_as_nothing_remembered():
    for bad in ("text", [], {"mime": [], "chosen": "x"}, {"mime": {"a": 1}, "chosen": "x"}, {"mime": {}, "chosen": 3},
                {"mime": {}, "chosen": "x", "kde": 5}):
        preferences.save({"browser_backup": bad})
        assert db._backup() is None, bad
    preferences.save({"browser_backup": {"mime": {"text/html": ""}, "chosen": "x.desktop", "kde": ""}})
    assert db._backup()["chosen"] == "x.desktop"


def test_the_environment_passed_to_xdg_settings_carries_the_session(monkeypatch):
    monkeypatch.setenv("KDE_SESSION_VERSION", "6")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("LD_PRELOAD", "/evil.so")
    env = db._env()
    assert env["KDE_SESSION_VERSION"] == "6" and env["XDG_CURRENT_DESKTOP"] == "KDE" and "LD_PRELOAD" not in env


def test_only_kde_five_and_six_get_a_kde_tool(monkeypatch):
    monkeypatch.setattr(db.shutil, "which", lambda name: f"/usr/bin/{name}")
    for version, expected in (("6", "kreadconfig6"), ("5", "kreadconfig5"), ("4", None), ("", None), ("6; rm -rf", None)):
        monkeypatch.setenv("KDE_SESSION_VERSION", version)
        assert db._kde_tool("kreadconfig") == expected


# -- the menu entry Cygnus writes ----------------------------------------------------------------------------------------
def test_a_browser_that_leaves_the_types_out_gets_them_and_a_note():
    mime, added = integrate.complete_browser_types("text/html;application/pdf;", "Network;WebBrowser;")
    assert mime == "text/html;application/pdf;x-scheme-handler/http;x-scheme-handler/https;"
    assert added == ["x-scheme-handler/http", "x-scheme-handler/https"]
    assert integrate.complete_browser_types("", "Network;WebBrowser;")[0] == ";".join(integrate.BROWSER_MIME_TYPES) + ";"


def test_a_complete_browser_and_an_ordinary_program_are_left_alone():
    full = ";".join(integrate.BROWSER_MIME_TYPES) + ";application/pdf;"
    assert integrate.complete_browser_types(full, "Network;WebBrowser;") == (full, [])
    assert integrate.complete_browser_types("text/plain;", "Utility;TextEditor;") == ("text/plain;", [])
    assert integrate.complete_browser_types("", "Network;") == ("", [])  # not filed as a browser: not made into one


def _plan(upstream: dict):
    return integrate.plan_appimage_integration(app_key="net.imput.test", display_name="Test", appimage_path="/x/Test.AppImage",
                                               fs_uuid=None, location_label="SSD", upstream_desktop=upstream,
                                               upstream_desktop_id="test.desktop", icon=None, icon_kind=None)


def _written_entry(plan):
    from cygnus.core.desktop import entry as desktop_entry

    data = next(f.data for f in plan.files if f.kind == "desktop_entry")
    return desktop_entry.parse(data.decode())


def test_the_entry_written_for_a_trimmed_browser_can_be_chosen(apps):
    plan = _plan({"Name": "Test", "Exec": "test %U", "Categories": "Network;WebBrowser;", "MimeType": "text/html;"})
    written = _written_entry(plan)
    assert set(db.MIME_TYPES) <= set(written.get_list("MimeType"))
    assert any("did not say it opens" in n for n in plan.notes)
    (apps / "test.desktop").write_text(written.serialize())  # and the chooser's own rule agrees it now qualifies
    assert not any("does not say" in b for b in db.assess("test.desktop")["blockers"])


def test_the_entry_written_for_an_ordinary_program_is_unchanged():
    plan = _plan({"Name": "Test", "Exec": "test %U", "Categories": "Utility;", "MimeType": "text/plain;"})
    assert _written_entry(plan).get("MimeType") == "text/plain;" and not any("did not say" in n for n in plan.notes)


# -- round 16 findings -------------------------------------------------------------------------------------------------------------------------
@real
def test_a_line_spaced_by_hand_does_not_make_a_good_choice_look_like_a_failure_and_undo_still_works(apps):
    """xdg-mime only edits lines written as `type=name`; given `type = name` it adds a second line. The desktop reads the LAST one, so the choice
    took, and so must Cygnus's reading, its memory and its undo."""
    _browser(apps, "helium.desktop")
    _browser(apps, "firefox.desktop")
    _mimeapps().parent.mkdir(parents=True, exist_ok=True)
    _mimeapps().write_text("[Default Applications]\nx-scheme-handler/http = firefox.desktop\n")
    state = db.enable("helium.desktop")
    assert state["changed"] and state["default"] == "helium.desktop" and state["explicit"]["x-scheme-handler/http"] == "helium.desktop"
    db.restore()
    assert db._default_list(_mimeapps(), "x-scheme-handler/http") == ["firefox.desktop"] and "helium" not in _mimeapps().read_text()


@real
def test_a_default_list_with_fallbacks_comes_back_whole(apps):
    _browser(apps, "helium.desktop")
    _seed(**{"x-scheme-handler/http": "firefox.desktop;chromium.desktop;", "text/html": "a.desktop;b.desktop;c.desktop;"})
    db.enable("helium.desktop")
    assert db._default_list(_mimeapps(), "x-scheme-handler/http") == ["helium.desktop"]
    db.restore()
    assert db._default_list(_mimeapps(), "x-scheme-handler/http") == ["firefox.desktop", "chromium.desktop"]
    assert db._default_list(_mimeapps(), "text/html") == ["a.desktop", "b.desktop", "c.desktop"]


def test_the_list_is_read_the_way_glib_reads_it_last_line_wins_and_only_real_names(tmp_path):
    path = tmp_path / "mimeapps.list"
    path.write_text("[Default Applications]\nx = a.desktop\nx=b.desktop;-bad.desktop;c d.desktop;ok.desktop\n[Other]\nx=zzz.desktop\n")
    assert db._default_list(path, "x") == ["b.desktop", "ok.desktop"]
    assert db._default_list(tmp_path / "missing", "x") == []


def test_writing_a_default_list_changes_only_that_type_and_keeps_the_rest(tmp_path, monkeypatch):
    from cygnus.core import handlers

    path = Path(os.environ["XDG_CONFIG_HOME"]) / "mimeapps.list"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# my comment\n[Added Associations]\ntext/html=old.desktop;\n\n[Default Applications]\na/b = x.desktop\ntext/html=old.desktop;\na/b=y.desktop;\nc/d=z.desktop;\n")
    handlers._write_default("a/b", ["one.desktop", "two.desktop"])
    text = path.read_text()
    assert text.count("a/b") == 1 and "a/b=one.desktop;two.desktop;" in text and "# my comment" in text and "[Added Associations]" in text
    assert "c/d=z.desktop;" in text and "text/html=old.desktop;" in text and text.index("a/b=") < text.index("c/d=")
    handlers._write_default("a/b", [])
    assert "a/b" not in path.read_text() and "c/d=z.desktop;" in path.read_text()
    before = path.read_bytes()
    handlers._write_default("not/there", [])
    assert path.read_bytes() == before  # nothing wanted, nothing there: the file is not rewritten
    handlers._write_default("new/type", ["n.desktop"])
    assert "new/type=n.desktop;" in path.read_text()


def test_a_default_list_is_created_with_its_section_when_there_is_no_file():
    from cygnus.core import handlers

    path = Path(os.environ["XDG_CONFIG_HOME"]) / "mimeapps.list"
    assert not path.exists()
    handlers._write_default("a/b", ["x.desktop"])
    assert path.read_text() == "[Default Applications]\na/b=x.desktop;\n"


def test_a_named_pipe_with_a_menu_entry_s_name_does_not_hold_the_listing_up(apps):
    _browser(apps, "real.desktop")
    os.mkfifo(apps / "fifo.desktop")
    assert [b["desktop_id"] for b in db.candidates()] == ["real.desktop"]
    assert db._load(apps / "fifo.desktop") is None and db._load(apps / "nothing.desktop") is None


def test_a_bare_program_name_is_looked_up_where_the_desktop_tools_look_not_on_the_session_path(apps, tmp_path, monkeypatch):
    private = tmp_path / "private-bin"
    private.mkdir()
    program = private / "myhelium"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{private}:{os.environ['PATH']}")  # on the session's path only
    verdict = db.assess(_browser(apps, "mine.desktop", exec_line="myhelium %U"))
    assert verdict["blockers"] and "/usr/bin" in verdict["blockers"][0] and "full path" in verdict["blockers"][0]
    assert not db.assess(_browser(apps, "full.desktop", exec_line=f"{program} %U"))["blockers"]


@pytest.mark.parametrize("bad", [
    {"mime": {"x-scheme-handler/http": "evil\\nfoo.desktop"}, "chosen": "a.desktop"},
    {"mime": {"x-scheme-handler/http": "-rf.desktop"}, "chosen": "a.desktop"},
    {"mime": {}, "chosen": "a b.desktop"}, {"mime": {}, "chosen": "a.desktop", "kde": "--delete"},
    {"mime": {}, "chosen": "a.desktop", "kde": "x\nBrowserApplication=y"},
    {"mime": {}, "chosen": "a.desktop", "lists": {"x": "not a list"}}, {"mime": {}, "chosen": "a.desktop", "lists": {"x": ["-bad.desktop"]}}])
def test_a_remembered_value_that_could_not_be_a_menu_entry_or_could_be_read_as_an_option_is_not_trusted(bad):
    preferences.save({"browser_backup": bad})
    assert db._backup() is None


def test_a_remembered_state_with_lists_and_a_plain_kde_value_is_accepted():
    preferences.save({"browser_backup": {"mime": {"text/html": "a.desktop"}, "lists": {"text/html": ["a.desktop", "b.desktop"]}, "chosen": "h.desktop",
                                         "kde": "firefox"}})
    assert db._backup()["chosen"] == "h.desktop"


@real
def test_a_failed_choice_on_kde_puts_kdes_own_setting_back_too(apps, monkeypatch):
    """xdg-settings writes BrowserApplication before the read-back fails; the rollback must remove it again."""
    _kde(monkeypatch)
    _browser(apps, "helium.desktop")

    def run(argv, **kw):  # the lists are fine, but the desktop keeps answering Firefox when asked
        if argv[0] == "gio":
            return NS(stdout="Default application for “x”: firefox.desktop\nRegistered applications:\n", stderr="", returncode=0)
        return proc.run(argv, **kw)

    with pytest.raises(CygnusError, match="put back as they were"):
        db.enable("helium.desktop", run)
    assert "BrowserApplication" not in ((_config() / "kdeglobals").read_text() if (_config() / "kdeglobals").exists() else "")


@real
def test_a_disagreeing_xdg_settings_check_alone_is_reported_and_undone(apps):
    _browser(apps, "helium.desktop")

    def run(argv, **kw):  # every other place agrees; only the program browsers ask says no
        if argv[:3] == ["xdg-settings", "check", "default-web-browser"]:
            return NS(stdout="no\n", stderr="", returncode=0)
        return proc.run(argv, **kw)

    with pytest.raises(CygnusError, match="xdg-settings, the program browsers ask, does not agree.*put back as they were"):
        db.enable("helium.desktop", run)
    assert not _mimeapps().exists() or "helium" not in _mimeapps().read_text()
    assert "browser_backup" not in preferences.load()


# -- the undo never raises over a write that fails -----------------------------------------------------------------------------------------------
@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to a read-only folder")
@real
def test_an_undo_that_cannot_write_says_so_instead_of_raising(apps):
    _browser(apps, "helium.desktop")
    _browser(apps, "firefox.desktop")
    _seed(**{"x-scheme-handler/https": "firefox.desktop"})
    db.enable("helium.desktop")
    _config().chmod(0o500)  # the settings folder can no longer be written
    try:
        with pytest.raises(CygnusError, match="could not be put back"):
            db.restore()
    finally:
        _config().chmod(0o700)


def test_a_failed_choice_whose_rollback_cannot_write_reports_the_real_reason_and_the_failed_undo(apps, monkeypatch):
    _browser(apps, "helium.desktop")
    monkeypatch.setattr(db.handlers, "_write_default", lambda *a: (_ for _ in ()).throw(OSError(13, "Permission denied")))

    def run(argv, **kw):
        if argv[0] == "gio":
            return NS(stdout="Default application for \u201cx\u201d: firefox.desktop\nRegistered applications:\n", stderr="", returncode=0)
        if argv[:2] == ["xdg-settings", "set"]:  # a half-done choice: it wrote one line and then the desktop still says Firefox
            _mimeapps().write_text(_mimeapps().read_text() + "x-scheme-handler/http=helium.desktop;\n")
            return NS(stdout="", stderr="", returncode=0)
        return NS(stdout="no\n", stderr="", returncode=0)

    monkeypatch.setattr(db.shutil, "which", lambda name: f"/usr/bin/{name}")
    _mimeapps().parent.mkdir(parents=True, exist_ok=True)
    _mimeapps().write_text("[Default Applications]\ntext/html=old.desktop;\n")
    with pytest.raises(CygnusError) as err:  # the read-back fails, and putting the line back cannot write either: both are said, nothing is raised
        db.enable("helium.desktop", run)
    assert "could not be made the default browser" in str(err.value) and "could not be put back" in str(err.value)


def test_the_writers_cut_lines_only_where_glib_does(monkeypatch):
    """Found by review: str.splitlines also cuts at U+2028 and form feeds, so a comment became two lines, the second without an "=", and GLib then
    ignores the whole file. A CRLF file keeps its line ends too."""
    raw = "# note\u2028end\x0cfeed\r\n[Default Applications]\r\ntext/html=old.desktop;\r\nx-scheme-handler/http=a.desktop;\r\n".encode()
    _mimeapps().parent.mkdir(parents=True, exist_ok=True)
    _mimeapps().write_bytes(raw)
    assert db._default_list(_mimeapps(), "text/html") == ["old.desktop"]
    handlers._write_default("text/html", ["new.desktop"])
    written = _mimeapps().read_bytes()
    assert b"# note\xe2\x80\xa8end\x0cfeed\r\n" in written and b"text/html=new.desktop;" in written and b"old.desktop" not in written
    assert b"x-scheme-handler/http=a.desktop;\r\n" in written
    handlers._forget_default("x-scheme-handler/http", "a.desktop")
    after = _mimeapps().read_bytes()
    assert b"# note\xe2\x80\xa8end\x0cfeed\r\n" in after and b"a.desktop" not in after
    assert [line for line in after.decode().split("\n") if line and "=" not in line and not line.startswith(("#", "["))] == []


def test_set_list_trusts_the_file_and_not_the_writer(apps, monkeypatch):
    _mimeapps().parent.mkdir(parents=True, exist_ok=True)
    _mimeapps().write_text("[Default Applications]\ntext/html=old.desktop;\n")
    assert db._set_list("text/html", ["new.desktop"]) is True
    assert db._default_list(_mimeapps(), "text/html") == ["new.desktop"]
    monkeypatch.setattr(db.handlers, "_write_default", lambda *a: None)  # a writer that says nothing and changes nothing
    assert db._set_list("text/html", ["other.desktop"]) is False


def test_a_kde_setting_that_cannot_be_written_back_is_reported(monkeypatch):
    monkeypatch.setenv("KDE_SESSION_VERSION", "6")
    monkeypatch.setattr(db.shutil, "which", lambda name: f"/usr/bin/{name}")

    def slow(argv, **kw):
        raise CygnusError("timed out")

    assert db._kde_set("x.desktop", slow) is False
    assert db._kde_set("x.desktop", lambda argv, **kw: NS(stdout="", stderr="", returncode=1)) is False
    assert db._kde_set("x.desktop", lambda argv, **kw: NS(stdout="", stderr="", returncode=0)) is True
