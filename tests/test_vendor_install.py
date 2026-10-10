"""The vendor's install scripts of a deb or rpm, written into the converted package's pacman install file when a person chose that.
The generated file is only parsed and its text functions read here: no script is ever run."""

import subprocess
from pathlib import Path

import pytest

import builders
from cygnus.core.backends import foreign
from cygnus.core.detect import detect_file
from cygnus.core.errors import CygnusError
from cygnus.core.models import PackageFormat
from cygnus.core.util import scriptrun
from cygnus.core.ops import convert, vendor_install

DEB, RPM = PackageFormat.DEB, PackageFormat.RPM
FILES = {"usr/bin/hello": b"#!/bin/sh\necho hello\n", "usr/share/hello/a.txt": b"a\n"}
TRICKY = ("#!/bin/sh\nset -e\necho \"$HOME `id -u` \\n $(date)\"\ncat <<EOF\nnot the end\nEOF\n"
          "CYGNUS_SCRIPT_END_X\n\tindented\\ttab\nprintf 'café'\n")


def installed_text(text, name="postinst", fmt=DEB, tmp=None):
    out, included, notes = vendor_install.build(fmt, {name: text})
    return out, included, notes


def read_function(tmp_path, out, name):
    """What a generated `_cygnus_text_<name>` function prints, found by sourcing the file (which only defines functions)."""
    path = tmp_path / "script.install"
    path.write_text(out)
    raw = subprocess.run(["bash", "-c", f"source {path}; _cygnus_text_{name}"], capture_output=True, check=True).stdout
    return raw.decode("utf-8")  # as bytes, not as text: nothing (a carriage return, a missing newline) is translated on the way


def test_a_deb_gets_the_arguments_debian_would_have_given_each_script():
    out, included, notes = vendor_install.build(DEB, {n: f"#!/bin/sh\necho {n}\n" for n in ("preinst", "postinst", "prerm", "postrm")})
    assert set(included) == {"preinst", "postinst", "prerm", "postrm"} and len(included) == 4 and notes == []
    body = {fn: out.split(f"{fn}() {{", 1)[1].split("}", 1)[0] for fn in ("pre_install", "post_install", "pre_upgrade", "post_upgrade", "pre_remove", "post_remove")}
    assert "_cygnus_run preinst /usr/bin/sh '' install" in body["pre_install"]
    assert "_cygnus_run preinst /usr/bin/sh '' upgrade \"$2\"" in body["pre_upgrade"]
    assert "_cygnus_run postinst /usr/bin/sh '' configure" in body["post_install"] and "configure" in body["post_install"]
    assert "_cygnus_run postinst /usr/bin/sh '' configure \"$2\"" in body["post_upgrade"]
    assert "_cygnus_run prerm /usr/bin/sh '' remove" in body["pre_remove"] and "_cygnus_run postrm /usr/bin/sh '' remove" in body["post_remove"]
    assert "prerm" not in body["pre_upgrade"] and "postrm" not in body["post_upgrade"]  # an upgrade runs the new package's pre and post only


