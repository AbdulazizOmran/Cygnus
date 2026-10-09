"""Flatpak inputs: single-file bundles, .flatpakref and .flatpakrepo files."""

from __future__ import annotations

import base64
import binascii
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path

from cygnus.core.errors import DetectionError
from cygnus.core.models import Candidate, PackageFormat, Severity

# flatpak build-bundle writes the a{sv} key "flatpak" with uint32 0xe5890001 first,
# precisely so that bundles can be sniffed (see flatpak-builtins-build-bundle.c).
BUNDLE_MAGIC = b"flatpak\x00\x01\x00\x89\xe5"
_MAX_KEYFILE = 256 * 1024
_MAX_APPSTREAM = 8 * 1024 * 1024


def _keyfile_groups(text: str) -> dict[str, dict[str, str]]:
    """Parse with GLib.KeyFile — the parser Flatpak itself uses — so Cygnus sees exactly what flatpak sees."""
    try:
        import gi

        from gi.repository import GLib
    except ImportError as exc:
        raise DetectionError(f"GLib introspection is unavailable: {exc}") from exc
    kf = GLib.KeyFile()
    try:
        kf.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
    except GLib.Error as exc:
        raise DetectionError(f"invalid key file: {exc.message}") from exc
    groups: dict[str, dict[str, str]] = {}
    for group in kf.get_groups()[0]:
        groups[group] = {key: kf.get_value(group, key) for key in kf.get_keys(group)[0]}
    return groups


def parse_metadata(text: str) -> dict[str, dict[str, str]]:
    return _keyfile_groups(text)


def _valid_gpg_key(value: str | None) -> bool:
    if not value:
        return False
    try:
        data = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    # An OpenPGP transferable public key starts with a public-key packet (tag 6), old or new format.
    return len(data) > 32 and data[0] in (0x98, 0x99, 0xC6)


def inspect_ref_file(path: Path, text: str) -> Candidate:
    groups = _keyfile_groups(text)
    if "Flatpak Ref" in groups:
        sec = groups["Flatpak Ref"]
        cand = Candidate(format=PackageFormat.FLATPAK_REF_FILE, source=str(path))
        cand.name = sec.get("Title") or sec.get("Name")
        cand.identity["flatpak_id"] = sec.get("Name", "")
        cand.summary = sec.get("Comment") or sec.get("Description")
        kind = "runtime" if sec.get("IsRuntime", "false").lower() == "true" else "app"
        cand.identity["flatpak_ref"] = f"{kind}/{sec.get('Name', '')}//{sec.get('Branch', 'stable')}"
        has_key = _valid_gpg_key(sec.get("GPGKey"))
        cand.metadata.update({
            "branch": sec.get("Branch"),
            "remote_url": sec.get("Url"),
            "suggest_remote_name": sec.get("SuggestRemoteName"),
            "runtime_repo": sec.get("RuntimeRepo"),
            "has_gpg_key": has_key,
            "homepage": sec.get("Homepage"),
        })
        if not has_key:
            cand.add("FLATPAKREF_NO_GPG", Severity.WARNING,
                     "The .flatpakref does not pin a valid GPG key for its remote")
        return cand
    if "Flatpak Repo" in groups:
        sec = groups["Flatpak Repo"]
        cand = Candidate(format=PackageFormat.FLATPAK_REPO_FILE, source=str(path))
        cand.name = sec.get("Title") or path.stem
        cand.metadata.update({"remote_url": sec.get("Url"), "has_gpg_key": _valid_gpg_key(sec.get("GPGKey")),
                              "homepage": sec.get("Homepage"), "comment": sec.get("Comment")})
        return cand
    raise DetectionError("key file is neither a .flatpakref nor a .flatpakrepo")


