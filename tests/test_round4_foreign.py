"""Review round 4: the DEB/RPM script classifier and conversion policy."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import builders  # noqa: E402
from script_corpus import BENIGN, HOSTILE  # noqa: E402

from cygnus.core.backends import foreign  # noqa: E402
from cygnus.core.detect import detect_file  # noqa: E402


def _verdict(cand):
    return foreign.analyse(cand, satisfy=lambda deps: {}, sonames_on_host=set(), glibc_on_host=(2, 40))


@pytest.mark.parametrize("name", sorted(BENIGN))
def test_ordinary_packaging_scripts_do_not_block_conversion(name):
    analysis = foreign.classify_scripts({name: BENIGN[name]})
    assert not analysis.blocks_auto_conversion, (analysis.unknown, analysis.blocking)


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_or_unreadable_constructs_always_block_conversion(name):
    analysis = foreign.classify_scripts({name: HOSTILE[name]})
    assert analysis.blocks_auto_conversion, (name, analysis.categories, analysis.review)


@pytest.mark.parametrize("script", [
    'case "$1" in\n  configure)\n    true\n  ;;\nesac\nrm -rf /usr\n',          # after the case
    "case $1 in a) true;; b) true;; esac\nrm -rf /usr",                              # one-line case
    "case $1 in\n (a|b)\n true;;\nesac\nrm -rf /usr",                                # (a|b) patterns
    'case $1 in\n  configure)\n    rm -rf /usr\n  ;;\nesac',                         # inside a branch
    "case $1 in\n a)\n if true; then rm -rf /usr; fi\n ;;\nesac",                    # nested
    "for f in a b; do rm -rf /usr; done",
    "while true; do rm -rf /usr; done",
    "if [ -x /x ]; then\n  rm -rf /usr\nelse\n  true\nfi",
    "true && rm -rf /usr",
    "false || rm -rf /usr",
    "(cd /; rm -rf usr)",
    "ldconfig; rm -rf /usr/lib",
    "\\rm -rf /usr",
    "'rm' -rf /usr",
])
def test_every_command_is_analysed_wherever_it_sits(script):
    assert foreign.classify_scripts({"x": script}).blocks_auto_conversion


@pytest.mark.parametrize("script", [
    "rm -rf /usr/share/hello",   # recursive removal is never "safe"
    "echo 'rm -rf /usr' > /dev/null || true\nrm /etc/x",
    "cp /tmp/x -t/usr/bin",
])
def test_blocking_commands_are_reported_as_such(script):
    assert foreign.classify_scripts({"x": script}).blocks_auto_conversion


@pytest.mark.parametrize("script", [
    "true",
    "echo \"a quoted \\`backtick' and $1\" >&2",
    "ldconfig >/dev/null 2>&1 || true",
    "/sbin/ldconfig",
    "/usr/bin/update-desktop-database &> /dev/null || :",
    "if [ \"$1\" = configure ]; then\n  systemctl daemon-reload || true\nfi",
    "case $1 in\n  a|b)\n    true\n  ;;\n  *)\n    echo x\n  ;;\nesac",
    "mkdir -p /opt/hello/cache",
    "# a comment \\\ntrue",
])
def test_these_are_understood_and_do_not_block(script):
    analysis = foreign.classify_scripts({"x": script})
    assert not analysis.blocks_auto_conversion, (analysis.unknown, analysis.blocking)


def test_a_comment_ending_in_a_backslash_does_not_hide_the_next_line():
    assert foreign.classify_scripts({"x": "# note \\\nrm -rf /usr"}).blocks_auto_conversion


@pytest.mark.parametrize("head, blocks", [
    ("#!/bin/sh", False), ("#!/bin/sh -e", False), ("#!/bin/bash", False), ("#!/usr/bin/env bash", False),
    ("#!/usr/bin/python3", True), ("#!/usr/bin/env python3", True), ("#!/usr/bin/perl -w", True),
    ("#!/usr/bin/env -S bash -e", True),
])
def test_a_scripts_own_interpreter_decides_whether_it_can_be_read_as_shell(head, blocks):
    text = head + "\nprint('x') if 0 else 0\n" if blocks else head + "\ntrue\n"
    assert foreign.classify_scripts({"postinst": text}).blocks_auto_conversion is blocks


# -- whole packages --------------------------------------------------------------------------------------
def test_a_debhelper_package_still_converts(tmp_path):
    cand = detect_file(builders.build_deb(tmp_path, depends="", scripts={
        "postinst": BENIGN["debhelper-postinst"], "prerm": BENIGN["debhelper-prerm"],
        "postrm": BENIGN["debhelper-postrm"]}))
    verdict = _verdict(cand)
    assert verdict.strategy == "convert", (verdict.summary, verdict.scripts.unknown, verdict.scripts.blocking)


def test_a_deb_whose_postinst_is_python_is_not_converted(tmp_path):
    cand = detect_file(builders.build_deb(tmp_path, depends="", scripts={
        "postinst": "#!/usr/bin/python3\nimport os\nos.system('echo hi')\n"}))
    verdict = _verdict(cand)
    assert verdict.strategy == "review" and any("python3" in u for u in verdict.scripts.unknown)


def test_ordinary_rpm_scriptlets_still_convert(tmp_path):
    cand = detect_file(builders.build_rpm(tmp_path, requires=("/bin/sh",), postin=BENIGN["rpm-post"],
                                          files={"usr/bin/hello": b"#!/bin/sh\n"}))
    assert _verdict(cand).strategy == "convert"


POSTTRANS, POSTTRANSPROG, SYSUSERS = 1152, 1154, 5109


def test_an_rpm_scriptlet_in_another_language_is_not_read_as_shell(tmp_path):
    body = 'if posix.access("/usr/share/x") then os.execute("rm -rf /usr/share/x") end'
    cand = detect_file(builders.build_rpm(tmp_path, requires=("/bin/sh",), postin=None,
                                          files={"usr/bin/hello": b"#!/bin/sh\n"},
                                          extra=((POSTTRANS, builders.STRING, body),
                                                 (POSTTRANSPROG, builders.STRING, "<lua>"))))
    assert cand.metadata["scriptlets"]["posttrans"]["interpreter"] == "<lua>"
    verdict = _verdict(cand)
    assert verdict.strategy == "review" and any("<lua>" in u for u in verdict.scripts.unknown)


def test_an_rpm_that_declares_system_users_is_not_converted(tmp_path):
    cand = detect_file(builders.build_rpm(tmp_path, requires=("/bin/sh",), postin=None,
                                          files={"usr/bin/hello": b"#!/bin/sh\n"},
                                          extra=((SYSUSERS, builders.STRING_ARRAY, ["u hello - 'Hello daemon'"]),)))
    verdict = _verdict(cand)
    assert verdict.strategy == "review" and any("system user" in b for b in verdict.scripts.blocking)


# -- review round 4: payload errors, mode listing, binaries ----------------------------------------------
def test_a_damaged_payload_is_a_cygnus_error_not_a_bare_value_error(tmp_path):
    from cygnus.core.errors import CygnusError

    cand = detect_file(builders.build_deb(tmp_path, depends=""))
    cand.installed_size = foreign.MAX_EXTRACT_BYTES + 1
    with pytest.raises(foreign.PayloadError, match="too large to inspect") as info:
        foreign.extract_payload(cand, tmp_path / "root")
    assert isinstance(info.value, CygnusError) and isinstance(info.value, ValueError)  # old callers still work


def test_a_failed_or_truncated_mode_listing_is_reported_not_treated_as_no_setuid_files(tmp_path, monkeypatch):
    cand = detect_file(builders.build_deb(tmp_path, depends=""))
    real = foreign.proc.run
    monkeypatch.setattr(foreign.proc, "run", lambda argv, **kw: real(["false"], **{k: v for k, v in kw.items()
                                                                                    if k == "timeout"}))
    with pytest.raises(foreign.PayloadError, match="cannot be listed"):
        foreign.archive_special_modes(cand)


def test_too_many_programs_to_check_means_the_package_is_not_converted(tmp_path, monkeypatch):
    monkeypatch.setattr(foreign, "MAX_ELF_FILES", 2)
    elf = b"\x7fELF\x02\x01\x01" + b"\0" * 80
    files = {"usr/bin/hello": b"#!/bin/sh\n", **{f"usr/lib/hello/lib{i}.so": elf for i in range(4)}}
    cand = detect_file(builders.build_deb(tmp_path, depends="", files=files))
    verdict = foreign.analyse(cand, satisfy=lambda deps: {}, sonames_on_host=set(), glibc_on_host=(2, 40))
    assert verdict.strategy == "refuse" and any(i.code == "FOREIGN_TOO_MANY_BINARIES" for i in verdict.issues)


# -- round 5: classifier details -------------------------------------------------------------------------
@pytest.mark.parametrize("script, category", [
    ("chmod u+rws /opt/foo/bin/run", "setuid"), ("chmod 2755 /opt/foo/bin/run", "setuid"),
    ("chmod +s /opt/foo/bin/run", "setuid"), ("chmod 4755 /opt/foo/bin/run", "setuid"),
    ("chmod g=rxs /opt/foo/bin/run", "setuid"), ("chmod 0755 /opt/foo/bin/run", "filesystem"),
    ("chmod u+x /opt/foo/bin/run", "filesystem"),
])
def test_setuid_and_setgid_modes_are_recognised_in_every_spelling(script, category):
    assert category in foreign.classify_scripts({"x": script}).categories


@pytest.mark.parametrize("script", [
    "install -d -o root -g root -m 0755 /opt/foo/cache", "chown root /opt/foo/cache", "chown root:root /opt/foo/x",
    "install -m 0755 -o root x /opt/foo/bin/x", "mkdir -m 0755 /opt/foo/d", "cp --mode=0644 a /opt/foo/b",
    "touch -d yesterday /opt/foo/stamp", "chgrp users /opt/foo/shared",
])
def test_option_values_are_not_mistaken_for_paths(script):
    analysis = foreign.classify_scripts({"x": script})
    assert not analysis.blocks_auto_conversion, (script, analysis.blocking)


@pytest.mark.parametrize("script", [
    "install -m 0755 -o root x /usr/bin/x", "install -Dm755 x --target-directory /usr/bin", "chown root /etc/sudoers",
    "chmod 755 /usr/bin/x", "chown root:root /usr/lib/systemd/system/x.service", "cp -t /etc/sudoers.d a",
    "chmod --reference=/etc/x /usr/bin/y", "rmdir /usr/bin/x", "mv -S .bak a /etc/b",
])
def test_the_real_paths_of_those_commands_are_still_judged(script):
    assert foreign.classify_scripts({"x": script}).blocks_auto_conversion, script


def test_cd_stays_unknown_because_relative_paths_after_it_cannot_be_judged():
    assert foreign.classify_scripts({"x": "cd /usr/bin && rm -f x"}).blocks_auto_conversion


# -- round 5: the payload scan ---------------------------------------------------------------------------
import io  # noqa: E402
import tarfile  # noqa: E402


def _deb_with(tmp_path, entries):
    """A deb whose data.tar holds `entries`: (path, "file", bytes) | (path, "symlink", target) | (path, "fifo", None)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, kind, value in [("usr/bin/hello", "file", b"#!/bin/sh\n"), *entries]:
            info = tarfile.TarInfo("./" + name)
            if kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, value
                tar.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                tar.addfile(info)
            else:
                info.size, info.mode = len(value), 0o755
                tar.addfile(info, io.BytesIO(value))
    control = {"./control": b"Package: hello\nVersion: 1\nArchitecture: amd64\nMaintainer: T <t@x.invalid>\n"
                            b"Installed-Size: 1\nDepends: \nDescription: t\n"}
    path = tmp_path / "hello_1_amd64.deb"
    path.write_bytes(builders._ar([("debian-binary", b"2.0\n"), ("control.tar.xz", builders._tar_bytes(control, "w:xz")),
                                   ("data.tar.gz", buf.getvalue())]))
    return foreign.analyse(detect_file(path), satisfy=lambda d: {}, sonames_on_host=set(), glibc_on_host=(2, 40))


