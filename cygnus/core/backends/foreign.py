"""DEB / RPM policy engine (architecture §10.3): prefer native, analyse, convert only when demonstrably safe.

Nothing here installs anything. The payload is extracted only into a private temporary directory
for inspection; maintainer scripts are classified as text and never executed.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cygnus.core.backends import translate
from cygnus.core.detect import deb as debmod
from cygnus.core.errors import CygnusError, DetectionError
from cygnus.core.models import Candidate, PackageFormat
from cygnus.core.recovery.model import Action, Issue, IssueSeverity, Resolution, SafetyClass, explain_only
from cygnus.core.util import proc

MAX_EXTRACT_BYTES = 4 * 1024**3
MAX_EXTRACT_ENTRIES = 200_000
MAX_ELF_FILES = 2000  # more than this and the libraries the rest need are unknown: the package is not converted


class PayloadError(DetectionError, ValueError):
    """The payload of a .deb/.rpm could not be listed or unpacked (damaged, or too large to inspect)."""

# Script classification: each script is split into simple commands by a small shell lexer, and every
# command is judged by its program name (matched exactly; a system-directory path such as /sbin/ldconfig
# counts as the program). Anything the lexer does not positively understand (command substitution,
# process substitution, here-documents, functions, redirections to real files, wrapper programs such
# as env/xargs/sh -c) makes the script "unknown", and anything unknown blocks automatic conversion
# (architecture §10.3). Scripts are never run; the cost of a wrong judgement is a silently dropped step.
_STRUCTURE = {"if", "then", "else", "elif", "fi", "case", "esac", "for", "while", "until", "do", "done", "in",
              "{", "}", "true", "false", ":", "break", "continue", "return", "exit", "shift", "set", "wait",
              "local", "export", "unset", "readonly"}
# first word -> (category, treatment, blocks)
_COMMANDS: dict[str, tuple[str, str, bool]] = {
    "echo": ("message", "informational output", False),
    "printf": ("message", "informational output", False),
    "ldconfig": ("ldconfig", "handled by pacman hooks", False),
    "update-desktop-database": ("desktop-caches", "handled by pacman hooks / desktop integration", False),
    "gtk-update-icon-cache": ("desktop-caches", "handled by pacman hooks", False),
    "update-mime-database": ("desktop-caches", "handled by pacman hooks", False),
    "glib-compile-schemas": ("desktop-caches", "handled by pacman hooks", False),
    "xdg-icon-resource": ("desktop-caches", "handled by desktop integration", False),
    "xdg-desktop-menu": ("desktop-caches", "handled by desktop integration", False),
    "update-icon-caches": ("desktop-caches", "handled by pacman hooks", False),
    "systemctl": ("systemd", "translated into an approved service action", False),
    "deb-systemd-helper": ("systemd", "translated into an approved service action", False),
    "deb-systemd-invoke": ("systemd", "translated into an approved service action", False),
    "invoke-rc.d": ("sysv-service", "dropped (SysV init is not used on CachyOS)", False),
    "update-rc.d": ("sysv-service", "dropped (SysV init is not used on CachyOS)", False),
    "service": ("sysv-service", "dropped (SysV init is not used on CachyOS)", False),
    "chkconfig": ("sysv-service", "dropped", False),
    # Conversion drops the scripts, so a user or group the program needs would never exist.
    "useradd": ("users-groups", "creates a system user", True),
    "adduser": ("users-groups", "creates a system user", True),
    "groupadd": ("users-groups", "creates a group", True),
    "addgroup": ("users-groups", "creates a group", True),
    "usermod": ("users-groups", "review: changes a user account", True),
    "gpasswd": ("users-groups", "review: changes group membership", True),
    "update-alternatives": ("alternatives", "dropped; a symlink is packaged if needed", False),
    "setcap": ("capabilities", "file capabilities: reviewed explicitly", False),
    "apparmor_parser": ("apparmor", "dropped (AppArmor is not active on this system)", False),
    "pkill": ("process-kill", "dropped (Cygnus asks you to close the app instead)", False),
    "killall": ("process-kill", "dropped", False),
    "kill": ("process-kill", "dropped", False),
    "pgrep": ("process-kill", "dropped", False),
    "sleep": ("structure", "shell control flow", False),
    "dpkg-maintscript-helper": ("dpkg-infra", "dropped (Debian packaging infrastructure)", False),
    "dpkg-trigger": ("dpkg-infra", "dropped", False),
    "dpkg": ("dpkg-infra", "dropped", False),
    "dpkg-divert": ("dpkg-infra", "dropped", False),
    "db_get": ("debconf", "interactive Debian configuration", True),
    "db_input": ("debconf", "interactive Debian configuration", True),
    "db_go": ("debconf", "interactive Debian configuration", True),
    "db_set": ("debconf", "interactive Debian configuration", True),
    "modprobe": ("kernel-module", "kernel modules", True),
    "insmod": ("kernel-module", "kernel modules", True),
    "rmmod": ("kernel-module", "kernel modules", True),
    "dkms": ("kernel-module", "kernel modules", True),
    "depmod": ("kernel-module", "kernel modules", True),
    "test": ("structure", "shell control flow", False),
    "[": ("structure", "shell control flow", False),
    "[[": ("structure", "shell control flow", False),
    "mkdir": ("filesystem", "file operations: reviewed against the package's own files", False),
    "ln": ("filesystem", "file operations: reviewed", False),
    "touch": ("filesystem", "file operations: reviewed", False),
    "rmdir": ("filesystem", "file operations: reviewed", False),
    # read-only questions about the system (output only: redirections to real files are refused separately)
    "getent": ("read-only", "asks about users, groups or hosts", False),
    "grep": ("read-only", "searches text", False), "egrep": ("read-only", "searches text", False),
    "fgrep": ("read-only", "searches text", False), "id": ("read-only", "asks about a user", False),
    "ls": ("read-only", "lists files", False), "stat": ("read-only", "asks about a file", False),
    "readlink": ("read-only", "asks about a link", False), "basename": ("read-only", "text only", False),
    "dirname": ("read-only", "text only", False), "uname": ("read-only", "asks about the system", False),
    "whoami": ("read-only", "asks about the user", False),
    "install-info": ("dpkg-infra", "handled by pacman hooks (info pages)", False),
    "udevadm": ("udev", "handled by pacman hooks (udev rules)", False),
    "rm": ("filesystem", "file operations: reviewed", False),
    "cp": ("filesystem", "file operations: reviewed", False),
    "mv": ("filesystem", "file operations: reviewed", False),
    "install": ("filesystem", "file operations: reviewed", False),
    "chmod": ("filesystem", "file operations: reviewed", False),
    "chown": ("filesystem", "file operations: reviewed", False),
    "chgrp": ("filesystem", "file operations: reviewed", False),
}
_REVIEW_CATEGORIES = {"setuid", "users-groups", "capabilities", "systemd-other", "filesystem"}
_FILE_OPS = {"rm", "rmdir", "mv", "cp", "ln", "install", "chmod", "chown", "chgrp", "touch", "mkdir"}
# File operations outside these are not allowed in an automatically converted package.
_CRITICAL_PREFIXES = ("/etc", "/usr/bin", "/usr/sbin", "/usr/lib/systemd", "/boot", "/root", "/home", "/var/lib",
                      "/bin", "/sbin", "/lib", "/dev", "/sys", "/proc", "/run")
SCRIPT_RULES = [(cat, None, treatment, blocks) for cat, treatment, blocks in
                {v for v in _COMMANDS.values()}]  # kept for documentation/introspection


@dataclass(slots=True, kw_only=True)
class ScriptAnalysis:
    categories: dict[str, list[str]] = field(default_factory=dict)  # category -> sample lines
    unknown: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)  # recognised, but needs the user's explicit review
    blocking: list[str] = field(default_factory=list)

    @property
    def blocks_auto_conversion(self) -> bool:
        return bool(self.unknown) or bool(self.blocking)

    @property
    def acknowledgeable(self) -> bool:
        """Scripts Cygnus could not read, and nothing positively found in what it CAN read that makes the result wrong
        (a user the package declares, a kernel module in a readable script...). Converting never runs a script, so the
        person may knowingly go ahead, after being shown what the scripts mention. Note that a script with any part
        Cygnus cannot read is unreadable as a whole: a group created inside it is not "found", only mentioned, which is why
        what they mention is shown first and has to be acknowledged."""
        return bool(self.unknown) and not self.blocking


_SYSTEM_BIN = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/")
_WORD_OPERATORS = {";", ";;", "&&", "||", "|", "&", "\n", "(", ")"}
_OK_REDIRECT_TARGETS = {"/dev/null"}


class _Unknown(Exception):
    """A construct the lexer will not guess about."""


def _check_expansion(text: str, i: int) -> None:
    """bash can run code that is only found in a variable's value: `$[x]` and `${x@P}` evaluate it, `${!x}` follows it."""
    if text.startswith("$[", i):
        raise _Unknown("arithmetic expansion")
    if text.startswith("${", i):
        inner = text[i + 2:text.find("}", i + 2) if text.find("}", i + 2) != -1 else len(text)]
        if inner.startswith("!") or "@" in inner:
            raise _Unknown("a parameter expansion that evaluates a variable's contents")


