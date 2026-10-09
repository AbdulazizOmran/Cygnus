"""The effects of install scripts that can be reproduced safely without running them.

A converted package never runs its vendor's scripts. Some of what they do is declarative and only involves the package's own
files, and doing it ourselves makes the program work as its vendor intended:
  * `xdg-icon-resource install … DIR/FILE NAME`  -> the menu icon, in the sizes the package ships;
  * `update-alternatives --install LINK NAME PATH` and `ln -s TARGET LINK` -> a command in /usr/bin that points into the package.

The commands are found by a plain text search of the scripts (so it also works in scripts Cygnus cannot read as a whole), and
every effect is checked against the package's own files and a short list of rules before anything is added: a link only under
/usr/bin and only to a file of the package, never over something that exists or that another package owns, never one of
Debian's generic alternatives (x-www-browser…); an icon only for a name a shipped menu entry asks for, only square standard
sizes read from the image's own header. Nothing is ever executed, and every addition is shown to the person."""

from __future__ import annotations

import os
import re
import shutil
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

# Debian's generic alternative names: a vendor registering with these would make its program the system-wide default
# for something nobody asked it to be, and Arch has no such mechanism.
GENERIC_ALTERNATIVES = frozenset({
    "x-www-browser", "gnome-www-browser", "www-browser", "x-terminal-emulator", "x-session-manager", "x-window-manager",
    "editor", "pager", "vi", "view", "ex", "vim", "awk", "nawk", "cc", "c++", "c89", "c99", "java", "javac", "python",
    "python3", "sh", "bash", "ld", "nc", "netcat", "which", "rsh", "ssh-askpass", "gnome-text-editor", "emacs",
    "desktop-theme", "x-cursor-theme", "gdm3-theme.gresource", "default.plymouth", "rcp"})
INSTALL_SCRIPTS = frozenset({"postinst", "postin", "posttrans"})  # run after the files are there
ICON_SIZES = (16, 22, 24, 32, 36, 48, 64, 72, 96, 128, 192, 256, 512)
MAX_ICONS = 12
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,100}")
_CONTROL = {";", "&&", "||", "|", "&", "(", ")", "{", "}", "then", "do", "else", "fi", "done", "if", "elif", "while",
            "until", "for", "case", "esac", "in"}


@dataclass(frozen=True)
class Effect:
    """What a script line asks for, as literally written (no variables evaluated)."""
    kind: str  # "icon" | "alternative" | "link"
    args: tuple[str, ...]
    script: str


@dataclass(frozen=True)
class Addition:
    kind: str  # "icon" | "link"
    path: str  # where, relative to the package root
    source: str  # what it comes from (a file of the package, or the link's target)
    text: str  # for the person


class _Scanned(NamedTuple):
    code: str  # the line without a trailing comment
    skeleton: str  # `code` with the inside of every quotation and of ((...)) replaced by NUL: what is really shell syntax
    open: bool  # the line ends inside a quotation (the shell goes on reading the next line as part of it)
    dangling: bool  # the line ends in a backslash that continues it


_WORD_START = " \t;|&()"


def _scan(line: str) -> _Scanned:
    """Read one line the way the shell does where it matters here: quotations ('...', "...", $'...' with its escapes), a
    backslash-escaped character, and a `#` that starts a comment only at the start of a word (so `$#`, `${x#p}` and `${#x}`
    are not comments)."""
    code: list[str] = []
    skeleton: list[str] = []
    state = ""  # "" outside a quotation, or the quote that is open: ' " $'
    i, n = 0, len(line)
    dangling = False
    while i < n:
        c = line[i]
        if c == "\\" and state != "'":
            if i + 1 >= n:
                dangling = True
                code.append(c)
                skeleton.append("\0")
                break
            code += [c, line[i + 1]]
            skeleton += ["\0", "\0"]
            i += 2
            continue
        if state == "":
            if c == "#" and (i == 0 or line[i - 1] in _WORD_START):
                break  # a comment
            code.append(c)
            skeleton.append(c)
            if c == "'" or c == '"':
                state = c
            elif c == "$" and line[i + 1:i + 2] == "'":
                code.append("'")
                skeleton.append("'")
                i += 1
                state = "$'"
        else:
            code.append(c)
            if (state == '"' and c == '"') or (state in ("'", "$'") and c == "'"):
                skeleton.append(c)
                state = ""
            else:
                skeleton.append("\0")
        i += 1
    flat = re.sub(r"\(\(.*?\)\)", lambda m: "\0" * len(m.group()), "".join(skeleton))  # arithmetic: `1 << n` is a shift
    return _Scanned("".join(code), flat, state != "", dangling)


