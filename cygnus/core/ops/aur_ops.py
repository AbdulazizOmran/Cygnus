"""AUR: fetch → review → build as the user → install through the helper (architecture §10.2).

A PKGBUILD is a shell script written by a community member. Cygnus never runs it before you have
seen it: dependencies come from the static .SRCINFO file (`makepkg --printsrcinfo` would already
execute the PKGBUILD), the review shows every file and what changed since the version you last
approved, and only that exact commit is built, as you, never as root.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from cygnus.core import paths
from cygnus.core.errors import CygnusError
from cygnus.core.util import proc
from cygnus.core.util.fs import atomic_write

AUR_GIT = "https://aur.archlinux.org"
PKGBASE = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]{0,99}\Z")
MAX_REVIEW_FILE = 256 * 1024
MAX_REVIEW_FILES = 200
MAX_DIFF = 1024 * 1024
# Characters that can hide text in a terminal or editor: control characters (escape sequences,
# carriage returns), format characters (zero-width spaces, bidi overrides, soft hyphens, BOM, tags),
# unassigned and private-use code points, separators other than the plain space, and characters that
# are drawn as nothing at all (variation selectors, Hangul fillers, the blank braille pattern).
_CANDIDATES = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]|[^\x00-\x7f]")
_BLANK = frozenset("\u034f\u115f\u1160\u17b4\u17b5\u180b\u180c\u180d\u180e\u180f\u2800\u3164\uffa0"
                   + "".join(map(chr, range(0xFE00, 0xFE10))) + "".join(map(chr, range(0xE0100, 0xE01F0))))
_ARRAY_START = re.compile(r"(?m)^[ \t]*(?:sha\d+sums|md5sums|b2sums)(?:_[a-z0-9_]+)?=\(")


def _hides(ch: str) -> bool:
    if ch in _BLANK:
        return True
    cat = unicodedata.category(ch)
    return cat[0] == "C" or cat in ("Zl", "Zp", "Zs")  # the ASCII space never gets here


def hides_text(text: str) -> bool:
    return any(_hides(ch) for ch in set(_CANDIDATES.findall(text)))


def visible(text: str) -> str:
    """Text safe to show in a terminal or a label: anything that could hide or rewrite what is on
    screen (escape sequences, carriage returns, bidi overrides, zero-width characters) is shown as
    \\xNN or \\u{NNNN} instead."""
    def show(m: re.Match[str]) -> str:
        ch = m.group()
        if not _hides(ch):
            return ch
        return f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{{{ord(ch):04x}}}"
    return _CANDIDATES.sub(show, text)


def _skipped_checksums(text: str) -> list[int]:
    """Lines where a checksum array contains SKIP (bounded scan: no regex backtracking)."""
    lines = []
    for m in _ARRAY_START.finditer(text):
        end = text.find(")", m.end(), m.end() + 4096)
        if "SKIP" in text[m.end(): end if end != -1 else m.end() + 4096]:
            lines.append(text.count("\n", 0, m.start()) + 1)
    return lines


_INSTALL_REF = re.compile(r"(?m)^[ \t]*install[ \t]*=[ \t]*['\"]?([^'\"\s]+)")
_NAME_ASSIGN = re.compile(r"(?m)^[ \t]*(pkgname|pkgbase)[ \t]*=[ \t]*(\([^)\n]*\)|\S+)")
_VARIABLE = re.compile(r"\$\{?(pkgname|pkgbase)\}?")


def _install_names(pkgbuild: str, srcinfo: str) -> set[str]:
    """The install-script names the build may run. `install=$pkgname.install` is the usual way to write it:
    the variables are replaced by every name the PKGBUILD or .SRCINFO gives them, and a name that still
    contains something Cygnus cannot expand stays as it is, so the review reports it as not shown."""
    values: dict[str, set[str]] = {"pkgname": set(), "pkgbase": set()}
    for text in (pkgbuild, srcinfo):
        for m in _NAME_ASSIGN.finditer(text):
            for word in re.findall(r"[^\s()'\"]+", m.group(2)):
                values[m.group(1)].add(word)
        for m in re.finditer(r"(?m)^[ \t]*(pkgname|pkgbase)[ \t]*=[ \t]*(\S+)$", text):  # .SRCINFO: "pkgname = x"
            values[m.group(1)].add(m.group(2))
    names: set[str] = set()
    for source in (pkgbuild, srcinfo):
        for m in _INSTALL_REF.finditer(source):
            ref = m.group(1)
            variants = {ref}
            for var in ("pkgname", "pkgbase"):
                if any(f"${var}" in v or f"${{{var}}}" in v for v in variants):
                    variants = {re.sub(rf"\$\{{?{var}\}}?", word, v) for v in variants
                                for word in (values[var] or {f"${var}"})}
            names.update(variants)
    return names

# Things a reviewer should look at twice. Hints only: the review itself is what matters.
_HINTS = [
    (re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(ba|z|da)?sh\b"), "downloads and runs a script"),
    (re.compile(r"\bsudo\b|\bpkexec\b|\bdoas\b"), "asks for administrator rights while building"),
    (re.compile(r"base64\s+(-d|--decode)|\bxxd\s+-r\b"), "decodes hidden data"),
    (re.compile(r"\beval\b"), "evaluates generated code"),
    (re.compile(r"(~|\$HOME|\$\{HOME\})/"), "touches your home folder"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+/(?!\S*\$pkgdir)"), "deletes outside the build folder"),
    (re.compile(r"(?m)^[ \t]*(source|\.)[ \t]+\S"), "runs code from another file"),
    (re.compile(r"http://"), "downloads over plain http"),
]


def _git_env(home: Path) -> dict[str, str]:
    env = proc.clean_env()
    # Your git configuration (aliases, hooks, credential helpers) must not apply to an AUR checkout.
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0", HOME=str(home))
    return env


def _git(args: list[str], cwd: Path | None, home: Path, timeout: float = 120) -> str:
    res = proc.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=user", *args],
                   timeout=timeout, cwd=str(cwd) if cwd else None, env=_git_env(home))
    if res.returncode != 0:
        raise CygnusError(f"git {args[0]} failed: {res.stderr.strip()[:300]}")
    return res.stdout


def aur_dir() -> Path:
    return paths.cache_dir() / "aur"


def _approvals_path() -> Path:
    return paths.data_dir() / "aur-approvals.json"


def approvals() -> dict[str, str]:
    try:
        return json.loads(_approvals_path().read_text())
    except (OSError, ValueError):
        return {}


def approve(pkgbase: str, commit: str) -> None:
    data = approvals()
    data[pkgbase] = commit
    _approvals_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write(_approvals_path(), json.dumps(data, indent=1, sort_keys=True).encode())


def fetch(pkgbase: str, *, base_url: str | None = None) -> Path:
    """Clone or update the package's AUR repository; returns the checkout. Nothing is run."""
    if not PKGBASE.match(pkgbase):
        raise CygnusError(f"invalid AUR package name {pkgbase!r}")
    root = aur_dir()
    root.mkdir(parents=True, exist_ok=True)
    checkout = root / pkgbase
    url = f"{base_url or AUR_GIT}/{pkgbase}.git"
    if (checkout / ".git").is_dir():
        _git(["fetch", "--quiet", "origin"], checkout, root)
        _git(["reset", "--quiet", "--hard", "origin/HEAD"], checkout, root)
        _git(["clean", "-qfdx"], checkout, root)
    else:
        shutil.rmtree(checkout, ignore_errors=True)
        _git(["clone", "--quiet", url, str(checkout)], None, root, timeout=300)
    if not (checkout / "PKGBUILD").is_file():
        raise CygnusError(f"{pkgbase} is not an AUR package (no PKGBUILD)")
    return checkout