def _lex(text: str) -> list[tuple[str, str]]:
    """Split shell text into ("word", text) and ("op", text) tokens, the way sh would read it, up to the
    point where it would have to guess. Raises _Unknown for anything outside the understood subset."""
    tokens: list[tuple[str, str]] = []
    word: list[str] = []
    in_word = False
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal in_word
        if in_word:
            tokens.append(("word", "".join(word)))
            word.clear()
            in_word = False

    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":  # line continuation
                i += 2
                continue
            word.append(text[i + 1] if i + 1 < n else "")
            in_word = True
            i += 2
            continue
        if c == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise _Unknown("unterminated quote")
            word.append(text[i + 1:end])
            in_word = True
            i = end + 1
            continue
        if c == '"':
            i += 1
            in_word = True
            while True:
                if i >= n:
                    raise _Unknown("unterminated quote")
                d = text[i]
                if d == '"':
                    i += 1
                    break
                if d == "\\" and i + 1 < n:  # \` \$ \" \\ are literal characters inside double quotes
                    word.append(text[i + 1] if text[i + 1] in '`$"\\' else d + text[i + 1])
                    i += 2
                    continue
                if d == "`" or text.startswith("$(", i) or text.startswith("${", i) and "}" not in text[i:]:
                    raise _Unknown("command substitution")
                if d == "$":
                    _check_expansion(text, i)
                word.append(d)
                i += 1
            continue
        if c == "`" or text.startswith("$(", i):
            raise _Unknown("command substitution")
        if c == "$":
            _check_expansion(text, i)
        if text.startswith("$'", i) or text.startswith('$"', i):
            raise _Unknown("shell quoting that is decoded when the script runs")
        if c == "#" and not in_word:  # a comment runs to the end of the line, and a backslash does not continue it
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c in " \t\r":
            flush()
            i += 1
            continue
        if c == "\n":
            flush()
            tokens.append(("op", "\n"))
            i += 1
            continue
        if c in "<>":
            if text.startswith("<<", i) or text.startswith("<(", i) or text.startswith(">(", i):
                raise _Unknown("here-document or process substitution")
            fd = ""
            if in_word and "".join(word).isdigit():  # "2>" : a file descriptor number in front of it
                fd = "".join(word)
                word.clear()
                in_word = False
            flush()
            op = c
            i += 1
            if i < n and text[i] == c and c == ">":  # >>
                op += ">"
                i += 1
            elif i < n and text[i] == "&":  # >&2, <&0, >&-
                op += "&"
                i += 1
            j = i
            while j < n and text[j] in " \t":
                j += 1
            k = j
            while k < n and text[k] not in " \t\r\n;&|()<>":
                k += 1
            target = text[j:k]
            if target.startswith(("'", '"')):
                target = target.strip("'\"")
            if op.endswith("&") and (target.isdigit() or target == "-"):
                pass  # a descriptor, not a file
            elif target not in _OK_REDIRECT_TARGETS:
                raise _Unknown(f"redirection {fd}{op} {target[:40]}")
            i = k
            continue
        if c == "&" and text.startswith("&>", i):  # &> /dev/null
            flush()
            j = i + 2
            if text.startswith(">", j):
                j += 1
            while j < n and text[j] in " \t":
                j += 1
            k = j
            while k < n and text[k] not in " \t\r\n;&|()<>":
                k += 1
            if text[j:k].strip("'\"") not in _OK_REDIRECT_TARGETS:
                raise _Unknown(f"redirection to {text[j:k][:40]}")
            i = k
            continue
        two = text[i:i + 2]
        if two in ("&&", "||", ";;"):
            flush()
            tokens.append(("op", two))
            i += 2
            continue
        if c == "(" and text.startswith("((", i):
            raise _Unknown("arithmetic evaluation")
        if c in ";|&()":
            flush()
            tokens.append(("op", c))
            i += 1
            continue
        word.append(c)
        in_word = True
        i += 1
    flush()
    return tokens


_OPENERS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "time"}
_CLOSERS = {"fi", "done", "esac", "}", "in"}


def _commands(text: str) -> list[list[str]]:
    """The simple commands of a script, as word lists. Raises _Unknown if the script cannot be read with confidence."""
    segments: list[tuple[list[str], str]] = []  # (words, the operator that ended them)
    current: list[str] = []
    for kind, value in _lex(text):
        if kind == "word":
            current.append(value)
        else:
            segments.append((current, value))
            current = []
    segments.append((current, "\n"))
    commands: list[list[str]] = []
    in_case = 0  # depth of `case … in`
    expecting_pattern = False  # inside a case's pattern list: `a|b)` are patterns, not commands
    for words, op in segments:
        if expecting_pattern:
            if words == ["esac"]:
                expecting_pattern = False
                in_case = max(0, in_case - 1)
            elif op == ")":
                expecting_pattern = False
            continue
        if op == "(" and not words:
            continue
        if words and words[0] == "case":
            in_case += 1
            after = words[words.index("in") + 1:] if "in" in words else []
            expecting_pattern = not (after and op == ")")  # `case X in a)` on one line has its pattern already
            continue
        if words == ["esac"]:
            in_case = max(0, in_case - 1)
            continue
        if op == ";;" and in_case:
            expecting_pattern = True
        while words and words[0] in _OPENERS:
            words = words[1:]
        if words and words[0] in ("for", "select"):
            continue  # `for x in a b c`: the list is data (substitutions were already refused)
        if words and words[0] in _CLOSERS:
            words = words[1:]
        if words:
            commands.append(words)
    return commands


def _program(first: str) -> str:
    """`/sbin/ldconfig` and `ldconfig` are the same program; any other directory is not a system one."""
    for prefix in _SYSTEM_BIN:
        if first.startswith(prefix) and "/" not in first[len(prefix):]:
            return first[len(prefix):]
    return first


_OPTIONS_WITH_TARGET_DIR = ("cp", "mv", "install", "ln")
_VALUE_SHORT = {"install": "gmoS", "cp": "S", "mv": "S", "ln": "S", "mkdir": "m", "touch": "dtr"}  # options with a value
_VALUE_LONG = {"--mode", "--owner", "--group", "--suffix", "--date", "--time", "--context"}


def _file_arguments(program: str, words: list[str]) -> list[str]:
    """Every path a file command is told to touch: its operands and the directory given with -t/--target-directory.
    The values of options like -o/-g/-m are not paths, and neither is chmod's mode or chown's owner."""
    paths: list[str] = []
    args = words[1:]
    i = 0
    options_done = False
    first_is_not_a_path = program in ("chmod", "chown", "chgrp")  # chmod MODE FILE, chown OWNER FILE
    while i < len(args):
        a = args[i]
        i += 1
        if options_done or not a.startswith("-") or a == "-":
            if first_is_not_a_path and "/" not in a:
                first_is_not_a_path = False  # the mode or owner; a real path always has a "/" and is judged
                continue
            first_is_not_a_path = False
            paths.append(a)
            continue
        if a == "--":
            options_done = True
            continue
        if a.startswith("--"):
            name, equals, value = a.partition("=")
            if name in ("--target-directory", "--reference"):
                if equals:
                    paths.append(value)
                elif i < len(args):
                    paths.append(args[i])
                    i += 1
                first_is_not_a_path = False
            elif name in _VALUE_LONG and not equals and i < len(args):
                i += 1  # its value is the next word
            continue
        cluster = a[1:]
        for j, ch in enumerate(cluster):
            takes_target = ch == "t" and program in _OPTIONS_WITH_TARGET_DIR
            if takes_target or ch in _VALUE_SHORT.get(program, ""):  # -t DIR, -tDIR, -at DIR, -o root, -m0755
                rest = cluster[j + 1:]
                value = rest or (args[i] if i < len(args) else None)
                if not rest and i < len(args):
                    i += 1
                if takes_target and value:
                    paths.append(value)
                break
    return paths