# `<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`; not `<<<word` (a here-string) and not a shift inside (( )) or a quotation
_HEREDOC = re.compile(r"(?<![<$])<<(?!<)(-?)\s*(?:'([A-Za-z_][A-Za-z0-9_]*)'|\"([A-Za-z_][A-Za-z0-9_]*)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")


def _heredoc_delimiter(scan: _Scanned) -> tuple[str, bool] | None:
    for m in _HEREDOC.finditer(scan.code):
        if scan.skeleton[m.start():m.start() + 2] == "<<":
            return next(g for g in m.groups()[1:] if g), m.group(1) == "-"
    return None


def _logical_lines(text: str) -> list[str]:
    """Lines as the shell reads them: continuations and quotations that run over several lines joined, comments cut off and
    here-document bodies skipped."""
    out: list[str] = []
    pending = ""
    skip_until: tuple[str, bool] | None = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        if skip_until is not None:
            delimiter, tabs_ok = skip_until
            if (raw.lstrip("\t") if tabs_ok else raw) == delimiter:  # `<<-` ignores leading tabs, `<<` ignores nothing
                skip_until = None
            continue
        if not pending and raw.strip().startswith("#"):
            continue  # a comment ends at its line, even when that ends in a backslash
        line = pending + raw
        pending = ""
        scan = _scan(line)
        if scan.dangling:
            pending = line[:-1] + " "
            continue
        if scan.open:
            pending = line + "\n"
            continue
        if not scan.code.strip():
            continue
        out.append(scan.code)
        skip_until = _heredoc_delimiter(scan)
    return out


@dataclass(frozen=True)
class Statement:
    """One simple command of a script. `conditional`: it sits inside an if, a case or a function body, or follows && / ||, so
    whether it runs at all depends on something Cygnus cannot know. (A `for` loop is taken to run; a `while` or `until` loop
    may not run even once, so what is inside one is conditional.)"""
    words: tuple[str, ...]
    conditional: bool


_OPERATOR = frozenset("();<>|&")


