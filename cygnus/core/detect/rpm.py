"""RPM package inspection: bounds-checked lead/header parser; rpm(8) is not required."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from cygnus.core.errors import DetectionError
from cygnus.core.models import Candidate, PackageFormat, Severity

RPM_LEAD_MAGIC = b"\xed\xab\xee\xdb"
HEADER_MAGIC = b"\x8e\xad\xe8\x01"
_MAX_INDEX = 65536
_MAX_STORE = 64 * 1024 * 1024

# Header tags (rpmtag.h)
TAGS = {
    1000: "name", 1001: "version", 1002: "release", 1003: "epoch", 1004: "summary",
    1009: "size", 1014: "license", 1015: "packager", 1020: "url", 1021: "os", 1022: "arch",
    1023: "prein", 1024: "postin", 1025: "preun", 1026: "postun",
    1047: "providename", 1048: "requireflags", 1049: "requirename", 1050: "requireversion",
    1053: "conflictflags", 1054: "conflictname", 1055: "conflictversion",
    1065: "triggerscripts", 1085: "preinprog", 1086: "postinprog", 1087: "preunprog", 1088: "postunprog",
    1092: "triggerscriptprog", 1153: "pretransprog", 1154: "posttransprog", 5067: "filetriggerscriptprog",
    5077: "transfiletriggerscriptprog", 5109: "sysusers",
    1090: "obsoletename", 1112: "provideflags", 1113: "provideversion",
    1124: "payloadformat", 1125: "payloadcompressor", 1151: "pretrans", 1152: "posttrans",
    5046: "recommendname", 5049: "suggestname", 5052: "supplementname", 5055: "enhancename",
    5066: "filetriggerscripts", 5076: "transfiletriggerscripts",
}
SIGNATURE_TAGS = {267: "dsa", 268: "rsa", 269: "sha1", 273: "sha256", 1002: "pgp", 1004: "md5", 1006: "gpg"}
SCRIPT_TAGS = ("pretrans", "prein", "postin", "preun", "postun", "posttrans", "triggerscripts",
               "filetriggerscripts", "transfiletriggerscripts")
RPMSENSE_LESS, RPMSENSE_GREATER, RPMSENSE_EQUAL = 0x02, 0x04, 0x08
RPMSENSE_RPMLIB = 1 << 24
RPM_ARCH_TO_ARCH = {"x86_64": "x86_64", "noarch": "any", "aarch64": "aarch64", "i686": "i686", "i386": "i686"}


# Expected shape per tag: str (STRING or the first entry of I18NSTRING), strs, ints, bin.
_STR_TAGS = {"name", "version", "release", "summary", "license", "packager", "url", "os", "arch", "prein",
             "postin", "preun", "postun", "preinprog", "postinprog", "preunprog", "postunprog", "payloadformat",
             "payloadcompressor", "pretrans", "posttrans", "pretransprog", "posttransprog"}
_INT_TAGS = {"epoch", "size", "requireflags", "conflictflags", "provideflags"}
_BIN_TAGS = {"dsa", "rsa", "sha1", "sha256", "pgp", "md5", "gpg"}
_MAX_DECODED_ITEMS = 200_000


def _program(value: Any) -> str | None:
    """The program a scriptlet runs with. A list (one per trigger) gives the first program that is not a
    shell, if there is one, so a single odd trigger is never hidden behind shell ones."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return next((v for v in value if v.split()[:1] and v.split()[0].rsplit("/", 1)[-1] not in
                     ("sh", "bash", "dash")), value[0])
    return None