def _classify_command(words: list[str]) -> tuple[str, bool]:
    """Return (category, blocks) for one simple command; category 'unknown' when its effect is not known."""
    first = words[0]
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", first):
        return ("structure", False) if len(words) == 1 else ("unknown", True)  # VAR=x cmd … is not analysed
    program = _program(first)
    if program == "command":  # `command -v x` only asks whether x exists; `command x …` runs x
        if len(words) == 1 or words[1] in ("-v", "-V"):
            return "structure", False
        rest = words[2:] if words[1] == "-p" else words[1:]
        return _classify_command(rest) if rest else ("structure", False)
    if program == "which" or program == "type" or program == "hash":
        return "structure", False
    if program == "trap":  # trap [ACTION] SIGNAL…: the action runs later, so it is analysed like any other line
        args = [w for w in words[1:] if w not in ("-l", "-p", "--")]
        if len(args) < 2 or args[0] in ("", "-"):
            return "structure", False  # listing, resetting or ignoring signals
        try:
            inner = _commands(args[0])
        except _Unknown:
            return "unknown", True
        return _worst(_classify_command(c) for c in inner) if inner else ("structure", False)
    if program == "[[" and any(w in ("-eq", "-ne", "-lt", "-le", "-gt", "-ge") for w in words[2:]):
        return "unknown", True  # bash evaluates the operands of these as arithmetic, running code found in them
    if program in _STRUCTURE or program == ";;":
        return "structure", False
    if program == "chmod" and any(re.fullmatch(r"[0-7]*[2-7][0-7]{3}", w) or re.search(r"(?:^|,)[ugoa]*[+=][rwxXt]*s", w)
                                  for w in words[1:]):
        return "setuid", False  # setuid or setgid, octal or symbolic
    if program == "systemctl":
        verb = next((w for w in words[1:] if not w.startswith("-")), "")
        if verb in ("enable", "start", "daemon-reload", "restart", "try-restart", "reload", "is-active",
                    "is-enabled", "preset"):
            return "systemd", False
        return "systemd-other", False
    if program in _FILE_OPS:
        args = _file_arguments(program, words)
        if any("$" in a or a.startswith("~") for a in args):
            return "unknown", True  # a path that is only known when the script runs
        recursive = program == "rm" and any(w.startswith("-") and not w.startswith("--") and "r" in w.lower()
                                            or w == "--recursive" for w in words[1:])
        # Scripts run from /, so relative paths and ".." are resolved from there before judging them ("//etc" is "/etc").
        resolved = [os.path.normpath("/" + a.lstrip("/")) for a in args]
        critical = any(a in ("/", "/*") or (a + "/").startswith(tuple(p + "/" for p in _CRITICAL_PREFIXES))
                       for a in resolved)
        return "filesystem", bool(recursive or critical)
    if program in _COMMANDS:
        cat, _treatment, blocks = _COMMANDS[program]
        return cat, blocks
    return "unknown", True


def _worst(results) -> tuple[str, bool]:
    results = list(results)
    for cat, blocks in results:
        if cat == "unknown":
            return cat, blocks
    for cat, blocks in results:
        if blocks:
            return cat, blocks
    return results[0] if results else ("structure", False)


_SHELLS = {"sh", "bash", "dash", "ash"}


def _interpreter(text: str, declared: str | None) -> str | None:
    """The program that will run the script: the declared one (RPM), else the `#!` line (DEB)."""
    if declared:
        return declared.split()[0] if declared.split() else None
    m = re.match(r"#!\s*(\S+)(?:\s+(\S+))?", text)
    if not m:
        return None  # no `#!`: dpkg runs it with sh
    prog = m.group(1)
    if prog.rsplit("/", 1)[-1] == "env" and m.group(2):
        prog = m.group(2)  # `#!/usr/bin/env bash`; `env -S …` and the like end up as an unknown program
    return prog


_REMOVAL_SCRIPTS = frozenset({"prerm", "postrm", "preun", "postun"})  # run when the package is removed, not installed


def classify_scripts(scripts: dict[str, str], interpreters: dict[str, str | None] | None = None) -> ScriptAnalysis:
    analysis = ScriptAnalysis()
    for name, text in scripts.items():
        interp = _interpreter(text, (interpreters or {}).get(name))
        if interp and interp.rsplit("/", 1)[-1] not in _SHELLS:
            analysis.unknown.append(f"[{name}] runs with {interp}, which Cygnus cannot analyse")
            continue
        try:
            commands = _commands(text)
        except _Unknown as exc:
            analysis.unknown.append(f"[{name}] {exc}")
            continue
        for words in commands:
            line = " ".join(words)[:200]
            if name in _REMOVAL_SCRIPTS and _program(words[0]) in ("rm", "rmdir", "unlink"):
                # Cleanup of what the vendor's install script made: that script is never run here, and pacman removes the
                # package's own files, so dropping the cleanup loses nothing and cannot harm. It is only noted.
                samples = analysis.categories.setdefault("cleanup", [])
                if len(samples) < 5:
                    samples.append(line)
                continue
            cat, blocks = _classify_command(words)
            if cat == "unknown":
                if len(analysis.unknown) < 50:
                    analysis.unknown.append(line)
                continue
            samples = analysis.categories.setdefault(cat, [])
            if len(samples) < 5:
                samples.append(line)
            if blocks:
                analysis.blocking.append(line)
            elif cat in _REVIEW_CATEGORIES and len(analysis.review) < 50:
                analysis.review.append(line)
    return analysis


# -- payload inspection ---------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Sensitive:
    """A packaged file that runs with administrator rights, or changes who may get them, after install."""
    path: str  # relative, in the merged /usr layout (e.g. "usr/share/libalpm/hooks/x.hook")
    reason: str
    blocks: bool  # True: never converted; False: shown to the user for explicit review


@dataclass(slots=True, kw_only=True)
class PayloadAnalysis:
    files: int = 0
    total_bytes: int = 0
    top_dirs: dict[str, int] = field(default_factory=dict)
    self_contained_root: str | None = None  # e.g. "opt/discord"
    foreign_paths: list[str] = field(default_factory=list)
    elf_files: int = 0
    needed: dict[str, list[str]] = field(default_factory=dict)  # soname -> binaries needing it
    bundled_sonames: set[str] = field(default_factory=set)
    max_glibc: tuple[int, int] | None = None
    setuid_files: list[str] = field(default_factory=list)
    units: list[str] = field(default_factory=list)
    desktop_files: list[str] = field(default_factory=list)
    kernel_modules: list[str] = field(default_factory=list)
    elf_bits: int = 64
    elf_unchecked: int = 0  # ELF files beyond MAX_ELF_FILES: their needed libraries were not looked at
    sensitive: list[Sensitive] = field(default_factory=list)
    unsupported_roots: list[str] = field(default_factory=list)  # top-level folders conversion cannot carry
    programs: set[str] = field(default_factory=set)  # ELF files with a program interpreter: what actually gets run
    elf_needs: dict[str, list[str]] = field(default_factory=dict)  # ELF file -> libraries it names
    elf_names: dict[str, set[str]] = field(default_factory=dict)  # ELF file -> names others may load it by
    left_out_links: list[str] = field(default_factory=list)  # links into places conversion leaves out (cron, apt, init)

    def reachable(self) -> set[str]:
        """The ELF files that are loaded whenever a program starts: the programs, and every library of the package that
        they name, directly or through other libraries of the package. What is not reached is loaded on demand."""
        by_name: dict[str, list[str]] = {}
        for rel, names in self.elf_names.items():
            for name in names:
                by_name.setdefault(name, []).append(rel)
        seen, queue = set(self.programs), list(self.programs)
        while queue:
            for lib in self.elf_needs.get(queue.pop(), ()):
                for dep in by_name.get(lib, ()):
                    if dep not in seen:
                        seen.add(dep)
                        queue.append(dep)
        return seen

    def split_missing(self, missing: list[str]) -> tuple[list[str], dict[str, list[str]]]:
        """(libraries a program cannot start without, libraries only on-demand parts want -> those parts).
        Nothing is called optional when the package has no program of its own (a plugin or library package) or when
        not every file could be looked at."""
        if not self.programs or self.elf_unchecked:
            return list(missing), {}
        live = self.reachable()
        required: list[str] = []
        optional: dict[str, list[str]] = {}
        for lib in missing:
            users = sorted(set(self.needed.get(lib, [])))
            if any(user in live for user in users):
                required.append(lib)
            else:
                optional[lib] = users
        return required, optional


_LEFT_OUT_PREFIXES = ("etc/apt", "etc/cron.", "etc/yum.repos.d", "etc/init.d", "etc/default")
_FOREIGN_PREFIXES = ("usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu", "usr/lib64", "lib64", "etc/init.d",
                     "etc/apt", "etc/cron.", "etc/default", "etc/yum.repos.d", "usr/share/doc-base")


def make_traversable(root: Path) -> None:
    """Folders in a package may carry modes like 0644 that nobody can enter (and that would lock users out once installed);
    give every folder 0755. Links are never followed. Done as soon as a package is unpacked, so that nothing under such a
    folder escapes the analysis (a walk cannot see into a folder it cannot enter)."""
    pending = [root]
    while pending:
        d = pending.pop()
        os.chmod(d, (os.lstat(d).st_mode & 0o7777) | 0o755)
        with os.scandir(d) as it:
            pending.extend(Path(e.path) for e in it if e.is_dir(follow_symlinks=False))