def test_an_rpm_gets_the_count_rpm_would_have_given_each_scriptlet_and_only_programs_cygnus_knows():
    texts = {"prein": "echo pre", "postin": "echo post", "preun": "echo preun", "postun": "echo postun", "pretrans": "echo pt", "posttrans": "echo tt",
             "triggerscripts": "echo trig"}
    out, included, notes = vendor_install.build(RPM, texts, {k: "/bin/sh" for k in texts})
    assert sorted(included) == sorted(["prein", "postin", "preun", "postun", "pretrans", "posttrans"]) and any("triggerscripts is not run" in n for n in notes)
    body = {fn: out.split(f"{fn}() {{", 1)[1].split("}", 1)[0] for fn in ("pre_install", "post_install", "pre_upgrade", "post_upgrade", "pre_remove", "post_remove")}
    assert "_cygnus_run prein /usr/bin/sh '' 1" in body["pre_install"] and "_cygnus_run pretrans /usr/bin/sh '' 1" in body["pre_install"]
    assert "_cygnus_run prein /usr/bin/sh '' 2" in body["pre_upgrade"] and "_cygnus_run pretrans /usr/bin/sh '' 2" in body["pre_upgrade"]
    assert "_cygnus_run postin /usr/bin/sh '' 1" in body["post_install"] and "_cygnus_run posttrans /usr/bin/sh '' 1" in body["post_install"]
    assert "_cygnus_run postin /usr/bin/sh '' 2" in body["post_upgrade"] and "_cygnus_run posttrans /usr/bin/sh '' 2" in body["post_upgrade"]
    assert "_cygnus_run preun /usr/bin/sh '' 0" in body["pre_remove"] and "_cygnus_run postun /usr/bin/sh '' 0" in body["post_remove"]
    out, included, notes = vendor_install.build(RPM, {"postin": "print('x')", "prein": "x"}, {"postin": "<lua>", "prein": "/usr/bin/python3"})
    assert included == ["prein"] and any("postin is not run" in n and "lua" in n for n in notes)


@pytest.mark.parametrize("name, why", [("config", "debconf"), ("triggers", "other packages")])
def test_scripts_that_have_no_pacman_equivalent_are_left_out_and_said_to_be(name, why):
    out, included, notes = vendor_install.build(DEB, {name: "#!/bin/sh\ntrue\n", "postinst": "#!/bin/sh\ntrue\n"})
    assert included == ["postinst"] and any(f"{name} is not run" in n and why in n for n in notes) and f"_cygnus_text_{name}" not in out


@pytest.mark.parametrize("texts", [{}, {"postinst": ""}, {"postinst": "   \n"}, {"config": "x"}])
def test_nothing_is_written_when_there_is_nothing_to_run(texts):
    assert vendor_install.build(DEB, texts)[0] is None


@pytest.mark.parametrize("text, reason", [("#!/usr/bin/ruby\nputs 1\n", "run scripts with"), ("#!/tmp/evil\n", "run it yourself"), ("#!/bin/sh\n\0x\n", "not text"),
                                          ("#!/bin/sh\n# caf\ufffd\n", "not text"),
                                          ("#!/bin/sh\n" + "#" * vendor_install.MAX_EMBEDDED, "shown to you whole")])
def test_a_script_that_cannot_be_written_into_the_package_is_left_out_with_the_reason(text, reason):
    out, included, notes = vendor_install.build(DEB, {"postinst": text})
    assert out is None and included == [] and reason in " ".join(notes)


def test_the_script_is_embedded_exactly_and_the_file_is_valid_shell(tmp_path):
    out, included, _ = installed_text(TRICKY)
    assert included == ["postinst"]
    path = tmp_path / "x.install"
    path.write_text(out)
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0  # parses
    assert read_function(tmp_path, out, "postinst") == TRICKY  # byte for byte, nothing expanded, a line like the delimiter included
    for exact in ("echo no trailing newline", "echo x\n\n\n", "x", "#!/bin/sh\ntrue", "caf\u00e9 \u2713\n", "line\\\n", "a  \t \nb \n"):
        assert read_function(tmp_path, installed_text(exact)[0], "postinst") == exact, repr(exact)  # byte for byte: nothing is added or taken


def test_a_script_that_contains_the_delimiter_line_is_still_embedded_exactly(tmp_path):
    base = vendor_install._delimiter("echo x\n")
    text = f"echo x\n{base}\n{base}_1\necho after the would-be end\n"
    assert vendor_install._delimiter(text) == f"{base}_2" and vendor_install._delimiter("") == base  # one that is not a line of the script
    out, _, _ = installed_text(text)
    assert read_function(tmp_path, out, "postinst") == text