def parse_srcinfo(text: str) -> dict[str, Any]:
    """The static metadata the AUR publishes next to the PKGBUILD (no shell involved)."""
    base: dict[str, list[str]] = {}
    packages: dict[str, dict[str, list[str]]] = {}
    current = base
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = (x.strip() for x in line.partition("="))
        if key == "pkgbase":
            base["pkgbase"] = [value]
            current = base
        elif key == "pkgname":
            current = packages.setdefault(value, {})
        else:
            current.setdefault(key, []).append(value)

    def arch_keys(d: dict[str, list[str]], key: str) -> list[str]:
        return d.get(key, []) + d.get(f"{key}_x86_64", [])

    deps = set(arch_keys(base, "depends"))
    for p in packages.values():
        deps.update(arch_keys(p, "depends"))
    return {"pkgbase": (base.get("pkgbase") or [""])[0], "pkgver": (base.get("pkgver") or [""])[0],
            "pkgrel": (base.get("pkgrel") or [""])[0], "pkgnames": list(packages),
            "arch": base.get("arch", []), "depends": sorted(deps),
            "makedepends": sorted(set(arch_keys(base, "makedepends"))),
            "checkdepends": sorted(set(arch_keys(base, "checkdepends"))),
            "validpgpkeys": base.get("validpgpkeys", []), "source": arch_keys(base, "source")}