# One line of `bsdtar -tvf`: type and permissions, link count, owner, group, size (or "major, minor" for a device), date, name.
_LISTED = re.compile(r"([-dlhpcbs])[-rwxsStTlL]{9}[+@.]?\s+\d+\s+(\S+)\s+(\S+)\s+(\d+)(?:,\s*\d+)?\s+[A-Za-z]{3}\s+\d{1,2}\s+"
                     r"(?:\d{4}|\d{1,2}:\d{2})\s(.*)")


def parse_listing(listing: str) -> list[tuple[str, int, str]]:
    """(type letter, size, name as listed) for every line of a `bsdtar -tvf` listing. A line that does not have exactly the
    expected shape (an owner name with a space in it shifts every column) is refused rather than guessed at, because the
    sizes it carries are what keeps a small file from unpacking into a full disk."""
    out: list[tuple[str, int, str]] = []
    for line in listing.split("\n"):  # only a newline ends a line (splitlines() also splits on form feeds and the like)
        if not line.strip():
            continue
        m = _LISTED.fullmatch(line)
        if m is None:
            raise PayloadError("the package's file list has an entry Cygnus cannot read reliably, so it is not converted")
        kind, name = m.group(1), m.group(5)
        if (kind == "l" and name.count(" -> ") > 1) or (kind == "h" and name.count(" link to ") > 1):
            # the name or the target contains the separator: where one ends and the other begins cannot be told
            raise PayloadError("the package has a link Cygnus cannot read reliably (its name contains \" -> \"), so it is "
                               "not converted")
        out.append((kind, int(m.group(4)), name))
    return out


def not_unpacked(listing: str, dest: Path) -> list[str]:
    """Entries of a `bsdtar -tvf` listing (everything except folders) that are not in `dest` after unpacking."""
    lost: list[str] = []
    for kind, _size, name in parse_listing(listing):
        if kind == "d":
            continue
        if kind == "l":
            name = name.split(" -> ", 1)[0]
        elif kind == "h":
            name = name.split(" link to ", 1)[0]
        rel = re.sub(r"^(\./)+", "", name).lstrip("/")
        if not rel or rel.split("/")[0] == ".." or "/../" in rel or not os.path.lexists(dest / rel):
            lost.append("/" + rel if rel else name)
    return lost


def extract_payload(cand: Candidate, dest: Path) -> None:
    """Extract only the data payload into `dest` with bsdtar's safe defaults (no absolute paths, no '..')."""
    bsdtar = proc.require("bsdtar", "libarchive")
    if cand.installed_size and cand.installed_size > MAX_EXTRACT_BYTES:
        raise PayloadError("payload is too large to inspect")
    if cand.format is PackageFormat.DEB:
        with open(cand.source, "rb") as fh:
            members = debmod.read_ar_members(fh, os.path.getsize(cand.source))
            data = next(m for m in members if m.name.startswith("data.tar"))
            fh.seek(data.offset)
            inner = dest.parent / ("payload-" + data.name)
            with open(inner, "wb") as out:
                remaining = data.size
                while remaining:
                    chunk = fh.read(min(remaining, 1 << 20))
                    out.write(chunk)
                    remaining -= len(chunk)
        source = inner
    else:
        source = Path(cand.source)
    # The size a package declares about itself is not trusted: list the archive first and add up
    # what extracting would really write (a small compressed file can expand enormously).
    listing = proc.run([bsdtar, "-tvf", str(source)], timeout=600, max_output=64 << 20)
    if not listing.ok or listing.truncated:
        raise PayloadError("the payload cannot be listed (damaged, or too many files)")
    entries = parse_listing(listing.stdout)
    if len(entries) > MAX_EXTRACT_ENTRIES or sum(size for _, size, _ in entries) > MAX_EXTRACT_BYTES:
        raise PayloadError("payload is too large to inspect")
    res = proc.run([bsdtar, "-x", "--no-same-owner", "--no-same-permissions", "-f", str(source), "-C", str(dest)],
                   timeout=600)
    if not res.ok:
        raise PayloadError(f"payload extraction failed: {res.stderr.strip()[:300]}")
    make_traversable(dest)
    lost = not_unpacked(listing.stdout, dest)  # an entry the unpacker skipped would otherwise be lost without a word
    if lost:
        raise PayloadError("these entries of the package could not be unpacked, so it is not converted: "
                           + ", ".join(lost[:6]) + (f" and {len(lost) - 6} more" if len(lost) > 6 else ""))


def _readelf(path: Path) -> tuple[list[str], list[str], tuple[int, int] | None, str | None, bool]:
    """(libraries it names, rpaths, newest glibc it needs, its own soname, whether it is a program). A program is a file
    with a program interpreter: that is what starts running; a shared library, even one with the execute bit, is
    only loaded by something else (a PIE program has the same ELF type as a library, so the type says nothing)."""
    res = proc.run(["readelf", "-W", "-d", "-V", "-l", str(path)], timeout=30)
    needed, rpaths, glibc, soname, program = [], [], None, None, False
    for line in res.stdout.splitlines():
        if "Requesting program interpreter" in line:
            program = True
        if "(SONAME)" in line and "[" in line:
            soname = line.split("[", 1)[1].rstrip("]").strip()
        if "(NEEDED)" in line and "[" in line:
            needed.append(line.split("[", 1)[1].rstrip("]").strip())
        elif "(RPATH)" in line or "(RUNPATH)" in line:
            rpaths.append(line.split("[", 1)[1].rstrip("]").strip())
        for m in re.finditer(r"GLIBC_(\d+)\.(\d+)", line):
            v = (int(m.group(1)), int(m.group(2)))
            glibc = max(glibc, v) if glibc else v
    return needed, rpaths, glibc, soname, program


_CANON = (("usr/sbin", "usr/bin"), ("usr/lib64", "usr/lib"), ("bin", "usr/bin"), ("sbin", "usr/bin"),
          ("lib64", "usr/lib"), ("lib", "usr/lib"))
_CONVERTIBLE_ROOTS = {"usr", "etc", "opt"}


def _canon(rel: str) -> str:
    """The path as it will be installed: Debian/Fedora's /bin, /sbin, /lib, /lib64 are merged into /usr."""
    rel = re.sub(r"^(\./)+", "", rel).lstrip("/")
    for src, dst in _CANON:
        if rel == src or rel.startswith(src + "/"):
            return dst + rel[len(src):]
    return rel


_UNIT_DIRS = r"(?:usr/lib|etc)/systemd/(?:system|user)"
# (pattern on the canonical path, why it matters, blocks conversion?)
_SENSITIVE_RULES: list[tuple[re.Pattern[str], str, bool]] = [(re.compile(p), why, b) for p, why, b in [
    (r"usr/share/libalpm/hooks/|etc/pacman\.d/hooks/", "a pacman hook: it runs commands as administrator on every "
                                                       "package change", True),
    (r"etc/ld\.so\.preload$", "loads a library into every program", True),
    (r"etc/ld\.so\.conf(\.d/.*)?$", "changes where every program finds its libraries", False),
    (r"etc/sudoers(\.d/.*)?$|etc/doas\.conf$", "grants administrator rights", True),
    (r"(?:etc|usr/share)/polkit-1/rules\.d/", "a polkit rule: it can grant administrator rights", True),
    (r"usr/share/polkit-1/actions/", "defines actions that ask for administrator approval", False),
    (r"etc/pam\.d/|etc/security/|usr/lib/security/", "changes how logins are checked (PAM)", True),
    (r"etc/(?:passwd|shadow|group|gshadow|subuid|subgid|fstab|crypttab|nsswitch\.conf|resolv\.conf|hosts|"
     r"machine-id)$", "replaces a system account or network file", True),
    (r"etc/ssh/", "changes the SSH configuration", True),
    (r"etc/(?:ca-certificates|ssl|pki)/|usr/share/ca-certificates/", "changes which certificates are trusted", True),
    (r"etc/(?:profile(?:\.d/.*)?|bash\.bashrc|bashrc|zsh/.*|csh\.login|shells|environment)$|etc/environment\.d/"
     r"|etc/(?:fish|bash_completion\.d)/|usr/share/fish/vendor_(?:conf|functions)\.d/",
     "runs in every shell (root's too) or sets the environment for everyone", True),
    (r"etc/skel/", "adds files to every new user's home folder", False),
    (r"etc/(?:crontab|anacrontab|cron\.(?:allow|deny))$", "a scheduled job that runs as administrator", True),
    (_UNIT_DIRS + r"/[^/]+\.(?:wants|requires|upholds)/", "turns a service on at boot without your approval", True),
    (r"usr/lib/systemd/(?:system|user)-(?:environment-)?generators/|usr/lib/systemd/system-(?:shutdown|sleep)/",
     "runs programs as administrator while the system starts, sleeps or stops", True),
    (r"usr/lib/(?:initcpio|dracut|kernel/install\.d)/|etc/(?:mkinitcpio|dracut|kernel)(?:\.|/|$)",
     "runs when the boot image is built", True),
    (r"(?:etc|usr/lib)/NetworkManager/dispatcher\.d/", "runs as administrator whenever the network changes", True),
    (r"etc/grub\.d/", "runs as administrator whenever the boot menu is rebuilt", True),
    # pacman's own systemd hooks (/usr/share/libalpm/hooks/*-systemd-*.hook) apply these AS ADMINISTRATOR the moment
    # the package is installed: create users and groups, create/change/remove any path, register binary handlers,
    # set kernel parameters (even a core-dump pipe to a program), load kernel modules.
    (r"usr/lib/(?:sysusers|tmpfiles|binfmt|sysctl|modules-load)\.d/|etc/(?:sysusers|tmpfiles|binfmt|sysctl|modules-load)\.d/"
     r"|etc/sysctl\.conf$", "applied as administrator by a pacman hook when the package is installed (users, files, "
     "binary handlers, kernel settings or modules)", True),
    # Hooks that load (run the start-up code of) every library in these folders, as administrator, while installing.
    (r"usr/lib/(?:gio/modules|gtk-[234]\.0/[^/]+/immodules|gdk-pixbuf-2\.0/[^/]+/loaders|vlc/plugins)/",
     "a plugin library that a pacman hook loads as administrator while installing, and that other programs then load", True),
    # systemd's own settings (not unit files, which are judged below): logind, journald, networkd, resolved…
    (r"etc/systemd/(?!system/|user/)", "changes systemd's own settings", True),
    (r"usr/lib/systemd/(?:system|user)-preset/", "turns services on by preset", False),
    (_UNIT_DIRS + r"/[^/]+\.(?:mount|automount|swap)$", "a systemd mount or swap unit", False),
    (r"usr/share/dbus-1/(?:system\.d|system-services)/|etc/dbus-1/system\.d/", "gives a program access to the "
     "system bus or starts it as administrator", False),
    (r"etc/xdg/autostart/", "starts a program when any user logs in", False),
]]
_UDEV_RULES = re.compile(r"(?:usr/lib|etc)/udev/rules\.d/[^/]+\.rules$")
# RUN+=, RUN=, RUN:=, RUN{program}+=… and PROGRAM== run a program as administrator; RUN{builtin} only a udev builtin.
_UDEV_RUNS = re.compile(r"\bRUN\s*(?:\{\s*(?!builtin\b)[^}]*\})?\s*(?:\+=|:=|=(?!=))|\bPROGRAM\s*(?:\+?=|==|:=)"
                        r"|IMPORT\s*\{\s*program\s*\}", re.I)