def test_defining_the_functions_runs_nothing_and_the_runner_is_a_function_not_a_command(tmp_path):
    out, _, _ = installed_text("#!/bin/sh\necho SHOULD-NOT-RUN > " + str(tmp_path / "ran") + "\n")
    path = tmp_path / "x.install"
    path.write_text(out)
    res = subprocess.run(["bash", "-c", f"source {path}; type pre_install post_install pre_remove post_remove _cygnus_run"], capture_output=True, text=True)
    assert "post_install is a function" in res.stdout and not (tmp_path / "ran").exists()
    assert "systemd-run --quiet --wait --pipe --collect" in out and "-p RuntimeMaxSec=600" in out  # the way it is run: like sudo, in a unit of its own


# -- conversion, with a real makepkg (the package is built and listed, never installed) --------------------------------------------------
needs_tools = pytest.mark.needs_tool("makepkg", "fakeroot", "bsdtar", "zstd")
READABLE = "#!/bin/sh\nset -e\necho configured\n"
UNREADABLE = "#!/bin/sh\nset -e\nsize=\"$(echo ${icon} | sed 's/[^0-9]//g')\"\ngroupadd grp\n"


def verdict_for(tmp_path, scripts, files=FILES):
    cand = detect_file(str(builders.build_deb(tmp_path, scripts=scripts, files=files)))
    return cand, foreign.analyse(cand, satisfy=lambda deps: {}, sonames_on_host=set(), glibc_on_host=(2, 40))


def listing(pkg):
    return subprocess.run(["bsdtar", "-tf", str(pkg)], capture_output=True, text=True).stdout


def read_member(pkg, name):
    return subprocess.run(["bsdtar", "-xOf", str(pkg), name], capture_output=True, text=True).stdout


@needs_tools
def test_by_default_no_script_travels_into_the_package(tmp_path):
    cand, v = verdict_for(tmp_path, {"postinst": READABLE})
    pkg, notes = convert.convert(cand, v, tmp_path / "out")
    assert ".INSTALL" not in listing(pkg) and not any("install scripts" in n for n in notes)


@needs_tools
def test_when_chosen_the_vendors_scripts_are_in_the_package_and_the_notes_say_so(tmp_path):
    cand, v = verdict_for(tmp_path, {"postinst": READABLE, "prerm": "#!/bin/sh\necho bye\n"})
    pkg, notes = convert.convert(cand, v, tmp_path / "out", run_vendor_scripts=True)
    assert ".INSTALL" in listing(pkg) and "usr/bin/hello" in listing(pkg)
    text = read_member(pkg, ".INSTALL")
    assert "_cygnus_run postinst /usr/bin/sh '' configure" in text and "echo configured" in text and "_cygnus_run prerm /usr/bin/sh '' remove" in text
    assert any("run as administrator" in n and "postinst" in n and "because you chose that" in n for n in notes)


@needs_tools
def test_a_package_with_no_scripts_says_so_and_has_no_install_file(tmp_path):
    cand, v = verdict_for(tmp_path, {})
    pkg, notes = convert.convert(cand, v, tmp_path / "out", run_vendor_scripts=True)
    assert ".INSTALL" not in listing(pkg) and any("no install scripts that Cygnus can run" in n for n in notes)


@needs_tools
@pytest.mark.parametrize("script", [UNREADABLE, "#!/bin/sh\nuseradd -r hello\n", "#!/bin/sh\nmodprobe loop\n"])
def test_a_block_that_was_only_about_scripts_no_longer_applies_when_they_are_run_but_not_otherwise(tmp_path, script):
    cand, v = verdict_for(tmp_path, {"postinst": script})
    assert v.strategy == "review"
    with pytest.raises(CygnusError, match="only converts packages its analysis found safe"):
        convert.convert(cand, v, tmp_path / "out")
    pkg, notes = convert.convert(cand, v, tmp_path / "out", run_vendor_scripts=True)
    assert ".INSTALL" in listing(pkg) and not any("were not run" in n for n in notes)  # nothing claims they were not run