def _read_header(fh, pos: int, file_size: int, tag_names: dict[int, str]) -> tuple[dict[str, Any], int]:
    fh.seek(pos)
    pre = fh.read(16)
    if len(pre) < 16 or pre[:4] != HEADER_MAGIC:
        raise DetectionError("corrupt RPM header")
    nindex, hsize = struct.unpack(">II", pre[8:16])
    if nindex > _MAX_INDEX or hsize > _MAX_STORE:
        raise DetectionError("RPM header is implausibly large")
    end = pos + 16 + nindex * 16 + hsize
    if end > file_size:
        raise DetectionError("RPM header extends beyond end of file")
    index = fh.read(nindex * 16)
    store = fh.read(hsize)
    values: dict[str, Any] = {}
    seen: set[int] = set()
    budget = _MAX_DECODED_ITEMS
    for i in range(nindex):
        tag, typ, off, count = struct.unpack_from(">iIiI", index, i * 16)
        if tag in seen:
            raise DetectionError(f"RPM header repeats tag {tag}")  # real headers never do
        seen.add(tag)
        name = tag_names.get(tag)
        if name is None:
            continue
        if off < 0 or off >= len(store) or count == 0:
            continue
        if typ in (6, 8, 9) and count > len(store) - off:  # every string needs at least one byte
            raise DetectionError("RPM header string count exceeds its data")
        budget -= count
        if budget < 0:
            raise DetectionError("RPM header is implausibly complex")
        value = _decode(store, typ, off, count)
        value = _coerce(name, value)
        if value is not None:
            values[name] = value
    return values, end


def _strings(store: bytes, off: int, count: int) -> list[str]:
    out = []
    for _ in range(count):
        end = store.find(b"\0", off)
        if end < 0:
            break
        out.append(store[off:end].decode("utf-8", "replace"))
        off = end + 1
    return out


def _decode(store: bytes, typ: int, off: int, count: int) -> Any:
    try:
        if typ == 6:  # STRING
            strings = _strings(store, off, 1)
            return strings[0] if strings else None
        if typ in (8, 9):  # STRING_ARRAY, I18NSTRING
            return _strings(store, off, count)
        if typ == 4:  # INT32
            return list(struct.unpack_from(f">{count}I", store, off))
        if typ == 5:  # INT64
            return list(struct.unpack_from(f">{count}Q", store, off))
        if typ == 3:  # INT16
            return list(struct.unpack_from(f">{count}H", store, off))
        if typ in (1, 2):  # CHAR, INT8
            return list(store[off : off + count])
        if typ == 7:  # BIN
            return store[off : off + count]
    except (struct.error, IndexError):
        return None
    return None


def _coerce(name: str, value: Any) -> Any:
    """Normalise to the shape inspect() expects; drop values whose declared type is wrong."""
    if name in _STR_TAGS:
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return value[0]  # I18NSTRING: first (default) locale
        return value if isinstance(value, str) else None
    if name in _INT_TAGS:
        return value if isinstance(value, list) and all(isinstance(v, int) for v in value) else None
    if name in _BIN_TAGS:
        return value if isinstance(value, (bytes, list)) else None
    # string-array tags (dependencies, scriptlet arrays for triggers, ...)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    return None


def _deps(names: list[str] | None, flags: list[int] | None, versions: list[str] | None,
          *, skip_internal: bool) -> list[str]:
    names = names or []
    flags = flags or [0] * len(names)
    versions = versions or [""] * len(names)
    if not len(names) == len(flags) == len(versions):  # zip() would silently drop entries (e.g. conflicts)
        raise DetectionError("the package's dependency lists do not match up")
    out = []
    for name, flag, ver in zip(names, flags, versions):
        if skip_internal and (name.startswith("rpmlib(") or flag & RPMSENSE_RPMLIB):
            continue
        op = ""
        if flag & RPMSENSE_LESS:
            op = "<="if flag & RPMSENSE_EQUAL else "<"
        elif flag & RPMSENSE_GREATER:
            op = ">=" if flag & RPMSENSE_EQUAL else ">"
        elif flag & RPMSENSE_EQUAL:
            op = "="
        out.append(f"{name} {op} {ver}" if op and ver else name)
    return list(dict.fromkeys(out))


def inspect(path: Path) -> Candidate:
    try:
        return _inspect(path)
    except DetectionError:
        raise
    except Exception as exc:  # noqa: BLE001 - any structural surprise in a hostile file
        raise DetectionError(f"malformed RPM header: {exc}") from exc