_LOGROTATE = re.compile(r"etc/logrotate\.(?:d/[^/]+|conf)$")
_LOGROTATE_RUNS = re.compile(r"^\s*(?:prerotate|postrotate|firstaction|lastaction|preremove)\b", re.M)
_MODPROBE = re.compile(r"(?:usr/lib|etc)/modprobe\.d/[^/]+\.conf$")
_MODPROBE_RUNS = re.compile(r"^\s*(?:install|remove)\s", re.M)
_DROPIN = re.compile(r"(?:usr/lib|etc)/systemd/(system|user)/([^/]+)\.d/[^/]+\.conf$")
_DROPIN_DIR = re.compile(r"(?:usr/lib|etc)/systemd/(system|user)/([^/]+)\.d$")  # a drop-in folder that is itself a link
_ETC_UNIT = re.compile(r"etc/systemd/(system|user)/([^/]+\.(?:service|socket|timer|path|target|mount|automount|swap|slice))$")
_UNIT_HOMES = {"system": ("/usr/lib/systemd/system", "/etc/systemd/system"),
               "user": ("/usr/lib/systemd/user", "/etc/systemd/user")}


def _system_has_unit(kind: str, name: str) -> bool:
    """Does this computer already have a unit of that name? (An /etc unit of the same name would replace it.)"""
    return any(os.path.lexists(os.path.join(home, name)) for home in _UNIT_HOMES.get(kind, ()))


def _ships_unit(shipped: set[str], kind: str, name: str) -> bool:
    return f"usr/lib/systemd/{kind}/{name}" in shipped or f"etc/systemd/{kind}/{name}" in shipped
MAX_SENSITIVE_READ = 256 * 1024


