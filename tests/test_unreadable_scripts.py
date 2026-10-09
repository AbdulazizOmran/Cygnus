"""Install scripts Cygnus cannot read: conversion never runs them, so a person who knows that may go ahead, after reading what
they mention. Anything Cygnus positively recognises as dangerous stays blocked (Chrome's scripts, with functions and command
substitution, are the case that matters)."""

import subprocess

import pytest

import builders
from cygnus.core.backends import foreign
from cygnus.core.detect import detect_file
from cygnus.core.errors import CygnusError
from cygnus.core.ops import convert
from cygnus.gui import service

pytestmark = pytest.mark.needs_tool("makepkg", "fakeroot", "bsdtar", "zstd")

UNREADABLE = """#!/bin/sh
set -e
size="$(echo ${icon} | sed 's/[^0-9]//g')"
setup_group() {
  getent group chromemgmt > /dev/null || groupadd chromemgmt
}
# useradd is only mentioned in this comment
update-alternatives --install /usr/bin/x-www-browser x-www-browser /usr/bin/hello 200
"""
FILES = {"usr/bin/hello": b"#!/bin/sh\necho hello\n", "usr/share/hello/a.txt": b"a\n", "usr/share/hello/b.txt": b"b\n"}


def _verdict(tmp_path, scripts, files=FILES):
    cand = detect_file(str(builders.build_deb(tmp_path, scripts=scripts, files=files)))
    return cand, foreign.analyse(cand, satisfy=lambda deps: {}, sonames_on_host=set(), glibc_on_host=(2, 40))


def test_scripts_too_complex_to_read_may_be_gone_past_knowingly(tmp_path):
    cand, v = _verdict(tmp_path, {"postinst": UNREADABLE})
    assert v.strategy == "review" and v.scripts_acknowledgeable
    assert {i.code: i.severity.value for i in v.issues}["FOREIGN_SCRIPTS_UNRECOGNISED"] == "blocker"  # still blocked by default
    assert "chromemgmt" in v.script_sources["postinst"]  # the person can read the whole script
    mentioned = {m["command"]: m for m in v.script_mentions}
    assert set(mentioned) == {"groupadd", "update-alternatives"}  # useradd is only in a comment; getent/sed are not notable
    assert mentioned["groupadd"]["scripts"] == ["postinst"] and "group" in mentioned["groupadd"]["what"]


def test_without_the_acknowledgement_nothing_is_converted(tmp_path):
    cand, v = _verdict(tmp_path, {"postinst": UNREADABLE})
    with pytest.raises(CygnusError, match="only converts packages its analysis found safe"):
        convert.convert(cand, v, tmp_path / "out")


def test_with_it_the_package_is_built_and_says_the_scripts_were_not_run(tmp_path):
    cand, v = _verdict(tmp_path, {"postinst": UNREADABLE})
    pkg, notes = convert.convert(cand, v, tmp_path / "out", accept_unread_scripts=True)
    assert "install scripts that Cygnus could not read were not run" in notes
    listing = subprocess.run(["bsdtar", "-tf", str(pkg)], capture_output=True, text=True).stdout
    assert "usr/bin/hello" in listing and ".INSTALL" not in listing  # no script travels into the pacman package


@pytest.mark.parametrize("script", ["#!/bin/sh\nuseradd -r hello\n", "#!/bin/sh\nmodprobe loop\n",
                                    "#!/bin/sh\n. /usr/share/debconf/confmodule\ndb_get x\n"])
def test_a_script_cygnus_can_read_that_creates_users_or_loads_modules_is_never_gone_past(tmp_path, script):
    cand, v = _verdict(tmp_path, {"postinst": script})
    assert v.strategy == "review" and not v.scripts_acknowledgeable
    with pytest.raises(CygnusError, match="only converts packages its analysis found safe"):
        convert.convert(cand, v, tmp_path / "out", accept_unread_scripts=True)


def test_a_user_the_package_declares_is_never_gone_past_even_beside_unreadable_scripts():
    scripts = foreign.ScriptAnalysis(unknown=["[postinst] command substitution"],
                                     blocking=["creates a system user or group: u hello - - -"])
    assert scripts.acknowledgeable is False
    assert foreign.ScriptAnalysis(unknown=["x"]).acknowledgeable is True
    assert foreign.ScriptAnalysis(blocking=["x"]).acknowledgeable is False
    assert foreign.ScriptAnalysis().acknowledgeable is False


def test_what_could_matter_if_dropped_is_flagged_and_listed_first(tmp_path):
    # inside an unreadable script a group is only mentioned, not found: so it is put in front of the person
    _, v = _verdict(tmp_path, {"postinst": UNREADABLE})
    assert [(m["command"], m["important"]) for m in v.script_mentions] == [("groupadd", True),
                                                                            ("update-alternatives", False)]


def test_the_acknowledgement_does_not_override_other_blockers(tmp_path):
    files = {**FILES, "etc/sudoers.d/hello": b"hello ALL=(ALL) NOPASSWD: ALL\n"}
    cand, v = _verdict(tmp_path, {"postinst": UNREADABLE}, files)
    assert v.strategy == "refuse" and not v.scripts_acknowledgeable
    with pytest.raises(CygnusError):
        convert.convert(cand, v, tmp_path / "out", accept_unread_scripts=True)


def test_a_clean_package_needs_no_acknowledgement(tmp_path):
    cand, v = _verdict(tmp_path, {"postinst": "#!/bin/sh\nupdate-desktop-database\n"})
    assert v.strategy == "convert" and not v.scripts_acknowledgeable and v.script_sources == {}


def test_words_are_matched_whole_not_inside_other_words():
    found = foreign.mentioned_commands({"postinst": "mygroupadd x\nsystemctl-foo\n# modprobe\n  groupadd  x\n"})
    assert [m["command"] for m in found] == ["groupadd"]


