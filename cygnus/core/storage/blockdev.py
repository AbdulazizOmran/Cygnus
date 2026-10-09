"""Block device information from lsblk(8), flattened with parent-disk attributes."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from cygnus.core.util import proc

_COLUMNS = (
    "NAME,PATH,MAJ:MIN,TYPE,FSTYPE,LABEL,UUID,PARTUUID,SIZE,ROTA,RM,HOTPLUG,"
    "TRAN,MODEL,SERIAL,PKNAME,RO,MOUNTPOINTS"
)


@dataclass(frozen=True, slots=True)
class BlockDevice:
    path: str
    name: str
    dev: tuple[int, int] | None
    type: str
    fstype: str | None
    label: str | None
    uuid: str | None
    partuuid: str | None
    size: int | None
    rotational: bool
    removable: bool
    hotplug: bool
    transport: str | None
    model: str | None
    serial: str | None
    read_only: bool
    mountpoints: tuple[str, ...] = field(default=())


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip() in ("1", "true")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dev(value: Any) -> tuple[int, int] | None:
    try:
        major, minor = str(value).split(":")
        return int(major), int(minor)
    except ValueError:
        return None


def parse(data: dict[str, Any]) -> list[BlockDevice]:
    """Flatten lsblk --json output; partitions inherit transport/model/serial from their disk."""
    out: list[BlockDevice] = []

    def walk(node: dict[str, Any], parent: dict[str, Any] | None) -> None:
        inherited = parent or {}

        def pick(key: str) -> Any:
            value = node.get(key)
            return value if value not in (None, "") else inherited.get(key)

        out.append(
            BlockDevice(
                path=node.get("path") or f"/dev/{node.get('name')}",
                name=node.get("name", ""),
                dev=_dev(node.get("maj:min")),
                type=node.get("type", ""),
                fstype=node.get("fstype") or None,
                label=node.get("label") or None,
                uuid=node.get("uuid") or None,
                partuuid=node.get("partuuid") or None,
                size=_int(node.get("size")),
                rotational=_bool(pick("rota")),
                removable=_bool(node.get("rm")) or _bool(inherited.get("rm")),
                hotplug=_bool(node.get("hotplug")) or _bool(inherited.get("hotplug")),
                transport=pick("tran") or None,
                model=(pick("model") or "").strip() or None,
                serial=(pick("serial") or "").strip() or None,
                read_only=_bool(node.get("ro")),
                mountpoints=tuple(m for m in (node.get("mountpoints") or []) if m),
            )
        )
        for child in node.get("children") or []:
            merged = dict(inherited)
            merged.update({k: v for k, v in node.items() if k != "children" and v not in (None, "")})
            walk(child, merged)

    for top in data.get("blockdevices", []):
        walk(top, None)
    return out


def read() -> list[BlockDevice]:
    result = proc.run(["lsblk", "--json", "--bytes", "-o", _COLUMNS], timeout=20)
    if not result.ok:
        return []
    return parse(json.loads(result.stdout))


def device_present(fs_uuid: str) -> bool:
    """True if a filesystem with this UUID is attached, checked without touching any mountpoint."""
    if not fs_uuid or "/" in fs_uuid or fs_uuid in (".", ".."):
        return False
    return os.path.exists(f"/dev/disk/by-uuid/{fs_uuid}")