def _read_small(path: Path) -> tuple[str | None, str | None]:
    """(text, problem). Only a regular file of at most MAX_SENSITIVE_READ bytes is read, never through a link and
    never blocking on a pipe; anything else comes back as "cannot be inspected" so it is refused, not skipped."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return None, "is not a regular file, so it cannot be inspected"
        if st.st_size > MAX_SENSITIVE_READ:
            return None, "is too large to inspect"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None, "cannot be opened, so it cannot be inspected"
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(MAX_SENSITIVE_READ + 1)
    if len(data) > MAX_SENSITIVE_READ:
        return None, "is too large to inspect"
    return data.decode("utf-8", "replace"), None


def _installed_target(links: dict[str, str], canon: str, target: str) -> str:
    """Where a link really leads once the package is installed: its target is read from the link's own folder (so
    `../../tmp/x` under /etc is /tmp/x), every component is followed through the package's own links (a link to a
    link, a link in the middle of the path), and `..` cannot climb above the root. Returns a path with no leading
    slash; "../" means "gave up" (too many hops)."""
    def absolute(link: str, tgt: str) -> str:
        return os.path.normpath("/" + (tgt if tgt.startswith("/") else os.path.join(os.path.dirname(link), tgt)).lstrip("/"))

    path = absolute(canon, target)
    for _ in range(40):
        parts = [p for p in path.split("/") if p]
        for i in range(len(parts)):
            key = _canon("/".join(parts[:i + 1]))
            if key in links:  # one of the package's own links: carry on from where it leads
                rest = "/".join(parts[i + 1:])
                path = absolute(key, links[key]) + ("/" + rest if rest else "")
                path = os.path.normpath(path)
                break
        else:
            return _canon(path.lstrip("/"))
    return "../"


def _link_leaves_the_package(final: str, shipped: set[str]) -> bool:
    """A link ends up somewhere the package does not own: not under /usr or /opt, and not one of its own files.
    Such a link in a place root reads (under /etc, a unit folder…) could be pointed at a file anyone can write
    (/tmp, /var/tmp, /dev/shm, a home folder) by the time it is read."""
    if final in ("usr", "opt") or final.startswith(("usr/", "opt/")):
        return False
    return not (final in shipped or any(s.startswith(final + "/") for s in shipped))


def privileged_paths(root: Path) -> list[Sensitive]:
    """Entries in an extracted payload that run with administrator rights or change who gets them. Files, links
    and special files are all looked at (a `.wants` link turns a service on); a link is judged by its own name
    (also as a folder name) and by where it points, and is never followed."""
    entries: list[tuple[str, Path, bool]] = []  # (canonical path, real path, is a link)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in [*filenames, *(d for d in dirnames if (here / d).is_symlink())]:
            p = here / name
            entries.append((_canon(p.relative_to(root).as_posix()), p, p.is_symlink()))
    shipped = {c for c, _p, _l in entries}
    links = {c: os.readlink(p) for c, p, is_link in entries if is_link}
    found: list[Sensitive] = []
    for canon, path, is_link in entries:
        candidates: list[Sensitive] = []
        for variant in (canon, canon + "/") if is_link else (canon,):  # a link may stand for a whole folder
            candidates += [Sensitive(canon, why, blocks) for pattern, why, blocks in _SENSITIVE_RULES
                           if pattern.search(variant)]
        if drop := (_DROPIN.search(canon) or (_DROPIN_DIR.search(canon) if is_link else None)):
            kind, unit = drop.group(1), drop.group(2)  # "<unit>.d" changes the unit named <unit>
            if _ships_unit(shipped, kind, unit):
                candidates.append(Sensitive(canon, "changes the package's own service", False))
            else:  # a drop-in for a service some other package provides runs code of its own as that service
                candidates.append(Sensitive(canon, "changes a service that this package does not provide", True))
        if unit_file := _ETC_UNIT.search(canon):
            kind, name = unit_file.group(1), unit_file.group(2)
            if _system_has_unit(kind, name):
                candidates.append(Sensitive(canon, f"replaces the {name} that this computer already has", True))
            else:
                candidates.append(Sensitive(canon, "a service kept in the administrator's folder (not turned on "
                                                   "automatically)", False))
        if _UDEV_RULES.search(canon) or _MODPROBE.search(canon) or _LOGROTATE.search(canon):
            udev, modprobe = bool(_UDEV_RULES.search(canon)), bool(_MODPROBE.search(canon))
            text, problem = (None, "is a link, so it cannot be inspected") if is_link else _read_small(path)
            if problem:
                candidates.append(Sensitive(canon, f"a file that {problem}", True))
            elif udev and _UDEV_RUNS.search(text):
                candidates.append(Sensitive(canon, "a udev rule that runs a program as administrator", True))
            elif modprobe and _MODPROBE_RUNS.search(text):
                candidates.append(Sensitive(canon, "a modprobe rule that runs a command as administrator", True))
            elif not (udev or modprobe) and _LOGROTATE_RUNS.search(text):
                candidates.append(Sensitive(canon, "a log-rotation script that runs as administrator on a schedule",
                                            True))
            else:
                candidates.append(Sensitive(canon, "a udev rule (device names and permissions)" if udev else
                                            "kernel module options" if modprobe else "log-rotation settings", False))
        if is_link and (candidates or canon.startswith("etc/")):
            target = links[canon]
            final = _installed_target(links, canon, target)
            if _link_leaves_the_package(final, shipped):
                candidates.append(Sensitive(canon, f"a link that ends up in /{final[:80]}, a place the package does not "
                                                   "own, in a folder that administrator programs read", True))
            else:
                candidates = [Sensitive(c.path, f"{c.reason} (a link to {target[:80]})", c.blocks) for c in candidates]
        if candidates:  # the strictest finding for this entry wins, whatever order the rules are in
            found.append(next((c for c in candidates if c.blocks), candidates[0]))
    return sorted(set(found), key=lambda s: (not s.blocks, s.path))


def only_empty_folders(path: Path) -> bool:
    """True when `path` is a real folder holding nothing but folders (no file, no link, however deep)."""
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return False
    except OSError:
        return False
    for _dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        if filenames or any(os.path.islink(os.path.join(_dirpath, d)) for d in dirnames):
            return False
    return True


def unsupported_roots(root: Path) -> list[str]:
    """Top-level folders (after the /bin, /lib… merge) that a converted package may not install into. A /var that holds only
    empty folders (a log folder the program would create itself) does not count: it is left out when converting."""
    tops = {_canon(f"{n}/x").split("/")[0] for n in os.listdir(root)}
    if "var" in tops and only_empty_folders(root / "var"):
        tops.discard("var")
    return sorted(tops - _CONVERTIBLE_ROOTS)


def _resolved_file(root: Path, rel: str) -> str | None:
    """The package file a path ends at, following every link inside the package (also links to folders): an absolute target
    means that path inside the package, because it will be installed there. Nothing outside the package is ever looked at.
    None when the chain leaves the package, is too long or ends at nothing."""
    resolved: list[str] = []
    queue = [p for p in rel.split("/") if p]
    hops = 0
    while queue:
        part = queue.pop(0)
        if part == ".":
            continue
        if part == "..":
            if not resolved:
                return None
            resolved.pop()
            continue
        path = root.joinpath(*resolved, part)
        if os.path.islink(path):
            hops += 1
            if hops > 40:
                return None
            try:
                target = os.readlink(path)
            except OSError:
                return None
            if target.startswith("/"):
                resolved = []
            queue = [p for p in target.split("/") if p] + queue
            continue
        resolved.append(part)
    final = root.joinpath(*resolved) if resolved else None
    return "/".join(resolved) if final is not None and final.is_file() else None


def analyse_payload(root: Path) -> PayloadAnalysis:
    pa = PayloadAnalysis()
    elf_paths: list[Path] = []
    links: list[tuple[str, str]] = []  # (symlink path, where it points) for links that may name a library
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            try:
                st = os.lstat(p)
            except OSError:
                continue
            pa.files += 1
            top = rel.split("/")[0] if "/" not in rel.split("/", 1)[-1] else "/".join(rel.split("/")[:2])
            pa.top_dirs[top] = pa.top_dirs.get(top, 0) + 1
            if rel.endswith(".ko") or rel.endswith(".ko.zst") or rel.endswith(".ko.xz") or name == "dkms.conf" \
                    or rel.startswith(("lib/modules/", "usr/lib/modules/")):
                pa.kernel_modules.append(rel)
            if stat.S_ISLNK(st.st_mode):
                if rel.startswith(_LEFT_OUT_PREFIXES):
                    pa.left_out_links.append(rel)
                if ".so" in name:
                    try:
                        links.append((rel, os.readlink(p)))
                    except OSError:
                        pass
                continue
            pa.total_bytes += st.st_size
            if st.st_mode & stat.S_ISUID:
                pa.setuid_files.append(rel)
            if rel.startswith(_FOREIGN_PREFIXES):
                pa.foreign_paths.append(rel)
            if re.search(r"(^|/)(lib/systemd/system|usr/lib/systemd/system)/[^/]+\.(service|socket|timer)$", rel):
                pa.units.append(rel)
            if rel.endswith(".desktop") and "/applications/" in rel:
                pa.desktop_files.append(rel)
            if st.st_size > 64:
                with open(p, "rb") as fh:
                    head = fh.read(5)
                    if head[:4] == b"\x7fELF":
                        if ".so" in name:
                            pa.bundled_sonames.add(name)  # only a real library counts, not a text file or a stray name
                        if len(elf_paths) < MAX_ELF_FILES:
                            elf_paths.append(p)
                        else:
                            pa.elf_unchecked += 1
                        if head[4] == 1:
                            pa.elf_bits = 32
    pa.elf_files = len(elf_paths)
    pa.sensitive = privileged_paths(root)
    pa.unsupported_roots = unsupported_roots(root)
    for p in elf_paths:
        needed, _rpaths, glibc, soname, program = _readelf(p)
        if soname:
            pa.bundled_sonames.add(soname)
        rel = p.relative_to(root).as_posix()
        pa.elf_needs[rel] = needed
        pa.elf_names[rel] = {p.name} | ({soname} if soname else set())
        if program:
            pa.programs.add(rel)
        for n in needed:
            pa.needed.setdefault(n, []).append(rel)
        if glibc:
            pa.max_glibc = max(pa.max_glibc, glibc) if pa.max_glibc else glibc
    for link, _target in links:  # libfoo.so.1 -> libfoo.so.1.2.3: a program names the link, the file is what is loaded
        real = _resolved_file(root, link)
        if real is not None and (real in pa.elf_names or pa.elf_unchecked):
            pa.bundled_sonames.add(os.path.basename(link))  # a link that leads nowhere in the package is not a library
            if real in pa.elf_names:
                pa.elf_names[real].add(os.path.basename(link))
    # Self-contained: (almost) everything lives in one application directory.
    roots: dict[str, int] = {}
    for d, n in pa.top_dirs.items():
        parts = d.split("/")
        if parts[0] == "opt" and len(parts) >= 2:
            roots["/".join(parts[:2])] = roots.get("/".join(parts[:2]), 0) + n
    for dirpath, _dirs, files in os.walk(root):
        rel = Path(dirpath).relative_to(root).as_posix()
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "usr" and parts[1] in ("share", "lib") and files:
            key = "/".join(parts)
            roots[key] = roots.get(key, 0) + sum(len(f) for _, _, f in os.walk(dirpath))
    if roots:
        best = max(roots, key=lambda k: roots[k])
        if roots[best] >= 0.8 * pa.files:
            pa.self_contained_root = best
    return pa


def host_glibc() -> tuple[int, int] | None:
    try:
        v = os.confstr("CS_GNU_LIBC_VERSION")  # "glibc 2.43"
    except (ValueError, OSError):
        return None
    m = re.search(r"(\d+)\.(\d+)", v or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def host_sonames() -> set[str]:
    res = proc.run(["ldconfig", "-p"], timeout=30)
    return {line.split()[0] for line in res.stdout.splitlines()[1:] if line.strip()}


def soname_to_provides(soname: str, bits: int = 64) -> str | None:
    """libfoo.so.1 -> 'libfoo.so=1-64'; libLLVM.so.23.1 -> 'libLLVM.so=23.1-64' (Arch soname provides)."""
    m = re.match(r"^(.+\.so)\.([0-9][0-9.]*)$", soname)
    return f"{m.group(1)}={m.group(2)}-{bits}" if m else None


def archive_special_modes(cand: Candidate) -> list[str]:
    """Setuid/setgid entries, read from the archive headers (extraction as a user strips these bits)."""
    bsdtar = proc.require("bsdtar", "libarchive")
    tmpdir = None
    try:
        if cand.format is PackageFormat.DEB:
            tmpdir = Path(tempfile.mkdtemp(prefix="cygnus-deb-"))
            with open(cand.source, "rb") as fh:
                members = debmod.read_ar_members(fh, os.path.getsize(cand.source))
                data = next(m for m in members if m.name.startswith("data.tar"))
                fh.seek(data.offset)
                inner = tmpdir / ("payload-" + data.name)
                with open(inner, "wb") as out:
                    remaining = data.size
                    while remaining:
                        chunk = fh.read(min(remaining, 1 << 20))
                        out.write(chunk)
                        remaining -= len(chunk)
            source = inner
        else:
            source = Path(cand.source)
        res = proc.run([bsdtar, "-tvf", str(source)], timeout=300, max_output=64 << 20)
        if not res.ok or res.truncated:  # a partial listing would hide setuid files
            raise PayloadError("the file modes of the package cannot be listed")
        special = []
        for line in res.stdout.splitlines():
            mode = line[:10]
            if len(mode) == 10 and (mode[3] in "sS" or mode[6] in "sS"):
                special.append(line.split()[-1] if " -> " not in line else line.split(" -> ")[0].split()[-1])
        return special
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


@dataclass(slots=True, kw_only=True)
class ForeignVerdict:
    strategy: str  # native-alternative | convert | portable | refuse | review
    summary: str
    scripts: ScriptAnalysis
    payload: PayloadAnalysis | None
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    unresolved_sonames: list[str] = field(default_factory=list)
    repo_dependencies: list[str] = field(default_factory=list)
    # packages only on-demand parts of the program want: [{"package", "libraries", "files"}], never installed unasked
    optional_dependencies: list[dict[str, Any]] = field(default_factory=list)
    # install scripts Cygnus could not read, and nothing recognised as dangerous: the person may go ahead knowing the
    # scripts are never run (what they set up will be missing). What they mention, and their text, are for that decision.
    additions: list[dict[str, str]] = field(default_factory=list)  # what Cygnus adds to the package itself (icon, command link)
    scripts_acknowledgeable: bool = False
    script_mentions: list[dict[str, Any]] = field(default_factory=list)
    script_effects: list[dict[str, Any]] = field(default_factory=list)  # what the scripts would create or change, grouped
    script_sources: dict[str, str] = field(default_factory=dict)
    replaces_converted: dict[str, Any] | None = None  # {"name", "version"}: a copy Cygnus converted earlier, to be replaced
    issues: list[Issue] = field(default_factory=list)


# Commands worth telling the reader about when a script cannot be read in full: what each would have done.
_MENTIONED = {name: why for name, (cat, why, _b) in _COMMANDS.items()
              if cat in {"users-groups", "systemd", "sysv-service", "alternatives", "capabilities", "apparmor",
                         "kernel-module", "debconf"}}
_MENTIONED.update({"apt-config": "adds or removes a software repository (Debian's package manager)",
                   "apt-key": "trusts a software repository's signing key",
                   "eval": "runs text that is built while the script runs", "curl": "downloads files",
                   "wget": "downloads files", "gpg": "handles signing keys"})
# What can leave the converted program not working if it is dropped: shown first, and flagged
_IMPORTANT = {name for name, (cat, _why, _b) in _COMMANDS.items()
              if cat in {"users-groups", "kernel-module", "debconf", "systemd"}}
MAX_SHOWN_SCRIPT = 60_000


def mentioned_commands(sources: dict[str, str]) -> list[dict[str, Any]]:
    """The notable commands that appear in the scripts, with what they would have done and in which script. A plain
    word search of the text (comments left out), so it can miss a command that is put together while the script runs."""
    found: dict[str, set[str]] = {}
    for script, text in sources.items():
        code = "\n".join(line for line in str(text).splitlines() if not line.lstrip().startswith("#"))
        for name in _MENTIONED:
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", code):
                found.setdefault(name, set()).add(script)
    return sorted(({"command": n, "what": _MENTIONED[n], "scripts": sorted(s), "important": n in _IMPORTANT}
                   for n, s in found.items()), key=lambda m: (not m["important"], m["command"]))


def _resolve(sonames: list[str], bits: int, satisfy, locate) -> tuple[dict[str, str], list[str], str | None]:
    """(soname -> the repository package that provides it, the sonames nothing provides, why a lookup failed).
    First by the "provides" packages declare for their libraries; what that does not find is looked up by file name
    (many packages, Qt 5 above all, declare no provides although they ship the library)."""
    if not sonames or satisfy is None:
        return {}, list(sonames), None
    provides = {n: soname_to_provides(n, bits) for n in sonames}
    sat = satisfy([p for p in provides.values() if p])
    found = {n: sat[provides[n]]["repo"]["name"] for n in sonames
             if provides[n] and (sat.get(provides[n]) or {}).get("repo")}
    left = [n for n in sonames if n not in found]
    problem = None
    if left and locate is not None:
        try:
            located = locate(left)
        except CygnusError as exc:
            located, problem = {}, str(exc)
        if located:
            known = satisfy(sorted(set(located.values())))  # only a package the repositories really offer
            for n in left:
                if located.get(n) and (known.get(located[n]) or {}).get("repo"):
                    found[n] = located[n]
    return found, [n for n in sonames if n not in found], problem


def install_script_texts(cand: Candidate) -> dict[str, str]:
    """The text of the package's maintainer scripts, by name (deb: postinst…, rpm: postin…)."""
    if cand.format is PackageFormat.DEB:
        return dict(cand.metadata.get("maintainer_scripts") or {})
    return {k: v["body"] for k, v in (cand.metadata.get("scriptlets") or {}).items()}