@needs_tools
def test_running_the_scripts_never_lets_a_package_through_that_is_refused_for_what_it_ships(tmp_path):
    cand, v = verdict_for(tmp_path, {"postinst": READABLE}, files={**FILES, "var/lib/hello/data": b"x"})  # outside /usr, /etc and /opt
    assert v.strategy == "refuse"
    with pytest.raises(CygnusError, match="only converts packages its analysis found safe"):
        convert.convert(cand, v, tmp_path / "out", run_vendor_scripts=True)


# -- through the screen functions ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def offline(monkeypatch):
    from cygnus.core.backends import pacman as pm

    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    monkeypatch.setattr(pm, "read_config", lambda: cfg)
    monkeypatch.setattr(pm, "run_worker", lambda c, req: {"packages": {}, "satisfiers": {}})
    monkeypatch.setattr("cygnus.core.backends.aur.info", lambda names, **kw: {})
    monkeypatch.setattr("cygnus.core.backends.sources._flathub_search", lambda q: [])


@needs_tools
def test_the_screen_is_given_every_script_in_full_and_a_package_without_scripts_offers_nothing(tmp_path, offline):
    from cygnus.gui import service

    deb = builders.build_deb(tmp_path, depends="", scripts={"postinst": READABLE, "config": "#!/bin/sh\ntrue\n"}, files=FILES)
    plan = service.analyse(str(deb), None)
    shown = plan["vendor_scripts"]
    assert shown["names"] == ["postinst"] and shown["texts"]["postinst"] == READABLE and "as administrator" in shown["warning"]
    assert any("config is not run" in n for n in shown["left_out"])
    (tmp_path / "other").mkdir()
    assert service.analyse(str(builders.build_deb(tmp_path / "other", depends="", files=FILES)), None)["vendor_scripts"] is None


@needs_tools
def test_the_choice_is_what_puts_the_scripts_in_the_package_and_a_scripts_only_block_is_lifted_by_it(tmp_path, offline):
    from cygnus.gui import service

    deb = builders.build_deb(tmp_path, depends="", scripts={"postinst": "#!/bin/sh\nuseradd -r hello\n"}, files=FILES)
    plan = service.analyse(str(deb), None)
    assert plan["strategy"] == "review" and plan["installable"] is False and plan["vendor_scripts"]["names"] == ["postinst"]
    with pytest.raises(CygnusError, match="only converts packages its analysis found safe"):
        service.convert_foreign(str(deb), lambda line: None, plan["sha256"], accept_unread_scripts=True)  # not enough: it is a dangerous script
    result = service.convert_foreign(str(deb), lambda line: None, plan["sha256"], run_vendor_scripts=True)
    assert ".INSTALL" in listing(result["package"]) and any("run as administrator" in n for n in result["notes"])
    (tmp_path / "p").mkdir()
    plain = builders.build_deb(tmp_path / "p", depends="", scripts={"postinst": READABLE}, files=FILES)
    result = service.convert_foreign(str(plain), lambda line: None, None)
    assert ".INSTALL" not in listing(result["package"])


class FakeHelper:
    """Stands in for the helper: remembers the package it was asked to install, and says yes."""

    def __init__(self):
        self.packages = []

    def plan_packages(self, *, local_files=(), **kw):
        from cygnus.core.privilege import Plan

        self.packages += [p for p, _sha in local_files]
        return Plan("plan-1", {}, "Install the package")

    def commit(self, plan, on_progress=lambda line: None):
        return True, "ok"


@needs_tools
def test_the_terminal_needs_the_flag_to_run_the_vendors_scripts_and_then_shows_them_in_full(tmp_path, monkeypatch, capsys, offline):
    import argparse

    from cygnus.cli import main as cli
    from cygnus.core import privilege

    deb = builders.build_deb(tmp_path, depends="", scripts={"postinst": "#!/bin/sh\nuseradd -r hello\n"}, files=FILES)

    def ns(**kw):
        return argparse.Namespace(file=str(deb), to=None, yes=True, program=None, registry=None, with_optional=False, accept_scripts=kw.get("accept", False),
                                  run_scripts=kw.get("run", False), scripts_as_admin=False)

    assert cli.cmd_install(ns(accept=True)) == 1  # a dangerous script is never gone past by --accept-scripts
    fake = FakeHelper()
    monkeypatch.setattr(privilege, "HelperClient", lambda *a, **k: fake)
    assert cli.cmd_install(ns(run=True)) == 0
    out = capsys.readouterr().out
    assert "--- postinst ---" in out and "useradd -r hello" in out and "pacman runs them as administrator" in out
    [package] = fake.packages
    assert ".INSTALL" in listing(package)


