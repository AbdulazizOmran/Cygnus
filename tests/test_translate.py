"""Install-script effects that are reproduced safely, without running anything: the menu icon and the command links.
The script text is Google Chrome's real postinst (the lines that matter); the package tree is a small stand-in."""

import os
import struct
import zlib
from pathlib import Path

import pytest

from cygnus.core.backends import translate

CHROME_POSTINST = r'''#!/bin/sh
set -e
XDG_ICON_RESOURCE="`command -v xdg-icon-resource 2> /dev/null || true`"
if [ ! -x "$XDG_ICON_RESOURCE" ]; then
  echo "Error: Could not find xdg-icon-resource" >&2
  exit 1
fi
for icon in  product_logo_16.png product_logo_24.png product_logo_32.png product_logo_48.png product_logo_64.png product_logo_128.png product_logo_256.png; do
  size="$(echo ${icon} | sed 's/[^0-9]//g')"
  "$XDG_ICON_RESOURCE" install --size "${size}" "/opt/google/chrome/${icon}" \
    "google-chrome"
done
PRIORITY=200
update-alternatives --install /usr/bin/x-www-browser x-www-browser \
  /usr/bin/google-chrome-stable $PRIORITY
update-alternatives --install /usr/bin/gnome-www-browser gnome-www-browser \
  /usr/bin/google-chrome-stable $PRIORITY
update-alternatives --install /usr/bin/google-chrome google-chrome \
  /usr/bin/google-chrome-stable $PRIORITY
PGP_KEY_DATA=$(cat <<KEYDATA
update-alternatives --install /usr/bin/evil evil /opt/google/chrome/chrome 1
KEYDATA
)
'''


def png(path: Path, size: int, height: int | None = None) -> None:
    """A real PNG of the given size (the header is what is read, but a real one is what is shipped)."""
    h = height or size

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * size for _ in range(h))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture
def chrome(tmp_path):
    root = tmp_path / "root"
    (root / "opt/google/chrome").mkdir(parents=True)
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/share/applications").mkdir(parents=True)
    for size in (16, 32, 48):
        png(root / f"opt/google/chrome/product_logo_{size}.png", size)
    png(root / "opt/google/chrome/product_logo_17.png", 17)  # not a standard size
    png(root / "opt/google/chrome/wide.png", 16, 32)  # not square
    (root / "opt/google/chrome/google-chrome").write_bytes(b"#!/bin/sh\n")
    os.symlink("/opt/google/chrome/google-chrome", root / "usr/bin/google-chrome-stable")
    (root / "usr/share/applications/google-chrome.desktop").write_text(
        "[Desktop Entry]\nName=Google Chrome\nExec=/usr/bin/google-chrome-stable %U\nIcon=google-chrome\n")
    return root


def test_the_effects_are_found_even_when_the_program_is_called_through_a_variable_and_inside_a_loop():
    found = translate.effects({"postinst": CHROME_POSTINST})
    kinds = [(e.kind, e.args[:2]) for e in found]
    assert ("icon", ("/opt/google/chrome/${icon}", "google-chrome")) in kinds
    assert [e.args[0] for e in found if e.kind == "alternative"] == ["/usr/bin/x-www-browser", "/usr/bin/gnome-www-browser",
                                                                      "/usr/bin/google-chrome"]
    assert not any("evil" in " ".join(e.args) for e in found)  # text inside a here-document is data, not a command


def test_only_scripts_that_run_after_the_files_are_there_count():
    assert translate.effects({"prerm": CHROME_POSTINST, "postrm": CHROME_POSTINST}) == []
    assert translate.effects({"postin": "ln -s /opt/x/x /usr/bin/x"})


def test_chromes_icon_and_command_are_added_and_the_generic_aliases_are_not(chrome):
    additions = translate.plan(chrome, {"postinst": CHROME_POSTINST})
    assert [a.path for a in additions if a.kind == "link"] == ["usr/bin/google-chrome"]  # not x-www-browser, not evil
    assert sorted(a.path for a in additions if a.kind == "icon") == [
        f"usr/share/icons/hicolor/{s}x{s}/apps/google-chrome.png" for s in (16, 32, 48)]  # no 17x17, no 16x32
    notes = translate.apply(chrome, additions)
    assert os.readlink(chrome / "usr/bin/google-chrome") == "/usr/bin/google-chrome-stable"
    icon = chrome / "usr/share/icons/hicolor/32x32/apps/google-chrome.png"
    assert icon.read_bytes() == (chrome / "opt/google/chrome/product_logo_32.png").read_bytes()
    assert oct(icon.stat().st_mode & 0o777) == "0o644"
    assert notes == ["added the command google-chrome (starts /usr/bin/google-chrome-stable)",
                     "added the menu icon google-chrome (3 sizes: 16, 32, 48)"]  # the sizes of one icon share a line