# -- through the GUI service ----------------------------------------------------------------------------------------
@pytest.fixture
def offline(monkeypatch):
    from cygnus.core.backends import pacman as pm

    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    monkeypatch.setattr(pm, "read_config", lambda: cfg)
    monkeypatch.setattr(pm, "run_worker", lambda c, req: {"packages": {}, "satisfiers": {}})
    monkeypatch.setattr("cygnus.core.backends.aur.info", lambda names, **kw: {})
    monkeypatch.setattr("cygnus.core.backends.sources._flathub_search", lambda q: [])


def test_the_window_is_told_it_can_convert_after_review_and_what_to_read(tmp_path, offline):
    deb = builders.build_deb(tmp_path, depends="", scripts={"postinst": UNREADABLE}, files=FILES)
    plan = service.analyse(str(deb), None)
    assert plan["installable"] is False and plan["installable_after_review"] is True
    assert "FOREIGN_SCRIPTS_UNRECOGNISED" not in {i["code"] for i in plan["issues"]}  # the page explains it instead
    assert {m["command"] for m in plan["scripts"]["mentions"]} == {"groupadd", "update-alternatives"}
    assert "chromemgmt" in plan["scripts"]["texts"]["postinst"] and plan["scripts"]["unreadable"]
    with pytest.raises(CygnusError, match="only converts packages its analysis found safe"):
        service.convert_foreign(str(deb), lambda line: None, plan["sha256"])
    result = service.convert_foreign(str(deb), lambda line: None, plan["sha256"], accept_unread_scripts=True)
    assert "install scripts that Cygnus could not read were not run" in result["notes"]


def test_an_ordinary_package_offers_no_review_path(tmp_path, offline):
    deb = builders.build_deb(tmp_path, depends="", files=FILES)
    plan = service.analyse(str(deb), None)
    assert plan["installable"] is True and plan["installable_after_review"] is False and plan["scripts"] is None


def test_the_window_cannot_ask_for_an_optional_package_the_analysis_did_not_offer(tmp_path, offline):
    deb = builders.build_deb(tmp_path, depends="", files=FILES)
    with pytest.raises(CygnusError, match="not an optional library"):
        service.convert_foreign(str(deb), lambda line: None, None, optional=["qt5-base"])


def test_a_package_that_may_not_be_gone_past_still_shows_why_in_red(tmp_path, offline):
    deb = builders.build_deb(tmp_path, depends="", scripts={"postinst": "#!/bin/sh\nuseradd -r hello\n"}, files=FILES)
    plan = service.analyse(str(deb), None)
    assert plan["installable"] is False and plan["installable_after_review"] is False and plan["scripts"] is None
    assert "FOREIGN_SCRIPTS_UNRECOGNISED" in {i["code"] for i in plan["issues"]}


# -- a copy Cygnus converted earlier is not "an Arch package that pacman keeps up to date" -----------------------------
def _installed(monkeypatch, name="hello", version="1.2.3-1"):
    from cygnus.core.backends import pacman as pm

    info = {name: {"local": {"name": name, "version": version}, "sync": None}}
    monkeypatch.setattr(pm, "run_worker", lambda c, req: {"packages": info if req["op"] == "info" else {}, "satisfiers": {}})


def test_a_package_that_arch_itself_installed_is_still_reported_as_installed(tmp_path, offline, monkeypatch):
    _installed(monkeypatch)
    plan = service.analyse(str(builders.build_deb(tmp_path, depends="", files=FILES)), None)
    assert plan["strategy"] == "native-alternative" and plan["installable"] is False


def test_a_copy_cygnus_converted_earlier_can_be_replaced_by_a_newer_file(tmp_path, offline, monkeypatch):
    _installed(monkeypatch)
    service.record_package_install("hello", "1.2.2-1", origin="converted", source={"file": "/x/old.deb"})
    plan = service.analyse(str(builders.build_deb(tmp_path, depends="", files=FILES)), None)
    assert plan["strategy"] == "convert" and plan["installable"] is True
    [notice] = [i for i in plan["issues"] if i["code"] == "REPLACES_CONVERTED"]
    assert "earlier file" in notice["title"] and "1.2.3-1" in notice["explanation"] and "Nothing updates it" in notice["explanation"]


def test_a_package_installed_from_a_local_file_is_not_mistaken_for_a_converted_one(tmp_path, offline, monkeypatch):
    _installed(monkeypatch)
    service.record_package_install("hello", "1.2.2-1", origin="local", source={"file": "/x/hello.pkg.tar.zst"})
    plan = service.analyse(str(builders.build_deb(tmp_path, depends="", files=FILES)), None)
    assert plan["strategy"] == "native-alternative"


def test_reconverting_a_program_keeps_what_its_own_older_copy_owns(tmp_path, offline, monkeypatch):
    files = {**FILES, "usr/bin/hello-real": b"#!/bin/sh\n"}
    scripts = {"postinst": "#!/bin/sh\nupdate-alternatives --install /usr/bin/hello-cmd hello-cmd /usr/bin/hello-real 50\n"}
    deb = builders.build_deb(tmp_path, depends="", scripts=scripts, files=files)  # the package is named "hello"
    monkeypatch.setattr(service, "_owner", lambda path: "hello" if path == "/usr/bin/hello-cmd" else None)
    assert [a["path"] for a in service.analyse(str(deb), None)["additions"]] == ["usr/bin/hello-cmd"]
    monkeypatch.setattr(service, "_owner", lambda path: "another-package" if path == "/usr/bin/hello-cmd" else None)
    assert service.analyse(str(deb), None)["additions"] == []