# -- what is shown is the whole of what is written -----------------------------------------------------------------------------------------
def test_a_script_is_written_only_if_it_fits_whole_in_what_is_shown_and_was_not_cut_by_the_reader():
    edge = "#!/bin/sh\n" + "#" * (vendor_install.MAX_EMBEDDED - len("#!/bin/sh\n"))
    over = edge + "#"
    assert len(edge) == vendor_install.MAX_EMBEDDED
    assert vendor_install.build(DEB, {"postinst": edge})[1] == ["postinst"]
    out, included, notes = vendor_install.build(DEB, {"postinst": over})
    assert out is None and included == [] and "shown to you whole" in " ".join(notes)
    out, included, notes = vendor_install.build(DEB, {"postinst": "#!/bin/sh\ntrue\n", "prerm": "#!/bin/sh\ntrue\n"}, uninspected=["prerm"])
    assert included == ["postinst"] and any("prerm is not run" in n and "only the start" in n for n in notes)
    assert vendor_install.build(RPM, {"postin": "echo x"}, {"postin": "/bin/sh"}, uninspected=["postin"])[0] is None  # an rpm scriptlet the reader cut


def test_the_screen_is_only_offered_scripts_that_would_really_be_written(tmp_path, offline):
    from cygnus.gui import service

    long_script = "#!/bin/sh\n" + "# padding\n" * 7000 + "echo END\n"  # over what can be shown whole
    assert len(long_script) > vendor_install.MAX_EMBEDDED
    deb = builders.build_deb(tmp_path, depends="", scripts={"postinst": long_script, "prerm": "#!/bin/sh\ntrue\n"}, files=FILES)
    shown = service.analyse(str(deb), None)["vendor_scripts"]
    assert shown["names"] == ["prerm"] and any("postinst is not run" in n for n in shown["left_out"])
    (tmp_path / "only").mkdir()
    deb = builders.build_deb(tmp_path / "only", depends="", scripts={"postinst": long_script}, files=FILES)
    assert service.analyse(str(deb), None)["vendor_scripts"] is None  # nothing that could be run: nothing offered


@pytest.mark.parametrize("sha", [None, "", "abc", "A" * 64, "0" * 63])
def test_the_vendors_scripts_are_never_run_for_a_file_that_is_not_tied_to_the_analysis_you_read(tmp_path, offline, sha):
    from cygnus.gui import service

    deb = builders.build_deb(tmp_path, depends="", scripts={"postinst": READABLE}, files=FILES)
    with pytest.raises(CygnusError, match="checksum of the analysis"):
        service.convert_foreign(str(deb), lambda line: None, sha, run_vendor_scripts=True)


@needs_tools
def test_a_package_that_declares_users_is_not_converted_just_because_its_scripts_are_run(tmp_path, offline):
    from cygnus.core.backends import foreign as fb

    rpm = builders.build_rpm(tmp_path, files={"usr/bin/hello": b"#!/bin/sh\necho hi\n"},
                             extra=((5109, builders.STRING_ARRAY, ["u hello - \"Hello\" /var/lib/hello -"]),))
    cand = detect_file(str(rpm))
    assert cand.metadata["sysusers"]
    verdict = fb.analyse(cand, satisfy=lambda deps: {}, sonames_on_host=set(), glibc_on_host=(2, 40))
    with pytest.raises(CygnusError, match="declares system users"):
        convert.convert(cand, verdict, tmp_path / "out", run_vendor_scripts=True)


