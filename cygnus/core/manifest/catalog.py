"""Loading manifests with trust decisions and rollback protection (architecture §13.3, §13.5).

Trust levels:
  curated          bundled with Cygnus (integrity comes from the installed package itself) or
                   signed with the Cygnus project key
  vendor-signed    signed by a key pinned for the vendor's domain
  unverified       anything else (user-imported, AI-drafted, expired, unknown signer)
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from pydantic import ValidationError

from cygnus.core.errors import CygnusError
from cygnus.core.manifest import signing
from cygnus.core.manifest.schema import Manifest

MAX_MANIFEST_BYTES = 512 * 1024
MAX_SIGNATURE_BYTES = 8 * 1024


class ManifestError(CygnusError):
    pass


@dataclass(frozen=True, slots=True)
class TrustedKey:
    key: signing.PublicKey
    owner: str  # "cygnus-project" or a vendor domain
    level: str  # "curated" | "vendor-signed"


@dataclass(slots=True)
class TrustStore:
    keys: dict[str, TrustedKey] = field(default_factory=dict)

    def add(self, key: signing.PublicKey, owner: str, level: str) -> None:
        if level not in ("curated", "vendor-signed"):
            raise ValueError("invalid trust level")
        known = self.keys.get(key.key_id_hex)
        if known is not None and (known.owner, known.level, known.key) != (owner, level, key):
            # Pin on first use: a key id already trusted for one owner never changes hands silently.
            raise ManifestError(f"key {key.key_id_hex} is already trusted for {known.owner}; "
                                f"refusing to re-assign it to {owner}")
        self.keys[key.key_id_hex] = TrustedKey(key, owner, level)


@dataclass(slots=True, kw_only=True)
class LoadedManifest:
    manifest: Manifest
    trust_level: str
    origin: str
    signer: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def authentic(self) -> bool:
        """It came from Cygnus itself or from a pinned vendor key (even if it has since expired)."""
        return self.signer is not None

    @property
    def can_drive_actions(self) -> bool:
        """Only curated or vendor-signed, unexpired manifests may propose actions."""
        return self.trust_level in ("curated", "vendor-signed")


def _parse(raw: bytes) -> Manifest:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest is too large")
    try:
        return Manifest.model_validate_json(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise ManifestError(f"invalid manifest at {loc}: {first.get('msg')}") from exc
    except Exception as exc:  # noqa: BLE001 - anything else is still just an invalid manifest
        raise ManifestError(f"invalid manifest: {exc}") from exc


def reverse_domain(domain: str) -> str:
    return ".".join(reversed(domain.lower().strip(".").split(".")))


def identifiers_outside_domain(manifest: Manifest, domain: str) -> list[str]:
    """Identifiers the manifest claims that are NOT under the vendor's reverse domain.

    A vendor key proves control of a domain, so it may only speak for applications whose
    reverse-DNS ids live under that domain (e.g. whatpulse.org -> org.whatpulse.*). Package
    names cannot be bound to a domain, so a vendor-signed manifest's package names are only hints.
    """
    prefix = reverse_domain(domain) + "."
    app = manifest.application
    ids = [app.id, *app.appstream_ids, *app.flatpak_ids]
    return [i for i in ids if not (i.lower() + ".").startswith(prefix)]


def load(raw: bytes, *, origin: str, signature: str | None = None, trust: TrustStore | None = None,
         last_serial: int | None = None, now: datetime | None = None) -> LoadedManifest:
    """Validate, authenticate and classify a manifest.

    origin: "bundled" (shipped inside the Cygnus package), "network" or "import".
    last_serial: highest serial previously accepted for this application (rollback protection).
    """
    manifest = _parse(raw)
    now = now or datetime.now(UTC)
    if last_serial is not None and manifest.serial < last_serial:
        raise ManifestError(
            f"manifest serial {manifest.serial} is older than the one already accepted ({last_serial}); "
            "refusing a possible rollback")

    level, signer, warnings = "unverified", None, []
    if origin == "bundled":
        level, signer = "curated", "cygnus-package"
    elif signature is not None and trust is not None:
        key_id = _signature_key_id(signature)
        tk = trust.keys.get(key_id) if key_id else None
        if tk is None:
            warnings.append("signed by an unknown key")
        else:
            signing.verify(raw, signature, tk.key)  # raises SignatureError on mismatch
            vendor_domain = manifest.application.vendor.domain
            if tk.level == "vendor-signed" and tk.owner != vendor_domain:
                warnings.append(f"signed by {tk.owner}, which is not the vendor domain {vendor_domain}")
            elif tk.level == "vendor-signed" and (foreign := identifiers_outside_domain(manifest, tk.owner)):
                warnings.append(f"signed by {tk.owner} but claims identifiers outside that domain: "
                                + ", ".join(foreign))
            else:
                level, signer = tk.level, tk.owner
    elif signature is not None:
        warnings.append("signature present but not checked (no trusted keys available)")
    else:
        warnings.append("manifest is not signed")

    if manifest.expires <= now:
        warnings.append(f"manifest expired on {manifest.expires:%Y-%m-%d}")
        level = "unverified"
    return LoadedManifest(manifest=manifest, trust_level=level, origin=origin, signer=signer, warnings=warnings)


def _signature_key_id(signature: str) -> str | None:
    lines = signature.strip().splitlines()
    if len(lines) < 2:
        return None
    try:
        blob = signing._b64(lines[1], 74, "signature")
    except signing.SignatureError:
        return None
    return blob[2:10][::-1].hex().upper()


def bundled_manifests() -> dict[str, LoadedManifest]:
    """Curated manifests shipped inside the Cygnus package, keyed by application id."""
    out: dict[str, LoadedManifest] = {}
    root = resources.files("cygnus.data") / "manifests"
    for entry in root.iterdir():
        if entry.name.endswith(".json"):
            loaded = load(entry.read_bytes(), origin="bundled")
            out[loaded.manifest.application.id] = loaded
    return out


_TRUST_ORDER = {"curated": 0, "vendor-signed": 1, "package-metadata": 2, "unverified": 3}


def find_for(identifiers: dict[str, str], catalog: dict[str, LoadedManifest]) -> LoadedManifest | None:
    """Match a detected candidate's identity to a manifest; the most trusted manifest always wins,
    so a lower-trust manifest can never shadow a curated one by claiming the same identifiers."""
    values = {v for v in identifiers.values() if v}
    matches = []
    for loaded in catalog.values():
        app = loaded.manifest.application
        known = {app.id, *app.appstream_ids, *app.flatpak_ids}
        if loaded.trust_level == "curated":
            # Only these are bound to the vendor's domain by a signature; package names and desktop-file names
            # of any other manifest are hints, so they must not let it claim someone else's application.
            known |= {*app.package_names, *app.desktop_ids}
        if values & known:
            matches.append(loaded)
    if not matches:
        return None
    return min(matches, key=lambda m: _TRUST_ORDER.get(m.trust_level, 9))


def _read_capped(path: Path, limit: int, error: type[CygnusError], what: str) -> bytes | None:
    """Read a regular file of at most `limit` bytes; None if it does not exist.

    The size is taken from the opened descriptor, so the file cannot grow or be swapped between a
    check and the read, and a huge or special file is refused before its content is read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise error(f"cannot read the {what} {path}: {exc.strerror or exc}") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise error(f"the {what} {path} is not a regular file")
        if info.st_size > limit:
            raise error(f"the {what} {path} is too large")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise error(f"the {what} {path} is too large")
    return data


def load_file(path: Path, *, trust: TrustStore | None = None, last_serial: int | None = None) -> LoadedManifest:
    raw = _read_capped(path, MAX_MANIFEST_BYTES, ManifestError, "manifest")
    if raw is None:
        raise ManifestError(f"manifest {path} does not exist")
    sig_bytes = _read_capped(path.with_name(path.name + ".minisig"), MAX_SIGNATURE_BYTES,
                             signing.SignatureError, "signature file")
    try:
        signature = None if sig_bytes is None else sig_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise signing.SignatureError("the signature file is not text") from exc
    return load(raw, origin="import", signature=signature, trust=trust, last_serial=last_serial)


def dumps_schema() -> str:
    from cygnus.core.manifest.schema import json_schema

    return json.dumps(json_schema(), indent=2, sort_keys=True)
