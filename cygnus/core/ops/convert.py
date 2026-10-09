"""Turn a .deb or .rpm into a local pacman package (architecture §10.3).

Only packages the policy engine marked "convert" are converted: their payload resolves against your
repositories and their install scripts do nothing Arch's own hooks don't already do, so no script is
carried over. pacman then owns every file, and removing the package removes them all.

What changes on the way:
  * Debian/Fedora's /bin, /sbin, /lib, /lib64 and /usr/sbin, /usr/lib64 move under /usr/bin and /usr/lib
    (on Arch those are symlinks owned by the `filesystem` package, so pacman would refuse them);
  * vendor repository, cron, SysV-init and /etc/default files are left out (the analysis lists them);
  * setuid/setgid bits are not carried over (the analysis lists the files that had them);
  * dependencies are the Arch packages that provide the libraries the binaries need.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Collection
from pathlib import Path
from typing import Callable

from cygnus.core import paths
from cygnus.core.backends import foreign, translate
from cygnus.core.errors import CygnusError
from cygnus.core.models import Candidate, PackageFormat
from cygnus.core.util import proc

_MERGE = {"bin": "usr/bin", "sbin": "usr/bin", "usr/sbin": "usr/bin", "lib": "usr/lib", "lib64": "usr/lib",
          "usr/lib64": "usr/lib"}
_DROP = ("etc/apt", "etc/yum.repos.d", "etc/init.d", "etc/default", "etc/cron.d", "etc/cron.daily",
         "etc/cron.hourly", "etc/cron.weekly", "etc/cron.monthly")
_ALLOWED_TOP = ("usr", "etc", "opt")
_PKG_NAME = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]{0,99}\Z")


def pacman_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9@._+-]", "-", name.lower()).lstrip(".-") or "converted"
    return n[:100]


def pacman_version(version: str) -> tuple[str | None, str]:
    """('epoch' or None, pkgver) from a Debian/RPM version; pkgver may not contain '-' or ':'."""
    epoch, _, rest = version.rpartition(":") if ":" in version else ("", "", version)
    upstream = rest.rsplit("-", 1)[0] if "-" in rest else rest
    pkgver = re.sub(r"[^A-Za-z0-9._+]", "_", upstream).strip("._") or "0"
    return (epoch if epoch.isdigit() else None), pkgver


def _sq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


# Directories a package must ship as real folders: Cygnus moves and deletes inside them, and on Arch
# several are owned by the `filesystem` package (a link there would also replace a system directory).
_STRUCTURAL = ("bin", "sbin", "lib", "lib64", "usr", "usr/bin", "usr/sbin", "usr/lib", "usr/lib64", "usr/share",
               "etc", "opt", "var")


def _no_links_on_the_way(root: Path, rel: Path) -> None:
    """Refuse when any directory between `root` and `rel` is a symlink: following one could reach
    files outside the package (e.g. a packaged usr/lib -> ../../../.config)."""
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise CygnusError(f"the package contains a link at /{current.relative_to(root)} that Cygnus will not follow")


def _normalise(root: Path) -> list[str]:
    """Merge legacy directories into /usr, drop excluded files, and refuse anything unexpected.
    Nothing here ever follows a symlink that came from the package."""
    foreign.make_traversable(root)
    for rel in _STRUCTURAL:
        if (root / rel).is_symlink():
            raise CygnusError(f"the package replaces the folder /{rel} with a link; Cygnus does not convert it")
    notes = []
    for src_rel, dst_rel in _MERGE.items():
        src = root / src_rel
        if not src.is_dir():
            continue
        dst = root / dst_rel
        for dirpath, dirnames, filenames in os.walk(src, topdown=True, followlinks=False):
            here = Path(dirpath)
            rel_dir = here.relative_to(src)
            for d in list(dirnames):
                if (here / d).is_symlink():  # move the link itself, never descend into it
                    dirnames.remove(d)
                    filenames.append(d)
                else:
                    target = dst / rel_dir / d
                    _no_links_on_the_way(root, (target / "x").relative_to(root))
                    if target.is_symlink():
                        raise CygnusError(f"/{target.relative_to(root)} is a link; Cygnus will not merge into it")
                    target.mkdir(parents=True, exist_ok=True)
            if filenames:
                _no_links_on_the_way(root, (dst / rel_dir / "x").relative_to(root))
                (dst / rel_dir).mkdir(parents=True, exist_ok=True)
            for f in filenames:
                target = dst / rel_dir / f
                _no_links_on_the_way(root, target.relative_to(root))
                if target.exists() or target.is_symlink():
                    raise CygnusError(f"/{src_rel}/{rel_dir / f} and /{dst_rel}/{rel_dir / f} would be the same file")
                os.rename(here / f, target)  # rename moves a link itself; it never follows it
        shutil.rmtree(src)
        notes.append(f"/{src_rel} moved to /{dst_rel}")
    for rel in _DROP:
        path = root / rel
        _no_links_on_the_way(root, Path(rel))
        if path.is_symlink() or path.is_file():
            path.unlink()
            notes.append(f"/{rel} left out")
        elif path.is_dir():
            shutil.rmtree(path)
            notes.append(f"/{rel} left out")
    if foreign.only_empty_folders(root / "var"):  # empty folders only: makepkg would drop them, the program makes them itself
        shutil.rmtree(root / "var")
        notes.append("/var left out (it held only empty folders; the program creates them when it runs)")
    for top in sorted(p.name for p in root.iterdir()):
        if top not in _ALLOWED_TOP:
            raise CygnusError(f"the package installs into /{top}, which Cygnus does not convert")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for d in dirnames:  # no setuid or setgid on a folder either
            path = Path(dirpath) / d
            mode = os.lstat(path).st_mode
            if not os.path.islink(path) and mode & 0o6000:
                os.chmod(path, mode & ~0o6000, follow_symlinks=False)
        for f in filenames:
            path = Path(dirpath) / f
            mode = os.lstat(path).st_mode
            # No setuid/setgid, and nothing writable by anyone but the owner: extraction already applies the user's umask,
            # but what a package may ship must not depend on what that happens to be.
            if not os.path.islink(path) and mode & 0o6022:
                os.chmod(path, mode & ~0o6022, follow_symlinks=False)
    return notes


# The revision of Cygnus's own conversion, written as the package's release number (155.0.1-<revision>). It goes up whenever
# the converted package comes out differently for the same vendor file (2: menu icons and command links added; 3: nothing of the vendor's is purged or renamed), so that a
# better conversion of the same file is a real upgrade for pacman instead of the same version again.
CONVERSION_REVISION = 3


def _optional_reason(option: dict) -> str:
    files = sorted({re.sub(r"[^A-Za-z0-9._+-]", "", os.path.basename(f)) for f in option.get("files", [])} - {""})
    return ("needed by " + ", ".join(files[:3]) + (" …" if len(files) > 3 else "")) if files else "optional parts"


_PACKAGE_METADATA = {".PKGINFO", ".BUILDINFO", ".MTREE", ".INSTALL", ".CHANGELOG"}


def _tree_snapshot(root: Path) -> dict[str, tuple[str, int, str]]:
    """path -> (kind, size, link target) for everything under `root`: kind d (folder), l (link) or f (file); the size only for
    files and the target only for links."""
    out: dict[str, tuple[str, int, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in [*dirnames, *filenames]:
            path = here / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                out[rel] = ("l", 0, os.readlink(path))
            elif name in dirnames:
                out[rel] = ("d", 0, "")
            else:
                out[rel] = ("f", os.lstat(path).st_size, "")
    return out


def _package_snapshot(pkg: Path) -> dict[str, tuple[str, int, str]]:
    """The same, read from the built package. A hard link counts as a file whose size is not compared (it has none here)."""
    listing = proc.run(["bsdtar", "-tvf", str(pkg)], timeout=600, max_output=64 << 20)
    if not listing.ok or listing.truncated:
        raise CygnusError("the converted package cannot be listed")
    out: dict[str, tuple[str, int, str]] = {}
    for letter, size, name in foreign.parse_listing(listing.stdout):
        kind = {"d": "d", "l": "l", "h": "h"}.get(letter, "f")
        target = ""
        if kind == "l":
            name, _, target = name.partition(" -> ")
        elif kind == "h":
            name = name.split(" link to ", 1)[0]
        name = re.sub(r"^(\./)+", "", name).rstrip("/")
        if name and name not in _PACKAGE_METADATA:
            out[name] = (kind, size if kind == "f" else 0, target)
    return out


def compare_with_tree(root: Path, pkg: Path) -> list[str]:
    """What differs between the tree Cygnus meant to ship and the package makepkg built (empty when they are the same). This is
    the guard against anything being lost or changed silently on the way: a file, a link (and where it points), a folder (even
    an empty one), a file that came out a different size."""
    meant, built = _tree_snapshot(root), _package_snapshot(pkg)
    problems = [f"/{p} is missing" for p in sorted(meant.keys() - built.keys())]
    problems += [f"/{p} was added" for p in sorted(built.keys() - meant.keys())]
    kinds = {"f": "file", "l": "link", "d": "folder"}
    for path in sorted(meant.keys() & built.keys()):
        (mk, ms, mt), (bk, bs, bt) = meant[path], built[path]
        if bk == "h":  # a hard link: a file that has no size of its own in the listing
            bk, bs = "f", ms
        if mk != bk:
            problems.append(f"/{path} became a {kinds[bk]} (it was a {kinds[mk]})")
        elif mk == "f" and ms != bs:
            problems.append(f"/{path} changed size ({ms} became {bs})")
        elif mk == "l" and mt != bt:
            problems.append(f"/{path} points somewhere else ({mt} became {bt})")
    return problems


def convert(cand: Candidate, verdict: foreign.ForeignVerdict, out_dir: Path,
            progress: Callable[[str], None] = lambda _: None,
            install_optional: Collection[str] = (), accept_unread_scripts: bool = False,
            owner: Callable[[str], str | None] | None = None) -> tuple[Path, list[str]]:
    """Build the pacman package; returns (package file, notes about what changed). The optional libraries the analysis
    found (only some parts of the program want them) become real dependencies when named in `install_optional`, and
    "optional dependencies" (which pacman lists after installing) otherwise."""
    if cand.format not in (PackageFormat.DEB, PackageFormat.RPM):
        raise CygnusError("only .deb and .rpm files are converted")
    # Install scripts are never run, so one Cygnus could not read may be gone past by someone who knows that; anything it
    # recognised as dangerous (a user to create, a kernel module...) never can.
    unread_ok = accept_unread_scripts and verdict.strategy == "review" and verdict.scripts_acknowledgeable
    if verdict.strategy != "convert" and not unread_ok:
        raise CygnusError("Cygnus only converts packages its analysis found safe to convert "
                          f"(this one: {verdict.summary})")
    name = pacman_name(cand.name or Path(cand.source).stem)
    epoch, pkgver = pacman_version(cand.version or "0")
    arch = "any" if cand.arch in ("all", "noarch", "any") else "x86_64"
    deps = [d for d in verdict.repo_dependencies if _PKG_NAME.match(d)]
    optional = {o["package"]: o for o in verdict.optional_dependencies if _PKG_NAME.match(o["package"])}
    unknown = sorted(set(install_optional) - set(optional))
    if unknown:  # only what the analysis offered, never a name that arrived from somewhere else
        raise CygnusError(f"{', '.join(unknown)} is not an optional library of this package")
    deps += [n for n in optional if n in install_optional and n not in deps]
    optdeps = [f"{n}: " + _optional_reason(o) for n, o in optional.items() if n not in install_optional and n not in deps]
    work_root = paths.cache_dir() / "convert"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=work_root) as tmp:
        work = Path(tmp)
        root = work / "root"
        root.mkdir()
        progress("Unpacking…")
        foreign.extract_payload(cand, root)
        notes = _normalise(root)
        # What the vendor's scripts did that can be done safely without running them (the menu icon, a command link), made
        # from the package's own files only; the final scan below covers them too.
        notes += translate.apply(root, translate.plan(root, foreign.install_script_texts(cand), owner))
        if unread_ok:
            notes.append("install scripts that Cygnus could not read were not run")
        # The tree that pacman will own is checked again, whatever the earlier analysis said.
        blocked = [s for s in foreign.privileged_paths(root) if s.blocks]
        if blocked:
            raise CygnusError("the package ships files that run with administrator rights, so it is not converted: "
                              + "; ".join(f"/{s.path} ({s.reason})" for s in blocked[:4]))
        summary = re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]", " ", cand.summary or name)
        desc = f"{summary[:200]} (converted from {Path(cand.source).name} by Cygnus)"
        lines = [
            f"pkgname={name}", f"pkgver={pkgver}", f"pkgrel={CONVERSION_REVISION}", *( [f"epoch={epoch}"] if epoch else []),
            f"pkgdesc={_sq(desc)}", f"arch=({arch})", "license=('custom:unknown')",
            f"depends=({' '.join(_sq(d) for d in deps)})",
            *([f"optdepends=({' '.join(_sq(d) for d in optdeps)})"] if optdeps else []),
            # Ship the vendor's files exactly as they are. makepkg's own defaults would otherwise delete libtool (.la) files,
            # .pod files, usr/share/info/dir and EMPTY FOLDERS, and rename man pages to .gz.
            "options=(!strip !debug !lto docs libtool staticlibs emptydirs !purge !zipman)",
            "package() {",
            '  cp -a --no-preserve=ownership "$startdir/root/." "$pkgdir/"',
            "}",
        ]
        (work / "PKGBUILD").write_text("\n".join(lines) + "\n")
        out_dir.mkdir(parents=True, exist_ok=True)
        fresh = work / "pkgdest"  # only what this build made (never an older file with a similar name)
        fresh.mkdir()
        env = proc.clean_env({"PKGDEST": str(fresh), "BUILDDIR": str(work / "build"),
                              "SRCDEST": str(work / "src"), "LOGDEST": str(work)})
        progress("Building the pacman package…")
        res = proc.run(["makepkg", "--nodeps", "--noconfirm", "--force", "--noextract"], timeout=1800,
                       cwd=str(work), env=env, max_output=8 << 20)
        if res.returncode != 0:
            raise CygnusError("makepkg failed: " + (res.stderr or res.stdout).strip().splitlines()[-1][:300])
        built = sorted(fresh.glob(f"{name}-*.pkg.tar.*"))
        if len(built) != 1:
            raise CygnusError("makepkg produced no package" if not built else "makepkg produced more than one package")
        differences = compare_with_tree(root, built[0])
        if differences:
            raise CygnusError("the converted package does not hold exactly what Cygnus meant to ship, so it is not used: "
                              + "; ".join(differences[:8]) + (f"; and {len(differences) - 8} more" if len(differences) > 8 else ""))
        final = out_dir / built[0].name
        final.unlink(missing_ok=True)
        shutil.move(built[0], final)
    return final, notes
