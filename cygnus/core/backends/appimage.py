"""AppImage trust, updates and prerequisites (architecture §2.6, §7.1, §17)."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cygnus.core.detect import elf
from cygnus.core.recovery.model import Issue, IssueSeverity, Resolution, SafetyClass, explain_only
from cygnus.core.storage import mountinfo
from cygnus.core.util import http, proc

SIG_SECTIONS = (".sha256_sig", ".sig_key")


# -- signatures -------------------------------------------------------------------------------------
def _sections(path: Path) -> dict[str, elf.Section]:
    with open(path, "rb") as fh:
        info = elf.read_elf(fh, path.stat().st_size)
    return {s.name: s for s in info.sections}


def signed_digest(path: Path, sections: dict[str, elf.Section] | None = None) -> str:
    """SHA-256 (hex) of the file with the signature sections zero-filled — the data AppImage signatures cover."""
    sections = sections or _sections(path)
    skip = sorted((s.offset, s.size) for name, s in sections.items() if name in SIG_SECTIONS)
    h, pos = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for off, size in skip:
            f.seek(pos)
            remaining = off - pos
            while remaining > 0:
                block = f.read(min(remaining, 1 << 20))
                if not block:
                    break
                h.update(block)
                remaining -= len(block)
            h.update(b"\0" * size)
            pos = off + size
        f.seek(pos)
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


@dataclass(slots=True, kw_only=True)
class SignatureResult:
    status: str  # verified | unsigned | bad-signature | wrong-key | error
    detail: str
    signer_fingerprint: str | None = None


def _gpg(home: str, *args: str, timeout: float = 60) -> proc.Result:
    return proc.run(["gpg", "--homedir", home, "--batch", "--no-autostart", "--no-tty", *args], timeout=timeout)


def verify_signature(path: Path, pinned_fingerprint: str) -> SignatureResult:
    """Verify the embedded OpenPGP signature against a *pinned* key fingerprint.

    The key embedded in the AppImage only supplies key material: it is trusted solely if its
    fingerprint equals the one pinned by a manifest.
    """
    pinned = pinned_fingerprint.replace(" ", "").upper()
    sections = _sections(path)
    blobs = {}
    with open(path, "rb") as f:
        for name in SIG_SECTIONS:
            sec = sections.get(name)
            blobs[name] = elf.read_section(f, sec).rstrip(b"\0") if sec else b""
    if not blobs[".sha256_sig"] or not blobs[".sig_key"]:
        return SignatureResult(status="unsigned", detail="the AppImage carries no embedded signature")
    if proc.which("gpg") is None:
        return SignatureResult(status="error", detail="gpg is not installed")
    digest = signed_digest(path, sections)
    home = tempfile.mkdtemp(prefix="cygnus-gpg-")
    try:
        os.chmod(home, 0o700)
        Path(home, "key.asc").write_bytes(blobs[".sig_key"])
        Path(home, "sig.asc").write_bytes(blobs[".sha256_sig"])
        Path(home, "data").write_text(digest)
        imp = _gpg(home, "--import", str(Path(home, "key.asc")))
        if not imp.ok:
            return SignatureResult(status="error", detail=f"embedded key could not be read: {imp.stderr.strip()[:200]}")
        res = _gpg(home, "--status-fd", "1", "--verify", str(Path(home, "sig.asc")), str(Path(home, "data")))
        status_words = {line.split()[1] for line in res.stdout.splitlines() if line.startswith("[GNUPG:] ")
                        and len(line.split()) > 1}
        bad = status_words & {"BADSIG", "ERRSIG", "REVKEYSIG", "EXPKEYSIG", "KEYREVOKED", "KEYEXPIRED", "EXPSIG"}
        if bad:
            return SignatureResult(status="bad-signature" if "BADSIG" in bad else "error",
                                   detail="signature rejected: " + ", ".join(sorted(bad)))
        valid = [line.split() for line in res.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
        if not valid:
            bad = "BADSIG" in res.stdout
            return SignatureResult(status="bad-signature" if bad else "error",
                                   detail="the signature does not match the file" if bad
                                   else f"gpg could not verify: {res.stderr.strip()[:200]}")
        fields = valid[0]
        signing_fpr = fields[2].upper()
        primary_fpr = fields[11].upper() if len(fields) > 11 else signing_fpr
        if pinned not in (primary_fpr, signing_fpr):
            return SignatureResult(status="wrong-key", signer_fingerprint=primary_fpr,
                                   detail=f"signed by {primary_fpr}, but the vendor's key is {pinned}")
        return SignatureResult(status="verified", signer_fingerprint=primary_fpr,
                               detail=f"good signature by the vendor key {pinned}")
    finally:
        proc.run(["gpgconf", "--homedir", home, "--kill", "all"], timeout=15)
        shutil.rmtree(home, ignore_errors=True)


# -- updates ----------------------------------------------------------------------------------------
@dataclass(slots=True, kw_only=True)
class UpdateCheck:
    status: str  # up-to-date | update-available | unknown | unsupported
    detail: str
    available_version: str | None = None
    download_url: str | None = None
    expected_sha256: str | None = None
    expected_sha1: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)


def parse_zsync_header(data: bytes) -> dict[str, str]:
    header: dict[str, str] = {}
    for raw in data.split(b"\n"):
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            break
        key, sep, value = line.partition(":")
        if sep:
            header[key.strip()] = value.strip()
    return header


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def check_zsync(path: Path, url: str, fetch: Callable[..., bytes] = http.get) -> UpdateCheck:
    header = parse_zsync_header(fetch(url, range_end=8191))
    if "SHA-1" not in header or "Length" not in header or not header["Length"].isdigit() \
            or not re.fullmatch(r"[0-9a-fA-F]{40}", header["SHA-1"]):
        return UpdateCheck(status="unknown", detail="the update information file is not a valid zsync control file")
    remote_len = int(header["Length"])
    target = header.get("URL") or header.get("Filename") or ""
    download = target if target.startswith("https://") else url.rsplit("/", 1)[0] + "/" + target
    facts = {"remote_length": remote_len, "remote_mtime": header.get("MTime"), "zsync": url}
    if path.stat().st_size == remote_len and _sha1(path) == header["SHA-1"].lower():
        return UpdateCheck(status="up-to-date", detail="matches the vendor's current build", facts=facts)
    return UpdateCheck(status="update-available", detail=f"the vendor published a different build ({header.get('MTime')})",
                       download_url=download, expected_sha1=header["SHA-1"].lower(), facts=facts)


_PRE = re.compile(r"(alpha|beta|rc|pre|dev|a|b)(\d*)$", re.I)
_FUSED_PRE = re.compile(r"(\d+)(alpha|beta|rc|pre|dev|a|b)(\d*)", re.I)
_PRE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 3, "rc": 3}


def version_key(v: str) -> tuple:
    """Total order for release versions: numeric parts compared as numbers, pre-releases (beta, rc…)
    sort *before* the final release, and ints are never compared with strings."""
    v = (v or "").strip().lstrip("vV")
    main, _, _build = v.partition("+")
    parts = re.split(r"[.\-_]", main)
    nums: list[int] = []
    pre: tuple = (1,)  # final release
    in_pre = False
    for part in parts:
        if part.isdigit():
            if in_pre:  # "beta.1": the number belongs to the pre-release tag
                pre = (pre[0], pre[1], pre[2] * 1000 + int(part))
            else:
                nums.append(int(part))
        elif (m := _PRE.match(part)):
            pre = (0, _PRE_RANK.get(m.group(1).lower(), 0), int(m.group(2) or 0))
            in_pre = True
        elif not in_pre and (m := _FUSED_PRE.fullmatch(part)):  # "0rc1" as in "1.0rc1"
            nums.append(int(m.group(1)))
            pre = (0, _PRE_RANK[m.group(2).lower()], int(m.group(3) or 0))
            in_pre = True
        elif part:
            digits = re.match(r"(\d+)", part)
            if digits:
                nums.append(int(digits.group(1)))
    while nums and nums[-1] == 0:
        nums.pop()
    return (tuple(nums), pre)


def check_github(owner: str, repo: str, tag: str, filename_glob: str, current_version: str | None,
                 fetch_json: Callable[..., Any] = http.get_json) -> UpdateCheck:
    endpoint = (f"https://api.github.com/repos/{owner}/{repo}/releases/latest" if tag == "latest"
                else f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}")
    rel = fetch_json(endpoint, headers={"Accept": "application/vnd.github+json"})
    target_glob = filename_glob.removesuffix(".zsync")
    assets = [a for a in rel.get("assets", []) if fnmatch.fnmatch(a.get("name", ""), target_glob)]
    if not assets:
        return UpdateCheck(status="unknown", detail=f"no release asset matches {target_glob}")
    asset = assets[0]
    version = (rel.get("tag_name") or "").lstrip("v")
    digest = (asset.get("digest") or "").removeprefix("sha256:") or None
    facts = {"release": rel.get("tag_name"), "asset": asset.get("name"), "published": rel.get("published_at")}
    if current_version and version_key(version) <= version_key(current_version):
        return UpdateCheck(status="up-to-date", detail=f"{current_version} is the latest release", facts=facts,
                           available_version=version)
    return UpdateCheck(status="update-available", detail=f"version {version} is available", facts=facts,
                       available_version=version, download_url=asset.get("browser_download_url"),
                       expected_sha256=digest)


def check_update(path: Path, update_info: dict[str, str] | None, current_version: str | None, **kw) -> UpdateCheck:
    if not update_info:
        return UpdateCheck(status="unsupported", detail="this AppImage has no update information")
    kind = update_info.get("type")
    if kind == "zsync":
        return check_zsync(path, update_info["url"], **({"fetch": kw["fetch"]} if "fetch" in kw else {}))
    if kind in ("gh-releases-zsync", "gh-releases-direct"):
        return check_github(update_info["owner"], update_info["repo"], update_info["tag"], update_info["filename"],
                            current_version, **({"fetch_json": kw["fetch_json"]} if "fetch_json" in kw else {}))
    return UpdateCheck(status="unsupported", detail=f"update method {kind!r} is not supported yet")


# -- prerequisites ----------------------------------------------------------------------------------
def prerequisite_issues(path: Path, mounts: list[mountinfo.Mount] | None = None) -> list[Issue]:
    problems = []
    if not (proc.which("fusermount3") or proc.which("fusermount")):
        problems.append("no FUSE helper (fusermount3/fusermount) is installed")
    if not os.access("/dev/fuse", os.R_OK | os.W_OK):
        problems.append("/dev/fuse is not accessible")
    mounts = mounts if mounts is not None else mountinfo.read()
    m = mountinfo.mount_for_path(mounts, os.path.abspath(path))
    if m is not None and "noexec" in m.options:
        problems.append(f"{m.mountpoint} is mounted noexec, so programs cannot run from it")
    if not problems:
        return []
    return [Issue(code="APPIMAGE_RUNTIME_UNAVAILABLE", severity=IssueSeverity.BLOCKER,
                  title="AppImages cannot start on this system as configured",
                  explanation="; ".join(problems),
                  resolutions=[
                      Resolution(id="extract-and-run", title="Run in extract-and-run mode",
                                 explanation="The AppImage unpacks itself to a temporary folder on each start "
                                             "instead of mounting with FUSE (slower start).",
                                 safety=SafetyClass.AUTO, rank=30),
                      explain_only("fix-system", "Fix the cause", "Install fuse3 (Settings, under Optional parts, does it for "
                                                                  "you), or store the AppImage on a drive that allows "
                                                                  "running programs."),
                  ])]