def test_a_file_that_another_installed_package_owns_is_never_added(chrome):
    owned = {"/usr/bin/google-chrome": "google-chrome", "/usr/share/icons/hicolor/32x32/apps/google-chrome.png": "other"}
    additions = translate.plan(chrome, {"postinst": CHROME_POSTINST}, owner=lambda path: owned.get(path))
    paths = [a.path for a in additions]
    assert "usr/bin/google-chrome" not in paths and "usr/share/icons/hicolor/32x32/apps/google-chrome.png" not in paths
    assert "usr/share/icons/hicolor/16x16/apps/google-chrome.png" in paths


def test_nothing_is_added_over_a_file_that_the_package_already_has(chrome):
    os.symlink("/opt/google/chrome/google-chrome", chrome / "usr/bin/google-chrome")
    (chrome / "usr/share/pixmaps").mkdir()
    (chrome / "usr/share/pixmaps/google-chrome.png").write_bytes(b"x")
    assert translate.plan(chrome, {"postinst": CHROME_POSTINST}) == []  # it already has both a command and an icon


def test_an_icon_is_only_made_for_a_name_a_menu_entry_of_the_package_asks_for(chrome):
    (chrome / "usr/share/applications/google-chrome.desktop").write_text("[Desktop Entry]\nName=C\nIcon=/opt/x/logo.png\n")
    assert [a for a in translate.plan(chrome, {"postinst": CHROME_POSTINST}) if a.kind == "icon"] == []
    (chrome / "usr/share/applications/google-chrome.desktop").write_text("[Desktop Entry]\nName=C\nIcon=something-else\n")
    assert [a for a in translate.plan(chrome, {"postinst": CHROME_POSTINST}) if a.kind == "icon"] == []


@pytest.mark.parametrize("line", [
    "ln -s /opt/google/chrome/google-chrome $HOME/bin/chrome",  # a variable: not what it will be when run
    "ln -s /opt/google/chrome/google-chrome /etc/profile.d/chrome.sh",  # not /usr/bin
    "ln -s /usr/lib/libc.so.6 /usr/bin/mylibc",  # not a file of the package
    "ln -s /opt/google/chrome/google-chrome /usr/bin/..",
    "ln -s /opt/google/chrome/google-chrome '/usr/bin/a b'",
    "ln -s /opt/google/chrome/google-chrome /usr/bin/-rf",
    "ln -s /usr/bin/google-chrome /usr/bin/google-chrome",  # a link to itself
    "update-alternatives --install /usr/bin/editor editor /opt/google/chrome/google-chrome 50",  # generic alternative
    "update-alternatives --install /usr/bin/java java /opt/google/chrome/google-chrome 50",
])
def test_a_link_that_breaks_a_rule_is_never_made(chrome, line):
    assert [a for a in translate.plan(chrome, {"postinst": line}) if a.kind == "link"] == []


def test_a_plain_launcher_link_is_made_when_the_package_does_not_ship_it(chrome):
    additions = translate.plan(chrome, {"postinst": "ln -sf /opt/google/chrome/google-chrome /usr/bin/chrome"})
    assert [(a.kind, a.path, a.source) for a in additions] == [("link", "usr/bin/chrome", "/opt/google/chrome/google-chrome")]


def test_a_folder_that_is_a_link_cannot_lead_the_addition_out_of_the_package(chrome, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "tool").write_bytes(b"x")
    (chrome / "opt/evil").symlink_to(outside)
    additions = translate.plan(chrome, {"postinst": "ln -s /opt/evil/tool /usr/bin/tool"})
    assert additions == []  # the target is reached through a link: not a file of the package
    os.rmdir(chrome / "usr/bin") if not any((chrome / "usr/bin").iterdir()) else None