@dataclass(slots=True, kw_only=True)
class Review:
    pkgbase: str
    commit: str
    previously_approved: str | None
    files: dict[str, str] = field(default_factory=dict)  # path -> text (or a placeholder)
    diff: str = ""  # since the approved commit, when there is one
    hints: list[dict[str, str]] = field(default_factory=list)
    srcinfo: dict[str, Any] = field(default_factory=dict)
    complete: bool = True  # False: something could not be shown in full, so Cygnus will not build it
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _executed(name: str) -> bool:
    """Files that run as code: makepkg sources the PKGBUILD; pacman runs .install scripts as root."""
    return name == "PKGBUILD" or name.endswith((".install", ".sh"))


def review(checkout: Path) -> Review:
    """Everything a reviewer must see. Nothing is ever left out silently: anything that cannot be
    shown in full (a link, a binary file, hidden characters in code, too much) makes the review
    incomplete, and an incomplete review cannot be built."""
    home = aur_dir()
    commit = _git(["rev-parse", "HEAD"], checkout, home).strip()
    pkgbase = checkout.name
    approved = approvals().get(pkgbase)
    r = Review(pkgbase=pkgbase, commit=commit, previously_approved=approved)
    names = [n for n in _git(["ls-files", "-z"], checkout, home).split("\0") if n]
    names.sort(key=lambda n: (n != "PKGBUILD", n != ".SRCINFO", not _executed(n), n))
    if len(names) > MAX_REVIEW_FILES:
        r.complete = False
        r.problems.append(f"it has {len(names)} files; Cygnus shows at most {MAX_REVIEW_FILES}")
        names = names[:MAX_REVIEW_FILES]
    placeholders: set[str] = set()
    raw: dict[str, str] = {}
    for name in names:
        p = checkout / name
        if p.is_symlink() or not p.is_file():
            r.complete = False
            r.problems.append(f"{name} is a link or special file; Cygnus cannot show you what it really contains")
            r.files[name], _ = "(a link or special file)", placeholders.add(name)
            continue
        with open(p, "rb") as fh:  # never more than the cap is read, however big the file is
            data = fh.read(MAX_REVIEW_FILE + 1)
        if len(data) > MAX_REVIEW_FILE:
            r.complete = False
            r.problems.append(f"{name} is larger than {MAX_REVIEW_FILE // 1024} KiB")
            data = data[:MAX_REVIEW_FILE]
        if b"\0" in data:
            r.complete = False
            r.problems.append(f"{name} is a binary file; Cygnus cannot show it to you")
            r.files[name], _ = f"(binary file, {p.stat().st_size} bytes)", placeholders.add(name)
            continue
        raw[name] = data.decode("utf-8", "replace")
    # Files the build runs: the PKGBUILD, install scripts, and whatever install= names.
    executed = {n for n in raw if _executed(n)}
    executed.update(_install_names(raw.get("PKGBUILD", ""), raw.get(".SRCINFO", "")))
    for name, text in raw.items():
        if name in executed and ("\ufffd" in text or hides_text(text)):
            r.complete = False
            r.problems.append(f"{name} contains invisible or binary characters that could hide code from you")
        r.files[name] = visible(text)
    for name in sorted(executed - set(raw) - placeholders):
        r.complete = False
        r.problems.append(f"the build runs {name}, which is not among the files Cygnus could show you")
    if approved and approved != commit:
        try:
            diff = _git(["diff", "--text", f"{approved}..{commit}", "--", "."], checkout, home)
        except CygnusError:
            r.complete = False
            r.problems.append("the version you approved is no longer in the history")
            diff = ""
        if len(diff) > MAX_DIFF:
            r.complete = False
            r.problems.append("the changes are too large to show")
        r.diff = visible(diff[:MAX_DIFF])
    for name, text in raw.items():
        if name == ".SRCINFO":
            continue
        for rx, why in _HINTS:
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                r.hints.append({"file": name, "line": str(line), "why": why,
                                "text": visible(text.splitlines()[line - 1].strip()[:160])})
        for line in _skipped_checksums(text):
            r.hints.append({"file": name, "line": str(line), "why": "has sources without a checksum",
                            "text": visible(text.splitlines()[line - 1].strip()[:160])})
    if ".SRCINFO" in raw:
        r.srcinfo = parse_srcinfo(raw[".SRCINFO"])
    if "PKGBUILD" not in raw:
        r.complete = False
        if "PKGBUILD" not in placeholders:
            r.problems.append("there is no PKGBUILD to review")
    return r