def _words(code: str) -> list[tuple[str, bool]] | None:
    """The words of one logical line as the shell splits them: (text, quoted). `quoted` is True when any part of the word was
    quoted or escaped, so a quoted "fi" is told apart from the keyword `fi`. A run of operator characters (`&&`, `;;`, `2>&1`'s
    `>&`, `()`) is one word. None when a quotation is left open."""
    words: list[tuple[str, bool]] = []
    cur: list[str] = []
    quoted = have = False

    def end() -> None:
        nonlocal quoted, have
        if have:
            words.append(("".join(cur), quoted))
        cur.clear()
        quoted = have = False

    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c in " \t\n":
            end()
            i += 1
        elif c in _OPERATOR:
            end()
            j = i
            while j < n and code[j] in _OPERATOR:
                j += 1
            words.append((code[i:j], False))
            i = j
        elif c == "\\":
            have = True
            if i + 1 < n:
                cur.append(code[i + 1])
                quoted = True
            i += 2
        elif c == "'" or (c == "$" and code[i + 1:i + 2] == "'"):
            have = quoted = True
            ansi = c == "$"
            j = i + (2 if ansi else 1)
            buf: list[str] = []
            while j < n and code[j] != "'":
                if ansi and code[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(code[j])
                j += 1
            if j >= n:
                return None
            cur.append("".join(buf))
            i = j + 1
        elif c == '"':
            have = quoted = True
            j = i + 1
            buf = []
            while j < n and code[j] != '"':
                if code[j] == "\\" and j + 1 < n and code[j + 1] in '"\\$`':
                    j += 1
                buf.append(code[j])
                j += 1
            if j >= n:
                return None
            cur.append("".join(buf))
            i = j + 1
        else:
            have = True
            cur.append(c)
            i += 1
    end()
    return words


def _loop_runs_surely(words: list[tuple[str, bool]], start: int) -> bool:
    """A `for` loop whose list is written out (`for size in 16 24 32`) runs; one whose list is worked out while the script runs
    (`$(ls ...)`, a glob, a variable) may be empty and then does not."""
    for w, _quoted in words[start:]:
        if w in (";", "do"):
            break
        if re.search(r"[$`*?\[]", w):
            return False
    return True


def statements(text: str) -> list[Statement]:
    """The simple commands of a script and whether each one is unconditional. A tolerant reading that also works on scripts the
    strict classifier gives up on (it never runs anything): control words are followed (only unquoted ones: `echo "fi"` is not
    the end of an if), conditions and loop headers are not commands, and redirections stay inside the command they belong to."""
    out: list[Statement] = []
    stack: list[str] = []  # "cond" (if / case / a brace group), "fn" (a function body), "loop" (for), "test" (while / until)
    ended = [False]  # the script may stop here (an `exit`): what follows is not sure to be reached
    after_logic = [False]  # the previous line ended in && or ||: this line's first command depends on it
    for line in _logical_lines(text):
        words = _words(line)
        if words is None:  # a quotation that never closes: nothing is learnt from such a line
            continue
        current: list[str] = []
        header = [""]

        def flush() -> None:
            if not current:
                return
            conditional = after_logic[0] or "cond" in stack or "fn" in stack or "test" in stack or ended[0]
            out.append(Statement(tuple(current), conditional))
            if current[0] in ("exit", "return") and "fn" not in stack:
                status = current[1] if len(current) > 1 else ""
                # An exit that ends the script quietly (status 0, or not known) means what follows may never run; one with an
                # error status aborts the install, so what follows either runs or nothing matters (Chrome's own guard).
                if not conditional or not re.fullmatch(r"[1-9][0-9]*", status):
                    ended[0] = True
            current.clear()
            after_logic[0] = False

        for index, (w, quoted) in enumerate(words):
            if quoted:  # a quoted word is an argument, never a keyword or an operator
                if not header[0]:
                    current.append(w)
                continue
            if header[0] == "case" and w == "in":
                header[0] = ""
                continue
            if header[0] and w not in (";", "then", "do"):
                continue  # the condition of an if/while and the list of a for are not commands
            if w in ("if", "elif"):
                flush()
                if w == "if":
                    stack.append("cond")
                header[0] = "cond"
            elif w in ("for", "while", "until"):
                flush()
                stack.append("loop" if w == "for" and _loop_runs_surely(words, index + 1) else "test")
                header[0] = "for" if w == "for" else "cond"
            elif w == "case":
                flush()
                stack.append("cond")
                header[0] = "case"
            elif w in ("then", "else", "do"):
                flush()
                header[0] = ""
            elif w in ("fi", "esac", "done", "}"):
                flush()
                kinds = ("loop", "test") if w == "done" else ("cond", "fn")
                for position in range(len(stack) - 1, -1, -1):
                    if stack[position] in kinds:
                        del stack[position]
                        break
            elif w == "{":
                is_function = bool(current) and (current[-1] == "()" or current[0] == "function")
                flush()
                stack.append("fn" if is_function else "cond")
            elif w in (";", ";;", "(", ")", "|", "!"):
                flush()
                header[0] = "" if w == ";" else header[0]
            elif w == "&" and current and current[-1] in (">", ">>", "<"):
                current.append(w)  # `>&2`: a redirection, not "run in the background"
            elif w == "&":
                flush()
            elif w in ("&&", "||"):
                flush()
                after_logic[0] = True
            else:
                current.append(w)
        flush()
    return out


_REDIRECT = re.compile(r"[<>&|]*[<>][<>&|]*")  # >  >>  >&  &>  >|  <  <<  <<<  <&


def _without_redirections(words: list[str]) -> list[str]:
    """The words of a command without its redirections (`2>/dev/null`, `>> log`, `<<EOF`, `>&2`): the file a redirection names
    is not an argument of the program."""
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if _REDIRECT.fullmatch(w):
            if out and re.fullmatch(r"\d", out[-1]):
                out.pop()  # the descriptor number in front: 2>/dev/null
            i += 2  # the operator and the file, word or descriptor it names
            continue
        out.append(w)
        i += 1
    return out


def _positionals(words: list[str], with_value: set[str]) -> list[str]:
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif w in with_value:
            skip = True
        elif not w.startswith("-") and w not in (">", ">>", "<", "&"):
            out.append(w)
    return out


_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_QUIET_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero"}


@dataclass(frozen=True)
class ScriptEffect:
    """Something an install script does to the system: `kind`, the `target` it is done to, whether that target is written out
    literally (not decided by a variable while the script runs), whether it is conditional, and in which script."""
    kind: str
    target: str
    literal: bool
    conditional: bool
    script: str
    detail: str = ""


def _program_of(words: list[str]) -> tuple[str, list[str]]:
    """(the program, its arguments) with leading VAR=value assignments skipped and a tool called through a variable that was
    looked up earlier (`"$XDG_ICON_RESOURCE" install …`) recognised."""
    rest = list(words)
    while rest and _ASSIGNMENT.fullmatch(rest[0]):
        rest.pop(0)
    if not rest:
        return "", []
    program = os.path.basename(rest[0])
    if re.fullmatch(r"\$\{?[A-Za-z_]*ICON_RESOURCE[A-Za-z_]*\}?", rest[0]):
        program = "xdg-icon-resource"
    return program, rest[1:]


def _is_literal(target: str) -> bool:
    return bool(target) and not re.search(r"[$`~*?\[]", target)


_SERVICE_VERBS = {"enable", "start", "restart", "try-restart", "reload", "disable", "stop", "mask", "unmask", "preset"}
_USER_PROGRAMS = {"useradd", "adduser", "groupadd", "addgroup", "usermod", "userdel", "groupdel", "gpasswd", "chsh", "passwd"}
_DOWNLOAD_PROGRAMS = {"curl", "wget", "apt-get", "apt", "dpkg", "yum", "dnf", "rpm", "add-apt-repository", "apt-key", "apt-config"}


def _effects_of(words: list[str], script: str, conditional: bool) -> list[ScriptEffect]:
    raw, words = words, _without_redirections(words)
    program, args = _program_of(words)
    if not program:
        return []
    found: list[ScriptEffect] = []

    def add(kind: str, target: str, detail: str = "") -> None:
        found.append(ScriptEffect(kind, target, _is_literal(target), conditional, script, detail))

    # redirections: `> FILE` and `>> FILE` write to a file whatever the program is
    for i, w in enumerate(raw):
        if w in (">", ">>", ">|", "&>", "&>>") and i + 1 < len(raw) and not raw[i + 1].startswith("&") and raw[i + 1] not in _QUIET_TARGETS \
                and raw[i + 1] not in (">", ">>", "<", "&"):
            add("write", raw[i + 1])
    if program in ("tee",):
        for target in _positionals(args, set()):
            if target not in _QUIET_TARGETS:
                add("write", target)
    elif program == "mkdir":
        for target in _positionals(args, {"-m", "--mode"}):
            add("mkdir", target)
    elif program == "install":
        flags = [a for a in args if a.startswith("-")]
        places = _positionals(args, {"-m", "-o", "-g", "-t", "-T", "--mode", "--owner", "--group"})
        if any(f.startswith("-") and not f.startswith("--") and "d" in f[1:] for f in flags) or "--directory" in flags:
            for target in places:
                add("mkdir", target)
        elif len(places) >= 2:
            add("write", places[-1])
    elif program in ("cp", "mv"):
        places = _positionals(args, {"-t", "-T", "--target-directory"})
        if len(places) >= 2:
            add("write", places[-1])
    elif program == "touch":
        for target in _positionals(args, {"-d", "-t", "-r", "--date", "--reference"}):
            add("write", target)
    elif program == "sed" and any(a == "-i" or (a.startswith("-i") and not a.startswith("--")) or a.startswith("--in-place")
                                  for a in args):
        places = _positionals(args, {"-e", "-f", "--expression", "--file"})
        files = places if any(a in ("-e", "-f") for a in args) else places[1:]
        for target in files:
            add("write", target)
    elif program == "ln":
        flags = [a for a in args if a.startswith("-")]
        places = _positionals(args, set())
        if (any("s" in f for f in flags if not f.startswith("--")) or "--symbolic" in flags) and len(places) == 2:
            add("link", places[1], places[0])
    elif program == "rm":
        for target in _positionals(args, set()):
            add("delete", target)
    elif program == "chmod":
        places = _positionals(args, set())
        if len(places) >= 2:
            for target in places[1:]:
                add("chmod", target, places[0])
    elif program in ("chown", "chgrp", "setcap"):
        places = _positionals(args, set())
        for target in places[1:]:
            add("owner", target, places[0])
    elif program in _USER_PROGRAMS:
        places = _positionals(args, {"-g", "-G", "-u", "-d", "-s", "-c", "-m", "--gid", "--uid", "--home", "--shell", "--groups"})
        if places:
            add("user", places[-1], program)
    elif program == "systemctl":
        verb = next((a for a in args if not a.startswith("-")), "")
        if verb in _SERVICE_VERBS:
            for target in [a for a in _positionals(args, set()) if a != verb] or [""]:
                add("service", target or "(the unit named by the script)", verb)
    elif program in ("deb-systemd-helper", "deb-systemd-invoke"):
        places = _positionals(args, set())
        for target in places[1:] or [""]:
            add("service", target or "(a unit)", places[0] if places else "")
    elif program in ("invoke-rc.d", "update-rc.d", "service"):
        places = _positionals(args, set())
        if places:
            add("service", places[0], program)
    elif program in ("modprobe", "insmod", "depmod", "dkms"):
        add("kernel", (_positionals(args, set()) or [program])[0], program)
    elif program == "update-alternatives" and "--install" in args:
        i = args.index("--install")
        if len(args) >= i + 4:
            found.append(ScriptEffect("alternative", args[i + 1], _is_literal(args[i + 1]), conditional, script,
                                      f"{args[i + 2]} {args[i + 3]}"))
    elif program == "xdg-icon-resource" and args and args[0] == "install":
        places = _positionals(args[1:], {"--size", "--context", "--theme", "--mode"})
        if len(places) >= 2:
            size = next((args[1:][i + 1] for i, w in enumerate(args[1:]) if w == "--size" and i + 1 < len(args[1:])), "")
            found.append(ScriptEffect("icon", places[-1], _is_literal(places[-1]), conditional, script, f"{places[-2]}|{size}"))
    elif program in _DOWNLOAD_PROGRAMS:
        urls = [a for a in args if re.match(r"https?://", a)]
        add("repo" if program in ("apt-config", "apt-key", "add-apt-repository") else "download", urls[0] if urls else program, program)
    return found


# the scripts that run when the package is installed (not when it is removed)
EFFECT_SCRIPTS = frozenset({"preinst", "postinst", "prein", "postin", "pretrans", "posttrans"})


def script_effects(scripts: dict[str, str]) -> list[ScriptEffect]:
    """Everything the install-time scripts do to the system, by a tolerant reading of their text. Nothing is run."""
    found: list[ScriptEffect] = []
    for name, text in scripts.items():
        if name not in EFFECT_SCRIPTS:
            continue
        for statement in statements(str(text)):
            found += _effects_of(list(statement.words), name, statement.conditional)
    return found


_GROUPS = (("write", "Files they write or change"), ("mkdir", "Folders they create"), ("chmod", "Permissions they set"),
           ("owner", "Owners they change"), ("link", "Links they make"), ("alternative", "Commands they register"),
           ("icon", "Icons they install"), ("user", "Users and groups they create or change"),
           ("service", "Services they start or enable"), ("repo", "Software repositories and keys they set up"),
           ("download", "Programs they download or install"), ("kernel", "Kernel modules they load"),
           ("delete", "Files they delete"))


def describe_effects(found: list[ScriptEffect], limit: int = 12) -> list[dict]:
    """The effects as the person reads them, grouped: [{"title", "items": [...]}], each item with what was found and, where it
    applies, that the path is decided while the script runs or that the step only happens under a condition."""
    out = []
    for kind, title in _GROUPS:
        seen: dict[str, str] = {}
        for e in found:
            if e.kind != kind:
                continue
            text = e.target
            if kind in ("chmod", "owner") and e.detail:
                text = f"{e.target} ({e.detail})"
            elif kind == "link" and e.detail:
                text = f"{e.target} -> {e.detail}"
            elif kind in ("user", "service", "download", "repo") and e.detail and e.detail != e.target:
                text = f"{e.target} ({e.detail})"
            notes = [n for n, on in (("the path is only known while the script runs", not e.literal),
                                     ("only if a condition holds", e.conditional)) if on]
            seen.setdefault(f"{text} [{e.script}]" + (f" ({'; '.join(notes)})" if notes else ""), kind)
        items = list(seen)
        if items:
            shown = items[:limit] + ([f"and {len(items) - limit} more"] if len(items) > limit else [])
            out.append({"title": title, "items": shown})
    return out


def effects(scripts: dict[str, str]) -> list[Effect]:
    """The effects Cygnus can reproduce safely: those that run unconditionally, written out literally, in the install-time
    scripts (`scripts`: script name -> text). Icons, command links, empty folders and plain execute permissions."""
    found: list[Effect] = []
    for e in script_effects(scripts):
        if e.script not in INSTALL_SCRIPTS or e.conditional:
            continue
        if e.kind == "icon":
            source, _, size = e.detail.partition("|")
            found.append(Effect("icon", (source, e.target, size), e.script))
        elif e.kind == "alternative":
            name, _, path = e.detail.partition(" ")
            found.append(Effect("alternative", (e.target, name, path), e.script))
        elif e.kind == "link":
            found.append(Effect("link", (e.detail, e.target), e.script))
        elif e.kind == "mkdir":
            found.append(Effect("mkdir", (e.target,), e.script))
        elif e.kind == "chmod":
            found.append(Effect("chmod", (e.detail, e.target), e.script))
    return found


def _within(root: Path, rel: str, create: bool = False) -> Path | None:
    """`root`/`rel` if it stays inside the package and no folder on the way is a link, else None. With `create`, folders
    that do not exist yet are fine (they are made when something is added), but one that exists must be a real folder."""
    rel = os.path.normpath(rel.lstrip("/"))
    if rel.startswith("..") or rel == ".":
        return None
    current = root
    for part in Path(rel).parts[:-1]:
        current = current / part
        if current.is_symlink():
            return None
        if not current.is_dir():
            if create and not current.exists():
                continue
            return None
    return root / rel


def _png_size(path: Path) -> int | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return w if w == h and w in ICON_SIZES else None


def _canon(path: str) -> str:
    """The path as it will be installed (/bin, /sbin and /usr/sbin are all /usr/bin on this system)."""
    path = os.path.normpath("/" + path.lstrip("/"))
    for src in ("/usr/sbin", "/sbin", "/bin"):
        if path == src or path.startswith(src + "/"):
            return "/usr/bin" + path[len(src):]
    return path


_LEGACY = (("usr/bin", ("bin", "sbin", "usr/sbin")), ("usr/lib", ("lib", "lib64", "usr/lib64")))


def _aliases(rel: str) -> list[str]:
    """`rel` and where an old-style package would still have it before conversion merges /bin, /sbin and /lib into /usr: the
    analysis looks at the package as it comes, the conversion at it merged, and both must plan the same."""
    rel = rel.lstrip("/")
    out = [rel]
    for new, olds in _LEGACY:
        if rel == new or rel.startswith(new + "/"):
            out += [old + rel[len(new):] for old in olds]
    return out


def _present(root: Path, rel: str) -> bool:
    for candidate in _aliases(rel):
        path = _within(root, candidate)
        if path is not None and (path.exists() or path.is_symlink()):
            return True
    return False


def _link_additions(root: Path, found: list[Effect], owner) -> list[Addition]:
    out: list[Addition] = []
    for e in found:
        if e.kind == "alternative":
            link, name, target = e.args
            if name in GENERIC_ALTERNATIVES or os.path.basename(link) in GENERIC_ALTERNATIVES:
                continue
        elif e.kind == "link":
            target, link = e.args
        else:
            continue
        if any("$" in a or "`" in a for a in (link, target)):
            continue  # only what is written out, never what a variable would be at run time
        link, target = _canon(link), _canon(target)
        base = os.path.basename(link)
        if os.path.dirname(link) != "/usr/bin" or not _NAME.fullmatch(base) or not target.startswith(("/usr/", "/opt/")):
            continue
        if link == target or any(out_.path == f"usr/bin/{base}" for out_ in out):  # itself, or already planned
            continue
        sources = [p for p in (_within(root, a) for a in _aliases(target)) if p is not None and (p.is_file() or p.is_symlink())]
        if not sources or _within(root, link, create=True) is None:
            continue  # a target that is not one of the package's own files
        if _present(root, link) or owner(link):
            continue  # never over a file that exists or that another package owns
        out.append(Addition("link", f"usr/bin/{base}", target, f"the command {base} (starts {target})"))
    return out


def _icon_additions(root: Path, found: list[Effect], owner) -> list[Addition]:
    wanted: dict[str, str] = {}  # icon name -> the folder (or file) it is installed from
    for e in found:
        if e.kind == "icon":
            source, name = e.args[0], e.args[1]
            if "$" not in name and _NAME.fullmatch(name):
                wanted.setdefault(name, source)
    apps = _within(root, "usr/share/applications/x")
    asked: set[str] = set()
    if apps is not None and apps.parent.is_dir():
        for desktop in sorted(apps.parent.glob("*.desktop")):
            try:
                text = desktop.read_text(errors="replace")[:100_000]
            except OSError:
                continue
            asked |= {m.group(1).strip() for m in re.finditer(r"^Icon=([^\n/]+)$", text, re.M)}
    out: list[Addition] = []
    for name, source in wanted.items():
        if name not in asked:
            continue  # nothing in the package asks for this icon
        have = list((root / "usr/share/icons").glob(f"*/*/apps/{name}.*")) if (root / "usr/share/icons").is_dir() else []
        have += list((root / "usr/share/pixmaps").glob(f"{name}.*")) if (root / "usr/share/pixmaps").is_dir() else []
        if have:
            continue
        folder = os.path.dirname(source.split("$", 1)[0]) if "$" in source else None
        if folder is None:
            candidates = [source] if source.lower().endswith(".png") else []
        else:
            where = _within(root, folder + "/x")
            candidates = sorted(str(p.relative_to(root)) for p in where.parent.iterdir()
                                if where is not None and p.suffix.lower() == ".png" and p.is_file() and not p.is_symlink()) \
                if where is not None and where.parent.is_dir() else []
        sizes: dict[int, str] = {}
        for rel in candidates:
            path = _within(root, rel)
            size = _png_size(path) if path is not None and path.is_file() and not path.is_symlink() else None
            if size and size not in sizes:
                sizes[size] = rel
        for size in sorted(sizes)[:MAX_ICONS]:
            dest_rel = f"usr/share/icons/hicolor/{size}x{size}/apps/{name}.png"
            dest = _within(root, dest_rel, create=True)  # the folders may not exist yet: they are made when it is added
            if dest is None or dest.exists() or owner(f"/{dest_rel}"):
                continue
            out.append(Addition("icon", dest_rel, sizes[size], f"the menu icon {name} ({size}x{size})"))
    return out


_SAFE_PATH = re.compile(r"/(?:[A-Za-z0-9._+@-]+/?)+")


def _dir_additions(root: Path, found: list[Effect]) -> list[Addition]:
    """Empty folders the script makes (`mkdir -p /opt/app/logs`): a program often expects them to exist."""
    out: list[Addition] = []
    for e in found:
        if e.kind != "mkdir":
            continue
        path = _canon(e.args[0])
        if not _SAFE_PATH.fullmatch(path) or path.split("/")[1] not in ("opt", "usr", "etc") or ".." in path.split("/"):
            continue
        rel = path.strip("/")
        dest = _within(root, rel, create=True)
        if dest is None or dest.exists() or dest.is_symlink() or any(a.path == rel for a in out):
            continue
        out.append(Addition("dir", rel, "", f"the empty folder /{rel}"))
    return out


def _new_mode(current: int, spec: str, is_dir: bool = False) -> int | None:
    """The permission bits `chmod SPEC` would give, or None when SPEC is not a plain one (setuid, setgid, sticky, several
    clauses and the like are never reproduced)."""
    spec = spec.strip()
    if re.fullmatch(r"0?[0-7]{3}", spec):
        return int(spec, 8)
    m = re.fullmatch(r"([ugoa]*)([+\-=])([rwxX]+)", spec)
    if not m:
        return None
    who, op, perms = m.groups()
    bits = (4 if "r" in perms else 0) | (2 if "w" in perms else 0) | \
           (1 if "x" in perms or ("X" in perms and (is_dir or current & 0o111)) else 0)
    classes = [6, 3, 0] if not who or "a" in who else [s for ch, s in (("u", 6), ("g", 3), ("o", 0)) if ch in who]
    result = current & 0o777
    for shift in classes:
        if op == "+":
            result |= bits << shift
        elif op == "-":
            result &= ~(bits << shift)
        else:
            result = (result & ~(7 << shift)) | (bits << shift)
    return result


def _mode_additions(root: Path, found: list[Effect]) -> list[Addition]:
    """Permissions the script sets on files of the package (`chmod +x /opt/app/bin/tool`), so that what the vendor made
    executable is executable here. Only plain permissions, only on a regular file the package ships."""
    out: list[Addition] = []
    for e in found:
        if e.kind != "chmod":
            continue
        spec, target = e.args
        path = _canon(target)
        if not _SAFE_PATH.fullmatch(path) or ".." in path.split("/"):
            continue
        for candidate in _aliases(path):
            file = _within(root, candidate)
            if file is None or file.is_symlink() or not file.is_file():
                continue
            new = _new_mode(os.lstat(file).st_mode, spec)
            if new is not None and new & 0o022:
                break  # a vendor's `chmod 777` is not copied: nothing a package ships is writable by anyone but its owner
            if new is not None and new != os.lstat(file).st_mode & 0o777 and not any(a.path == candidate for a in out):
                out.append(Addition("mode", candidate, format(new, "o"),
                                    f"the permission {format(new, 'o')} on /{candidate}, as the vendor's script sets it"))
            break
    return out


def ignoring(owner: Callable[[str], str | None], package: str) -> Callable[[str], str | None]:
    """`owner`, except that the package being converted (an older copy of it may be installed) does not count: the files it
    owns are the ones the new copy replaces, and skipping them would make pacman remove them on update."""
    def other_owner(path: str) -> str | None:
        found = owner(path)
        return found if found and found != package else None
    return other_owner


def plan(root: Path, scripts: dict[str, str], owner: Callable[[str], str | None] | None = None) -> list[Addition]:
    """What can safely be added for this package tree (`root`), from the install-time scripts. Nothing is changed."""
    owner = owner or (lambda path: None)
    found = effects(scripts)
    if not found:
        return []
    return (_link_additions(root, found, owner) + _icon_additions(root, found, owner) + _dir_additions(root, found)
            + _mode_additions(root, found))


def describe(additions: list[Addition]) -> list[dict[str, str]]:
    """The additions as the person reads them: every size of one icon on a single line."""
    out: list[dict[str, str]] = []
    icons: dict[str, list[int]] = {}
    for a in additions:
        m = re.fullmatch(r"usr/share/icons/hicolor/(\d+)x\d+/apps/(.+)\.png", a.path) if a.kind == "icon" else None
        if m:
            icons.setdefault(m.group(2), []).append(int(m.group(1)))
        else:
            out.append({"kind": a.kind, "path": a.path, "text": a.text})
    for name, sizes in icons.items():
        sizes.sort()
        shown = ", ".join(str(s) for s in sizes)
        out.append({"kind": "icon", "path": f"usr/share/icons/hicolor/{sizes[0]}x{sizes[0]}/apps/{name}.png",
                    "text": f"the menu icon {name} ({len(sizes)} size{'s' if len(sizes) > 1 else ''}: {shown})"
                    if len(sizes) > 1 else f"the menu icon {name} ({sizes[0]}x{sizes[0]})"})
    return out


def apply(root: Path, additions: list[Addition]) -> list[str]:
    """Make the planned additions in the package tree; returns the notes for the person."""
    done: list[Addition] = []
    for a in additions:
        dest = _within(root, a.path, create=True)  # checked again: nothing is written through a link
        if dest is None:
            continue
        if a.kind == "mode":  # the file is already there: only its permission changes
            if dest.is_symlink() or not dest.is_file():
                continue
            mode = int(a.source, 8)
            if mode & 0o7022:  # checked again here: never setuid/setgid/sticky, never writable by group or others
                continue
            os.chmod(dest, mode)
            done.append(a)
            continue
        if dest.exists() or dest.is_symlink():
            continue
        if a.kind == "dir":
            dest.mkdir(parents=True, exist_ok=True)
            os.chmod(dest, 0o755)
            done.append(a)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if a.kind == "link":
            os.symlink(a.source, dest)
        else:
            source = _within(root, a.source)
            if source is None or not source.is_file():
                continue
            shutil.copyfile(source, dest)
            os.chmod(dest, 0o644)
        done.append(a)
    return ["added " + d["text"] for d in describe(done)]