def analyse(cand: Candidate, *, satisfy: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
            locate: Callable[[list[str]], dict[str, str]] | None = None,
            owner: Callable[[str], str | None] | None = None,
            find_alternatives: Callable[[Candidate], list[dict[str, Any]]] | None = None,
            inspect_payload: bool = True, sonames_on_host: set[str] | None = None,
            glibc_on_host: tuple[int, int] | None = None, accepted_arch: tuple[str, ...] = ("x86_64", "any"),
            ) -> ForeignVerdict:
    if cand.format not in (PackageFormat.DEB, PackageFormat.RPM):
        raise ValueError("not a .deb or .rpm candidate")
    if cand.format is PackageFormat.DEB:
        scripts_src, interpreters = cand.metadata.get("maintainer_scripts") or {}, {}
    else:
        rpm_scripts = cand.metadata.get("scriptlets") or {}
        scripts_src = {k: v["body"] for k, v in rpm_scripts.items()}
        interpreters = {k: v.get("interpreter") or "/bin/sh" for k, v in rpm_scripts.items()}
    scripts = classify_scripts(scripts_src, interpreters)
    for name in cand.metadata.get("uninspected_scripts") or []:
        scripts.unknown.append(f"[{name}] is too large to inspect")  # never converted unread
    for line in cand.metadata.get("sysusers") or []:  # users/groups the package declares: conversion would drop them
        scripts.blocking.append(f"creates a system user or group: {str(line)[:100]}")
    issues: list[Issue] = []
    alternatives = find_alternatives(cand) if find_alternatives else []

    if cand.arch not in accepted_arch:
        issues.append(Issue(code="ARCH_INCOMPATIBLE", severity=IssueSeverity.BLOCKER,
                            title=f"Built for {cand.arch}", explanation="This computer cannot run it.",
                            resolutions=[explain_only("none", "Get a matching build", "Ask the vendor for an x86_64 build.")]))

    payload = None
    additions: list[dict[str, str]] = []
    unresolved: list[str] = []
    repo_deps: list[str] = []
    optional_deps: list[dict[str, Any]] = []
    if inspect_payload:
        tmp = Path(tempfile.mkdtemp(prefix="cygnus-foreign-"))
        try:
            root = tmp / "root"
            root.mkdir()
            extract_payload(cand, root)
            payload = analyse_payload(root)
            additions = translate.describe(translate.plan(root, install_script_texts(cand), owner))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        try:
            payload.setuid_files = archive_special_modes(cand)
        except Exception:  # noqa: BLE001 - listing failure is reported as a review item below
            payload.setuid_files = ["(could not list file modes)"]
        host = sonames_on_host if sonames_on_host is not None else host_sonames()
        missing = sorted(n for n in payload.needed if n not in payload.bundled_sonames and n not in host)
        required, optional = payload.split_missing(missing)
        found, unresolved, lookup_problem = _resolve(required, payload.elf_bits, satisfy, locate)
        repo_deps.extend(found.values())
        ofound, ounresolved, oproblem = _resolve(sorted(optional), payload.elf_bits, satisfy, locate)
        for pkg in sorted(set(ofound.values()) - set(repo_deps)):  # a package already needed is not "optional"
            libs = sorted(n for n, v in ofound.items() if v == pkg)
            optional_deps.append({"package": pkg, "libraries": libs,
                                  "files": sorted({f for n in libs for f in optional[n]})})
        if ounresolved:
            parts = sorted({f for n in ounresolved for f in optional[n]})
            issues.append(Issue(
                code="LIB_OPTIONAL_MISSING", severity=IssueSeverity.NOTICE,
                title="Some optional parts of the program will not work",
                explanation=(", ".join(parts[:4]) + (" …" if len(parts) > 4 else "") + " would need "
                             + ", ".join(ounresolved[:6]) + (" …" if len(ounresolved) > 6 else "")
                             + ", which no repository provides. The program itself does not name them, so Cygnus "
                               "expects it to start and only the feature that uses these parts to be missing; if it "
                               "misbehaves, this is the first place to look."
                             + (f" (Looking for a provider failed: {oproblem})" if oproblem else "")),
                facts={"sonames": ounresolved, "files": parts}))
        glibc = glibc_on_host if glibc_on_host is not None else host_glibc()
        if payload.max_glibc and glibc and payload.max_glibc > glibc:
            issues.append(Issue(code="GLIBC_TOO_OLD", severity=IssueSeverity.BLOCKER,
                                title="This program needs a newer C library than this system has",
                                explanation=f"It requires glibc {'.'.join(map(str, payload.max_glibc))}; "
                                            f"this system has {'.'.join(map(str, glibc))}.",
                                resolutions=[explain_only("none", "Cannot run here",
                                                          "Wait for a system update or use another format.")]))
        if unresolved:
            issues.append(Issue(code="LIB_SONAME_MISSING", severity=IssueSeverity.BLOCKER,
                                title="Libraries this program needs are not available",
                                explanation=", ".join(unresolved[:12]) + (" …" if len(unresolved) > 12 else "")
                                + (f" (Looking for a provider failed: {lookup_problem})" if lookup_problem else ""),
                                facts={"sonames": unresolved},
                                resolutions=[explain_only("none", "Cannot be satisfied from your repositories",
                                                          "These are Debian/Fedora-specific library versions.")]))

    if payload is not None:
        if payload.kernel_modules:
            issues.append(Issue(code="FOREIGN_KERNEL_MODULES", severity=IssueSeverity.BLOCKER,
                                title="The package contains kernel modules",
                                explanation="Kernel modules built for another distribution's kernel cannot be "
                                            "installed safely: " + ", ".join(payload.kernel_modules[:5]),
                                resolutions=[explain_only("none", "Not installable", "Look for a DKMS or Arch "
                                                                                      "package of this driver.")]))
        multiarch = [p for p in payload.foreign_paths if p.startswith(("usr/lib/x86_64-linux-gnu",
                                                                        "lib/x86_64-linux-gnu"))]
        if multiarch and not payload.self_contained_root:
            issues.append(Issue(code="FOREIGN_MULTIARCH_PATHS", severity=IssueSeverity.BLOCKER,
                                title="The package installs into Debian-specific library folders",
                                explanation=", ".join(multiarch[:5]) + " — these paths do not exist on Arch-based "
                                                                       "systems.",
                                resolutions=[explain_only("none", "Not installable as-is",
                                                          "Use a native or Flatpak version instead.")]))
        dropped = [p for p in [*payload.foreign_paths, *payload.left_out_links] if p.startswith(_LEFT_OUT_PREFIXES)]
        if dropped:
            issues.append(Issue(code="FOREIGN_FILES_DROPPED", severity=IssueSeverity.NOTICE,
                                title="Some files would be left out",
                                explanation="Vendor repository, cron and SysV init files are not installed: "
                                            + ", ".join(dropped[:6])
                                            + (". The program will therefore not update itself through the vendor's "
                                               "repository: update it by converting the newer file."
                                               if any(d.startswith(("etc/apt", "etc/cron.")) for d in dropped) else "")))
        if payload.elf_unchecked:
            issues.append(Issue(code="FOREIGN_TOO_MANY_BINARIES", severity=IssueSeverity.BLOCKER,
                                title="The package has too many programs to check their libraries",
                                explanation=f"{payload.elf_unchecked} programs beyond the first {MAX_ELF_FILES} were "
                                            "not checked, so it cannot be shown that this system can run them.",
                                resolutions=[explain_only("none", "Not converted",
                                                          "Use a Flatpak or an AppImage of it instead.")]))
        blocking = [s for s in payload.sensitive if s.blocks]
        if blocking:
            issues.append(Issue(code="FOREIGN_ROOT_HOOKS", severity=IssueSeverity.BLOCKER,
                                title="The package ships files that run with administrator rights",
                                explanation="; ".join(f"/{s.path} ({s.reason})" for s in blocking[:6])
                                            + (" …" if len(blocking) > 6 else ""),
                                facts={"paths": [s.path for s in blocking]},
                                resolutions=[explain_only("none", "Not converted",
                                                          "A converted package carries no install steps, so files "
                                                          "that run as administrator are refused. Use the vendor's "
                                                          "own installer, a Flatpak or an AppImage instead.")]))
        scripts.review.extend(f"/{s.path}: {s.reason}" for s in payload.sensitive if not s.blocks)
        if payload.unsupported_roots:
            issues.append(Issue(code="FOREIGN_UNSUPPORTED_ROOT", severity=IssueSeverity.BLOCKER,
                                title="The package installs outside /usr, /etc and /opt",
                                explanation="It installs into: " + ", ".join(f"/{r}" for r in payload.unsupported_roots[:6]),
                                resolutions=[explain_only("none", "Not converted",
                                                          "Cygnus only converts packages that stay inside /usr, /etc "
                                                          "and /opt.")]))
        if payload.setuid_files:
            scripts.review.extend(f"setuid file: {f} (Cygnus does not keep the setuid bit)" for f in payload.setuid_files[:10])
    if scripts.review:
        issues.append(Issue(code="FOREIGN_SCRIPTS_REVIEW", severity=IssueSeverity.NOTICE,
                            title="Some install steps need your review",
                            explanation="; ".join(scripts.review[:8])
                                        + (f"; and {len(scripts.review) - 8} more" if len(scripts.review) > 8 else ""),
                            facts={"review": scripts.review}))
    blocked = any(i.severity is IssueSeverity.BLOCKER for i in issues)
    script_mentions: list[dict[str, Any]] = []
    script_effects: list[dict[str, Any]] = []
    script_sources: dict[str, str] = {}
    installed = [a for a in alternatives if a.get("kind") == "installed"]
    vendor_alt = [a for a in alternatives if a.get("vendor_supported")]
    if installed:
        best = installed[0]
        issues.insert(0, Issue(
            code="ALREADY_INSTALLED_NATIVE", severity=IssueSeverity.NOTICE,
            title=f"Already installed as an Arch package: {best['name']} {best.get('version', '')}".rstrip(),
            explanation="This system already has an Arch package of it, which pacman keeps up to date. "
                        "Installing this file as well would conflict with it.",
            facts={"alternatives": alternatives}, resolutions=[]))
        strategy, summary = "native-alternative", f"Not needed: {best['label']} as an Arch package."
    elif alternatives:
        best = (vendor_alt or alternatives)[0]
        issues.insert(0, Issue(
            code="NATIVE_ALTERNATIVE", severity=IssueSeverity.NOTICE,
            title=f"A native version is available: {best['label']}",
            explanation="Packages made for this system integrate better and update properly.",
            facts={"alternatives": alternatives},
            resolutions=[Resolution(id=f"use-{a['kind']}-{a['name']}", title=f"Use {a['label']} instead",
                                    explanation=a.get("note", ""), safety=SafetyClass.APPROVAL,
                                    recommended=(a is best), rank=10 + i,
                                    actions=[Action(kind="source.switch", params=a)])
                         for i, a in enumerate(alternatives[:5])]))
        strategy, summary = "native-alternative", f"Use {best['label']} instead of this {cand.format.value} file."
    elif blocked:
        strategy, summary = "refuse", "This package cannot be installed safely on CachyOS."
    elif scripts.blocks_auto_conversion:
        strategy = "review"
        summary = ("The package's install scripts do things Cygnus cannot translate automatically."
                   + (" You can convert it anyway after reading what they contain: Cygnus never runs them."
                      if scripts.acknowledgeable else " Manual review is needed."))
        issues.append(Issue(code="FOREIGN_SCRIPTS_UNRECOGNISED", severity=IssueSeverity.BLOCKER,
                            title="Install scripts need review",
                            explanation="Not translatable: " + "; ".join((scripts.unknown + scripts.blocking)[:6]),
                            facts={"unknown": scripts.unknown},
                            resolutions=[explain_only("review", "Not converted automatically",
                                                      "Cygnus will not run or guess the effect of these commands.")]))
        if scripts.acknowledgeable:
            script_mentions = mentioned_commands(scripts_src)
            script_effects = translate.describe_effects(translate.script_effects(scripts_src))
            script_sources = {k: str(v)[:MAX_SHOWN_SCRIPT] for k, v in scripts_src.items()}
    elif payload and payload.self_contained_root and not payload.setuid_files and not payload.units \
            and not scripts.review:
        strategy, summary = "portable", (f"Self-contained application in /{payload.self_contained_root}: it can be "
                                         "extracted to your chosen storage location.")
    else:
        strategy, summary = "convert", "Can be converted into a local pacman package (pacman will own every file)."
    return ForeignVerdict(strategy=strategy, summary=summary, scripts=scripts, payload=payload,
                          alternatives=alternatives, unresolved_sonames=unresolved, repo_dependencies=sorted(set(repo_deps)),
                          optional_dependencies=optional_deps, additions=additions, scripts_acknowledgeable=(
                              strategy == "review" and scripts.acknowledgeable),
                          script_mentions=script_mentions, script_effects=script_effects, script_sources=script_sources,
                          issues=issues)