# -- round 13: what is written is what is shown, and runs the way the script says -------------------------------------------------------------
@pytest.mark.parametrize("text", ["#!/bin/sh\r\ntrue\r\n", "#!/bin/sh\necho a\u200bb\n", "#!/bin/sh\necho \u202e.sh\n", "#!/bin/sh\necho\u00a0x\n", "\ufeff#!/bin/sh\ntrue\n",
                                  "#!/bin/sh\ntrue\u2028\n", "#!/bin/sh\n\x1b[2Jtrue\n", "#!/bin/sh\n\x0ctrue\n", "#!/bin/sh\n\x85true\n"])
def test_a_script_with_characters_that_cannot_be_seen_or_that_move_text_is_never_written(text):
    out, included, notes = vendor_install.build(DEB, {"postinst": text})
    assert out is None and included == [] and "cannot be seen" in " ".join(notes)
    assert vendor_install.build(DEB, {"postinst": "#!/bin/sh\n\techo caf\u00e9 \u2713 \u4e2d\n"})[1] == ["postinst"]  # tabs, accents and other scripts' letters are fine


def test_the_options_on_the_first_line_are_passed_and_a_line_that_asks_for_something_else_is_refused():
    out, included, _ = vendor_install.build(DEB, {"postinst": "#!/bin/sh -e\ntrue\n", "preinst": "#!/usr/bin/env bash\ntrue\n"})
    assert "_cygnus_run postinst /usr/bin/sh '-e' configure" in out and "_cygnus_run preinst /usr/bin/bash '' install" in out and len(included) == 2
    for first in ("#!/opt/vendor/bin/python3", "#!/bin/sh -o pipefail", "#!/usr/bin/env -S python3 -u", "#!/bin/dash", "#!/bin/sh -e -x"):
        out, included, notes = vendor_install.build(DEB, {"postinst": first + "\ntrue\n"})
        assert out is None and "is not run" in " ".join(notes), first


def test_for_an_rpm_the_declared_program_decides_and_a_first_line_that_disagrees_is_refused_so_analysis_and_writer_never_differ():
    out, included, _ = vendor_install.build(RPM, {"postin": "echo hi"}, {"postin": "/bin/sh"})
    assert "_cygnus_run postin /usr/bin/sh '' 1" in out
    out, _, _ = vendor_install.build(RPM, {"postin": "print('x')\n"}, {"postin": "python3"})  # a bare name is a program in /usr/bin
    assert "_cygnus_run postin /usr/bin/python3 '' 1" in out
    out, _, _ = vendor_install.build(RPM, {"postin": "#!/bin/sh\ntrue\n"}, {"postin": "/bin/sh"})
    assert "_cygnus_run postin /usr/bin/sh" in out  # agreeing: fine
    for declared, text in (("/bin/sh", "#!/usr/bin/perl\nprint 1;\n"), ("/usr/bin/python3", "#!/bin/sh\ngroupadd grp\n")):
        out, included, notes = vendor_install.build(RPM, {"postin": text}, {"postin": declared})
        assert out is None and "another program than the package declares" in " ".join(notes)


def test_printable_shows_no_character_a_terminal_would_obey():
    assert scriptrun.printable(b"a\x1b[2Jb\n\tc\x07") == "a?[2Jb\n\tc?" and scriptrun.printable(b"x\r\ny") == "x?\ny"
    assert scriptrun.printable("caf\u00e9 \u2713".encode()) == "caf\u00e9 \u2713" and "\ufffd" not in scriptrun.printable(b"ok")


# -- a deb whose script is a link ------------------------------------------------------------------------------------------------------------
def deb_with_control_members(dest, members):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            else:
                info.type = tarfile.LNKTYPE if kind == "hardlink" else tarfile.SYMTYPE
                info.linkname = payload
                tar.addfile(info)
    control = b"Package: hello\nVersion: 1.0-1\nArchitecture: amd64\nMaintainer: T <t@example.invalid>\nInstalled-Size: 4\nDescription: t\n"
    data = builders._tar_bytes({"./usr/bin/hello": b"#!/bin/sh\n"}, "w:gz")
    path = dest / "linked.deb"
    path.write_bytes(builders._ar([("debian-binary", b"2.0\n"), ("control.tar.xz", buf.getvalue()), ("data.tar.gz", data)]))
    return path


