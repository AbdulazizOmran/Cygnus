"""The vendor's own install scripts, written into a converted package's pacman install file (`.INSTALL`), for a person who chose
that (the default is that nothing of the vendor's runs).

pacman then runs them as administrator inside the install the person already approves, so no new way of running code is needed:
a package with an install script is already announced as one that "runs an install script as root". The scripts are written out as
they came (a deb's preinst/postinst/prerm/postrm, an rpm's pre/post scriptlets), and are given the arguments Debian or rpm would
have given them, so a script that tells a first install from an upgrade can. Scripts for programs Cygnus does not run scripts with
(lua, awk...), debconf's `config` and trigger scripts are left out, and said to be."""

from __future__ import annotations

import os
from collections.abc import Collection

from cygnus.core.backends import foreign
from cygnus.core.models import PackageFormat
from cygnus.core.util import scriptrun

# A script is written into the package only if the screen showed all of it: the length shown is the length allowed.
MAX_EMBEDDED = foreign.MAX_SHOWN_SCRIPT
# (script, pacman function, the arguments the vendor's packaging would have given it). pacman gives pre_upgrade/post_upgrade the new
# version as $1 and the old as $2; an upgrade runs the new package's pre/post scripts only (the old package's own are gone).
_DEB = (("preinst", "pre_install", ("install",)), ("preinst", "pre_upgrade", ("upgrade", '"$2"')),
        ("postinst", "post_install", ("configure",)), ("postinst", "post_upgrade", ("configure", '"$2"')),
        ("prerm", "pre_remove", ("remove",)), ("postrm", "post_remove", ("remove",)))
# rpm scriptlets are told how many copies of the package will be installed afterwards: 1 for a first install, 2 for an upgrade, and 0
# when the last copy is removed.
_RPM = (("pretrans", "pre_install", ("1",)), ("pretrans", "pre_upgrade", ("2",)), ("prein", "pre_install", ("1",)),
        ("prein", "pre_upgrade", ("2",)), ("postin", "post_install", ("1",)), ("postin", "post_upgrade", ("2",)),
        ("posttrans", "post_install", ("1",)), ("posttrans", "post_upgrade", ("2",)),
        ("preun", "pre_remove", ("0",)), ("postun", "post_remove", ("0",)))
_NOT_RUN = {"config": "it asks questions through debconf, which this system does not have", "triggers": "it reacts to other packages' changes, which pacman has no equivalent of",
            "triggerscripts": "it reacts to other packages' changes, which pacman has no equivalent of",
            "filetriggerscripts": "it reacts to other packages' changes, which pacman has no equivalent of",
            "transfiletriggerscripts": "it reacts to other packages' changes, which pacman has no equivalent of"}

_RUNNER = r'''# Written by Cygnus because you chose to run this package's own install scripts. They are the vendor's scripts, as they came, and
# they run as administrator. Each one is run in a system unit of its own when systemd answers (as `sudo` would run it, free of the
# limits of the program that is installing this package), and directly otherwise (with a clean environment, no input and a time limit).
# pacman does not stop for a script that fails, so neither does this: the failure is printed, and the install or removal goes on.
_cygnus_run() {
    local name="$1" interp="$2" opts="$3" dir file rc=0
    shift 3
    dir=$(mktemp -d /run/cygnus-script.XXXXXX 2>/dev/null) || dir=$(mktemp -d) || {
        echo "cygnus: the vendor's script $name was not run: there is nowhere to write it" >&2
        return 0
    }
    file="$dir/$name"
    if ! "_cygnus_text_$name" > "$file"; then
        echo "cygnus: the vendor's script $name was not run: it could not be written out" >&2
        rm -rf "$dir"
        return 0
    fi
    chmod 0700 "$dir" "$file"
    if [ -d /run/systemd/system ] && command -v systemd-run >/dev/null 2>&1             && systemd-run --quiet --wait --pipe --collect -- /usr/bin/true >/dev/null 2>&1; then
        systemd-run --quiet --wait --pipe --collect -p RuntimeMaxSec=600 --working-directory=/             --setenv=PATH=/usr/local/sbin:/usr/local/bin:/usr/bin --setenv=HOME=/root --setenv=LANG=C.UTF-8             -- "$interp" $opts "$file" "$@" || rc=$?
    else
        ( cd / && env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/bin HOME=/root LANG=C.UTF-8             timeout 600 "$interp" $opts "$file" "$@" </dev/null ) || rc=$?
    fi
    rm -rf "$dir"
    [ "$rc" -eq 0 ] || echo "cygnus: the vendor's script $name ended with status $rc" >&2
    return 0
}
'''


