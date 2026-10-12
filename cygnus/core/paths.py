"""XDG locations used by the unprivileged side of Cygnus."""

from __future__ import annotations

import os
from pathlib import Path

_DIR_NAME = "cygnus"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "")
    # The XDG spec says relative paths must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / _DIR_NAME


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / _DIR_NAME


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / _DIR_NAME


def is_in_cache(path: str | Path) -> bool:
    """Is `path` inside Cygnus's own cache folder, where it keeps downloads it is free to delete later? A program must never be adopted in place
    there (the cache is under the home folder, so it also counts as "already in storage" for a location on the home drive)."""
    try:
        return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(cache_dir()))
    except (OSError, ValueError):
        return False


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / _DIR_NAME


def runtime_dir() -> Path:
    """Per-login scratch space (tmpfs, cleared at reboot); the cache dir when there is no session."""
    value = os.environ.get("XDG_RUNTIME_DIR", "")
    return Path(value) / _DIR_NAME if value and os.path.isabs(value) else cache_dir() / "run"


def registry_path() -> Path:
    return data_dir() / "registry.db"


def boot_id() -> str:
    """Kernel boot ID; probe results are tied to it."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""