@pytest.mark.parametrize("kind", ["hardlink", "symlink"])
def test_a_maintainer_script_that_is_a_link_is_never_passed_as_harmless_and_never_written(tmp_path, kind):
    deb = deb_with_control_members(tmp_path, [("./control", "file", b"Package: hello\nVersion: 1.0-1\nArchitecture: amd64\nMaintainer: T <t@example.invalid>\nInstalled-Size: 4\nDescription: t\n"),
                                              ("./postinst.real", "file", b"#!/bin/sh\ngroupadd grp\n"), ("./postinst", kind, "postinst.real")])
    cand = detect_file(str(deb))
    assert "postinst" in cand.metadata["uninspected_scripts"] and "postinst" not in cand.metadata["maintainer_scripts"]
    verdict = foreign.analyse(cand, satisfy=lambda deps: {}, sonames_on_host=set(), glibc_on_host=(2, 40))
    assert verdict.strategy == "review" and any("postinst" in u and "could not be read in full" in u for u in verdict.scripts.unknown)
    out, included, notes = vendor_install.build(DEB, foreign.install_script_texts(cand), uninspected=cand.metadata["uninspected_scripts"])
    assert "postinst" not in included


# -- the generated runner, executed with harmless scripts only (no vendor script, and never the real systemd-run) --------------------------
RUN_ENV_KEYS = ("PATH", "HOME", "LANG")


def tools(tmp_path, *, with_systemd_run=None):
    """A folder of links to just the programs the runner needs, so that the real systemd-run is never reachable; `with_systemd_run` is the
    text of a stand-in for it."""
    import os
    import shutil

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("mktemp", "cat", "chmod", "rm", "env", "timeout", "head", "sh", "bash", "true", "sleep", "echo"):
        found = shutil.which(name)
        if found and not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(found)
    if with_systemd_run is not None:
        script = bin_dir / "systemd-run"
        script.write_text(with_systemd_run)
        script.chmod(0o755)
    return bin_dir


def run_function(tmp_path, out, call, *, bin_dir, extra_env=None):
    path = tmp_path / "runner.install"
    path.write_text(out)
    env = {"PATH": str(bin_dir), "TMPDIR": str(tmp_path), "SECRET_TOKEN": "hunter2", **(extra_env or {})}
    return subprocess.run(["/usr/bin/bash", "-c", f"source {path}; {call}; echo \"returned=$?\""], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)


def test_the_runner_runs_a_script_directly_with_a_clean_environment_no_input_and_the_arguments_and_always_returns_zero(tmp_path):
    script = f"#!/bin/sh\necho \"args=$*\" > {tmp_path}/out.txt\necho \"cwd=$(pwd) home=$HOME secret=${{SECRET_TOKEN:-none}}\" >> {tmp_path}/out.txt\nread line; echo \"stdin=[$line]\" >> {tmp_path}/out.txt\nexit 7\n"
    out, _, _ = vendor_install.build(DEB, {"postinst": script})
    res = run_function(tmp_path, out, "post_install; _cygnus_run postinst /usr/bin/sh '' configure two", bin_dir=tools(tmp_path))  # no systemd-run in reach: the direct branch
    lines = (tmp_path / "out.txt").read_text().splitlines()
    assert lines[0] == "args=configure two" and lines[1] == "cwd=/ home=/root secret=none" and lines[2] == "stdin=[]"
    assert "returned=0" in res.stdout and "ended with status 7" in res.stderr  # the failure is said, and pacman is not stopped by it
    assert not list(tmp_path.glob("cygnus-script.*"))  # the temporary folder is gone


