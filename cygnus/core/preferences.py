"""Small per-user preferences (config dir), e.g. optional features you do not want."""

from __future__ import annotations

import json
import os
from typing import Any

from cygnus.core import paths
from cygnus.core.errors import CygnusError
from cygnus.core.util.fs import atomic_write


def _path():
    return paths.config_dir() / "preferences.json"


def _clean_dismissed(raw: Any) -> dict[str, list[str]]:
    """Keep the well-formed entries of a hand-edited `dismissed` table and drop the rest."""
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for app_id, components in raw.items():
        if isinstance(app_id, str) and isinstance(components, list):
            kept = [c for c in components if isinstance(c, str)]
            if kept:
                clean[app_id] = kept
    return clean


def load() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "dismissed" in data:
        data["dismissed"] = _clean_dismissed(data["dismissed"])
    return data


def _set_aside_if_unreadable() -> None:
    """A file that exists but cannot be read (a hand edit left a stray comma, or it is not readable by us) is kept as
    preferences.json.unreadable before a new one is written: `load` returned nothing for it, so the next save would
    otherwise wipe everything in it, including the record of what each file type was opened by before Cygnus. An earlier
    copy is never overwritten: the next one is called .unreadable.1, .unreadable.2 ..."""
    path = _path()
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        pass
    for n in range(0, 100):
        target = path.with_name(path.name + ".unreadable" + (f".{n}" if n else ""))
        if not target.exists():
            break
    else:
        raise CygnusError(f"{path.name} cannot be read and 100 copies of earlier unreadable ones are already kept next to "
                          "it: look at them, remove what you do not need, then try again")
    try:
        os.replace(path, target)
    except OSError:
        pass


def save(data: dict[str, Any]) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    _set_aside_if_unreadable()
    atomic_write(_path(), json.dumps(data, indent=1, sort_keys=True).encode())


def dismissed(app_id: str) -> frozenset[str]:
    """Components of `app_id` you said you do not want (they are no longer checked or suggested)."""
    return frozenset(load().get("dismissed", {}).get(app_id, []))


def set_dismissed(app_id: str, component_id: str, dismiss: bool) -> None:
    data = load()
    data.setdefault("dismissed", {})
    per_app = set(data["dismissed"].get(app_id, []))
    (per_app.add if dismiss else per_app.discard)(component_id)
    if per_app:
        data["dismissed"][app_id] = sorted(per_app)
    else:
        data["dismissed"].pop(app_id, None)
    save(data)