def inspect_bundle(path: Path) -> Candidate:
    """Read bundle metadata via libflatpak (GObject introspection); nothing is installed."""
    try:
        import gi

        gi.require_version("Flatpak", "1.0")
        from gi.repository import Flatpak, Gio, GLib
    except (ImportError, ValueError) as exc:
        raise DetectionError(f"libflatpak introspection is unavailable: {exc}") from exc
    try:
        bundle = Flatpak.BundleRef.new(Gio.File.new_for_path(str(path)))
    except GLib.Error as exc:
        raise DetectionError(f"not a valid Flatpak bundle: {exc.message}") from exc

    cand = Candidate(format=PackageFormat.FLATPAK_BUNDLE, source=str(path))
    kind = "runtime" if bundle.get_kind() == Flatpak.RefKind.RUNTIME else "app"
    ref = bundle.format_ref()
    cand.name = bundle.get_name()
    cand.arch = bundle.get_arch()
    cand.identity["flatpak_id"] = bundle.get_name()
    cand.identity["flatpak_ref"] = ref
    cand.installed_size = bundle.get_installed_size() or None
    meta_bytes = bundle.get_metadata()
    meta_text = meta_bytes.get_data().decode("utf-8", "replace") if meta_bytes else ""
    if len(meta_text) > _MAX_KEYFILE:  # never cut: what is cut off could be the permissions
        raise DetectionError(f"the bundle's metadata is implausibly large ({len(meta_text)} bytes); not inspected")
    meta = parse_metadata(meta_text) if meta_text else {}
    group = "Runtime" if kind == "runtime" else "Application"
    runtime = meta.get(group, {}).get("runtime")
    cand.metadata.update({
        "kind": kind,
        "branch": bundle.get_branch(),
        "commit": bundle.get_commit(),
        "origin_url": bundle.get_origin() or None,
        "runtime_repo": bundle.get_runtime_repo_url() or None,
        "runtime": runtime,
        "sdk": meta.get(group, {}).get("sdk"),
        "command": meta.get(group, {}).get("command"),
        "permissions": meta.get("Context", {}),
        "session_bus": meta.get("Session Bus Policy", {}),
        "system_bus": meta.get("System Bus Policy", {}),
        "extensions": sorted(s.removeprefix("Extension ") for s in meta if s.startswith("Extension ")),
    })
    if runtime:
        cand.depends = [f"runtime/{runtime}"]
    if not bundle.get_origin():
        cand.add("FP_BUNDLE_NO_UPDATE_SOURCE", Severity.WARNING,
                 "This bundle has no update source: Flatpak cannot update it automatically")
    appstream = bundle.get_appstream()
    if appstream is not None:
        _apply_appstream(cand, appstream.get_data())
    return cand


def _apply_appstream(cand: Candidate, gz: bytes) -> None:
    try:
        # Bounded decompression: never inflate more than _MAX_APPSTREAM bytes.
        data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(gz, _MAX_APPSTREAM + 1)
        if len(data) > _MAX_APPSTREAM:
            return
        root = ET.fromstring(data)
    except (zlib.error, ET.ParseError, ValueError, LookupError):
        return
    comp = root.find("component") if root.tag == "components" else root
    if comp is None:
        return
    if (name := comp.findtext("name")):
        cand.name = name.strip()
    if (summary := comp.findtext("summary")):
        cand.summary = summary.strip()
    if (cid := comp.findtext("id")):
        cand.identity["appstream_id"] = cid.strip()
    rel = comp.find("releases/release")
    if rel is not None and rel.get("version"):
        cand.version = rel.get("version")
    dev = comp.find("developer")
    vendor = (dev.findtext("name") if dev is not None else None) or comp.findtext("developer_name")
    if vendor:
        cand.metadata["vendor"] = vendor.strip()
    if (lic := comp.findtext("project_license")):
        cand.metadata["license"] = lic.strip()


def candidate_from_metadata(*, ref: str, remote: str, metadata_text: str, download_size: int | None = None,
                            installed_size: int | None = None, eol: str | None = None) -> Candidate:
    """A Candidate for an app offered by a configured remote (same fields as a bundle's)."""
    parts = ref.split("/")
    if len(parts) != 4 or not all(parts):
        raise DetectionError(f"not a Flatpak ref: {ref!r}")
    kind, name, arch, branch = parts
    if len(metadata_text or "") > _MAX_KEYFILE:
        raise DetectionError(f"the metadata of {name} is implausibly large; not inspected")
    meta = parse_metadata(metadata_text) if metadata_text else {}
    group = "Runtime" if kind == "runtime" else "Application"
    runtime = meta.get(group, {}).get("runtime")
    cand = Candidate(format=PackageFormat.FLATPAK_REMOTE, source=f"{remote}:{ref}")
    cand.name, cand.arch = name, arch
    cand.identity.update(flatpak_id=name, flatpak_ref=ref)
    cand.installed_size = installed_size or None
    cand.metadata.update({
        "kind": kind, "branch": branch, "remote": remote, "download_size": download_size, "eol": eol,
        "runtime": runtime, "sdk": meta.get(group, {}).get("sdk"), "command": meta.get(group, {}).get("command"),
        "permissions": meta.get("Context", {}), "session_bus": meta.get("Session Bus Policy", {}),
        "system_bus": meta.get("System Bus Policy", {}),
        "extensions": sorted(x.removeprefix("Extension ") for x in meta if x.startswith("Extension ")),
        "origin_url": "remote",  # updates come from the remote it is installed from
    })
    if runtime:
        cand.depends = [f"runtime/{runtime}"]
    return cand
