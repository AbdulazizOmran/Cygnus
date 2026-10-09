"""The user registry: persistent record of storage locations, applications and their parts."""

from cygnus.core.registry.db import Registry, open_registry

__all__ = ["Registry", "open_registry"]