def _refused(verdict):
    return verdict.strategy == "refuse" and any(i.code == "FOREIGN_ROOT_HOOKS" for i in verdict.issues)


@pytest.mark.parametrize("link, target", [
    ("etc/systemd/system/NetworkManager.service.d", "/opt/evil/dropin"),   # a drop-in folder that is a link
    ("etc/tmpfiles.d", "/opt/evil/tmpfiles"), ("usr/lib/tmpfiles.d", "/opt/evil/tmpfiles"),
    ("usr/lib/systemd/system/multi-user.target.wants", "/opt/evil/wants"),
    ("usr/share/libalpm/hooks", "/opt/evil/hooks"), ("etc/sudoers.d", "/opt/evil/sudoers"),
    ("usr/lib/udev/rules.d/99-x.rules", "/opt/evil/x.rules"),
])
def test_a_link_cannot_stand_in_for_a_folder_or_file_that_would_be_refused(tmp_path, link, target):
    assert _refused(_deb_with(tmp_path, [(link, "symlink", target), ("opt/evil/dropin/override.conf", "file",
                                                                         b"[Service]\nExecStartPre=/opt/evil/bin/x\n")]))


@pytest.mark.parametrize("link, target", [
    ("etc/ld.so.conf.d/evil.conf", "/tmp/evil-ld.conf"), ("etc/systemd/system/evil.service", "/tmp/evil.service"),
    ("usr/share/dbus-1/system.d/evil.conf", "/var/tmp/evil.conf"), ("etc/systemd/system/e.service", "../../../../tmp/e"),
    ("etc/ld.so.conf.d/evil.conf", "//tmp/evil"),
])
def test_a_link_in_a_place_root_reads_may_not_point_at_a_place_anyone_can_write(tmp_path, link, target):
    assert _refused(_deb_with(tmp_path, [(link, "symlink", target)]))