def build(checkout: Path, approved_commit: str, out_dir: Path, progress: Callable[[str], None] = lambda _: None,
          *, timeout: float = 3 * 3600) -> list[Path]:
    """Build exactly the approved commit, as you, with makepkg. Dependencies must already be
    installed: makepkg checks them and stops otherwise (it is never allowed to install anything)."""
    home = aur_dir()
    head = _git(["rev-parse", "HEAD"], checkout, home).strip()
    if head != approved_commit:
        raise CygnusError("the build files changed after you reviewed them; review them again")
    dirty = _git(["status", "--porcelain", "--ignored"], checkout, home).strip()
    if dirty:
        raise CygnusError("the checkout contains files that are not part of the reviewed version")
    if os.geteuid() == 0:
        raise CygnusError("AUR packages are never built as root")
    shutil.rmtree(out_dir, ignore_errors=True)  # only this build's packages may come out of it
    out_dir.mkdir(parents=True)
    work = home / ".build" / checkout.name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    env = proc.clean_env()
    env.update(PKGDEST=str(out_dir), SRCDEST=str(work / "src-cache"), BUILDDIR=str(work),
               LOGDEST=str(work), HOME=os.environ.get("HOME", str(Path.home())))
    progress(f"Building {checkout.name} (this runs its build script as you)…")
    res = proc.run(["makepkg", "--noconfirm", "--cleanbuild", "--clean", "--needed", "--force"],
                   timeout=timeout, cwd=str(checkout), env=env, max_output=8 << 20)
    output = res.stdout + res.stderr  # makepkg prints headers to stderr and details to stdout
    for line in output.splitlines()[-40:]:
        progress(line)
    if res.returncode != 0:
        missing = re.findall(r"->\s+(\S+)\s*$", res.stdout, re.M) if "Missing dependencies" in output else []
        raise CygnusError("makepkg failed" + (f": missing dependencies {', '.join(missing)}" if missing else
                                              f" (exit status {res.returncode})"))
    _git(["clean", "-qfdx"], checkout, home)
    built = sorted(p for p in out_dir.glob("*.pkg.tar.*") if not p.name.endswith(".sig")
                   and "-debug-" not in p.name)
    if not built:
        raise CygnusError("makepkg finished but produced no package")
    return built