def _interpreter(fmt: PackageFormat, text: str, declared: str | None) -> scriptrun.Interpreter:
    """The program that runs this script, by one rule for what is shown and what is written: for an rpm scriptlet the program the package
    declares (as rpm runs it), for a deb the one its first line names. When an rpm scriptlet also has a first line that names another
    program, it is refused: the person could not tell which one would run."""
    if fmt is PackageFormat.RPM and declared:
        if declared.startswith("<"):
            raise scriptrun.ScriptRefused(f"it needs rpm's own {declared[:20]} interpreter, which Cygnus does not run scripts with")
        program = declared.strip()
        chosen = scriptrun.interpreter_for("#!" + (program if program.startswith("/") else "/usr/bin/" + program))  # a bare name is a program in /usr/bin
        if text.startswith("#!") and scriptrun.interpreter_for(text).program != chosen.program:
            raise scriptrun.ScriptRefused("its first line names another program than the package declares for it, so which one would run is not clear")
        return chosen
    return scriptrun.interpreter_for(text)


def _delimiter(text: str) -> str:
    """The word that ends a script's here-document: one that is not a line of the script, whatever the script contains."""
    lines = set(text.splitlines())
    delimiter, n = "CYGNUS_SCRIPT_END", 0
    while delimiter in lines:
        n += 1
        delimiter = f"CYGNUS_SCRIPT_END_{n}"
    return delimiter


def build(fmt: PackageFormat, texts: dict[str, str], interpreters: dict[str, str | None] | None = None,
          uninspected: Collection[str] = ()) -> tuple[str | None, list[str], list[str]]:
    """(the install file's text, the scripts it holds, notes about the scripts left out). The text is None when there is nothing to
    write. `texts` maps a script's name to its text; `interpreters` the programs an rpm declares for its scriptlets; `uninspected` the
    scripts the reader only kept the start of (they are never written, however much of them there is)."""
    plan = _DEB if fmt is PackageFormat.DEB else _RPM
    wanted = {name for name, _fn, _a in plan}
    included: list[str] = []
    notes: list[str] = []
    bodies: dict[str, tuple[scriptrun.Interpreter, str]] = {}
    for name in sorted(texts):
        text = texts[name]
        if name not in wanted:
            notes.append(f"{name} is not run: {_NOT_RUN.get(name, 'pacman has nothing that runs it')}")
            continue
        try:
            if not isinstance(text, str) or not text.strip():
                continue
            if name in uninspected:
                raise scriptrun.ScriptRefused("only the start of it could be read, so it cannot be shown to you whole")
            if "\0" in text or "\ufffd" in text:
                raise scriptrun.ScriptRefused("it contains bytes that are not text, so it cannot be shown to you exactly as it would run")
            invisible = scriptrun.text_problem(text)
            if invisible:
                raise scriptrun.ScriptRefused(invisible)
            if len(text) > MAX_EMBEDDED:
                raise scriptrun.ScriptRefused(f"it is longer than the {MAX_EMBEDDED:,} characters that can be shown to you whole")
            bodies[name] = (_interpreter(fmt, text, (interpreters or {}).get(name)), text)
        except scriptrun.ScriptRefused as exc:
            notes.append(f"{name} is not run: {exc}")
    if not bodies:
        return None, included, notes
    out = [_RUNNER]
    for name, (_interp, text) in sorted(bodies.items()):
        delimiter = _delimiter(text)
        # A here-document always ends with a newline: for a text that does not, the one added is taken off again (head -c -1), so the
        # script that is written is byte for byte the one that was shown.
        complete = text.endswith("\n")
        out.append(f"_cygnus_text_{name}() {{ cat <<'{delimiter}'{'' if complete else ' | head -c -1'}\n{text if complete else text + chr(10)}{delimiter}\n}}\n")
        included.append(name)
    for function in ("pre_install", "post_install", "pre_upgrade", "post_upgrade", "pre_remove", "post_remove"):
        calls = [f"    _cygnus_run {name} {bodies[name][0].program} '{' '.join(bodies[name][0].options)}' {' '.join(args)}"
                 for name, fn, args in plan if fn == function and name in bodies]
        if calls:
            out.append(f"{function}() {{\n" + "\n".join(calls) + "\n}\n")
    return "\n".join(out), included, notes


def script_names(fmt: PackageFormat, texts: dict[str, str]) -> list[str]:
    """The scripts a package carries that this can run (for the screen: what the box would run)."""
    wanted = {name for name, _fn, _a in (_DEB if fmt is PackageFormat.DEB else _RPM)}
    return sorted(n for n, t in texts.items() if n in wanted and isinstance(t, str) and t.strip())

