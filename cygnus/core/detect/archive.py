"""Portable archives (.tar.gz, .tar.xz, .tar.zst, .tar.bz2, .tar): a program that is unpacked and run where it is, with no installer.

Everything the person is shown, and everything the installer later relies on, is read here in ONE pass over the archive with
Python's tarfile (names, types, modes, sizes and link targets exactly as stored, not as a text listing would show them), with the
decompressed size, the number of entries and every member that is read capped. Nothing is extracted. What is unsafe (a path that
leaves the folder, a hard link, a device file, a link that leads out, the same name twice, a file written through a link) is not
repaired but reported as a reason the archive is refused, so what the person approved is exactly what gets unpacked."""

from __future__ import annotations

import base64
import hashlib
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from cygnus.core.desktop import entry as desktop_entry
from cygnus.core.errors import DetectionError, UnsafeInputError
from cygnus.core.models import Candidate, PackageFormat, Severity
from cygnus.core.util import scriptrun
from cygnus.core.util.limits import CappedReader, decompressor

RESERVED = ".cygnus-installation"  # the marker file Cygnus puts in a program's folder: an archive may not carry one
MAX_ENTRIES = 200_000
MAX_BYTES = 8 * 1024**3  # file data in all, once unpacked
_STREAM_SLACK = 1024**3  # tar headers, padding and long names on top of the file data
MAX_HEADER = 1024 * 1024  # a long-name or extended header this big is refused: tarfile reads one whole, however much it claims
MAX_PATH_BYTES, MAX_NAME_BYTES = 3500, 255  # Linux allows 4096 and 255: room is left for where the program's folder is
MAX_LINK_WORK = 3_000_000  # steps in all spent following links to see where they lead
MAX_DESKTOP_FILES, MAX_DESKTOP_BYTES = 16, 64 * 1024
MAX_ICONS, MAX_ICON_BYTES, MAX_ICON_POOL = 32, 256 * 1024, 8 * 1024**2
MAX_SCRIPTS, MAX_SCRIPT_BYTES = 8, 64 * 1024
MAX_SCRIPT_CANDIDATES = 32  # install-named files near the top that are read before the ones directly in the program's folder are picked
MAX_PROGRAM_HEADS = 4000
_HEAD = 512

# Scripts whose names say they belong to an installation, directly in the program's folder: listed for the person, and run only if the
# person chooses to run them.
INSTALL_SCRIPT = re.compile(r"(?i)^\.?(?:(?:pre|post)(?:inst|rm)|(?:un)?install|setup|update)(?:\.(?:sh|bash))?$")
# Files at the top of a source tree: an archive that has these and no program in it is something to build, not to run.
SOURCE_MARKERS = frozenset({"configure", "configure.ac", "makefile", "cmakelists.txt", "meson.build", "cargo.toml", "go.mod", "setup.py"})
SYSTEM_ROOTS = frozenset({"usr", "etc", "opt", "var", "bin", "sbin", "lib", "lib64", "boot", "srv"})
# Programs inside an application folder that are its own helpers: never picked on their own as "the" program.
_HELPERS = re.compile(r"(?i)sandbox|crashpad|crashreport|updater|update|uninstall|installer|bootstrap|helper|handler|\.so(\.|$)")


class HeaderTooBig(UnsafeInputError):
    """An entry's header claims to be far bigger than any real one."""


class _Stop(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def shown(name: str) -> str:
    """A name from an archive, made safe to put in a message."""
    return "".join(c if c.isprintable() else "?" for c in name)[:80]


def normal_name(raw: str) -> str | None:
    """The member's path as a tidy relative path ("" for the top folder itself), or None when it is absolute or climbs out."""
    if raw.startswith("/") or "\x00" in raw:
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts)