def test_at_most_a_dozen_icon_sizes_are_added(chrome):
    for size in (22, 24, 36, 64, 72, 96, 128, 192, 256, 512):
        png(chrome / f"opt/google/chrome/product_logo_{size}.png", size)
    icons = [a for a in translate.plan(chrome, {"postinst": CHROME_POSTINST}) if a.kind == "icon"]
    assert len(icons) == translate.MAX_ICONS  # 13 sizes are shipped, the smallest 12 are used


def test_unbalanced_quotes_in_a_script_teach_nothing_and_break_nothing():
    assert translate.effects({"postinst": "ln -s 'unterminated /usr/bin/x\necho done\n"}) == []


def test_an_icon_folder_reached_through_a_link_is_never_written_to(chrome, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (chrome / "usr/share/icons").symlink_to(outside)  # the package ships usr/share/icons as a link out of itself
    additions = translate.plan(chrome, {"postinst": CHROME_POSTINST})
    assert [a for a in additions if a.kind == "icon"] == []
    translate.apply(chrome, [translate.Addition("icon", "usr/share/icons/hicolor/32x32/apps/x.png",
                                                "opt/google/chrome/product_logo_32.png", "x")])  # even if one were forced
    assert list(outside.iterdir()) == []


def test_missing_icon_folders_are_made_inside_the_package(chrome):
    assert not (chrome / "usr/share/icons").exists()
    translate.apply(chrome, translate.plan(chrome, {"postinst": CHROME_POSTINST}))
    assert (chrome / "usr/share/icons/hicolor/48x48/apps/google-chrome.png").is_file()


def test_the_package_being_replaced_does_not_count_as_another_owner(chrome):
    # after Chrome is converted once, google-chrome-stable owns its icon and command: the next update must keep adding them
    owned = {"/usr/bin/google-chrome": "google-chrome-stable",
             "/usr/share/icons/hicolor/32x32/apps/google-chrome.png": "google-chrome-stable"}
    own = translate.ignoring(lambda path: owned.get(path), "google-chrome-stable")
    paths = [a.path for a in translate.plan(chrome, {"postinst": CHROME_POSTINST}, owner=own)]
    assert "usr/bin/google-chrome" in paths and "usr/share/icons/hicolor/32x32/apps/google-chrome.png" in paths
    other = translate.ignoring(lambda path: "some-other-package" if path in owned else None, "google-chrome-stable")
    paths = [a.path for a in translate.plan(chrome, {"postinst": CHROME_POSTINST}, owner=other)]
    assert "usr/bin/google-chrome" not in paths and "usr/share/icons/hicolor/32x32/apps/google-chrome.png" not in paths


def test_an_old_style_layout_is_planned_the_same_before_and_after_conversion_merges_it(tmp_path):
    # the analysis sees /bin/foo as it comes; conversion sees /usr/bin/foo: both must list the same link
    old = tmp_path / "old"
    (old / "bin").mkdir(parents=True)
    (old / "bin/foo").write_bytes(b"#!/bin/sh\n")
    merged = tmp_path / "merged"
    (merged / "usr/bin").mkdir(parents=True)
    (merged / "usr/bin/foo").write_bytes(b"#!/bin/sh\n")
    script = {"postinst": "update-alternatives --install /usr/bin/foo-cmd foo-cmd /usr/bin/foo 50"}
    assert [a.path for a in translate.plan(old, script)] == [a.path for a in translate.plan(merged, script)] == ["usr/bin/foo-cmd"]
    (old / "sbin").mkdir()
    (old / "sbin/foo-cmd").write_bytes(b"x")  # the command already exists, under its old name
    assert translate.plan(old, script) == []


def test_the_sizes_of_one_icon_are_described_on_a_single_line():
    adds = [translate.Addition("icon", f"usr/share/icons/hicolor/{n}x{n}/apps/app.png", "x", f"the menu icon app ({n}x{n})")
            for n in (48, 16, 256)] + [translate.Addition("link", "usr/bin/app", "/opt/app/app", "the command app (starts /opt/app/app)"),
                                       translate.Addition("icon", "usr/share/icons/hicolor/32x32/apps/solo.png", "x", "x")]
    assert [d["text"] for d in translate.describe(adds)] == [
        "the command app (starts /opt/app/app)", "the menu icon app (3 sizes: 16, 48, 256)", "the menu icon solo (32x32)"]


# -- the effects model: what the scripts would do, read from their text ------------------------------------------------
CHROME_LIKE = r'''#!/bin/sh
set -e
DEFAULTS_FILE="/etc/default/google-chrome"
chrome_management_service_setup() {
  getent group chromemgmt > /dev/null || groupadd chromemgmt
  chgrp chromemgmt "/opt/google/chrome/chrome-management-service"
  chmod 2755 "/opt/google/chrome/chrome-management-service"
  mkdir -p "/etc/opt/chrome/policies/enrollment"
  touch "$SIGNING_KEY_FILE"
}
chrome_management_service_setup
if [ -f "/opt/google/chrome/apparmor.d/google-chrome-stable" ]; then
  cp "/opt/google/chrome/apparmor.d/google-chrome-stable" "/etc/apparmor.d/google-chrome-stable"
fi
for icon in a.png b.png; do
  "$XDG_ICON_RESOURCE" install --size 16 "/opt/google/chrome/${icon}" "google-chrome"
done
update-alternatives --install /usr/bin/google-chrome google-chrome /usr/bin/google-chrome-stable 200
echo 'repo_add_once="true"' >"$DEFAULTS_FILE"
echo done > /dev/null 2>&1
systemctl enable chrome-helper.service || true
curl -fsSL https://dl.google.com/linux/linux_signing_key.pub -o /tmp/key
sed -i -e 's/a/b/' /etc/default/google-chrome
useradd --system chromed
'''


def _effects():
    return translate.script_effects({"postinst": CHROME_LIKE})


def test_each_command_is_marked_conditional_when_it_may_not_run():
    found = {(e.kind, e.target): e for e in _effects()}
    assert found[("user", "chromemgmt")].conditional is True  # after ||, inside a function
    assert found[("mkdir", "/etc/opt/chrome/policies/enrollment")].conditional is True  # inside a function
    assert found[("write", "/etc/apparmor.d/google-chrome-stable")].conditional is True  # inside an if
    assert found[("alternative", "/usr/bin/google-chrome")].conditional is False  # at the top
    assert found[("icon", "google-chrome")].conditional is False  # a for loop runs
    assert found[("user", "chromed")].conditional is False and found[("service", "chrome-helper.service")].conditional is False


def test_a_path_that_a_variable_decides_is_said_to_be_decided_while_running():
    found = {(e.kind, e.target): e for e in _effects()}
    assert found[("write", "$DEFAULTS_FILE")].literal is False and found[("write", "$SIGNING_KEY_FILE")].literal is False
    assert found[("write", "/etc/default/google-chrome")].literal is True


def test_quiet_redirections_and_standard_streams_are_not_writes():
    targets = [e.target for e in _effects() if e.kind == "write"]
    assert "/dev/null" not in targets and "&1" not in targets


def test_downloads_users_services_and_edits_are_found():
    kinds = {(e.kind, e.target) for e in _effects()}
    assert ("download", "https://dl.google.com/linux/linux_signing_key.pub") in kinds
    assert ("write", "/etc/default/google-chrome") in kinds  # sed -i
    assert ("service", "chrome-helper.service") in kinds and ("user", "chromed") in kinds


def test_the_effects_are_grouped_and_qualified_for_the_person():
    groups = {g["title"]: g["items"] for g in translate.describe_effects(_effects())}
    assert "/etc/opt/chrome/policies/enrollment [postinst] (only if a condition holds)" in groups["Folders they create"]
    assert any(i.startswith("$DEFAULTS_FILE [postinst] (the path is only known while the script runs)")
               for i in groups["Files they write or change"])
    assert any(i.startswith("chromed (useradd) [postinst]") for i in groups["Users and groups they create or change"])


def test_removal_scripts_say_nothing_about_what_installing_does():
    assert translate.script_effects({"prerm": "rm -rf /opt/x\nmkdir /opt/y", "postrm": "useradd z"}) == []


def test_only_unconditional_literal_effects_can_be_reproduced():
    kinds = {e.kind for e in translate.effects({"postinst": CHROME_LIKE})}
    assert kinds == {"icon", "alternative"}  # the folder, the group and the copy are all conditional


@pytest.mark.parametrize("current, spec, expected", [
    (0o644, "755", 0o755), (0o644, "0755", 0o755), (0o600, "+x", 0o711), (0o644, "a+x", 0o755), (0o644, "u+x", 0o744),
    (0o755, "go-w", 0o755), (0o777, "go-w", 0o755), (0o644, "u=rwx", 0o744), (0o644, "g+rX", 0o644), (0o755, "o-rwx", 0o750),
    (0o644, "4755", None), (0o644, "2755", None), (0o644, "u+s", None), (0o644, "+t", None), (0o644, "u+x,g+x", None),
    (0o644, "rwx", None), (0o644, "", None), (0o644, "8", None)])
def test_only_plain_permissions_are_ever_applied(current, spec, expected):
    assert translate._new_mode(current, spec) == expected


def test_a_conditional_or_nested_command_is_not_taken_for_a_top_level_one():
    texts = [s for s in translate.statements("if true; then\n  touch /a\nfi\ntouch /b\nfor i in 1; do\n touch /c\ndone\n"
                                              "f() {\n touch /d\n}\ntrue || touch /e\ntouch /f\n")]
    flags = {s.words[-1]: s.conditional for s in texts if s.words[0] == "touch"}
    assert flags == {"/a": True, "/b": False, "/c": False, "/d": True, "/e": True, "/f": False}


def test_a_command_link_with_the_same_name_as_its_target_is_made(chrome):
    additions = translate.plan(chrome, {"postinst": "ln -sf /opt/google/chrome/google-chrome /usr/bin/google-chrome"})
    assert [(a.kind, a.path, a.source) for a in additions] == [("link", "usr/bin/google-chrome", "/opt/google/chrome/google-chrome")]


@pytest.mark.parametrize("line", [
    "ln -sf /opt/google/chrome/google-chrome /usr/bin/chrome 2>/dev/null",
    "ln -sf /opt/google/chrome/google-chrome /usr/bin/chrome >/dev/null 2>&1",
    "ln -sf /opt/google/chrome/google-chrome /usr/bin/chrome &>/dev/null",
    "ln -sf /opt/google/chrome/google-chrome /usr/bin/chrome > /dev/null",
    "ln -sf /opt/google/chrome/google-chrome /usr/bin/chrome 2> /tmp/err.log",
])
def test_a_redirection_after_a_command_does_not_hide_the_command(chrome, line):
    additions = translate.plan(chrome, {"postinst": line})
    assert [(a.kind, a.path) for a in additions] == [("link", "usr/bin/chrome")]


def test_a_redirection_does_not_hide_an_icon_install_either(chrome):
    quiet = CHROME_POSTINST.replace('    "google-chrome"\ndone', '    "google-chrome" 2>/dev/null\ndone')
    assert quiet != CHROME_POSTINST
    icons = [a for a in translate.plan(chrome, {"postinst": CHROME_POSTINST}) if a.kind == "icon"]
    assert icons and [a for a in translate.plan(chrome, {"postinst": quiet}) if a.kind == "icon"] == icons


@pytest.mark.parametrize("words, expected", [
    (["ln", "-s", "a", "b", "2", ">", "/dev/null"], ["ln", "-s", "a", "b"]),
    (["ln", "-s", "a", "b", ">", "/dev/null", "2", ">&", "1"], ["ln", "-s", "a", "b"]),
    (["cat", "<<", "EOF", ">", "out"], ["cat"]),
    (["chmod", "2755", "x"], ["chmod", "2755", "x"]),  # a number that is not in front of a redirection stays
    (["echo", "a", "&>>", "log"], ["echo", "a"]),
])
def test_redirections_are_taken_off_a_command_before_its_arguments_are_read(words, expected):
    assert translate._without_redirections(words) == expected


LN = "ln -s /opt/google/chrome/google-chrome /usr/bin/chrome"


@pytest.mark.parametrize("script", [
    "# an old note \\\n" + LN,  # a comment that ends in a backslash does not swallow the next line
    "n=3\nx=$((1<<n))\n" + LN + "\nn",  # arithmetic is not a here-document
    "grep -q a <<<word\n" + LN + "\nword",  # nor is a here-string
    'echo "Installing\ndone"; ' + LN,  # a quotation that runs over several lines
    "echo 'it takes\nlong'\n" + LN,
])
def test_the_scanner_reads_the_shell_the_way_the_shell_does(chrome, script):
    assert [(a.kind, a.path) for a in translate.plan(chrome, {"postinst": script})] == [("link", "usr/bin/chrome")]


def test_a_while_loop_may_not_run_so_what_is_inside_it_is_conditional():
    inside = translate.statements("while false; do\n  " + LN + "\ndone\n" + LN + "\nuntil true; do " + LN + "; done\n")
    assert [s.conditional for s in inside] == [True, False, True]
    assert [s.conditional for s in translate.statements("for i in 1 2; do " + LN + "; done")] == [False]  # a for loop runs
    assert translate.plan(Path("/nonexistent"), {"postinst": "while false; do " + LN + "; done"}) == []


def test_a_heredoc_body_is_still_skipped():
    text = "cat <<EOF > /etc/x\n" + LN + "\nEOF\ntrue\n"
    assert translate.statements(text) == [translate.Statement(("cat", "<<", "EOF", ">", "/etc/x"), False), translate.Statement(("true",), False)]


# -- round 7: the scanner must not lose or invent commands --------------------------------------------------------------
def _links(chrome, script):
    return [(a.kind, a.path) for a in translate.plan(chrome, {"postinst": script})]


@pytest.mark.parametrize("script", [
    "set -- a\nif [ -d /nonexistent ] && [ $# -gt 0 ]; then\n  " + LN + "\nfi\n",  # `$#` is not a comment: the `then` is seen
    "if [ ${#x} -gt 3 ]; then\n  " + LN + "\nfi\n",
    "d=${1#/}; if [ -d /nonexistent ]; then\n  " + LN + "\nfi\n",
])
def test_a_hash_inside_a_word_is_not_a_comment_so_a_conditional_stays_conditional(chrome, script):
    assert _links(chrome, script) == []  # the link is only made if something holds: never reproduced as if it always happened


@pytest.mark.parametrize("script", [
    "n=3\n(( mask = 1 << n ))\n" + LN + "\nn",  # a shift in (( )), not a here-document
    'echo "use 1 << n"\n' + LN + "\nn",  # nor inside quotation marks
    "printf $'it\\'s\\n'\n" + LN,  # an ANSI-C quotation with an escaped quote does not swallow the rest of the script
    "echo hi # it's a comment with an apostrophe\n" + LN,
    "echo hi # a comment ending in a backslash \\\n" + LN,
])
def test_things_that_look_like_a_heredoc_or_an_open_quote_do_not_swallow_what_follows(chrome, script):
    assert _links(chrome, script) == [("link", "usr/bin/chrome")]


def test_a_heredoc_ends_at_its_own_delimiter_exactly():
    plain = "cat <<EOF\nbody\n  EOF\n" + LN + "\nEOF\ntrue\n"  # an indented EOF does not end `<<EOF`: the ln is body text
    assert [s.words[0] for s in translate.statements(plain)] == ["cat", "true"]
    dashed = "cat <<-EOF\n\tbody\n\tEOF\n" + LN + "\n"  # `<<-` ignores leading tabs
    assert [s.words[0] for s in translate.statements(dashed)] == ["cat", "ln"]
    spaces = "cat <<-EOF\n body\n  EOF\n" + LN + "\nEOF\ntrue\n"  # ...but not leading spaces
    assert [s.words[0] for s in translate.statements(spaces)] == ["cat", "true"]


def test_a_clobbering_redirection_is_a_write():
    kinds = {(e.kind, e.target) for e in translate.script_effects({"postinst": "echo x >| /etc/app.conf\n"})}
    assert ("write", "/etc/app.conf") in kinds


@pytest.mark.parametrize("line, code, is_open, dangling", [
    ("echo $# done", "echo $# done", False, False),
    ("echo a # b", "echo a ", False, False),
    ("echo a#b", "echo a#b", False, False),
    ("echo 'it # not' x", "echo 'it # not' x", False, False),
    ('echo "a', 'echo "a', True, False),
    ("echo \\", "echo \\", False, True),
    ("echo \\\\", "echo \\\\", False, False),  # an escaped backslash is not a continuation
    ("x=$'a\\'b' y", "x=$'a\\'b' y", False, False),
])
def test_the_line_scanner(line, code, is_open, dangling):
    scan = translate._scan(line)
    assert (scan.code, scan.open, scan.dangling) == (code, is_open, dangling)


def test_nothing_after_an_unconditional_exit_is_reproduced_but_an_exit_inside_a_condition_changes_nothing():
    assert translate.plan(Path("/nonexistent"), {"postinst": "exit 0\n" + LN + "\n"}) == []  # never reached
    after = translate.statements("exit 0\n" + LN + "\n")
    assert [s.conditional for s in after] == [False, True]
    guarded = translate.statements('if [ ! -x "$X" ]; then\n  echo no >&2\n  exit 1\nfi\n' + LN + "\n")  # Chrome's own guard
    assert [(s.words[0], s.conditional) for s in guarded if s.words[0] in ("exit", "ln")] == [("exit", True), ("ln", False)]


# -- round 8: the scanner against what the real shell does --------------------------------------------------------------
def _conditional_ln(script):
    return [s.conditional for s in translate.statements(script) if s.words[0] == "ln"]


@pytest.mark.parametrize("script", [
    "command -v update-menus >/dev/null 2>&1 || exit 0\n" + LN + "\n",  # the quiet-skip guard: what follows may never run
    'case "$1" in\n  configure) ;;\n  *) exit 0 ;;\nesac\n' + LN + "\n",
    'if [ "$1" != configure ]; then\n  exit 0\nfi\n' + LN + "\n",
    "exit\n" + LN + "\n",
])
def test_what_follows_a_quiet_exit_is_not_sure_to_run(script):
    assert _conditional_ln(script) == [True]


@pytest.mark.parametrize("script", [
    'if [ ! -x "$X" ]; then\n  exit 1\nfi\n' + LN + "\n",  # an error exit aborts the install: what follows is the normal path
    "f() {\n  exit 0\n}\n" + LN + "\n",  # inside a function it only matters when the function is called
    "f() { return 0; }\n" + LN + "\n",
    "function g {\n  exit 0\n}\n" + LN + "\n",
])
def test_an_error_exit_or_an_exit_inside_a_function_does_not_make_what_follows_conditional(script):
    assert _conditional_ln(script) == [False]


def test_a_quoted_keyword_is_an_argument_not_the_end_of_an_if():
    script = 'if [ "$1" = configure ]; then\n  echo "fi"\n  echo "}"\n  ' + LN + '\nfi\n'
    assert _conditional_ln(script) == [True]
    assert [s.words for s in translate.statements('echo "case" & ' + LN)][-1][0] == "ln"


def test_a_line_ending_in_and_or_or_makes_the_next_line_depend_on_it():
    assert _conditional_ln('[ "$1" = configure ] &&\n' + LN + "\n") == [True]
    assert _conditional_ln("true ||\n" + LN + "\n") == [True]
    assert _conditional_ln("true &&\n" + LN + "\n" + LN + "\n") == [True, False]  # only the one that follows it


def test_an_ansi_c_quote_on_the_same_line_does_not_hide_the_command_after_it():
    assert _conditional_ln("echo $'\\'' ; " + LN + "\n") == [False]
    assert translate.statements("echo $'a b' c")[0].words == ("echo", "a b", "c")


def test_a_backslash_quoted_heredoc_delimiter_is_a_heredoc():
    text = "cat > /usr/share/doc/x <<\\EOF\n" + LN + "\nEOF\ntrue\n"
    assert [s.words[0] for s in translate.statements(text)] == ["cat", "true"]


def test_a_plain_heredoc_is_not_ended_by_a_tab_indented_delimiter():
    text = "cat <<EOF\nbody\n\tEOF\n" + LN + "\nEOF\ntrue\n"
    assert [s.words[0] for s in translate.statements(text)] == ["cat", "true"]


@pytest.mark.parametrize("header, sure", [
    ("for x in 16 24 32", True), ("for x in a.png b.png", True),
    ("for x in $(ls /opt/foo)", False), ("for x in /opt/foo/*.png", False), ('for x in "$LIST"', False), ("for x in `ls`", False)])
def test_a_for_loop_over_a_list_that_is_worked_out_while_the_script_runs_may_run_zero_times(header, sure):
    flags = [s.conditional for s in translate.statements(header + "; do\n  " + LN + "\ndone\n") if s.words[0] == "ln"]
    assert flags == [not sure]