def test_a_link_inside_the_package_is_only_reviewed_and_shows_where_it_points(tmp_path):
    verdict = _deb_with(tmp_path, [("etc/ld.so.conf.d/hello.conf", "file", b"/opt/hello/lib\n"),
                                   ("etc/ld.so.conf.d/alias.conf", "symlink", "hello.conf")])
    assert not any(i.code == "FOREIGN_ROOT_HOOKS" for i in verdict.issues)
    assert any("alias.conf" in r and "a link to hello.conf" in r for r in verdict.scripts.review)


@pytest.mark.parametrize("rule", [
    b'ACTION=="add", RUN{program}+="/opt/evil/bin/x"\n', b'ACTION=="add", RUN+="/opt/evil/bin/x"\n',
    b'ACTION=="add", RUN="/opt/evil/bin/x"\n', b'KERNEL=="x", RUN{program}:="/x"\n',
    b'SUBSYSTEM=="usb", PROGRAM=="/opt/x", SYMLINK+="y"\n', b'IMPORT{program}="/opt/x"\n',
])
def test_every_way_a_udev_rule_runs_a_program_is_recognised(tmp_path, rule):
    assert _refused(_deb_with(tmp_path, [("usr/lib/udev/rules.d/99-x.rules", "file", rule)]))


def test_a_udev_builtin_or_plain_rule_is_not_a_program(tmp_path):
    verdict = _deb_with(tmp_path, [("usr/lib/udev/rules.d/70-x.rules", "file",
                                    b'SUBSYSTEM=="hidraw", TAG+="uaccess", RUN{builtin}+="kmod load x"\n')])
    assert not any(i.code == "FOREIGN_ROOT_HOOKS" for i in verdict.issues)