class PathTree:
    """The paths of an archive as a tree, so that every check on a path costs its length (looking at each of its parents as a joined
    string costs the square of it: a 200 KB name would take half a minute). A node is [kind, children, link target]: kind d (a folder
    the archive lists), i (a folder only implied by something inside it), f (a file) or l (a link)."""

    __slots__ = ("root",)

    def __init__(self) -> None:
        self.root: dict[str, list] = {}

    def add(self, path: str, kind: str, target: str = "") -> str | None:
        """None when the entry fits, or why not: "through" (a parent is a file or a link: it would be written through it) or
        "duplicate" (the path is already there, or is a file or link where entries inside it have already been seen)."""
        parts = path.split("/")
        children = self.root
        for part in parts[:-1]:
            node = children.get(part)
            if node is None:
                node = children[part] = ["i", {}, ""]
            elif node[0] in ("f", "l"):
                return "through"
            children = node[1]
        node = children.get(parts[-1])
        if node is None:
            children[parts[-1]] = [kind, {}, target]
            return None
        if kind == "d" and node[0] in ("d", "i"):
            node[0] = "d"
            return None
        return "duplicate"


def limit_headers(tar: tarfile.TarFile) -> None:
    """Refuse a read bigger than MAX_HEADER from the archive stream (older tarfile versions read a long-name or extended header
    whole, however big it says it is). Real headers are a few hundred bytes, and every other read here is much smaller."""
    stream = tar.fileobj
    real = stream.read

    def read(size=-1, *args):
        if size is None or size < 0 or size > MAX_HEADER:
            raise HeaderTooBig("an entry's header claims to be bigger than Cygnus reads")
        return real(size, *args)

    stream.read = read


class SafeInfo(tarfile.TarInfo):
    """A tar entry header that refuses a long-name or extended record whose size field is far bigger than a real one, before any
    of it is read (newer tarfile versions read such a record in pieces, but still all of it, so the stream cap alone would let it
    grow to gigabytes). The records that stay for the rest of the archive (global ones) are held to one limit in all."""

    def _proc_member(self, tarfile_):
        if self.type in _RECORD_TYPES:
            if self.size > MAX_HEADER:
                raise HeaderTooBig("an entry's header claims to be bigger than Cygnus reads")
            if self.type == tarfile.XGLTYPE:
                kept = getattr(tarfile_, "_cygnus_kept", 0) + self.size
                if kept > MAX_HEADER:
                    raise HeaderTooBig("the archive's global records are bigger than Cygnus reads")
                tarfile_._cygnus_kept = kept
        return super()._proc_member(tarfile_)


_RECORD_TYPES = (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE)


def open_stream(fileobj: Any) -> tarfile.TarFile:
    """A tar archive read once from start to end, with the header limits above in force. Every reader of an archive Cygnus did not
    make opens it this way."""
    tar = tarfile.open(fileobj=fileobj, mode="r|", tarinfo=SafeInfo)
    limit_headers(tar)
    return tar


