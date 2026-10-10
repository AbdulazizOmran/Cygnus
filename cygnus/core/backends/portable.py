"""Checks for a program that comes in a plain archive (see detect/archive.py and ops/archive_ops.py): will the drive hold it, what
do the install scripts it carries say, and does it have the libraries it needs. Nothing here runs anything from the archive."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from cygnus.core.backends import foreign, translate

# Drives that are not made for Linux file names: Windows rules apply (what the NTFS driver, FAT and exFAT refuse or fold).
WINDOWS_RULES = ("ntfs", "ntfs3", "fuseblk", "vfat", "exfat", "msdos", "fat", "fat32")
_WINDOWS_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')  # a backslash too: Windows-style names cannot hold one
_WINDOWS_DEVICES = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)")
MAX_NAME_BYTES = 255


def _fold(name: str) -> str:
    """How a Windows-style drive tells names apart: letter by letter, in upper case where that is a single letter (Python's casefold
    would make "ß" and "SS" the same, which the NTFS table does not)."""
    return "".join(c.upper() if len(c.upper()) == 1 else c for c in name)


def name_problems(names: list[str], *, fs_type: str = "", caps: dict[str, Any] | None = None) -> list[str]:
    """Why the files of the archive could not all be kept by a drive, in words (at most five). Linux file systems hold any name
    up to 255 bytes; a Windows one refuses some characters, trailing dots and spaces and device names, and does not tell
    capital from small letters, so two files that differ only that way would become one."""
    caps = caps or {}
    problems: list[str] = []
    windows = fs_type.lower() in WINDOWS_RULES or caps.get("special_chars") is False
    folds = windows or caps.get("case_sensitive") is False
    long_name = next((n for n in names if any(len(part.encode()) > MAX_NAME_BYTES for part in n.split("/"))), None)
    if long_name:
        problems.append(f"a file name is longer than 255 bytes ({long_name[:50]}…)")
    if windows:
        for n in names:
            for part in n.split("/"):
                if _WINDOWS_BAD_CHARS.search(part):
                    problems.append(f"{n[:60]} has a character this kind of drive does not allow in a name")
                    break
                if part != part.rstrip(" .") or _WINDOWS_DEVICES.match(part):
                    problems.append(f"{n[:60]} has a name this kind of drive does not allow (it ends in a dot or a space, or is a device name)")
                    break
            if len(problems) >= 3:
                break
    if folds:
        seen: dict[str, str] = {}
        for n in names:
            key = _fold(n)
            if key in seen and seen[key] != n:
                problems.append(f"{seen[key][:45]} and {n[:45]} differ only in capital letters, which this drive treats as the same name")
                break
            seen.setdefault(key, n)
    return problems[:5]


# What a command in an archive's install script would have done, in plain words. These are descriptions of text, not of anything
# Cygnus does (unless the person ticks a script, nothing in the archive is run). The "important" ones are what a program may count on having been set up.
_MENTIONS: dict[str, tuple[str, bool]] = {
    "systemctl": ("starts, stops, enables or reloads system services", True), "service": ("starts or stops system services", True),
    "useradd": ("creates a user", True), "adduser": ("creates a user", True), "groupadd": ("creates a group", True),
    "usermod": ("changes a user's groups", True), "gpasswd": ("changes who is in a group", True),
    "modprobe": ("loads a kernel module", True), "insmod": ("loads a kernel module", True), "depmod": ("updates the kernel module lists", True),
    "setcap": ("gives a program special capabilities", True), "update-alternatives": ("changes which program answers to a common name", True),
    "apparmor_parser": ("loads an AppArmor security profile", True), "aa-enforce": ("changes an AppArmor profile", True),
    "udevadm": ("reloads device rules", True), "ldconfig": ("registers libraries with the system", True),
    "sudo": ("asks for administrator rights", True), "pkexec": ("asks for administrator rights", True), "su": ("switches to another user", True),
    "apt": ("installs software with Debian's package manager", True), "apt-get": ("installs software with Debian's package manager", True),
    "dpkg": ("installs software with Debian's package manager", True), "dnf": ("installs software with Fedora's package manager", True),
    "yum": ("installs software with a Red Hat package manager", True), "rpm": ("installs software with the RPM package manager", True),
    "zypper": ("installs software with SUSE's package manager", True), "pacman": ("installs software with Arch's package manager", True),
    "pkill": ("stops running programs by name", False), "killall": ("stops running programs by name", False),
    "rm": ("deletes files or folders", False), "chmod": ("changes who may use files", False), "chown": ("changes who owns files", False),
    "curl": ("downloads files", False), "wget": ("downloads files", False),
    "update-desktop-database": ("refreshes the list of programs and what opens what (Cygnus does this itself)", False),
    "gtk-update-icon-cache": ("refreshes the icon lists (Cygnus does this itself)", False),
    "xdg-desktop-menu": ("adds a menu entry (Cygnus makes the menu entry itself)", False),
    "xdg-icon-resource": ("installs an icon (Cygnus does this itself)", False), "update-mime-database": ("refreshes the file type lists", False),
}


_WRAPPERS = {"sudo", "pkexec", "doas", "exec", "command", "nohup", "env", "time", "nice", "ionice", "xargs", "builtin"}
_KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "(", "time"}
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")  # not a here-string (<<<)
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "ash", "fish"}


def _program_names(words: list[str]) -> set[str]:
    """The program of one command (and, behind `sudo` and the like, the program it runs): `rm`, `/usr/bin/rm` and `sudo rm` are all
    rm; a word that is only an argument, or text inside quotes, is not a program."""
    found: set[str] = set()
    words = list(words)
    while words:
        word = words.pop(0)
        if word in _KEYWORDS or _ASSIGNMENT.fullmatch(word) or word.startswith("-"):
            continue
        name = PurePosixPath(word).name
        found.add(name)
        if name not in _WRAPPERS and name != "su":
            break
    return found


def _programs(text: str) -> set[str]:
    """The programs the script's commands run. Read with the same shell reader as for a conversion; a script it cannot follow is
    read line by line instead (the first word of each part of each line)."""
    found: set[str] = set()
    try:
        for words in foreign._commands(text):
            found |= _program_names(words)
        return found
    except Exception:  # noqa: BLE001 - the reader refuses what it cannot be sure of: fall back to the lines
        pass
    heredoc: str | None = None  # the word that ends a here-document whose text is data (unless a shell is reading it)
    for line in text.splitlines():
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        code = re.sub(r"\s#.*$", "", line.strip())
        if not code or code.startswith("#"):
            continue
        here: set[str] = set()
        for part in re.split(r"&&|\|\||[;|&(`]", code):
            here |= _program_names(part.split())
        found |= here
        opens = _HEREDOC.search(code)
        if opens and not here & _SHELLS:
            heredoc = opens.group(2)
    return found


def mentions(scripts: dict[str, str]) -> list[dict[str, Any]]:
    """The notable commands in the scripts and what each would have done. Only a command counts (its program, as the shell would read
    it): a word in a message or a comment does not. A command a script puts together while it runs can still be missed. The listing is
    for the person to read; nothing is run."""
    found: dict[str, set[str]] = {}
    for script, text in scripts.items():
        for name in _programs(str(text)) & _MENTIONS.keys():
            found.setdefault(name, set()).add(script)
    return sorted(({"command": n, "what": _MENTIONS[n][0], "scripts": sorted(s), "important": _MENTIONS[n][1]} for n, s in found.items()),
                  key=lambda m: (not m["important"], m["command"]))


def scripts_report(scripts: dict[str, str], *, cap: int = foreign.MAX_SHOWN_SCRIPT) -> dict[str, Any] | None:
    """What the install scripts in the archive mention and would have done, read from their text (nothing is run). `cap` is how much of
    each text is shown: for a script that may be run it is the whole script, so what is read is all of what would run."""
    if not scripts:
        return None
    texts = {name: text[:cap] for name, text in scripts.items()}
    try:
        effects = translate.describe_effects(translate.script_effects(texts))
    except Exception:  # noqa: BLE001 - a script the reader cannot follow is still shown whole, and listed by what it mentions
        effects = []
    return {"unreadable": [], "mentions": mentions(texts), "effects": effects, "texts": texts}


def library_check(root: Path, *, satisfy: Callable[[list[str]], dict[str, dict[str, Any]]] | None,
                  locate: Callable[[list[str]], dict[str, str]] | None, sonames_on_host: set[str] | None = None,
                  glibc_on_host: tuple[int, int] | None = None) -> dict[str, Any]:
    """The libraries the unpacked program needs that this computer lacks, and where they can be had.

    {"packages": the repository packages that provide what is missing, "libraries": library -> package, "unresolved": libraries
    nothing provides, "optional": [{"package", "libraries", "files"}] for parts of the program only some features use,
    "glibc": {"needs", "have"} when the program was built for a newer C library than this computer has, plus counts}.
    Libraries the program carries itself are never missing."""
    payload = foreign.analyse_payload(root)
    host = sonames_on_host if sonames_on_host is not None else foreign.host_sonames()
    missing = sorted(n for n in payload.needed if n not in payload.bundled_sonames and n not in host)
    required, optional = payload.split_missing(missing)
    found, unresolved, problem = foreign._resolve(required, payload.elf_bits, satisfy, locate)
    ofound, ounresolved, oproblem = foreign._resolve(sorted(optional), payload.elf_bits, satisfy, locate)
    packages = sorted(set(found.values()))
    optional_out = []
    for pkg in sorted(set(ofound.values()) - set(packages)):
        libs = sorted(n for n, v in ofound.items() if v == pkg)
        optional_out.append({"package": pkg, "libraries": libs, "files": sorted({PurePosixPath(f).name for n in libs for f in optional[n]})[:6]})
    have = glibc_on_host if glibc_on_host is not None else foreign.host_glibc()
    glibc = None
    if payload.max_glibc and have and payload.max_glibc > have:
        glibc = {"needs": "%d.%d" % payload.max_glibc, "have": "%d.%d" % have}
    return {"packages": packages, "libraries": dict(sorted(found.items())), "unresolved": sorted(unresolved + ounresolved),
            "optional": optional_out, "glibc": glibc, "problem": problem or oproblem, "programs": len(payload.programs),
            "checked": payload.elf_files, "unchecked": payload.elf_unchecked, "bits": payload.elf_bits}