def _inspect(path: Path) -> Candidate:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        lead = fh.read(96)
        if len(lead) < 96 or lead[:4] != RPM_LEAD_MAGIC:
            raise DetectionError("not an RPM package")
        if lead[4] < 3:
            raise DetectionError("unsupported RPM format version")
        sig, sig_end = _read_header(fh, 96, size, SIGNATURE_TAGS)
        main_pos = sig_end + ((8 - sig_end % 8) % 8)
        hdr, _ = _read_header(fh, main_pos, size, TAGS)

    cand = Candidate(format=PackageFormat.RPM, source=str(path))
    cand.name = hdr.get("name")
    epoch = (hdr.get("epoch") or [None])[0]
    ver, rel = hdr.get("version"), hdr.get("release")
    cand.version = f"{f'{epoch}:' if epoch else ''}{ver}-{rel}" if ver else None
    rpm_arch = hdr.get("arch")
    cand.arch = RPM_ARCH_TO_ARCH.get(rpm_arch or "", rpm_arch)
    cand.summary = hdr.get("summary")
    cand.depends = _deps(hdr.get("requirename"), hdr.get("requireflags"), hdr.get("requireversion"),
                         skip_internal=True)
    cand.optional_depends = list(dict.fromkeys((hdr.get("recommendname") or []) + (hdr.get("suggestname") or [])))
    cand.conflicts = _deps(hdr.get("conflictname"), hdr.get("conflictflags"), hdr.get("conflictversion"),
                           skip_internal=False)
    cand.provides = _deps(hdr.get("providename"), hdr.get("provideflags"), hdr.get("provideversion"),
                          skip_internal=False)
    if isinstance(hdr.get("size"), list) and hdr["size"]:
        cand.installed_size = hdr["size"][0]
    interpreters = {"prein": "preinprog", "postin": "postinprog", "preun": "preunprog", "postun": "postunprog",
                    "pretrans": "pretransprog", "posttrans": "posttransprog",
                    "triggerscripts": "triggerscriptprog", "filetriggerscripts": "filetriggerscriptprog",
                    "transfiletriggerscripts": "transfiletriggerscriptprog"}
    scripts, oversized = {}, []
    for tag in SCRIPT_TAGS:
        value = hdr.get(tag)
        if isinstance(value, list):
            value = "\n# --- next trigger ---\n".join(value)
        if value:
            if len(value) > 512 * 1024:
                oversized.append(tag)  # only the start is kept: the rest is never treated as read
            scripts[tag] = {"interpreter": _program(hdr.get(interpreters[tag])), "body": value[:512 * 1024]}
    signed = any(k in sig for k in ("rsa", "dsa", "pgp", "gpg"))
    cand.metadata.update({
        "rpm_architecture": rpm_arch,
        "license": hdr.get("license"),
        "url": hdr.get("url"),
        "packager": hdr.get("packager"),
        "sysusers": hdr.get("sysusers") or [],
        "payload_format": hdr.get("payloadformat"),
        "payload_compressor": hdr.get("payloadcompressor"),
        "obsoletes": hdr.get("obsoletename") or [],
        "scriptlets": scripts,
        "uninspected_scripts": oversized,
        "signature_present": signed,  # present, not verified (rpm verifies it when installing)
        "digests": sorted(k for k in sig if k in ("sha1", "sha256", "md5")),
    })
    if scripts:
        cand.add("RPM_SCRIPTLETS", Severity.WARNING,
                 "The package contains scriptlets (" + ", ".join(sorted(scripts))
                 + "); they will be analysed, never run blindly", scriptlets=sorted(scripts))
    if not signed:
        cand.add("RPM_UNSIGNED", Severity.INFO, "The package carries no OpenPGP signature")
    cand.add("FOREIGN_PACKAGE_FORMAT", Severity.INFO,
             "RPM packages are built for Fedora/openSUSE; Cygnus will look for a native alternative first")
    if not cand.name or not cand.version:
        raise DetectionError("RPM header lacks name/version")
    return cand