def test_the_runner_says_so_when_it_cannot_run_a_script_and_still_returns_zero(tmp_path):
    out, _, _ = vendor_install.build(DEB, {"postinst": "#!/bin/sh\ntrue\n"})
    bin_dir = tools(tmp_path)
    (bin_dir / "mktemp").unlink()  # nowhere to write the script
    res = run_function(tmp_path, out, "_cygnus_run postinst /usr/bin/sh '' configure", bin_dir=bin_dir)
    assert "returned=0" in res.stdout and "was not run: there is nowhere to write it" in res.stderr


@pytest.mark.skipif(not Path("/run/systemd/system").is_dir(), reason="needs a systemd system")
def test_the_runner_uses_a_system_unit_with_the_fixed_flags_when_systemd_answers_and_falls_back_when_it_does_not(tmp_path):
    log = tmp_path / "systemd-run.log"
    fake = f"#!/usr/bin/sh\nprintf '%s\\n' \"$*\" >> {log}\nwhile [ \"$1\" != -- ]; do shift; done; shift\nexec \"$@\"\n"
    out, _, _ = vendor_install.build(DEB, {"postinst": f"#!/bin/sh -e\necho ran-as-unit > {tmp_path}/unit.txt\n"})
    res = run_function(tmp_path, out, "_cygnus_run postinst /usr/bin/sh '-e' configure", bin_dir=tools(tmp_path, with_systemd_run=fake))
    assert "returned=0" in res.stdout and (tmp_path / "unit.txt").read_text() == "ran-as-unit\n"
    calls = log.read_text().splitlines()
    assert calls[0].endswith("-- /usr/bin/true")  # first: is systemd answering at all?
    real = calls[1]
    assert real.startswith("--quiet --wait --pipe --collect -p RuntimeMaxSec=600 --working-directory=/ --setenv=PATH=/usr/local/sbin:/usr/local/bin:/usr/bin")
    assert "--setenv=HOME=/root" in real and real.split(" -- ")[1].startswith("/usr/bin/sh -e ") and real.endswith(" configure")
    log.unlink()
    (tmp_path / "unit.txt").unlink()
    broken = "#!/usr/bin/sh\nexit 1\n"  # systemd-run exists but cannot connect: the script is run directly instead of being reported as failed
    res = run_function(tmp_path, out, "_cygnus_run postinst /usr/bin/sh '-e' configure", bin_dir=tools(tmp_path, with_systemd_run=broken))
    assert (tmp_path / "unit.txt").read_text() == "ran-as-unit\n" and "returned=0" in res.stdout and "ended with status" not in res.stderr


def test_what_a_removal_script_deletes_is_listed_even_though_the_analysis_treats_it_as_clean_up(tmp_path, offline):
    from cygnus.gui import service

    deb = builders.build_deb(tmp_path, depends="", scripts={"postrm": "#!/bin/sh\nrm -rf /etc/shadow\n"}, files=FILES)
    shown = service.analyse(str(deb), None)["vendor_scripts"]
    assert shown["names"] == ["postrm"]
    by = {m["command"]: m for m in shown["mentions"]}
    assert "rm" in by and by["rm"]["scripts"] == ["postrm"] and "deletes files" in by["rm"]["what"]  # said before the box can be ticked


def test_a_script_text_that_is_shown_cut_says_so_and_that_script_is_not_run(tmp_path, offline):
    from cygnus.gui import service

    long_script = "#!/bin/sh\n" + "# padding\n" * 7000 + "echo END\n"
    deb = builders.build_deb(tmp_path, depends="", scripts={"postinst": long_script, "prerm": "#!/bin/sh\ntrue\n"}, files=FILES)
    shown = service.analyse(str(deb), None)["vendor_scripts"]
    assert shown["texts"]["prerm"] == "#!/bin/sh\ntrue\n"  # one that fits is shown as it is
    cut = shown["texts"]["postinst"]
    assert len(cut) < len(long_script) and "only the first 60,000 characters are shown" in cut and "this script is not run" in cut
    assert shown["names"] == ["prerm"]