def name_problem(name: str) -> str | None:
    """Why a path could not be kept by a Linux file system (too long), or None."""
    if len(name.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
        return "a path is longer than a Linux file system holds"
    if any(len(part.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES for part in name.split("/")):
        return "a file name is longer than a Linux file system holds"
    return None


def links_leave(links: dict[str, str], top: dict[str, list]) -> str | None:
    """The first link, if any, that really leads out of the unpacked folder once the links it passes through are followed (a link can
    look as if it stays inside by its own text and still leave through another link). Absolute targets leave. The work is capped
    and a link that takes too long to follow counts as leaving."""
    work = 0
    for link, target in links.items():
        if not target or target.startswith("/"):
            return link
        stack: list[dict[str, list]] = [top]
        for part in link.split("/")[:-1]:  # the folder the link is in
            node = stack[-1].get(part)
            stack.append(node[1] if node else {})
        queue = target.split("/")[::-1]
        hops = 0
        while queue:
            work += 1
            if work > MAX_LINK_WORK:
                return link
            part = queue.pop()
            if part in ("", "."):
                continue
            if part == "..":
                if len(stack) == 1:
                    return link
                stack.pop()
                continue
            node = stack[-1].get(part)
            if node is not None and node[0] == "l":  # a link on the way: what it says is read from the folder it is in
                hops += 1
                if hops > 40 or not node[2] or node[2].startswith("/"):
                    return link
                queue.extend(node[2].split("/")[::-1])
                continue
            stack.append(node[1] if node else {})
    return None


def _parents(name: str):
    parts = name.split("/")
    for i in range(1, len(parts)):
        yield "/".join(parts[:i])


def _interpreter(head: bytes) -> str:
    line = head.split(b"\n", 1)[0][2:].decode("utf-8", "replace").strip().split()
    if not line:
        return ""
    program = PurePosixPath(line[0]).name
    if program == "env":
        rest = [w for w in line[1:] if not w.startswith("-") and "=" not in w]
        return PurePosixPath(rest[0]).name if rest else "env"
    return program


def _link_escapes(link: str, target: str) -> bool:
    """True when a relative link target, read from the folder the link is in, climbs above the root of the unpacked tree."""
    if target.startswith("/") or not target:
        return True
    depth = len(link.split("/")) - 1
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        else:
            depth += 1
    return False


def scan(path: Path, compression: str) -> dict[str, Any]:
    """Read the archive once. The result holds what was found, and `problems` (code, message) for everything that makes it unsafe."""
    problems: list[dict[str, str]] = []

    def problem(code: str, message: str) -> None:
        if not any(p["code"] == code for p in problems):  # the first example of each kind is enough
            problems.append({"code": code, "message": message})

    kinds: dict[str, str] = {}  # cleaned path -> d | f | l
    tree = PathTree()
    link_targets: dict[str, str] = {}
    modes: dict[str, int] = {}
    sizes: dict[str, int] = {}
    heads: dict[str, bytes] = {}
    desktops: dict[str, bytes] = {}
    icons: dict[str, bytes] = {}
    scripts: dict[str, bytes] = {}
    oversize: dict[str, int] = {}
    icon_pool = 0
    total = 0
    count = 0
    special: list[str] = []
    try:
        with open(path, "rb") as raw:
            capped = CappedReader(decompressor(raw, compression), MAX_BYTES + _STREAM_SLACK)
            with open_stream(capped) as tar:
                for member in tar:
                    count += 1
                    if count > MAX_ENTRIES:
                        raise _Stop("ARCHIVE_TOO_MANY", f"it holds more than {MAX_ENTRIES:,} entries")
                    name = normal_name(member.name)
                    if name is None:
                        problem("ARCHIVE_UNSAFE_PATH", f"an entry has a path that leaves the folder it is unpacked into ({shown(member.name)})")
                        continue
                    if name == "":
                        continue
                    too_long = name_problem(name)
                    if too_long:
                        problem("ARCHIVE_NAME_TOO_LONG", f"{too_long} ({shown(name[:40])}…)")
                        continue
                    if member.isdir():
                        kind = "d"
                    elif member.isreg():
                        kind = "f"
                    elif member.issym():
                        kind = "l"
                    elif member.islnk():
                        problem("ARCHIVE_HARDLINK", f"it contains a hard link ({shown(name)}), which Cygnus does not unpack")
                        continue
                    else:
                        problem("ARCHIVE_SPECIAL_FILE", f"it contains a device file, pipe or socket ({shown(name)})")
                        continue
                    refused = tree.add(name, kind, member.linkname if kind == "l" else "")
                    if refused == "duplicate":
                        problem("ARCHIVE_DUPLICATE", f"the same path is in it twice, or is a file where other entries are inside it ({shown(name)}), so what ends up there would be a guess")
                        continue
                    if refused == "through":
                        problem("ARCHIVE_THROUGH_LINK", f"an entry is written inside a file or through a link ({shown(name)})")
                        continue
                    kinds[name] = kind
                    modes[name] = member.mode
                    if kind == "l":
                        link_targets[name] = member.linkname
                        continue
                    if kind == "d":
                        continue
                    size = member.size
                    sizes[name] = size
                    total += size
                    if total > MAX_BYTES:
                        raise _Stop("ARCHIVE_TOO_BIG", f"it would take more than {MAX_BYTES // 1024**3} GiB once unpacked")
                    if member.mode & 0o6000:
                        special.append(name)
                    base = name.rsplit("/", 1)[-1]
                    lowered = base.lower()
                    want_head = bool(member.mode & 0o111) and len(heads) < MAX_PROGRAM_HEADS
                    want_desktop = lowered.endswith(".desktop") and size <= MAX_DESKTOP_BYTES and len(desktops) < MAX_DESKTOP_FILES
                    want_icon = (lowered.endswith((".png", ".svg")) and 0 < size <= MAX_ICON_BYTES and len(icons) < MAX_ICONS
                                 and icon_pool + size <= MAX_ICON_POOL)
                    near_top = INSTALL_SCRIPT.match(base) is not None and name.count("/") <= 1  # (the folder everything is in may or may not be there)
                    want_script = near_top and size <= MAX_SCRIPT_BYTES and len(scripts) < MAX_SCRIPT_CANDIDATES
                    if near_top and size > MAX_SCRIPT_BYTES and len(oversize) < MAX_SCRIPTS:
                        oversize[name] = size  # not read, but not forgotten: the person is told it is there
                    if not (want_head or want_desktop or want_icon or want_script):
                        continue
                    handle = tar.extractfile(member)
                    if handle is None:
                        continue
                    data = handle.read(max(_HEAD if want_head else 0, size if (want_desktop or want_icon or want_script) else 0))
                    if want_desktop:
                        desktops[name] = data
                    if want_icon:
                        icons[name] = data
                        icon_pool += len(data)
                    if want_script:
                        scripts[name] = data
                    if want_head:
                        heads[name] = data[:_HEAD]
    except _Stop as stop:
        problem(stop.code, stop.message)
        return {"problems": problems, "stopped": True, "entries": count}
    except HeaderTooBig:
        problem("ARCHIVE_BAD_HEADER", "an entry's header is far bigger than a real one (it would use huge amounts of memory to read)")
        return {"problems": problems, "stopped": True, "entries": count}
    except UnsafeInputError:
        problem("ARCHIVE_TOO_BIG", f"it would take more than {MAX_BYTES // 1024**3} GiB once unpacked")
        return {"problems": problems, "stopped": True, "entries": count}
    except DetectionError:
        raise
    except MemoryError as exc:
        raise DetectionError("the archive is malformed (excessive memory use)") from exc
    except Exception as exc:  # noqa: BLE001 - tarfile and the decompressors raise many types on damaged or hostile input
        raise DetectionError(f"the archive cannot be read ({type(exc).__name__}: {exc})") from exc
    return {"problems": problems, "stopped": False, "entries": count, "kinds": kinds, "tree": tree, "links": link_targets, "modes": modes,
            "sizes": sizes, "heads": heads, "desktops": desktops, "icons": icons, "scripts": scripts, "oversize_scripts": oversize, "special": special,
            "total": total}


def _strip_root(kinds: dict[str, str]) -> str:
    """The one folder everything is inside (it is not made part of the installed program's path), or ""."""
    tops = {p.split("/")[0] for p in kinds}
    if len(tops) != 1:
        return ""
    top = next(iter(tops))
    return top if kinds.get(top) == "d" or any("/" in p for p in kinds) else ""


def _version_guess(*texts: str) -> str | None:
    for text in texts:
        m = re.search(r"(?<![\w.])v?(\d+(?:\.\d+){1,3}(?:[-+~.]?(?:rc|beta|alpha|b)\d*)?)(?![\w])", text)
        if m:
            return m.group(1)
    return None


def _name_guess(filename: str, root: str) -> str:
    stem = re.sub(r"(?i)\.(tar\.(gz|xz|zst|bz2)|tgz|txz|tbz2?|tar|pkg)$", "", filename)
    base = root or stem
    base = re.sub(r"[-_. ]v?\d+(?:\.\d+)+.*$", "", base)  # drop a version and what follows it
    base = re.sub(r"[-_. ](?:linux|x86_64|x86-64|amd64|x64|64bit|portable|bin|binary|generic|stable)(?![A-Za-z0-9]).*$", "", base, flags=re.I)
    return base.strip(" -_.") or stem or "application"


def _png_size(data: bytes) -> int:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 24:
        return max(int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    return 0


def _pick_icon(wanted: str | None, icons: dict[str, bytes], root: str) -> tuple[str, bytes, str] | None:
    """(path, bytes, "png" | "svg") of the icon the menu entry names, the biggest of them; None when there is none."""
    def rel(p: str) -> str:
        return p[len(root) + 1:] if root and p.startswith(root + "/") else p

    chosen: list[tuple[int, str, bytes, str]] = []
    stem = PurePosixPath(wanted).stem if wanted else ""
    for p, data in icons.items():
        if len(data) > MAX_ICON_BYTES:
            continue
        isvg = data.lstrip()[:5] in (b"<?xml", b"<svg ") or p.lower().endswith(".svg")
        png = not isvg and data[:8] == b"\x89PNG\r\n\x1a\n"
        if not (isvg or png):
            continue
        match = bool(stem) and PurePosixPath(p).stem == stem
        if wanted and not match:
            continue
        chosen.append((_png_size(data) if png else 10**6, p, data, "svg" if isvg else "png"))
    if not chosen and not wanted and len(icons) == 1:
        p, data = next(iter(icons.items()))
        png = data[:8] == b"\x89PNG\r\n\x1a\n"
        return (rel(p), data, "png") if png else None
    if not chosen:
        return None
    size, p, data, kind = max(chosen, key=lambda c: (c[0], -len(c[1])))
    return rel(p), data, kind


def choose_main(meta: dict[str, Any]) -> tuple[str | None, list[str]]:
    """(the program to start, or None when it cannot be told) and the candidates, best first (paths inside the unpacked folder).
    The menu entry that came with the archive decides when it can: its Exec names a program, often by a system path such as
    /usr/bin/discord, and the same name inside the archive is meant."""
    programs: dict[str, dict[str, Any]] = meta.get("executables") or {}
    if not programs:
        return None, []

    def depth(p: str) -> int:
        return p.count("/")

    def ordered(paths) -> list[str]:
        return sorted(paths, key=lambda p: (depth(p), len(p), p))

    for entry in meta.get("desktop_entries") or []:
        exec_value = (entry.get("fields") or {}).get("Exec")
        if not exec_value:
            continue
        try:
            argv = desktop_entry.split_exec(desktop_entry.unescape(exec_value))
        except ValueError:
            continue
        if argv and PurePosixPath(argv[0]).name == "env":
            argv = [a for a in argv[1:] if not a.startswith("-") and "=" not in a]
        if not argv:
            continue
        parts = [p for p in argv[0].split("/") if p]
        for k in range(len(parts), 0, -1):
            suffix = "/".join(parts[-k:])
            hits = [p for p in programs if (p == suffix or p.endswith("/" + suffix)) and not INSTALL_SCRIPT.match(PurePosixPath(p).name)]
            if hits:
                best = ordered(hits)[0]
                return best, [best] + [p for p in _ranked(programs, meta) if p != best][:11]
    ranked = _ranked(programs, meta)
    if len(ranked) == 1:
        return ranked[0], ranked
    stem = (meta.get("root") or meta.get("name_guess") or "").lower()
    named = [p for p in ranked if PurePosixPath(p).stem.lower() == stem or PurePosixPath(p).name.lower() in (stem, stem + ".sh")]
    if len(named) == 1:
        return named[0], ranked
    return None, ranked


def _ranked(programs: dict[str, dict[str, Any]], meta: dict[str, Any]) -> list[str]:
    """The programs of the archive that could be the one to start (not its helpers), the likeliest first."""
    stem = (meta.get("root") or meta.get("name_guess") or "").lower()
    plain = [p for p, info in programs.items() if info.get("kind") in ("elf", "script")
             and not _HELPERS.search(PurePosixPath(p).name) and not INSTALL_SCRIPT.match(PurePosixPath(p).name)]
    usable = [p for p in plain if p.count("/") <= 2] or plain  # near the top first; deeper only when nothing is

    def key(p: str) -> tuple:
        base = PurePosixPath(p).name.lower()
        return (0 if base in (stem, stem + ".sh") else 1, 0 if base in ("run", "run.sh", "start.sh", "launch.sh", "apprun") else 1,
                p.count("/"), p)

    return sorted(usable, key=key)[:12]


def inspect(path: Path, compression: str) -> Candidate:
    facts = scan(path, compression)
    cand = Candidate(format=PackageFormat.ARCHIVE, source=str(path))
    for p in facts["problems"]:
        cand.add(p["code"], Severity.ERROR, p["message"])
    meta = cand.metadata
    meta.update({"compression": compression or "none", "entries": facts["entries"], "stopped": facts.get("stopped", False)})
    if facts.get("stopped"):
        cand.name = _name_guess(path.name, "")
        return cand
    kinds: dict[str, str] = facts["kinds"]
    if not kinds:
        cand.add("ARCHIVE_EMPTY", Severity.ERROR, "the archive is empty")
        return cand
    root = _strip_root(kinds)
    cut = len(root) + 1 if root else 0

    def rel(p: str) -> str:
        return p[cut:]

    inside = {rel(p): k for p, k in kinds.items() if p != root}
    links = {rel(p): t for p, t in facts["links"].items() if p != root}
    tree: PathTree = facts["tree"]
    children = tree.root[root][1] if root and root in tree.root else tree.root
    leaving = links_leave(links, children)
    if leaving is not None:
        cand.add("ARCHIVE_UNSAFE_LINK", Severity.ERROR,
                 f"a link ({shown(leaving)} -> {shown(links[leaving])}) leads outside the folder the archive is unpacked into")
    sizes = {rel(p): s for p, s in facts["sizes"].items()}
    programs: dict[str, dict[str, Any]] = {}
    for p, head in facts["heads"].items():
        r = rel(p)
        if head[:4] == b"\x7fELF":
            programs[r] = {"kind": "elf", "size": sizes.get(r, 0)}
        elif head[:2] == b"#!":
            programs[r] = {"kind": "script", "interpreter": _interpreter(head), "size": sizes.get(r, 0)}
        else:
            programs[r] = {"kind": "other", "size": sizes.get(r, 0)}
    entries = []
    for p, data in sorted(facts["desktops"].items(), key=lambda kv: (kv[0].count("/"), kv[0])):
        if len(data) > MAX_DESKTOP_BYTES:
            continue
        de = desktop_entry.parse(data.decode("utf-8", "replace"))
        fields = dict(de.groups.get(desktop_entry.MAIN_GROUP, []))
        if fields.get("Type", "Application") != "Application" or fields.get("NoDisplay", "").lower() == "true":
            continue
        entries.append({"path": rel(p), "fields": fields, "actions": {a: dict(de.groups.get(f"Desktop Action {a}", []))
                                                                      for a in de.actions[:20]}})
    # Install scripts are the ones directly in the program's folder (a file called `update` deeper down is a part of the program). The first
    # MAX_SCRIPTS are offered; every other one that matched is listed with the reason it is not, so none is ever dropped without a word.
    script_sha256: dict[str, str] = {}
    script_problems: dict[str, str] = {}
    scripts: dict[str, str] = {}
    near = sorted(((rel(p), d) for p, d in facts["scripts"].items() if len(d) <= MAX_SCRIPT_BYTES and "/" not in rel(p)))
    for position, (name, data) in enumerate(near):
        if shown(name) != name:
            script_problems[shown(name)] = "its name has characters that cannot be shown"
        elif position >= MAX_SCRIPTS:
            script_problems[name] = f"there are more than {MAX_SCRIPTS} install scripts, and this one is not offered"
        else:
            scripts[name] = data.decode("utf-8", "replace")
            # The checksum of the script's exact bytes: what the person is shown is bound to it, so only those bytes can ever be run. A
            # script that could not be shown exactly as it would run (not valid text, a program Cygnus does not run scripts with...) gets
            # none: it is listed with the reason, and never offered.
            problem = scriptrun.runnable_problem(data)
            if problem:
                script_problems[name] = problem
            else:
                script_sha256[name] = hashlib.sha256(data).hexdigest()
    for p in facts.get("oversize_scripts", {}):
        if "/" not in rel(p):
            script_problems[shown(rel(p))] = f"it is larger than {MAX_SCRIPT_BYTES // 1024} KiB, so it cannot be shown to you whole"
    top = {p.split("/")[0] for p in inside}
    elf_count = sum(1 for i in programs.values() if i["kind"] == "elf")
    meta.update({"root": root, "total_bytes": facts["total"], "names": sorted(inside), "has_symlinks": bool(links),
                 "file_count": sum(1 for k in inside.values() if k == "f"), "link_count": len(links),
                 "executables": programs, "desktop_entries": entries, "scripts": scripts, "script_sha256": script_sha256, "script_problems": script_problems,
                 "special_modes": sorted(rel(p) for p in facts["special"]), "name_guess": _name_guess(path.name, root)})
    cand.installed_size = facts["total"]
    main_entry = entries[0] if entries else None
    wanted_icon = (main_entry["fields"].get("Icon") if main_entry else None)
    icon = _pick_icon(wanted_icon, facts["icons"], root)
    if icon:
        meta["icon"] = {"path": icon[0], "kind": icon[2], "bytes": len(icon[1]), "base64": base64.b64encode(icon[1]).decode()}
    cand.name = (main_entry["fields"].get("Name") if main_entry else None) or meta["name_guess"]
    cand.summary = (main_entry["fields"].get("Comment") or main_entry["fields"].get("GenericName")) if main_entry else None
    cand.version = _version_guess(path.name, root)
    if main_entry:
        cand.identity["desktop_id"] = PurePosixPath(main_entry["path"]).name
        meta["desktop_entry"] = main_entry["fields"]
        meta["desktop_action_groups"] = main_entry["actions"]
    main, candidates = choose_main(meta)
    meta["main"], meta["main_candidates"] = main, candidates
    # What kind of archive it is, when it is not something to unpack and run.
    if RESERVED in inside:
        cand.add("ARCHIVE_RESERVED_NAME", Severity.ERROR, f"it contains a file named {RESERVED}, which Cygnus uses to recognise the folders it made")
    if "+MANIFEST" in inside or "+COMPACT_MANIFEST" in inside:
        cand.add("ARCHIVE_FREEBSD_PACKAGE", Severity.ERROR, "this is a FreeBSD package, which only works on FreeBSD")
    at_root = [p for p, i in programs.items() if "/" not in p and i["kind"] in ("elf", "script")]
    if (top & {"usr", "etc"} or top == {"opt"} or root in ("usr", "etc", "opt", "var", "srv")) and not at_root:  # an AppDir has AppRun at its top; a system tree has no program there
        cand.add("ARCHIVE_SYSTEM_LAYOUT", Severity.ERROR,
                 "this archive is laid out like a system folder tree (usr/, etc/ ...) to be unpacked over /, which Cygnus never does "
                 "for a program that is not a package: look for a .deb, .rpm, Flatpak, AppImage or AUR version")
    top_lower = {t.lower() for t in top}
    if top_lower & SOURCE_MARKERS and not elf_count:
        cand.add("ARCHIVE_SOURCE_CODE", Severity.ERROR,
                 "this looks like source code to build, not a program that is ready to run: build it from the AUR or with the "
                 "project's own instructions")
    elif not any(i["kind"] in ("elf", "script") for i in programs.values()):
        cand.add("ARCHIVE_NO_PROGRAM", Severity.ERROR, "there is no program in this archive (nothing in it can be run)")
    if scripts:
        cand.add("ARCHIVE_SCRIPTS", Severity.INFO,
                 "it contains install scripts (" + ", ".join(shown(n) for n in sorted(scripts)[:4]) + "): Cygnus runs them only if you choose to")
    if meta["special_modes"]:
        cand.add("ARCHIVE_SPECIAL_MODES", Severity.INFO,
                 "these files are marked to run with extra privileges (" + ", ".join(shown(n) for n in meta["special_modes"][:3])
                 + "): Cygnus removes that mark, so nothing it installs ever runs with more rights than you")
    return cand