@pytest.mark.parametrize("path, tail", [("usr/lib/udev/rules.d/99-x.rules", b'RUN+="/opt/evil/bin/x"\n'),
                                        ("usr/lib/modprobe.d/x.conf", b"install usb-storage /opt/evil/bin/x\n")])
def test_a_rule_file_too_large_to_read_is_refused_not_half_read(tmp_path, path, tail):
    big = b"# padding\n" * (foreign.MAX_SENSITIVE_READ // 10 + 10) + tail  # the dangerous line is past the cap
    assert len(big) > foreign.MAX_SENSITIVE_READ
    assert _refused(_deb_with(tmp_path, [(path, "file", big)]))


def test_a_named_pipe_cannot_hang_the_scan(tmp_path):
    import threading

    root = tmp_path / "root"
    (root / "usr/lib/udev/rules.d").mkdir(parents=True)
    import os
    os.mkfifo(root / "usr/lib/udev/rules.d/99-fifo.rules")
    result = []
    worker = threading.Thread(target=lambda: result.append(foreign.privileged_paths(root)), daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "reading a FIFO must not block"
    [finding] = result[0]
    assert finding.blocks and "not a regular file" in finding.reason


def test_a_deb_with_a_named_pipe_named_like_a_rule_is_refused_not_hung(tmp_path):
    assert _refused(_deb_with(tmp_path, [("usr/lib/udev/rules.d/99-fifo.rules", "fifo", None)]))


@pytest.mark.parametrize("path, content, blocks", [
    ("etc/logrotate.d/hello", b"/var/log/hello.log {\n  weekly\n  postrotate\n    /usr/bin/hello --reopen\n  endscript\n}\n", True),
    ("etc/logrotate.d/hello", b"/var/log/hello.log {\n  weekly\n  rotate 4\n}\n", False),
    ("usr/lib/systemd/system-environment-generators/60-x", b"#!/bin/sh\n", True),
    ("usr/lib/systemd/user-environment-generators/60-x", b"#!/bin/sh\n", True),
    ("etc/fish/conf.d/hello.fish", b"echo hi\n", True), ("etc/bash_completion.d/hello", b"complete -F _x hello\n", True),
    ("usr/share/fish/vendor_conf.d/hello.fish", b"echo hi\n", True),
])
def test_other_places_that_run_code_for_root_are_covered(tmp_path, path, content, blocks):
    verdict = _deb_with(tmp_path, [(path, "file", content)])
    assert _refused(verdict) is blocks, (path, verdict.issues)


def test_the_strictest_finding_wins_whatever_order_the_rules_are_in(tmp_path):
    # matches both a review rule (dbus policy) and the link-target rule: the block must not be hidden
    verdict = _deb_with(tmp_path, [("usr/share/dbus-1/system.d/x.conf", "symlink", "/tmp/x"),
                                   ("etc/xdg/autostart/ok.desktop", "file", b"[Desktop Entry]\n")])
    assert _refused(verdict)


# -- advisor second look at the link check ----------------------------------------------------------------
@pytest.mark.parametrize("entries", [
    # a relative target that does NOT climb above the package root is still /tmp once installed
    [("etc/ld.so.conf.d/evil.conf", "symlink", "../../tmp/evil")],
    [("etc/ld.so.conf.d/evil.conf", "symlink", "../../../../../tmp/evil")],  # climbing past the root stays at the root
    [("etc/systemd/system/e.service", "symlink", "../../../tmp/e")],
    [("etc/ld.so.conf.d/evil.conf", "symlink", "../../var/tmp/evil")],
    [("etc/ld.so.conf.d/evil.conf", "symlink", "../../dev/shm/evil")],
    [("etc/ld.so.conf.d/evil.conf", "symlink", "../../home/user/evil.conf")],
    # a chain: the first link looks fine (it ends under /opt) but the link it reaches leaves the package
    [("etc/ld.so.conf.d/evil.conf", "symlink", "/opt/x"), ("opt/x", "symlink", "/tmp/y")],
    [("etc/ld.so.conf.d/evil.conf", "symlink", "../../opt/x"), ("opt/x", "symlink", "../tmp/y")],
    # a folder link in the middle of the path
    [("etc/ld.so.conf.d/evil.conf", "symlink", "/opt/d/x"), ("opt/d", "symlink", "/tmp")],
    [("etc/ld.so.conf.d/evil.conf", "symlink", "/opt/d/x"), ("opt/d", "symlink", "/var/tmp/shared")],
    # a loop never ends well
    [("etc/ld.so.conf.d/evil.conf", "symlink", "/opt/a"), ("opt/a", "symlink", "/opt/b"), ("opt/b", "symlink", "/opt/a")],
])
def test_a_link_that_is_in_effect_a_path_anyone_can_write_is_refused_however_it_is_spelled(tmp_path, entries):
    assert _refused(_deb_with(tmp_path, entries)), entries


@pytest.mark.parametrize("entries", [
    [("etc/ld.so.conf.d/hello.conf", "file", b"/opt/hello/lib\n"), ("etc/ld.so.conf.d/alias.conf", "symlink", "hello.conf")],
    [("etc/ld.so.conf.d/hello.conf", "symlink", "/usr/lib/hello/ld.conf")],
    [("etc/ld.so.conf.d/hello.conf", "symlink", "../../opt/hello/ld.conf"), ("opt/hello/ld.conf", "file", b"/opt/hello/lib\n")],
    [("etc/ld.so.conf.d/hello.conf", "symlink", "/opt/hello/ld.conf"), ("opt/hello", "symlink", "/usr/lib/hello")],
])
def test_a_link_that_stays_inside_the_package_or_under_usr_and_opt_is_only_reviewed(tmp_path, entries):
    verdict = _deb_with(tmp_path, entries)
    assert not any(i.code == "FOREIGN_ROOT_HOOKS" for i in verdict.issues), verdict.issues


def test_a_cron_job_that_is_a_link_is_still_reported_as_left_out_with_what_it_means():
    # Chrome's /etc/cron.daily/google-chrome is a link to a script in /opt: it must not slip past the notice
    import io, os, tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "etc/cron.daily").mkdir(parents=True)
        os.symlink("/opt/hello/cron/hello", root / "etc/cron.daily/hello")
        assert foreign.analyse_payload(root).left_out_links == ["etc/cron.daily/hello"]
    verdict = _deb_with(Path(tempfile.mkdtemp()), [("etc/cron.daily/hello", "symlink", "/opt/hello/cron/hello")])
    [issue] = [i for i in verdict.issues if i.code == "FOREIGN_FILES_DROPPED"]
    assert "etc/cron.daily/hello" in issue.explanation and "will therefore not update itself" in issue.explanation
